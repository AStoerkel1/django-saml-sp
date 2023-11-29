import collections
import datetime
import json
from urllib.parse import urlparse

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.module_loading import import_string
from django.utils.translation import gettext_lazy as _
from onelogin.saml2.idp_metadata_parser import OneLogin_Saml2_IdPMetadataParser


def _default_authn_context():
    return ["urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport"]


class IdP(models.Model):
    name = models.CharField(max_length=200)
    url_params = models.JSONField(
        _("URL Parameters"),
        default=dict,
        blank=True,
        help_text=_("Application-specific URL path parameters."),
    )
    base_url = models.CharField(
        _("Base URL"),
        max_length=200,
        help_text=_("Root URL for the site, including http/https, no trailing slash."),
    )
    entity_id = models.CharField(
        _("Entity ID"),
        max_length=200,
        blank=True,
        help_text=_("Leave blank to automatically use the metadata URL."),
    )
    contact_name = models.CharField(max_length=100)
    contact_email = models.EmailField(max_length=100)
    x509_certificate = models.TextField(blank=True)
    private_key = models.TextField(blank=True)
    certificate_expires = models.DateTimeField(null=True, blank=True)
    metadata_url = models.URLField(
        "Metadata URL",
        max_length=500,
        blank=True,
        help_text=_("Leave this blank if entering metadata XML directly."),
    )
    verify_metadata_cert = models.BooleanField(
        _("Verify metadata URL certificate"), default=True
    )
    metadata_xml = models.TextField(
        _("Metadata XML"),
        blank=True,
        help_text=_(
            "Automatically loaded from the metadata URL, if specified. "
            "Otherwise input directly."
        ),
    )
    lowercase_encoding = models.BooleanField(
        default=False, help_text=_("Check this if the identity provider is ADFS.")
    )
    saml_settings = models.TextField(
        blank=True,
        help_text=_("Settings imported and used by the python-saml library."),
        editable=False,
    )
    last_import = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)
    auth_case_sensitive = models.BooleanField(
        _("NameID is case sensitive"), default=True
    )
    create_users = models.BooleanField(
        _("Create users that do not already exist"), default=True
    )
    associate_users = models.BooleanField(
        _("Associate existing users with this IdP by username"), default=True
    )
    username_prefix = models.CharField(
        help_text=_("Prefix for usernames generated by this IdP"),
        max_length=20,
        blank=True,
    )
    username_suffix = models.CharField(
        help_text=_("Suffix for usernames generated by this IdP"),
        max_length=20,
        blank=True,
    )
    respect_expiration = models.BooleanField(
        _("Respect IdP session expiration"),
        default=False,
        help_text=_(
            "Expires the Django session based on the IdP session expiration. "
            "Only works when using SESSION_SERIALIZER=PickleSerializer."
        ),
    )
    logout_triggers_slo = models.BooleanField(
        _("Logout triggers SLO"),
        default=False,
        help_text=_("Whether logging out should trigger a SLO request to the IdP."),
    )
    login_redirect = models.CharField(
        max_length=200,
        blank=True,
        help_text=_("URL name or path to redirect after a successful login."),
    )
    logout_redirect = models.CharField(
        max_length=200,
        blank=True,
        help_text=_("URL name or path to redirect after logout."),
    )
    last_login = models.DateTimeField(null=True, blank=True, default=None)
    is_active = models.BooleanField(default=True)
    authenticate_method = models.CharField(max_length=200, blank=True)
    login_method = models.CharField(max_length=200, blank=True)
    logout_method = models.CharField(max_length=200, blank=True)
    prepare_request_method = models.CharField(max_length=200, blank=True)
    update_user_method = models.CharField(max_length=200, blank=True)
    state_timeout = models.IntegerField(
        default=60,
        help_text=_("Time (in seconds) the SAML login request state is valid for."),
    )
    require_attributes = models.BooleanField(
        default=True,
        help_text=_("Ensures the IdP provides attributes on responses."),
    )
    authn_comparison = models.CharField(
        max_length=100,
        default="exact",
        help_text=_("The Comparison attribute on RequestedAuthnContext."),
    )
    authn_context = models.JSONField(
        default=_default_authn_context,
        help_text=_(
            "true (urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport), "
            "false, or a list of AuthnContextClassRef names."
        ),
    )
    sort_order = models.IntegerField(default=0)

    class Meta:
        verbose_name = _("identity provider")
        ordering = ("sort_order", "name")

    def __str__(self):
        return self.name

    def get_url(self, name, default="/"):
        try:
            return reverse(name, kwargs=self.url_params)
        except NoReverseMatch:
            return default

    def get_entity_id(self):
        if self.entity_id:
            return self.entity_id
        else:
            return self.base_url + self.get_absolute_url()

    get_entity_id.short_description = _("Entity ID")

    def get_acs(self):
        return self.base_url + self.get_url("sp-idp-acs")

    get_acs.short_description = _("ACS")

    def get_slo(self):
        return self.base_url + self.get_url("sp-idp-slo")

    get_slo.short_description = _("SLO")

    def get_absolute_url(self):
        return self.get_url("sp-idp-metadata")

    def get_login_url(self):
        return self.get_url("sp-idp-login")

    def get_test_url(self):
        return self.get_url("sp-idp-test")

    def get_verify_url(self):
        return self.get_url("sp-idp-verify")

    def get_logout_url(self):
        return self.get_url("sp-idp-logout")

    def prepare_request(self, request):
        method = self.prepare_request_method or getattr(
            settings, "SP_PREPARE_REQUEST", "sp.utils.prepare_request"
        )
        return import_string(method)(request, self)

    @property
    def sp_settings(self):
        return {
            "strict": True,
            "sp": {
                "NameIDFormat": "urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified",
                "entityId": self.get_entity_id(),
                "assertionConsumerService": {
                    "url": self.get_acs(),
                    "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
                },
                "singleLogoutService": {
                    "url": self.get_slo(),
                    "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
                },
                "x509cert": self.x509_certificate,
                "privateKey": self.private_key,
            },
            "security": {
                "wantAttributeStatement": self.require_attributes,
                "metadataValidUntil": self.certificate_expires,
                "requestedAuthnContextComparison": self.authn_comparison,
                "requestedAuthnContext": self.authn_context,
            },
            "contactPerson": {
                "technical": {
                    "givenName": self.contact_name,
                    "emailAddress": self.contact_email,
                }
            },
        }

    @property
    def settings(self):
        settings_dict = json.loads(self.saml_settings)
        settings_dict.update(self.sp_settings)
        return settings_dict

    def generate_certificate(self):
        url_parts = urlparse(self.base_url)
        backend = default_backend()
        key = rsa.generate_private_key(
            public_exponent=65537, key_size=2048, backend=backend
        )
        self.private_key = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("ascii")
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, url_parts.netloc)])
        basic_contraints = x509.BasicConstraints(ca=True, path_length=0)
        now = timezone.now()
        self.certificate_expires = now + datetime.timedelta(days=3650)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(self.certificate_expires)
            .add_extension(basic_contraints, critical=False)
            .sign(key, hashes.SHA256(), backend)
        )
        self.x509_certificate = cert.public_bytes(serialization.Encoding.PEM).decode(
            "ascii"
        )
        self.save()

    def import_metadata(self):
        if self.metadata_url:
            self.metadata_xml = OneLogin_Saml2_IdPMetadataParser.get_metadata(
                self.metadata_url, validate_cert=self.verify_metadata_cert
            ).decode("utf-8")
        self.saml_settings = json.dumps(
            OneLogin_Saml2_IdPMetadataParser.parse(self.metadata_xml)
        )
        self.last_import = timezone.now()
        self.save()

    def mapped_attributes(self, saml):
        attrs = collections.OrderedDict()
        for attr in self.attributes.exclude(mapped_name=""):
            value = saml.get_attribute(attr.saml_attribute)
            if value is not None:
                attrs[attr.mapped_name] = value
        return attrs

    def get_nameid(self, saml):
        nameid_attr = self.attributes.filter(is_nameid=True).first()
        if nameid_attr:
            return saml.get_attribute(nameid_attr.saml_attribute)[0]
        else:
            return saml.get_nameid()

    def get_login_redirect(self, redir=None):
        return redir or self.login_redirect or settings.LOGIN_REDIRECT_URL

    def get_logout_redirect(self, redir=None):
        return redir or self.logout_redirect or settings.LOGOUT_REDIRECT_URL

    def authenticate(self, request, saml):
        method = self.authenticate_method or getattr(
            settings, "SP_AUTHENTICATE", "sp.utils.authenticate"
        )
        return import_string(method)(request, self, saml)

    def login(self, request, user, saml):
        method = self.login_method or getattr(settings, "SP_LOGIN", "sp.utils.login")
        return import_string(method)(request, user, self, saml)

    def logout(self, request):
        method = self.logout_method or getattr(settings, "SP_LOGOUT", "sp.utils.logout")
        return import_string(method)(request, self)

    def update_user(self, request, saml, user, created=None):
        method = self.update_user_method or getattr(
            settings, "SP_UPDATE_USER", "sp.utils.update_user"
        )
        return (
            import_string(method)(request, self, saml, user, created=created)
            if method
            else user
        )


class IdPUserDefaultValue(models.Model):
    idp = models.ForeignKey(
        IdP,
        verbose_name=_("identity provider"),
        related_name="user_defaults",
        on_delete=models.CASCADE,
    )
    field = models.CharField(max_length=200)
    value = models.TextField()

    class Meta:
        verbose_name = _("user default value")
        verbose_name_plural = _("user default values")
        unique_together = [
            ("idp", "field"),
        ]

    def __str__(self):
        return "{} -> {}".format(self.field, self.value)


class IdPAttribute(models.Model):
    idp = models.ForeignKey(
        IdP,
        verbose_name=_("identity provider"),
        related_name="attributes",
        on_delete=models.CASCADE,
    )
    saml_attribute = models.CharField(max_length=200)
    mapped_name = models.CharField(max_length=200, blank=True)
    is_nameid = models.BooleanField(
        _("Is NameID"),
        default=False,
        help_text=_(
            "Check if this should be the unique identifier of the SSO identity."
        ),
    )
    always_update = models.BooleanField(
        _("Always Update"),
        default=False,
        help_text=_(
            "Update this mapped user field on every successful authentication. "
            "By default, mapped fields are only set on user creation."
        ),
    )

    class Meta:
        verbose_name = _("attribute mapping")
        verbose_name_plural = _("attribute mappings")
        unique_together = [
            ("idp", "saml_attribute"),
        ]

    def __str__(self):
        if self.mapped_name:
            return "{} -> {}".format(self.saml_attribute, self.mapped_name)
        else:
            return "{} (unmapped)".format(self.saml_attribute)


class IdPUser(models.Model):
    idp = models.ForeignKey(IdP, related_name="users", on_delete=models.CASCADE)
    nameid = models.CharField(max_length=200, db_index=True)
    content_type = models.ForeignKey(
        ContentType, related_name="idp_users", on_delete=models.CASCADE
    )
    user_id = models.CharField(max_length=100)

    user = GenericForeignKey("content_type", "user_id")

    class Meta:
        unique_together = [
            ("idp", "nameid"),
            ("idp", "content_type", "user_id"),
        ]
