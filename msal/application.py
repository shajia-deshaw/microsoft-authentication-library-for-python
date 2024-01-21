import functools
import json
import time
import logging
import sys
import warnings
from threading import Lock
from typing import Optional  # Needed in Python 3.7 & 3.8
import os

from .oauth2cli import Client, JwtAssertionCreator
from .oauth2cli.oidc import decode_part
from .authority import Authority, WORLD_WIDE
from .mex import send_request as mex_send_request, send_request_iwa as mex_send_request_iwa
from .wstrust_request import send_request as wst_send_request
from .wstrust_response import *
from .token_cache import TokenCache, _get_username, _GRANT_TYPE_BROKER
import msal.telemetry
from .region import _detect_region
from .throttled_http_client import ThrottledHttpClient
from .cloudshell import _is_running_in_cloud_shell


# The __init__.py will import this. Not the other way around.
__version__ = "1.31.0"  # When releasing, also check and bump our dependencies's versions if needed

logger = logging.getLogger(__name__)
_AUTHORITY_TYPE_CLOUDSHELL = "CLOUDSHELL"

def _init_broker(enable_pii_log):  # Make it a function to allow mocking
    from . import broker  # Trigger Broker's initialization, lazily
    if enable_pii_log:
        broker._enable_pii_log()

def extract_certs(public_cert_content):
    # Parses raw public certificate file contents and returns a list of strings
    # Usage: headers = {"x5c": extract_certs(open("my_cert.pem").read())}
    public_certificates = re.findall(
        r'-----BEGIN CERTIFICATE-----(?P<cert_value>[^-]+)-----END CERTIFICATE-----',
        public_cert_content, re.I)
    if public_certificates:
        return [cert.strip() for cert in public_certificates]
    # The public cert tags are not found in the input,
    # let's make best effort to exclude a private key pem file.
    if "PRIVATE KEY" in public_cert_content:
        raise ValueError(
            "We expect your public key but detect a private key instead")
    return [public_cert_content.strip()]


def _merge_claims_challenge_and_capabilities(capabilities, claims_challenge):
    # Represent capabilities as {"access_token": {"xms_cc": {"values": capabilities}}}
    # and then merge/add it into incoming claims
    if not capabilities:
        return claims_challenge
    claims_dict = json.loads(claims_challenge) if claims_challenge else {}
    for key in ["access_token"]:  # We could add "id_token" if we'd decide to
        claims_dict.setdefault(key, {}).update(xms_cc={"values": capabilities})
    return json.dumps(claims_dict)


def _str2bytes(raw):
    # A conversion based on duck-typing rather than six.text_type
    try:
        return raw.encode(encoding="utf-8")
    except:
        return raw


def _parse_pfx(pfx_path, passphrase_bytes):
    # Cert concepts https://security.stackexchange.com/a/226758/125264
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.serialization import pkcs12
    with open(pfx_path, 'rb') as f:
        private_key, cert, _ = pkcs12.load_key_and_certificates(  # cryptography 2.5+
            # https://cryptography.io/en/latest/hazmat/primitives/asymmetric/serialization/#cryptography.hazmat.primitives.serialization.pkcs12.load_key_and_certificates
            f.read(), passphrase_bytes)
    if not (private_key and cert):
        raise ValueError("Your PFX file shall contain both private key and cert")
    cert_pem = cert.public_bytes(encoding=serialization.Encoding.PEM).decode()  # cryptography 1.0+
    x5c = [
        '\n'.join(cert_pem.splitlines()[1:-1])  # Strip the "--- header ---" and "--- footer ---"
    ]
    sha256_thumbprint = cert.fingerprint(hashes.SHA256()).hex()  # cryptography 0.7+
    sha1_thumbprint = cert.fingerprint(hashes.SHA1()).hex()  # cryptography 0.7+
        # https://cryptography.io/en/latest/x509/reference/#x-509-certificate-object
    return private_key, sha256_thumbprint, sha1_thumbprint, x5c


def _load_private_key_from_pem_str(private_key_pem_str, passphrase_bytes):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.backends import default_backend
    return serialization.load_pem_private_key(  # cryptography 0.6+
        _str2bytes(private_key_pem_str),
        passphrase_bytes,
        backend=default_backend(),  # It was a required param until 2020
        )


def _pii_less_home_account_id(home_account_id):
    parts = home_account_id.split(".")  # It could contain one or two parts
    parts[0] = "********"
    return ".".join(parts)


def _clean_up(result):
    if isinstance(result, dict):
        if "_msalruntime_telemetry" in result or "_msal_python_telemetry" in result:
            result["msal_telemetry"] = json.dumps({  # Telemetry as an opaque string
                "msalruntime_telemetry": result.get("_msalruntime_telemetry"),
                "msal_python_telemetry": result.get("_msal_python_telemetry"),
                }, separators=(",", ":"))
        return_value = {
            k: result[k] for k in result
            if k != "refresh_in"  # MSAL handled refresh_in, customers need not
            and not k.startswith('_')  # Skim internal properties
            }
        if "refresh_in" in result:  # To encourage proactive refresh
            return_value["refresh_on"] = int(time.time() + result["refresh_in"])
        return return_value
    return result  # It could be None


def _preferred_browser():
    """Register Edge and return a name suitable for subsequent webbrowser.get(...)
    when appropriate. Otherwise return None.
    """
    # On Linux, only Edge will provide device-based Conditional Access support
    if sys.platform != "linux":  # On other platforms, we have no browser preference
        return None
    browser_path = "/usr/bin/microsoft-edge"  # Use a full path owned by sys admin
        # Note: /usr/bin/microsoft-edge, /usr/bin/microsoft-edge-stable, etc.
        # are symlinks that point to the actual binaries which are found under
        # /opt/microsoft/msedge/msedge or /opt/microsoft/msedge-beta/msedge.
        # Either method can be used to detect an Edge installation.
    user_has_no_preference = "BROWSER" not in os.environ
    user_wont_mind_edge = "microsoft-edge" in os.environ.get("BROWSER", "")  # Note:
        # BROWSER could contain "microsoft-edge" or "/path/to/microsoft-edge".
        # Python documentation (https://docs.python.org/3/library/webbrowser.html)
        # does not document the name being implicitly register,
        # so there is no public API to know whether the ENV VAR browser would work.
        # Therefore, we would not bother examine the env var browser's type.
        # We would just register our own Edge instance.
    if (user_has_no_preference or user_wont_mind_edge) and os.path.exists(browser_path):
        try:
            import webbrowser  # Lazy import. Some distro may not have this.
            browser_name = "msal-edge"  # Avoid popular name "microsoft-edge"
                # otherwise `BROWSER="microsoft-edge"; webbrowser.get("microsoft-edge")`
                # would return a GenericBrowser instance which won't work.
            try:
                registration_available = isinstance(
                    webbrowser.get(browser_name), webbrowser.BackgroundBrowser)
            except webbrowser.Error:
                registration_available = False
            if not registration_available:
                logger.debug("Register %s with %s", browser_name, browser_path)
                # By registering our own browser instance with our own name,
                # rather than populating a process-wide BROWSER enn var,
                # this approach does not have side effect on non-MSAL code path.
                webbrowser.register(  # Even double-register happens to work fine
                    browser_name, None, webbrowser.BackgroundBrowser(browser_path))
            return browser_name
        except ImportError:
            pass  # We may still proceed
    return None


class _ClientWithCcsRoutingInfo(Client):

    def initiate_auth_code_flow(self, **kwargs):
        if kwargs.get("login_hint"):  # eSTS could have utilized this as-is, but nope
            kwargs["X-AnchorMailbox"] = "UPN:%s" % kwargs["login_hint"]
        return super(_ClientWithCcsRoutingInfo, self).initiate_auth_code_flow(
            client_info=1,  # To be used as CSS Routing info
            **kwargs)

    def obtain_token_by_auth_code_flow(
            self, auth_code_flow, auth_response, **kwargs):
        # Note: the obtain_token_by_browser() is also covered by this
        assert isinstance(auth_code_flow, dict) and isinstance(auth_response, dict)
        headers = kwargs.pop("headers", {})
        client_info = json.loads(
            decode_part(auth_response["client_info"])
            ) if auth_response.get("client_info") else {}
        if "uid" in client_info and "utid" in client_info:
            # Note: The value of X-AnchorMailbox is also case-insensitive
            headers["X-AnchorMailbox"] = "Oid:{uid}@{utid}".format(**client_info)
        return super(_ClientWithCcsRoutingInfo, self).obtain_token_by_auth_code_flow(
            auth_code_flow, auth_response, headers=headers, **kwargs)

    def obtain_token_by_username_password(self, username, password, **kwargs):
        headers = kwargs.pop("headers", {})
        headers["X-AnchorMailbox"] = "upn:{}".format(username)
        return super(_ClientWithCcsRoutingInfo, self).obtain_token_by_username_password(
            username, password, headers=headers, **kwargs)


def _msal_extension_check():
    # Can't run this in module or class level otherwise you'll get circular import error
    try:
        from msal_extensions import __version__ as v
        major, minor, _ = v.split(".", maxsplit=3)
        if not (int(major) >= 1 and int(minor) >= 2):
            warnings.warn(
                "Please upgrade msal-extensions. "
                "Only msal-extensions 1.2+ can work with msal 1.30+")
    except ImportError:
        pass  # The optional msal_extensions is not installed. Business as usual.
    except ValueError:
        logger.exception(f"msal_extensions version {v} not in major.minor.patch format")


class ClientApplication(object):
    """You do not usually directly use this class. Use its subclasses instead:
    :class:`PublicClientApplication` and :class:`ConfidentialClientApplication`.
    """
    ACQUIRE_TOKEN_SILENT_ID = "84"
    ACQUIRE_TOKEN_BY_REFRESH_TOKEN = "85"
    ACQUIRE_TOKEN_BY_USERNAME_PASSWORD_ID = "301"
    ACQUIRE_TOKEN_ON_BEHALF_OF_ID = "523"
    ACQUIRE_TOKEN_BY_DEVICE_FLOW_ID = "622"
    ACQUIRE_TOKEN_FOR_CLIENT_ID = "730"
    ACQUIRE_TOKEN_BY_AUTHORIZATION_CODE_ID = "832"
    ACQUIRE_TOKEN_INTERACTIVE = "169"
    ACQUIRE_TOKEN_INTEGRATED_WINDOWS_AUTH_ID = "870"
    GET_ACCOUNTS_ID = "902"
    REMOVE_ACCOUNT_ID = "903"

    ATTEMPT_REGION_DISCOVERY = True  # "TryAutoDetect"
    DISABLE_MSAL_FORCE_REGION = False  # Used in azure_region to disable MSAL_FORCE_REGION behavior
    _TOKEN_SOURCE = "token_source"
    _TOKEN_SOURCE_IDP = "identity_provider"
    _TOKEN_SOURCE_CACHE = "cache"
    _TOKEN_SOURCE_BROKER = "broker"

    _enable_broker = False
    _AUTH_SCHEME_UNSUPPORTED = (
        "auth_scheme is currently only available from broker. "
        "You can enable broker by following these instructions. "
        "https://msal-python.readthedocs.io/en/latest/#publicclientapplication")

    def __init__(
            self, client_id,
            client_credential=None, authority=None, validate_authority=True,
            token_cache=None,
            http_client=None,
            verify=True, proxies=None, timeout=None,
            client_claims=None, app_name=None, app_version=None,
            client_capabilities=None,
            azure_region=None,  # Note: We choose to add this param in this base class,
                # despite it is currently only needed by ConfidentialClientApplication.
                # This way, it holds the same positional param place for PCA,
                # when we would eventually want to add this feature to PCA in future.
            exclude_scopes=None,
            http_cache=None,
            instance_discovery=None,
            allow_broker=None,
            enable_pii_log=None,
            oidc_authority=None,
            ):
        """Create an instance of application.

        :param str client_id: Your app has a client_id after you register it on Microsoft Entra admin center.

        :param client_credential:
            For :class:`PublicClientApplication`, you use `None` here.

            For :class:`ConfidentialClientApplication`,
            it supports many different input formats for different scenarios.

            .. admonition:: Support using a client secret.

                Just feed in a string, such as ``"your client secret"``.

            .. admonition:: Support using a certificate in X.509 (.pem) format

                Feed in a dict in this form::

                    {
                        "private_key": "...-----BEGIN PRIVATE KEY-----... in PEM format",
                        "thumbprint": "A1B2C3D4E5F6...",
                        "passphrase": "Passphrase if the private_key is encrypted (Optional. Added in version 1.6.0)",
                    }

                MSAL Python requires a "private_key" in PEM format.
                If your cert is in PKCS12 (.pfx) format,
                you can convert it to X.509 (.pem) format,
                by ``openssl pkcs12 -in file.pfx -out file.pem -nodes``.

                The thumbprint is available in your app's registration in Azure Portal.
                Alternatively, you can `calculate the thumbprint <https://github.com/Azure/azure-sdk-for-python/blob/07d10639d7e47f4852eaeb74aef5d569db499d6e/sdk/identity/azure-identity/azure/identity/_credentials/certificate.py#L94-L97>`_.

            .. admonition:: Support Subject Name/Issuer Auth with a cert in .pem

                `Subject Name/Issuer Auth
                <https://github.com/AzureAD/microsoft-authentication-library-for-python/issues/60>`_
                is an approach to allow easier certificate rotation.

                *Added in version 0.5.0*::

                    {
                        "private_key": "...-----BEGIN PRIVATE KEY-----... in PEM format",
                        "thumbprint": "A1B2C3D4E5F6...",
                        "public_certificate": "...-----BEGIN CERTIFICATE-----...",
                        "passphrase": "Passphrase if the private_key is encrypted (Optional. Added in version 1.6.0)",
                    }

                ``public_certificate`` (optional) is public key certificate
                which will be sent through 'x5c' JWT header only for
                subject name and issuer authentication to support cert auto rolls.

                Per `specs <https://tools.ietf.org/html/rfc7515#section-4.1.6>`_,
                "the certificate containing
                the public key corresponding to the key used to digitally sign the
                JWS MUST be the first certificate.  This MAY be followed by
                additional certificates, with each subsequent certificate being the
                one used to certify the previous one."
                However, your certificate's issuer may use a different order.
                So, if your attempt ends up with an error AADSTS700027 -
                "The provided signature value did not match the expected signature value",
                you may try use only the leaf cert (in PEM/str format) instead.

            .. admonition:: Supporting raw assertion obtained from elsewhere

                *Added in version 1.13.0*:
                It can also be a completely pre-signed assertion that you've assembled yourself.
                Simply pass a container containing only the key "client_assertion", like this::

                    {
                        "client_assertion": "...a JWT with claims aud, exp, iss, jti, nbf, and sub..."
                    }

            .. admonition:: Supporting reading client cerficates from PFX files

                *Added in version 1.29.0*:
                Feed in a dictionary containing the path to a PFX file::

                    {
                        "private_key_pfx_path": "/path/to/your.pfx",
                        "passphrase": "Passphrase if the private_key is encrypted (Optional)",
                    }

                The following command will generate a .pfx file from your .key and .pem file::

                    openssl pkcs12 -export -out certificate.pfx -inkey privateKey.key -in certificate.pem

            .. admonition:: Support Subject Name/Issuer Auth with a cert in .pfx

                *Added in version 1.30.0*:
                If your .pfx file contains both the private key and public cert,
                you can opt in for Subject Name/Issuer Auth like this::

                    {
                        "private_key_pfx_path": "/path/to/your.pfx",
                        "public_certificate": True,
                        "passphrase": "Passphrase if the private_key is encrypted (Optional)",
                    }

        :type client_credential: Union[dict, str, None]

        :param dict client_claims:
            *Added in version 0.5.0*:
            It is a dictionary of extra claims that would be signed by
            by this :class:`ConfidentialClientApplication` 's private key.
            For example, you can use {"client_ip": "x.x.x.x"}.
            You may also override any of the following default claims::

                {
                    "aud": the_token_endpoint,
                    "iss": self.client_id,
                    "sub": same_as_issuer,
                    "exp": now + 10_min,
                    "iat": now,
                    "jti": a_random_uuid
                }

        :param str authority:
            A URL that identifies a token authority. It should be of the format
            ``https://login.microsoftonline.com/your_tenant``
            By default, we will use ``https://login.microsoftonline.com/common``

            *Changed in version 1.17*: you can also use predefined constant
            and a builder like this::

                from msal.authority import (
                    AuthorityBuilder,
                    AZURE_US_GOVERNMENT, AZURE_CHINA, AZURE_PUBLIC)
                my_authority = AuthorityBuilder(AZURE_PUBLIC, "contoso.onmicrosoft.com")
                # Now you get an equivalent of
                # "https://login.microsoftonline.com/contoso.onmicrosoft.com"

                # You can feed such an authority to msal's ClientApplication
                from msal import PublicClientApplication
                app = PublicClientApplication("my_client_id", authority=my_authority, ...)

        :param bool validate_authority: (optional) Turns authority validation
            on or off. This parameter default to true.
        :param TokenCache token_cache:
            Sets the token cache used by this ClientApplication instance.
            By default, an in-memory cache will be created and used.
        :param http_client: (optional)
            Your implementation of abstract class HttpClient <msal.oauth2cli.http.http_client>
            Defaults to a requests session instance.
            Since MSAL 1.11.0, the default session would be configured
            to attempt one retry on connection error.
            If you are providing your own http_client,
            it will be your http_client's duty to decide whether to perform retry.

        :param verify: (optional)
            It will be passed to the
            `verify parameter in the underlying requests library
            <http://docs.python-requests.org/en/v2.9.1/user/advanced/#ssl-cert-verification>`_
            This does not apply if you have chosen to pass your own Http client
        :param proxies: (optional)
            It will be passed to the
            `proxies parameter in the underlying requests library
            <http://docs.python-requests.org/en/v2.9.1/user/advanced/#proxies>`_
            This does not apply if you have chosen to pass your own Http client
        :param timeout: (optional)
            It will be passed to the
            `timeout parameter in the underlying requests library
            <http://docs.python-requests.org/en/v2.9.1/user/advanced/#timeouts>`_
            This does not apply if you have chosen to pass your own Http client
        :param app_name: (optional)
            You can provide your application name for Microsoft telemetry purposes.
            Default value is None, means it will not be passed to Microsoft.
        :param app_version: (optional)
            You can provide your application version for Microsoft telemetry purposes.
            Default value is None, means it will not be passed to Microsoft.
        :param list[str] client_capabilities: (optional)
            Allows configuration of one or more client capabilities, e.g. ["CP1"].

            Client capability is meant to inform the Microsoft identity platform
            (STS) what this client is capable for,
            so STS can decide to turn on certain features.
            For example, if client is capable to handle *claims challenge*,
            STS may issue
            `Continuous Access Evaluation (CAE) <https://learn.microsoft.com/entra/identity/conditional-access/concept-continuous-access-evaluation>`_
            access tokens to resources,
            knowing that when the resource emits a *claims challenge*
            the client will be able to handle those challenges.

            Implementation details:
            Client capability is implemented using "claims" parameter on the wire,
            for now.
            MSAL will combine them into
            `claims parameter <https://openid.net/specs/openid-connect-core-1_0-final.html#ClaimsParameter>`_
            which you will later provide via one of the acquire-token request.

        :param str azure_region: (optional)
            Instructs MSAL to use the Entra regional token service. This legacy feature is only available to
            first-party applications. Only ``acquire_token_for_client()`` is supported.

            Supports 4 values:

            1. ``azure_region=None`` - This default value means no region is configured.
               MSAL will use the region defined in env var ``MSAL_FORCE_REGION``.
            2. ``azure_region="some_region"`` - meaning the specified region is used.
            3. ``azure_region=True`` - meaning
               MSAL will try to auto-detect the region. This is not recommended.
            4. ``azure_region=False`` - meaning MSAL will use no region.

            .. note::
                Region auto-discovery has been tested on VMs and on Azure Functions. It is unreliable.
                Applications using this option should configure a short timeout.

                For more details and for the values of the region string
                  see https://learn.microsoft.com/entra/msal/dotnet/resources/region-discovery-troubleshooting

            New in version 1.12.0.

        :param list[str] exclude_scopes: (optional)
            Historically MSAL hardcodes `offline_access` scope,
            which would allow your app to have prolonged access to user's data.
            If that is unnecessary or undesirable for your app,
            now you can use this parameter to supply an exclusion list of scopes,
            such as ``exclude_scopes = ["offline_access"]``.

        :param dict http_cache:
            MSAL has long been caching tokens in the ``token_cache``.
            Recently, MSAL also introduced a concept of ``http_cache``,
            by automatically caching some finite amount of non-token http responses,
            so that *long-lived*
            ``PublicClientApplication`` and ``ConfidentialClientApplication``
            would be more performant and responsive in some situations.

            This ``http_cache`` parameter accepts any dict-like object.
            If not provided, MSAL will use an in-memory dict.

            If your app is a command-line app (CLI),
            you would want to persist your http_cache across different CLI runs.
            The following recipe shows a way to do so::

                # Just add the following lines at the beginning of your CLI script
                import sys, atexit, pickle
                http_cache_filename = sys.argv[0] + ".http_cache"
                try:
                    with open(http_cache_filename, "rb") as f:
                        persisted_http_cache = pickle.load(f)  # Take a snapshot
                except (
                        FileNotFoundError,  # Or IOError in Python 2
                        pickle.UnpicklingError,  # A corrupted http cache file
                        ):
                    persisted_http_cache = {}  # Recover by starting afresh
                atexit.register(lambda: pickle.dump(
                    # When exit, flush it back to the file.
                    # It may occasionally overwrite another process's concurrent write,
                    # but that is fine. Subsequent runs will reach eventual consistency.
                    persisted_http_cache, open(http_cache_file, "wb")))

                # And then you can implement your app as you normally would
                app = msal.PublicClientApplication(
                    "your_client_id",
                    ...,
                    http_cache=persisted_http_cache,  # Utilize persisted_http_cache
                    ...,
                    #token_cache=...,  # You may combine the old token_cache trick
                        # Please refer to token_cache recipe at
                        # https://msal-python.readthedocs.io/en/latest/#msal.SerializableTokenCache
                    )
                app.acquire_token_interactive(["your", "scope"], ...)

            Content inside ``http_cache`` are cheap to obtain.
            There is no need to share them among different apps.

            Content inside ``http_cache`` will contain no tokens nor
            Personally Identifiable Information (PII). Encryption is unnecessary.

            New in version 1.16.0.

        :param boolean instance_discovery:
            Historically, MSAL would connect to a central endpoint located at
            ``https://login.microsoftonline.com`` to acquire some metadata,
            especially when using an unfamiliar authority.
            This behavior is known as Instance Discovery.

            This parameter defaults to None, which enables the Instance Discovery.

            If you know some authorities which you allow MSAL to operate with as-is,
            without involving any Instance Discovery, the recommended pattern is::

                known_authorities = frozenset([  # Treat your known authorities as const
                    "https://contoso.com/adfs", "https://login.azs/foo"])
                ...
                authority = "https://contoso.com/adfs"  # Assuming your app will use this
                app1 = PublicClientApplication(
                    "client_id",
                    authority=authority,
                    # Conditionally disable Instance Discovery for known authorities
                    instance_discovery=authority not in known_authorities,
                    )

            If you do not know some authorities beforehand,
            yet still want MSAL to accept any authority that you will provide,
            you can use a ``False`` to unconditionally disable Instance Discovery.

            New in version 1.19.0.

        :param boolean allow_broker:
            Deprecated. Please use ``enable_broker_on_windows`` instead.

        :param boolean enable_pii_log:
            When enabled, logs may include PII (Personal Identifiable Information).
            This can be useful in troubleshooting broker behaviors.
            The default behavior is False.

            New in version 1.24.0.

        :param str oidc_authority:
            *Added in version 1.28.0*:
            It is a URL that identifies an OpenID Connect (OIDC) authority of
            the format ``https://contoso.com/tenant``.
            MSAL will append ".well-known/openid-configuration" to the authority
            and retrieve the OIDC metadata from there, to figure out the endpoints.

            Note: Broker will NOT be used for OIDC authority.
        """
        self.client_id = client_id
        self.client_credential = client_credential
        self.client_claims = client_claims
        self._client_capabilities = client_capabilities
        self._instance_discovery = instance_discovery

        if exclude_scopes and not isinstance(exclude_scopes, list):
            raise ValueError(
                "Invalid exclude_scopes={}. It need to be a list of strings.".format(
                repr(exclude_scopes)))
        self._exclude_scopes = frozenset(exclude_scopes or [])
        if "openid" in self._exclude_scopes:
            raise ValueError(
                'Invalid exclude_scopes={}. You can not opt out "openid" scope'.format(
                repr(exclude_scopes)))

        if http_client:
            self.http_client = http_client
        else:
            import requests  # Lazy load

            self.http_client = requests.Session()
            self.http_client.verify = verify
            self.http_client.proxies = proxies
            # Requests, does not support session - wide timeout
            # But you can patch that (https://github.com/psf/requests/issues/3341):
            self.http_client.request = functools.partial(
                self.http_client.request, timeout=timeout)

            # Enable a minimal retry. Better than nothing.
            # https://github.com/psf/requests/blob/v2.25.1/requests/adapters.py#L94-L108
            a = requests.adapters.HTTPAdapter(max_retries=1)
            self.http_client.mount("http://", a)
            self.http_client.mount("https://", a)
        self.http_client = ThrottledHttpClient(
            self.http_client,
            http_cache=http_cache,
            default_throttle_time=60
                # The default value 60 was recommended mainly for PCA at the end of
                # https://identitydivision.visualstudio.com/devex/_git/AuthLibrariesApiReview?version=GBdev&path=%2FService%20protection%2FIntial%20set%20of%20protection%20measures.md&_a=preview
                if isinstance(self, PublicClientApplication) else 5,
            )

        self.app_name = app_name
        self.app_version = app_version

        # Here the self.authority will not be the same type as authority in input
        if oidc_authority and authority:
            raise ValueError("You can not provide both authority and oidc_authority")
        try:
            authority_to_use = authority or "https://{}/common/".format(WORLD_WIDE)
            self.authority = Authority(
                authority_to_use,
                self.http_client,
                validate_authority=validate_authority,
                instance_discovery=self._instance_discovery,
                oidc_authority_url=oidc_authority,
                )
        except ValueError:  # Those are explicit authority validation errors
            raise
        except Exception:  # The rest are typically connection errors
            if validate_authority and not oidc_authority and (
                azure_region  # Opted in to use region
                or (azure_region is None and os.getenv("MSAL_FORCE_REGION"))  # Will use region
            ):
                # Since caller opts in to use region, here we tolerate connection
                # errors happened during authority validation at non-region endpoint
                self.authority = Authority(
                    authority_to_use,
                    self.http_client,
                    instance_discovery=False,
                    )
            else:
                raise

        self._decide_broker(allow_broker, enable_pii_log)
        self.token_cache = token_cache or TokenCache()
        self._region_configured = azure_region
        self._region_detected = None
        self.client, self._regional_client = self._build_client(
            client_credential, self.authority)
        self.authority_groups = None
        self._telemetry_buffer = {}
        self._telemetry_lock = Lock()
        _msal_extension_check()


    def _decide_broker(self, allow_broker, enable_pii_log):
        is_confidential_app = self.client_credential or isinstance(
            self, ConfidentialClientApplication)
        if is_confidential_app and allow_broker:
            raise ValueError("allow_broker=True is only supported in PublicClientApplication")
            # Historically, we chose to support ClientApplication("client_id", allow_broker=True)
        if allow_broker:
            warnings.warn(
                "allow_broker is deprecated. "
                "Please use PublicClientApplication(..., "
                "enable_broker_on_windows=True, "
                "enable_broker_on_mac=...)",
                DeprecationWarning)
        opted_in_for_broker = (
            self._enable_broker  # True means Opted-in from PCA
            or (
            # When we started the broker project on Windows platform,
            # the allow_broker was meant to be cross-platform. Now we realize
            # that other platforms have different redirect_uri requirements,
            # so the old allow_broker is deprecated and will only for Windows.
            allow_broker and sys.platform == "win32")
        )
        self._enable_broker = (  # This same variable will also store the state
            opted_in_for_broker
            and not is_confidential_app
            and not self.authority.is_adfs
            and not self.authority._is_b2c
        )
        if self._enable_broker:
            try:
                _init_broker(enable_pii_log)
            except RuntimeError:
                self._enable_broker = False
                logger.exception(
                    "Broker is unavailable on this platform. "
                    "We will fallback to non-broker.")
        logger.debug("Broker enabled? %s", self._enable_broker)

    def is_pop_supported(self):
        """Returns True if this client supports Proof-of-Possession Access Token."""
        return self._enable_broker

    def _decorate_scope(
            self, scopes,
            reserved_scope=frozenset(['openid', 'profile', 'offline_access'])):
        if not isinstance(scopes, (list, set, tuple)):
            raise ValueError("The input scopes should be a list, tuple, or set")
        scope_set = set(scopes)  # Input scopes is typically a list. Copy it to a set.
        if scope_set & reserved_scope:
            # These scopes are reserved for the API to provide good experience.
            # We could make the developer pass these and then if they do they will
            # come back asking why they don't see refresh token or user information.
            raise ValueError(
                """You cannot use any scope value that is reserved.
Your input: {}
The reserved list: {}""".format(list(scope_set), list(reserved_scope)))
            raise ValueError(
                "You cannot use any scope value that is in this reserved list: {}".format(
                    list(reserved_scope)))

        # client_id can also be used as a scope in B2C
        decorated = scope_set | reserved_scope
        decorated -= self._exclude_scopes
        return list(decorated)

    def _build_telemetry_context(
            self, api_id, correlation_id=None, refresh_reason=None):
        return msal.telemetry._TelemetryContext(
            self._telemetry_buffer, self._telemetry_lock, api_id,
            correlation_id=correlation_id, refresh_reason=refresh_reason)

    def _get_regional_authority(self, central_authority) -> Optional[Authority]:
        if self._region_configured is False:  # User opts out of ESTS-R
            return None  # Short circuit to completely bypass region detection
        if self._region_configured is None:  # User did not make an ESTS-R choice
            self._region_configured = os.getenv("MSAL_FORCE_REGION") or None
        self._region_detected = self._region_detected or _detect_region(
            self.http_client if self._region_configured is not None else None)
        if (self._region_configured != self.ATTEMPT_REGION_DISCOVERY
                and self._region_configured != self._region_detected):
            logger.warning('Region configured ({}) != region detected ({})'.format(
                repr(self._region_configured), repr(self._region_detected)))
        region_to_use = (
            self._region_detected
            if self._region_configured == self.ATTEMPT_REGION_DISCOVERY
            else self._region_configured)  # It will retain the None i.e. opted out
        logger.debug('Region to be used: {}'.format(repr(region_to_use)))
        if region_to_use:
            regional_host = ("{}.login.microsoft.com".format(region_to_use)
                if central_authority.instance in (
                    # The list came from point 3 of the algorithm section in this internal doc
                    # https://identitydivision.visualstudio.com/DevEx/_git/AuthLibrariesApiReview?path=/PinAuthToRegion/AAD%20SDK%20Proposal%20to%20Pin%20Auth%20to%20region.md&anchor=algorithm&_a=preview
                    "login.microsoftonline.com",
                    "login.microsoft.com",
                    "login.windows.net",
                    "sts.windows.net",
                    )
                else "{}.{}".format(region_to_use, central_authority.instance))
            return Authority(  # The central_authority has already been validated
                "https://{}/{}".format(regional_host, central_authority.tenant),
                self.http_client,
                instance_discovery=False,
                )
        return None

    def _build_client(self, client_credential, authority, skip_regional_client=False):
        client_assertion = None
        client_assertion_type = None
        default_headers = {
            "x-client-sku": "MSAL.Python", "x-client-ver": __version__,
            "x-client-os": sys.platform,
            "x-ms-lib-capability": "retry-after, h429",
        }
        if self.app_name:
            default_headers['x-app-name'] = self.app_name
        if self.app_version:
            default_headers['x-app-ver'] = self.app_version
        default_body = {"client_info": 1}
        if isinstance(client_credential, dict):
            client_assertion_type = Client.CLIENT_ASSERTION_TYPE_JWT
            # Use client_credential.get("...") rather than "..." in client_credential
            # so that we can ignore an empty string came from an empty ENV VAR.
            if client_credential.get("client_assertion"):
                client_assertion = client_credential['client_assertion']
            else:
                headers = {}
                sha1_thumbprint = sha256_thumbprint = None
                passphrase_bytes = _str2bytes(
                    client_credential["passphrase"]
                    ) if client_credential.get("passphrase") else None
                if client_credential.get("private_key_pfx_path"):
                    private_key, sha256_thumbprint, sha1_thumbprint, x5c = _parse_pfx(
                        client_credential["private_key_pfx_path"],
                        passphrase_bytes)
                    if client_credential.get("public_certificate") is True and x5c:
                        headers["x5c"] = x5c
                elif (
                        client_credential.get("private_key")  # PEM blob
                        and client_credential.get("thumbprint")):
                    sha1_thumbprint = client_credential["thumbprint"]
                    if passphrase_bytes:
                        private_key = _load_private_key_from_pem_str(
                            client_credential['private_key'], passphrase_bytes)
                    else:  # PEM without passphrase
                        private_key = client_credential['private_key']
                else:
                    raise ValueError(
                        "client_credential needs to follow this format "
                        "https://msal-python.readthedocs.io/en/latest/#msal.ClientApplication.params.client_credential")
                if ("x5c" not in headers  # So the .pfx file contains no certificate
                    and isinstance(client_credential.get('public_certificate'), str)
                ):  # Then we treat the public_certificate value as PEM content
                    headers["x5c"] = extract_certs(client_credential['public_certificate'])
                if sha256_thumbprint and not authority.is_adfs:
                    assertion_params = {
                        "algorithm": "PS256", "sha256_thumbprint": sha256_thumbprint,
                    }
                else:  # Fall back
                    if not sha1_thumbprint:
                        raise ValueError("You shall provide a thumbprint in SHA1.")
                    assertion_params = {
                        "algorithm": "RS256", "sha1_thumbprint": sha1_thumbprint,
                    }
                assertion = JwtAssertionCreator(
                    private_key, headers=headers, **assertion_params)
                client_assertion = assertion.create_regenerative_assertion(
                    audience=authority.token_endpoint, issuer=self.client_id,
                    additional_claims=self.client_claims or {})
        else:
            default_body['client_secret'] = client_credential
        central_configuration = {
            "authorization_endpoint": authority.authorization_endpoint,
            "token_endpoint": authority.token_endpoint,
            "device_authorization_endpoint": authority.device_authorization_endpoint,
            }
        central_client = _ClientWithCcsRoutingInfo(
            central_configuration,
            self.client_id,
            http_client=self.http_client,
            default_headers=default_headers,
            default_body=default_body,
            client_assertion=client_assertion,
            client_assertion_type=client_assertion_type,
            on_obtaining_tokens=lambda event: self.token_cache.add(dict(
                event, environment=authority.instance)),
            on_removing_rt=self.token_cache.remove_rt,
            on_updating_rt=self.token_cache.update_rt)

        regional_client = None
        if (client_credential  # Currently regional endpoint only serves some CCA flows
                and not skip_regional_client):
            regional_authority = self._get_regional_authority(authority)
            if regional_authority:
                regional_configuration = {
                    "authorization_endpoint": regional_authority.authorization_endpoint,
                    "token_endpoint": regional_authority.token_endpoint,
                    "device_authorization_endpoint":
                        regional_authority.device_authorization_endpoint,
                    }
                regional_client = _ClientWithCcsRoutingInfo(
                    regional_configuration,
                    self.client_id,
                    http_client=self.http_client,
                    default_headers=default_headers,
                    default_body=default_body,
                    client_assertion=client_assertion,
                    client_assertion_type=client_assertion_type,
                    on_obtaining_tokens=lambda event: self.token_cache.add(dict(
                        event, environment=authority.instance)),
                    on_removing_rt=self.token_cache.remove_rt,
                    on_updating_rt=self.token_cache.update_rt)
        return central_client, regional_client

    def initiate_auth_code_flow(
            self,
            scopes,  # type: list[str]
            redirect_uri=None,
            state=None,  # Recommended by OAuth2 for CSRF protection
            prompt=None,
            login_hint=None,  # type: Optional[str]
            domain_hint=None,  # type: Optional[str]
            claims_challenge=None,
            max_age=None,
            response_mode=None,  # type: Optional[str]
            ):
        """Initiate an auth code flow.

        Later when the response reaches your redirect_uri,
        you can use :func:`~acquire_token_by_auth_code_flow()`
        to complete the authentication/authorization.

        :param list scopes:
            It is a list of case-sensitive strings.
        :param str redirect_uri:
            Optional. If not specified, server will use the pre-registered one.
        :param str state:
            An opaque value used by the client to
            maintain state between the request and callback.
            If absent, this library will automatically generate one internally.
        :param str prompt:
            By default, no prompt value will be sent, not even string ``"none"``.
            You will have to specify a value explicitly.
            Its valid values are the constants defined in
            :class:`Prompt <msal.Prompt>`.

        :param str login_hint:
            Optional. Identifier of the user. Generally a User Principal Name (UPN).
        :param domain_hint:
            Can be one of "consumers" or "organizations" or your tenant domain "contoso.com".
            If included, it will skip the email-based discovery process that user goes
            through on the sign-in page, leading to a slightly more streamlined user experience.
            More information on possible values available in
            `Auth Code Flow doc <https://docs.microsoft.com/en-us/azure/active-directory/develop/v2-oauth2-auth-code-flow#request-an-authorization-code>`_ and
            `domain_hint doc <https://docs.microsoft.com/en-us/openspecs/windows_protocols/ms-oapx/86fb452d-e34a-494e-ac61-e526e263b6d8>`_.

        :param int max_age:
            OPTIONAL. Maximum Authentication Age.
            Specifies the allowable elapsed time in seconds
            since the last time the End-User was actively authenticated.
            If the elapsed time is greater than this value,
            Microsoft identity platform will actively re-authenticate the End-User.

            MSAL Python will also automatically validate the auth_time in ID token.

            New in version 1.15.

        :param str response_mode:
            OPTIONAL. Specifies the method with which response parameters should be returned.
            The default value is equivalent to ``query``, which is still secure enough in MSAL Python
            (because MSAL Python does not transfer tokens via query parameter in the first place).
            For even better security, we recommend using the value ``form_post``.
            In "form_post" mode, response parameters
            will be encoded as HTML form values that are transmitted via the HTTP POST method and
            encoded in the body using the application/x-www-form-urlencoded format.
            Valid values can be either "form_post" for HTTP POST to callback URI or
            "query" (the default) for HTTP GET with parameters encoded in query string.
            More information on possible values
            `here <https://openid.net/specs/oauth-v2-multiple-response-types-1_0.html#ResponseModes>`
            and `here <https://openid.net/specs/oauth-v2-form-post-response-mode-1_0.html#FormPostResponseMode>`

        :return:
            The auth code flow. It is a dict in this form::

                {
                    "auth_uri": "https://...",  // Guide user to visit this
                    "state": "...",  // You may choose to verify it by yourself,
                                     // or just let acquire_token_by_auth_code_flow()
                                     // do that for you.
                    "...": "...",  // Everything else are reserved and internal
                }

            The caller is expected to:

            1. somehow store this content, typically inside the current session,
            2. guide the end user (i.e. resource owner) to visit that auth_uri,
            3. and then relay this dict and subsequent auth response to
               :func:`~acquire_token_by_auth_code_flow()`.
        """
        client = _ClientWithCcsRoutingInfo(
            {"authorization_endpoint": self.authority.authorization_endpoint},
            self.client_id,
            http_client=self.http_client)
        flow = client.initiate_auth_code_flow(
            redirect_uri=redirect_uri, state=state, login_hint=login_hint,
            prompt=prompt,
            scope=self._decorate_scope(scopes),
            domain_hint=domain_hint,
            claims=_merge_claims_challenge_and_capabilities(
                self._client_capabilities, claims_challenge),
            max_age=max_age,
            response_mode=response_mode,
            )
        flow["claims_challenge"] = claims_challenge
        return flow

    def get_authorization_request_url(
            self,
            scopes,  # type: list[str]
            login_hint=None,  # type: Optional[str]
            state=None,  # Recommended by OAuth2 for CSRF protection
            redirect_uri=None,
            response_type="code",  # Could be "token" if you use Implicit Grant
            prompt=None,
            nonce=None,
            domain_hint=None,  # type: Optional[str]
            claims_challenge=None,
            **kwargs):
        """Constructs a URL for you to start a Authorization Code Grant.

        :param list[str] scopes: (Required)
            Scopes requested to access a protected API (a resource).
        :param str state: Recommended by OAuth2 for CSRF protection.
        :param str login_hint:
            Identifier of the user. Generally a User Principal Name (UPN).
        :param str redirect_uri:
            Address to return to upon receiving a response from the authority.
        :param str response_type:
            Default value is "code" for an OAuth2 Authorization Code grant.

            You could use other content such as "id_token" or "token",
            which would trigger an Implicit Grant, but that is
            `not recommended <https://docs.microsoft.com/en-us/azure/active-directory/develop/v2-oauth2-implicit-grant-flow#is-the-implicit-grant-suitable-for-my-app>`_.

        :param str prompt:
            By default, no prompt value will be sent, not even string ``"none"``.
            You will have to specify a value explicitly.
            Its valid values are the constants defined in
            :class:`Prompt <msal.Prompt>`.
        :param nonce:
            A cryptographically random value used to mitigate replay attacks. See also
            `OIDC specs <https://openid.net/specs/openid-connect-core-1_0.html#AuthRequest>`_.
        :param domain_hint:
            Can be one of "consumers" or "organizations" or your tenant domain "contoso.com".
            If included, it will skip the email-based discovery process that user goes
            through on the sign-in page, leading to a slightly more streamlined user experience.
            More information on possible values available in
            `Auth Code Flow doc <https://docs.microsoft.com/en-us/azure/active-directory/develop/v2-oauth2-auth-code-flow#request-an-authorization-code>`_ and
            `domain_hint doc <https://docs.microsoft.com/en-us/openspecs/windows_protocols/ms-oapx/86fb452d-e34a-494e-ac61-e526e263b6d8>`_.
        :param claims_challenge:
             The claims_challenge parameter requests specific claims requested by the resource provider
             in the form of a claims_challenge directive in the www-authenticate header to be
             returned from the UserInfo Endpoint and/or in the ID Token and/or Access Token.
             It is a string of a JSON object which contains lists of claims being requested from these locations.

        :return: The authorization url as a string.
        """
        authority = kwargs.pop("authority", None)  # Historically we support this
        if authority:
            warnings.warn(
                "We haven't decided if this method will accept authority parameter")
        # The previous implementation is, it will use self.authority by default.
        # Multi-tenant app can use new authority on demand
        the_authority = Authority(
            authority,
            self.http_client,
            instance_discovery=self._instance_discovery,
            ) if authority else self.authority

        client = _ClientWithCcsRoutingInfo(
            {"authorization_endpoint": the_authority.authorization_endpoint},
            self.client_id,
            http_client=self.http_client)
        warnings.warn(
            "Change your get_authorization_request_url() "
            "to initiate_auth_code_flow()", DeprecationWarning)
        with warnings.catch_warnings(record=True):
            return client.build_auth_request_uri(
                response_type=response_type,
                redirect_uri=redirect_uri, state=state, login_hint=login_hint,
                prompt=prompt,
                scope=self._decorate_scope(scopes),
                nonce=nonce,
                domain_hint=domain_hint,
                claims=_merge_claims_challenge_and_capabilities(
                    self._client_capabilities, claims_challenge),
                )

    def acquire_token_by_auth_code_flow(
            self, auth_code_flow, auth_response, scopes=None, **kwargs):
        """Validate the auth response being redirected back, and obtain tokens.

        It automatically provides nonce protection.

        :param dict auth_code_flow:
            The same dict returned by :func:`~initiate_auth_code_flow()`.
        :param dict auth_response:
            A dict of the query string received from auth server.
        :param list[str] scopes:
            Scopes requested to access a protected API (a resource).

            Most of the time, you can leave it empty.

            If you requested user consent for multiple resources, here you will
            need to provide a subset of what you required in
            :func:`~initiate_auth_code_flow()`.

            OAuth2 was designed mostly for singleton services,
            where tokens are always meant for the same resource and the only
            changes are in the scopes.
            In Microsoft Entra, tokens can be issued for multiple 3rd party resources.
            You can ask authorization code for multiple resources,
            but when you redeem it, the token is for only one intended
            recipient, called audience.
            So the developer need to specify a scope so that we can restrict the
            token to be issued for the corresponding audience.

        :return:
            * A dict containing "access_token" and/or "id_token", among others,
              depends on what scope was used.
              (See https://tools.ietf.org/html/rfc6749#section-5.1)
            * A dict containing "error", optionally "error_description", "error_uri".
              (It is either `this <https://tools.ietf.org/html/rfc6749#section-4.1.2.1>`_
              or `that <https://tools.ietf.org/html/rfc6749#section-5.2>`_)
            * Most client-side data error would result in ValueError exception.
              So the usage pattern could be without any protocol details::

                def authorize():  # A controller in a web app
                    try:
                        result = msal_app.acquire_token_by_auth_code_flow(
                            session.get("flow", {}), request.args)
                        if "error" in result:
                            return render_template("error.html", result)
                        use(result)  # Token(s) are available in result and cache
                    except ValueError:  # Usually caused by CSRF
                        pass  # Simply ignore them
                    return redirect(url_for("index"))
        """
        self._validate_ssh_cert_input_data(kwargs.get("data", {}))
        telemetry_context = self._build_telemetry_context(
            self.ACQUIRE_TOKEN_BY_AUTHORIZATION_CODE_ID)
        response = _clean_up(self.client.obtain_token_by_auth_code_flow(
            auth_code_flow,
            auth_response,
            scope=self._decorate_scope(scopes) if scopes else None,
            headers=telemetry_context.generate_headers(),
            data=dict(
                kwargs.pop("data", {}),
                claims=_merge_claims_challenge_and_capabilities(
                    self._client_capabilities,
                    auth_code_flow.pop("claims_challenge", None))),
            **kwargs))
        if "access_token" in response:
            response[self._TOKEN_SOURCE] = self._TOKEN_SOURCE_IDP
        telemetry_context.update_telemetry(response)
        return response

    def acquire_token_by_authorization_code(
            self,
            code,
            scopes,  # Syntactically required. STS accepts empty value though.
            redirect_uri=None,
                # REQUIRED, if the "redirect_uri" parameter was included in the
                # authorization request as described in Section 4.1.1, and their
                # values MUST be identical.
            nonce=None,
            claims_challenge=None,
            **kwargs):
        """The second half of the Authorization Code Grant.

        :param code: The authorization code returned from Authorization Server.
        :param list[str] scopes: (Required)
            Scopes requested to access a protected API (a resource).

            If you requested user consent for multiple resources, here you will
            typically want to provide a subset of what you required in AuthCode.

            OAuth2 was designed mostly for singleton services,
            where tokens are always meant for the same resource and the only
            changes are in the scopes.
            In Microsoft Entra, tokens can be issued for multiple 3rd party resources.
            You can ask authorization code for multiple resources,
            but when you redeem it, the token is for only one intended
            recipient, called audience.
            So the developer need to specify a scope so that we can restrict the
            token to be issued for the corresponding audience.

        :param nonce:
            If you provided a nonce when calling :func:`get_authorization_request_url`,
            same nonce should also be provided here, so that we'll validate it.
            An exception will be raised if the nonce in id token mismatches.

        :param claims_challenge:
            The claims_challenge parameter requests specific claims requested by the resource provider
            in the form of a claims_challenge directive in the www-authenticate header to be
            returned from the UserInfo Endpoint and/or in the ID Token and/or Access Token.
            It is a string of a JSON object which contains lists of claims being requested from these locations.

        :return: A dict representing the json response from Microsoft Entra:

            - A successful response would contain "access_token" key,
            - an error response would contain "error" and usually "error_description".
        """
        # If scope is absent on the wire, STS will give you a token associated
        # to the FIRST scope sent during the authorization request.
        # So in theory, you can omit scope here when you were working with only
        # one scope. But, MSAL decorates your scope anyway, so they are never
        # really empty.
        assert isinstance(scopes, list), "Invalid parameter type"
        self._validate_ssh_cert_input_data(kwargs.get("data", {}))
        warnings.warn(
            "Change your acquire_token_by_authorization_code() "
            "to acquire_token_by_auth_code_flow()", DeprecationWarning)
        with warnings.catch_warnings(record=True):
            telemetry_context = self._build_telemetry_context(
                self.ACQUIRE_TOKEN_BY_AUTHORIZATION_CODE_ID)
            response = _clean_up(self.client.obtain_token_by_authorization_code(
                code, redirect_uri=redirect_uri,
                scope=self._decorate_scope(scopes),
                headers=telemetry_context.generate_headers(),
                data=dict(
                    kwargs.pop("data", {}),
                    claims=_merge_claims_challenge_and_capabilities(
                        self._client_capabilities, claims_challenge)),
                nonce=nonce,
                **kwargs))
            if "access_token" in response:
                response[self._TOKEN_SOURCE] = self._TOKEN_SOURCE_IDP
            telemetry_context.update_telemetry(response)
            return response

    def get_accounts(self, username=None):
        """Get a list of accounts which previously signed in, i.e. exists in cache.

        An account can later be used in :func:`~acquire_token_silent`
        to find its tokens.

        :param username:
            Filter accounts with this username only. Case insensitive.
        :return: A list of account objects.
            Each account is a dict. For now, we only document its "username" field.
            Your app can choose to display those information to end user,
            and allow user to choose one of his/her accounts to proceed.
        """
        accounts = self._find_msal_accounts(environment=self.authority.instance)
        if not accounts:  # Now try other aliases of this authority instance
            for alias in self._get_authority_aliases(self.authority.instance):
                accounts = self._find_msal_accounts(environment=alias)
                if accounts:
                    break
        if username:
            # Federated account["username"] from AAD could contain mixed case
            lowercase_username = username.lower()
            accounts = [a for a in accounts
                if a["username"].lower() == lowercase_username]
            if not accounts:
                logger.debug((  # This would also happen when the cache is empty
                    "get_accounts(username='{}') finds no account. "
                    "If tokens were acquired without 'profile' scope, "
                    "they would contain no username for filtering. "
                    "Consider calling get_accounts(username=None) instead."
                    ).format(username))
        # Does not further filter by existing RTs here. It probably won't matter.
        # Because in most cases Accounts and RTs co-exist.
        # Even in the rare case when an RT is revoked and then removed,
        # acquire_token_silent() would then yield no result,
        # apps would fall back to other acquire methods. This is the standard pattern.
        return accounts

    def _find_msal_accounts(self, environment):
        interested_authority_types = [
            TokenCache.AuthorityType.ADFS, TokenCache.AuthorityType.MSSTS]
        if _is_running_in_cloud_shell():
            interested_authority_types.append(_AUTHORITY_TYPE_CLOUDSHELL)
        grouped_accounts = {
            a.get("home_account_id"):  # Grouped by home tenant's id
                {  # These are minimal amount of non-tenant-specific account info
                    "home_account_id": a.get("home_account_id"),
                    "environment": a.get("environment"),
                    "username": a.get("username"),
                    "account_source": a.get("account_source"),

                    # The following fields for backward compatibility, for now
                    "authority_type": a.get("authority_type"),
                    "local_account_id": a.get("local_account_id"),  # Tenant-specific
                    "realm": a.get("realm"),  # Tenant-specific
                }
            for a in self.token_cache.search(
                TokenCache.CredentialType.ACCOUNT,
                query={"environment": environment})
            if a["authority_type"] in interested_authority_types
            }
        return list(grouped_accounts.values())

    def _get_instance_metadata(self):  # This exists so it can be mocked in unit test
        resp = self.http_client.get(
            "https://login.microsoftonline.com/common/discovery/instance?api-version=1.1&authorization_endpoint=https://login.microsoftonline.com/common/oauth2/authorize",  # TBD: We may extend this to use self._instance_discovery endpoint
            headers={'Accept': 'application/json'})
        resp.raise_for_status()
        return json.loads(resp.text)['metadata']

    def _get_authority_aliases(self, instance):
        if self._instance_discovery is False:
            return []
        if self.authority._is_known_to_developer:
            # Then it is an ADFS/B2C/known_authority_hosts situation
            # which may not reach the central endpoint, so we skip it.
            return []
        if not self.authority_groups:
            self.authority_groups = [
                set(group['aliases']) for group in self._get_instance_metadata()]
        for group in self.authority_groups:
            if instance in group:
                return [alias for alias in group if alias != instance]
        return []

    def remove_account(self, account):
        """Sign me out and forget me from token cache"""
        if self._enable_broker:
            from .broker import _signout_silently
            error = _signout_silently(self.client_id, account["local_account_id"])
            if error:
                logger.debug("_signout_silently() returns error: %s", error)
        # Broker sign-out has been attempted, even if the _forget_me() below throws.
        self._forget_me(account)

    def _sign_out(self, home_account):
        # Remove all relevant RTs and ATs from token cache
        owned_by_home_account = {
            "environment": home_account["environment"],
            "home_account_id": home_account["home_account_id"],}  # realm-independent
        app_metadata = self._get_app_metadata(home_account["environment"])
        # Remove RTs/FRTs, and they are realm-independent
        for rt in [  # Remove RTs from a static list (rather than from a dynamic generator),
                    # to avoid changing self.token_cache while it is being iterated
            rt for rt in self.token_cache.search(
                TokenCache.CredentialType.REFRESH_TOKEN, query=owned_by_home_account)
                # Do RT's app ownership check as a precaution, in case family apps
                # and 3rd-party apps share same token cache, although they should not.
                if rt["client_id"] == self.client_id or (
                    app_metadata.get("family_id")  # Now let's settle family business
                    and rt.get("family_id") == app_metadata["family_id"])
        ]:
            self.token_cache.remove_rt(rt)
        for at in list(self.token_cache.search(  # Remove ATs from a static list,
                # to avoid changing self.token_cache while it is being iterated
            TokenCache.CredentialType.ACCESS_TOKEN, query=owned_by_home_account,
            # Regardless of realm, b/c we've removed realm-independent RTs anyway
        )):
            # To avoid the complexity of locating sibling family app's AT,
            # we skip AT's app ownership check.
            # It means ATs for other apps will also be removed, it is OK because:
            # * non-family apps are not supposed to share token cache to begin with;
            # * Even if it happens, we keep other app's RT already, so SSO still works
            self.token_cache.remove_at(at)

    def _forget_me(self, home_account):
        # It implies signout, and then also remove all relevant accounts and IDTs
        self._sign_out(home_account)
        owned_by_home_account = {
            "environment": home_account["environment"],
            "home_account_id": home_account["home_account_id"],}  # realm-independent
        for idt in list(self.token_cache.search(  # Remove IDTs from a static list,
                # to avoid changing self.token_cache while it is being iterated
            TokenCache.CredentialType.ID_TOKEN, query=owned_by_home_account, # regardless of realm
        )):
            self.token_cache.remove_idt(idt)
        for a in list(self.token_cache.search(  # Remove Accounts from a static list,
                # to avoid changing self.token_cache while it is being iterated
            TokenCache.CredentialType.ACCOUNT, query=owned_by_home_account,  # regardless of realm
        )):
            self.token_cache.remove_account(a)

    def _acquire_token_by_cloud_shell(self, scopes, data=None):
        from .cloudshell import _obtain_token
        response = _obtain_token(
            self.http_client, scopes, client_id=self.client_id, data=data)
        if "error" not in response:
            self.token_cache.add(dict(
                client_id=self.client_id,
                scope=response["scope"].split() if "scope" in response else scopes,
                token_endpoint=self.authority.token_endpoint,
                response=response,
                data=data or {},
                authority_type=_AUTHORITY_TYPE_CLOUDSHELL,
                ))
        if "access_token" in response:
            response[self._TOKEN_SOURCE] = self._TOKEN_SOURCE_BROKER
        return response

    def acquire_token_silent(
            self,
            scopes,  # type: List[str]
            account,  # type: Optional[Account]
            authority=None,  # See get_authorization_request_url()
            force_refresh=False,  # type: Optional[boolean]
            claims_challenge=None,
            auth_scheme=None,
            **kwargs):
        """Acquire an access token for given account, without user interaction.

        It has same parameters as the :func:`~acquire_token_silent_with_error`.
        The difference is the behavior of the return value.
        This method will combine the cache empty and refresh error
        into one return value, `None`.
        If your app does not care about the exact token refresh error during
        token cache look-up, then this method is easier and recommended.

        :return:
            - A dict containing no "error" key,
              and typically contains an "access_token" key,
              if cache lookup succeeded.
            - None when cache lookup does not yield a token.
        """
        if not account:
            return None  # A backward-compatible NO-OP to drop the account=None usage
        result = _clean_up(self._acquire_token_silent_with_error(
            scopes, account, authority=authority, force_refresh=force_refresh,
            claims_challenge=claims_challenge, auth_scheme=auth_scheme, **kwargs))
        return result if result and "error" not in result else None

    def acquire_token_silent_with_error(
            self,
            scopes,  # type: List[str]
            account,  # type: Optional[Account]
            authority=None,  # See get_authorization_request_url()
            force_refresh=False,  # type: Optional[boolean]
            claims_challenge=None,
            auth_scheme=None,
            **kwargs):
        """Acquire an access token for given account, without user interaction.

        It is done either by finding a valid access token from cache,
        or by finding a valid refresh token from cache and then automatically
        use it to redeem a new access token.

        This method will differentiate cache empty from token refresh error.
        If your app cares the exact token refresh error during
        token cache look-up, then this method is suitable.
        Otherwise, the other method :func:`~acquire_token_silent` is recommended.

        :param list[str] scopes: (Required)
            Scopes requested to access a protected API (a resource).
        :param account: (Required)
            One of the account object returned by :func:`~get_accounts`.
            Starting from MSAL Python 1.23,
            a ``None`` input will become a NO-OP and always return ``None``.
        :param force_refresh:
            If True, it will skip Access Token look-up,
            and try to find a Refresh Token to obtain a new Access Token.
        :param claims_challenge:
            The claims_challenge parameter requests specific claims requested by the resource provider
            in the form of a claims_challenge directive in the www-authenticate header to be
            returned from the UserInfo Endpoint and/or in the ID Token and/or Access Token.
            It is a string of a JSON object which contains lists of claims being requested from these locations.
        :param object auth_scheme:
            You can provide an ``msal.auth_scheme.PopAuthScheme`` object
            so that MSAL will get a Proof-of-Possession (POP) token for you.

            New in version 1.26.0.

        :return:
            - A dict containing no "error" key,
              and typically contains an "access_token" key,
              if cache lookup succeeded.
            - None when there is simply no token in the cache.
            - A dict containing an "error" key, when token refresh failed.
        """
        if not account:
            return None  # A backward-compatible NO-OP to drop the account=None usage
        return _clean_up(self._acquire_token_silent_with_error(
            scopes, account, authority=authority, force_refresh=force_refresh,
            claims_challenge=claims_challenge, auth_scheme=auth_scheme, **kwargs))

    def _acquire_token_silent_with_error(
            self,
            scopes,  # type: List[str]
            account,  # type: Optional[Account]
            authority=None,  # See get_authorization_request_url()
            force_refresh=False,  # type: Optional[boolean]
            claims_challenge=None,
            auth_scheme=None,
            **kwargs):
        assert isinstance(scopes, list), "Invalid parameter type"
        self._validate_ssh_cert_input_data(kwargs.get("data", {}))
        correlation_id = msal.telemetry._get_new_correlation_id()
        if authority:
            warnings.warn("We haven't decided how/if this method will accept authority parameter")
        # the_authority = Authority(
        #     authority,
        #     self.http_client,
        #     instance_discovery=self._instance_discovery,
        #     ) if authority else self.authority
        result = self._acquire_token_silent_from_cache_and_possibly_refresh_it(
            scopes, account, self.authority, force_refresh=force_refresh,
            claims_challenge=claims_challenge,
            correlation_id=correlation_id,
            auth_scheme=auth_scheme,
            **kwargs)
        if result and "error" not in result:
            return result
        final_result = result
        for alias in self._get_authority_aliases(self.authority.instance):
            if not list(self.token_cache.search(  # Need a list to test emptiness
                    self.token_cache.CredentialType.REFRESH_TOKEN,
                    # target=scopes,  # MUST NOT filter by scopes, because:
                        # 1. AAD RTs are scope-independent;
                        # 2. therefore target is optional per schema;
                    query={"environment": alias})):
                # Skip heavy weight logic when RT for this alias doesn't exist
                continue
            the_authority = Authority(
                "https://" + alias + "/" + self.authority.tenant,
                self.http_client,
                instance_discovery=False,
                )
            result = self._acquire_token_silent_from_cache_and_possibly_refresh_it(
                scopes, account, the_authority, force_refresh=force_refresh,
                claims_challenge=claims_challenge,
                correlation_id=correlation_id,
                auth_scheme=auth_scheme,
                **kwargs)
            if result:
                if "error" not in result:
                    return result
                final_result = result
        if final_result and final_result.get("suberror"):
            final_result["classification"] = {  # Suppress these suberrors, per #57
                "bad_token": "",
                "token_expired": "",
                "protection_policy_required": "",
                "client_mismatch": "",
                "device_authentication_failed": "",
                }.get(final_result["suberror"], final_result["suberror"])
        return final_result

    def _acquire_token_silent_from_cache_and_possibly_refresh_it(
            self,
            scopes,  # type: List[str]
            account,  # type: Optional[Account]
            authority,  # This can be different than self.authority
            force_refresh=False,  # type: Optional[boolean]
            claims_challenge=None,
            correlation_id=None,
            http_exceptions=None,
            auth_scheme=None,
            **kwargs):
        # This internal method has two calling patterns:
        # it accepts a non-empty account to find token for a user,
        # and accepts account=None to find a token for the current app.
        access_token_from_cache = None
        if not (force_refresh or claims_challenge or auth_scheme):  # Then attempt AT cache
            query={
                    "client_id": self.client_id,
                    "environment": authority.instance,
                    "realm": authority.tenant,
                    "home_account_id": (account or {}).get("home_account_id"),
                    }
            key_id = kwargs.get("data", {}).get("key_id")
            if key_id:  # Some token types (SSH-certs, POP) are bound to a key
                query["key_id"] = key_id
            now = time.time()
            refresh_reason = msal.telemetry.AT_ABSENT
            for entry in self.token_cache.search(  # A generator allows us to
                    # break early in cache-hit without finding a full list
                self.token_cache.CredentialType.ACCESS_TOKEN,
                target=scopes,
                query=query,
            ):  # This loop is about token search, not about token deletion.
                # Note that search() holds a lock during this loop;
                # that is fine because this loop is fast
                expires_in = int(entry["expires_on"]) - now
                if expires_in < 5*60:  # Then consider it expired
                    refresh_reason = msal.telemetry.AT_EXPIRED
                    continue  # Removal is not necessary, it will be overwritten
                logger.debug("Cache hit an AT")
                access_token_from_cache = {  # Mimic a real response
                    "access_token": entry["secret"],
                    "token_type": entry.get("token_type", "Bearer"),
                    "expires_in": int(expires_in),  # OAuth2 specs defines it as int
                    self._TOKEN_SOURCE: self._TOKEN_SOURCE_CACHE,
                    }
                if "refresh_on" in entry:
                    access_token_from_cache["refresh_on"] = int(entry["refresh_on"])
                    if int(entry["refresh_on"]) < now:  # aging
                        refresh_reason = msal.telemetry.AT_AGING
                        break  # With a fallback in hand, we break here to go refresh
                self._build_telemetry_context(-1).hit_an_access_token()
                return access_token_from_cache  # It is still good as new
        else:
            refresh_reason = msal.telemetry.FORCE_REFRESH  # TODO: It could also mean claims_challenge
        assert refresh_reason, "It should have been established at this point"
        if not http_exceptions:  # It can be a tuple of exceptions
            # The exact HTTP exceptions are transportation-layer dependent
            from requests.exceptions import RequestException  # Lazy load
            http_exceptions = (RequestException,)
        try:
            data = kwargs.get("data", {})
            if account and account.get("authority_type") == _AUTHORITY_TYPE_CLOUDSHELL:
                if auth_scheme:
                    raise ValueError("auth_scheme is not supported in Cloud Shell")
                return self._acquire_token_by_cloud_shell(scopes, data=data)

            if self._enable_broker and account and account.get("account_source") in (
                _GRANT_TYPE_BROKER,  # Broker successfully established this account previously.
                None,  # Unknown data from older MSAL. Broker might still work.
            ):
                from .broker import _acquire_token_silently
                response = _acquire_token_silently(
                    "https://{}/{}".format(self.authority.instance, self.authority.tenant),
                    self.client_id,
                    account["local_account_id"],
                    scopes,
                    claims=_merge_claims_challenge_and_capabilities(
                        self._client_capabilities, claims_challenge),
                    correlation_id=correlation_id,
                    auth_scheme=auth_scheme,
                    **data)
                if response:  # Broker provides a decisive outcome
                    account_was_established_by_broker = account.get(
                        "account_source") == _GRANT_TYPE_BROKER
                    broker_attempt_succeeded_just_now = "error" not in response
                    if account_was_established_by_broker or broker_attempt_succeeded_just_now:
                        return self._process_broker_response(response, scopes, data)

            if auth_scheme:
                raise ValueError(self._AUTH_SCHEME_UNSUPPORTED)
            if account:
                result = self._acquire_token_silent_by_finding_rt_belongs_to_me_or_my_family(
                    authority, self._decorate_scope(scopes), account,
                    refresh_reason=refresh_reason, claims_challenge=claims_challenge,
                    correlation_id=correlation_id,
                    **kwargs)
            else:  # The caller is acquire_token_for_client()
                result = self._acquire_token_for_client(
                    scopes, refresh_reason, claims_challenge=claims_challenge,
                    **kwargs)
            if result and "access_token" in result:
                result[self._TOKEN_SOURCE] = self._TOKEN_SOURCE_IDP
            if (result and "error" not in result) or (not access_token_from_cache):
                return result
        except http_exceptions:
            # Typically network error. Potential AAD outage?
            if not access_token_from_cache:  # It means there is no fall back option
                raise  # We choose to bubble up the exception
        return access_token_from_cache

    def _process_broker_response(self, response, scopes, data):
        if "error" not in response:
            self.token_cache.add(dict(
                client_id=self.client_id,
                scope=response["scope"].split() if "scope" in response else scopes,
                token_endpoint=self.authority.token_endpoint,
                response=response,
                data=data,
                _account_id=response["_account_id"],
                environment=self.authority.instance,  # Be consistent with non-broker flows
                grant_type=_GRANT_TYPE_BROKER,  # A pseudo grant type for TokenCache to mark account_source as broker
                ))
            response[self._TOKEN_SOURCE] = self._TOKEN_SOURCE_BROKER
        return _clean_up(response)

    def _acquire_token_silent_by_finding_rt_belongs_to_me_or_my_family(
            self, authority, scopes, account, **kwargs):
        query = {
            "environment": authority.instance,
            "home_account_id": (account or {}).get("home_account_id"),
            # "realm": authority.tenant,  # AAD RTs are tenant-independent
            }
        app_metadata = self._get_app_metadata(authority.instance)
        if not app_metadata:  # Meaning this app is now used for the first time.
            # When/if we have a way to directly detect current app's family,
            # we'll rewrite this block, to support multiple families.
            # For now, we try existing RTs (*). If it works, we are in that family.
            # (*) RTs of a different app/family are not supposed to be
            # shared with or accessible by us in the first place.
            at = self._acquire_token_silent_by_finding_specific_refresh_token(
                authority, scopes,
                dict(query, family_id="1"),  # A hack, we have only 1 family for now
                rt_remover=lambda rt_item: None,  # NO-OP b/c RTs are likely not mine
                break_condition=lambda response:  # Break loop when app not in family
                    # Based on an AAD-only behavior mentioned in internal doc here
                    # https://msazure.visualstudio.com/One/_git/ESTS-Docs/pullrequest/1138595
                    "client_mismatch" in response.get("error_additional_info", []),
                **kwargs)
            if at and "error" not in at:
                return at
        last_resp = None
        if app_metadata.get("family_id"):  # Meaning this app belongs to this family
            last_resp = at = self._acquire_token_silent_by_finding_specific_refresh_token(
                authority, scopes, dict(query, family_id=app_metadata["family_id"]),
                **kwargs)
            if at and "error" not in at:
                return at
        # Either this app is an orphan, so we will naturally use its own RT;
        # or all attempts above have failed, so we fall back to non-foci behavior.
        return self._acquire_token_silent_by_finding_specific_refresh_token(
            authority, scopes, dict(query, client_id=self.client_id),
            **kwargs) or last_resp

    def _get_app_metadata(self, environment):
        return self.token_cache._get_app_metadata(
            environment=environment, client_id=self.client_id, default={})

    def _acquire_token_silent_by_finding_specific_refresh_token(
            self, authority, scopes, query,
            rt_remover=None, break_condition=lambda response: False,
            refresh_reason=None, correlation_id=None, claims_challenge=None,
            **kwargs):
        matches = list(self.token_cache.search(  # We want a list to test emptiness
            self.token_cache.CredentialType.REFRESH_TOKEN,
            # target=scopes,  # AAD RTs are scope-independent
            query=query))
        logger.debug("Found %d RTs matching %s", len(matches), {
            k: _pii_less_home_account_id(v) if k == "home_account_id" and v else v
            for k, v in query.items()
        })

        response = None  # A distinguishable value to mean cache is empty
        if not matches:  # Then exit early to avoid expensive operations
            return response
        client, _ = self._build_client(
            # Potentially expensive if building regional client
            self.client_credential, authority, skip_regional_client=True)
        telemetry_context = self._build_telemetry_context(
            self.ACQUIRE_TOKEN_SILENT_ID,
            correlation_id=correlation_id, refresh_reason=refresh_reason)
        for entry in sorted(  # Since unfit RTs would not be aggressively removed,
                              # we start from newer RTs which are more likely fit.
                matches,
                key=lambda e: int(e.get("last_modification_time", "0")),
                reverse=True):
            logger.debug("Cache attempts an RT")
            headers = telemetry_context.generate_headers()
            if query.get("home_account_id"):  # Then use it as CCS Routing info
                headers["X-AnchorMailbox"] = "Oid:{}".format(  # case-insensitive value
                    query["home_account_id"].replace(".", "@"))
            response = client.obtain_token_by_refresh_token(
                entry, rt_getter=lambda token_item: token_item["secret"],
                on_removing_rt=lambda rt_item: None,  # Disable RT removal,
                    # because an invalid_grant could be caused by new MFA policy,
                    # the RT could still be useful for other MFA-less scope or tenant
                on_obtaining_tokens=lambda event: self.token_cache.add(dict(
                    event,
                    environment=authority.instance,
                    skip_account_creation=True,  # To honor a concurrent remove_account()
                    )),
                scope=scopes,
                headers=headers,
                data=dict(
                    kwargs.pop("data", {}),
                    claims=_merge_claims_challenge_and_capabilities(
                        self._client_capabilities, claims_challenge)),
                **kwargs)
            telemetry_context.update_telemetry(response)
            if "error" not in response:
                return response
            logger.debug("Refresh failed. {error}: {error_description}".format(
                error=response.get("error"),
                error_description=response.get("error_description"),
                ))
            if break_condition(response):
                break
        return response  # Returns the latest error (if any), or just None

    def _validate_ssh_cert_input_data(self, data):
        if data.get("token_type") == "ssh-cert":
            if not data.get("req_cnf"):
                raise ValueError(
                    "When requesting an SSH certificate, "
                    "you must include a string parameter named 'req_cnf' "
                    "containing the public key in JWK format "
                    "(https://tools.ietf.org/html/rfc7517).")
            if not data.get("key_id"):
                raise ValueError(
                    "When requesting an SSH certificate, "
                    "you must include a string parameter named 'key_id' "
                    "which identifies the key in the 'req_cnf' argument.")

    def acquire_token_by_refresh_token(self, refresh_token, scopes, **kwargs):
        """Acquire token(s) based on a refresh token (RT) obtained from elsewhere.

        You use this method only when you have old RTs from elsewhere,
        and now you want to migrate them into MSAL.
        Calling this method results in new tokens automatically storing into MSAL.

        You do NOT need to use this method if you are already using MSAL.
        MSAL maintains RT automatically inside its token cache,
        and an access token can be retrieved
        when you call :func:`~acquire_token_silent`.

        :param str refresh_token: The old refresh token, as a string.

        :param list scopes:
            The scopes associate with this old RT.
            Each scope needs to be in the Microsoft identity platform (v2) format.
            See `Scopes not resources <https://docs.microsoft.com/en-us/azure/active-directory/develop/migrate-python-adal-msal#scopes-not-resources>`_.

        :return:
            * A dict contains "error" and some other keys, when error happened.
            * A dict contains no "error" key means migration was successful.
        """
        self._validate_ssh_cert_input_data(kwargs.get("data", {}))
        telemetry_context = self._build_telemetry_context(
            self.ACQUIRE_TOKEN_BY_REFRESH_TOKEN,
            refresh_reason=msal.telemetry.FORCE_REFRESH)
        response = _clean_up(self.client.obtain_token_by_refresh_token(
            refresh_token,
            scope=self._decorate_scope(scopes),
            headers=telemetry_context.generate_headers(),
            rt_getter=lambda rt: rt,
            on_updating_rt=False,
            on_removing_rt=lambda rt_item: None,  # No OP
            **kwargs))
        if "access_token" in response:
            response[self._TOKEN_SOURCE] = self._TOKEN_SOURCE_IDP
        telemetry_context.update_telemetry(response)
        return response

    def acquire_token_by_username_password(
            self, username, password, scopes, claims_challenge=None,
            # Note: We shouldn't need to surface enable_msa_passthrough,
            # because this ROPC won't work with MSA account anyway.
            auth_scheme=None,
            **kwargs):
        """Gets a token for a given resource via user credentials.

        See this page for constraints of Username Password Flow.
        https://github.com/AzureAD/microsoft-authentication-library-for-python/wiki/Username-Password-Authentication

        :param str username: Typically a UPN in the form of an email address.
        :param str password: The password.
        :param list[str] scopes:
            Scopes requested to access a protected API (a resource).
        :param claims_challenge:
            The claims_challenge parameter requests specific claims requested by the resource provider
            in the form of a claims_challenge directive in the www-authenticate header to be
            returned from the UserInfo Endpoint and/or in the ID Token and/or Access Token.
            It is a string of a JSON object which contains lists of claims being requested from these locations.

        :param object auth_scheme:
            You can provide an ``msal.auth_scheme.PopAuthScheme`` object
            so that MSAL will get a Proof-of-Possession (POP) token for you.

            New in version 1.26.0.

        :return: A dict representing the json response from Microsoft Entra:

            - A successful response would contain "access_token" key,
            - an error response would contain "error" and usually "error_description".
        """
        claims = _merge_claims_challenge_and_capabilities(
                self._client_capabilities, claims_challenge)
        if self._enable_broker:
            from .broker import _signin_silently
            response = _signin_silently(
                "https://{}/{}".format(self.authority.instance, self.authority.tenant),
                self.client_id,
                scopes,  # Decorated scopes won't work due to offline_access
                MSALRuntime_Username=username,
                MSALRuntime_Password=password,
                validateAuthority="no" if (
                    self.authority._is_known_to_developer
                    or self._instance_discovery is False) else None,
                claims=claims,
                auth_scheme=auth_scheme,
                )
            return self._process_broker_response(response, scopes, kwargs.get("data", {}))

        if auth_scheme:
            raise ValueError(self._AUTH_SCHEME_UNSUPPORTED)
        scopes = self._decorate_scope(scopes)
        telemetry_context = self._build_telemetry_context(
            self.ACQUIRE_TOKEN_BY_USERNAME_PASSWORD_ID)
        headers = telemetry_context.generate_headers()
        data = dict(kwargs.pop("data", {}), claims=claims)
        response = None
        if not self.authority.is_adfs:
            user_realm_result = self.authority.user_realm_discovery(
                username, correlation_id=headers[msal.telemetry.CLIENT_REQUEST_ID])
            if user_realm_result.get("account_type") == "Federated":
                response = _clean_up(self._acquire_token_by_username_password_federated(
                    user_realm_result, username, password, scopes=scopes,
                    data=data,
                    headers=headers, **kwargs))
        if response is None:  # Either ADFS or not federated
            response = _clean_up(self.client.obtain_token_by_username_password(
                username, password, scope=scopes,
                headers=headers,
                data=data,
                **kwargs))
        if "access_token" in response:
            response[self._TOKEN_SOURCE] = self._TOKEN_SOURCE_IDP
        telemetry_context.update_telemetry(response)
        return response

    def _acquire_token_by_username_password_federated(
            self, user_realm_result, username, password, scopes=None, **kwargs):
        wstrust_endpoint = {}
        if user_realm_result.get("federation_metadata_url"):
            wstrust_endpoint = mex_send_request(
                user_realm_result["federation_metadata_url"],
                self.http_client)
            if wstrust_endpoint is None:
                raise ValueError("Unable to find wstrust endpoint from MEX. "
                    "This typically happens when attempting MSA accounts. "
                    "More details available here. "
                    "https://github.com/AzureAD/microsoft-authentication-library-for-python/wiki/Username-Password-Authentication")
        logger.debug("wstrust_endpoint = %s", wstrust_endpoint)
        wstrust_result = wst_send_request(
            username, password,
            user_realm_result.get("cloud_audience_urn", "urn:federation:MicrosoftOnline"),
            wstrust_endpoint.get("address",
                # Fallback to an AAD supplied endpoint
                user_realm_result.get("federation_active_auth_url")),
            wstrust_endpoint.get("action"), self.http_client)
        if not ("token" in wstrust_result and "type" in wstrust_result):
            raise RuntimeError("Unsuccessful RSTR. %s" % wstrust_result)
        GRANT_TYPE_SAML1_1 = 'urn:ietf:params:oauth:grant-type:saml1_1-bearer'
        grant_type = {
            SAML_TOKEN_TYPE_V1: GRANT_TYPE_SAML1_1,
            SAML_TOKEN_TYPE_V2: self.client.GRANT_TYPE_SAML2,
            WSS_SAML_TOKEN_PROFILE_V1_1: GRANT_TYPE_SAML1_1,
            WSS_SAML_TOKEN_PROFILE_V2: self.client.GRANT_TYPE_SAML2
            }.get(wstrust_result.get("type"))
        if not grant_type:
            raise RuntimeError(
                "RSTR returned unknown token type: %s", wstrust_result.get("type"))
        self.client.grant_assertion_encoders.setdefault(  # Register a non-standard type
            grant_type, self.client.encode_saml_assertion)
        return self.client.obtain_token_by_assertion(
            wstrust_result["token"], grant_type, scope=scopes,
            on_obtaining_tokens=lambda event: self.token_cache.add(dict(
                event,
                environment=self.authority.instance,
                username=username,  # Useful in case IDT contains no such info
                )),
            **kwargs)


class PublicClientApplication(ClientApplication):  # browser app or mobile app

    DEVICE_FLOW_CORRELATION_ID = "_correlation_id"
    CONSOLE_WINDOW_HANDLE = object()

    def __init__(self, client_id, client_credential=None, **kwargs):
        """Same as :func:`ClientApplication.__init__`,
        except that ``client_credential`` parameter shall remain ``None``.

        .. note::

            You may set enable_broker_on_windows and/or enable_broker_on_mac to True.

            **What is a broker, and why use it?**

            A broker is a component installed on your device.
            Broker implicitly gives your device an identity. By using a broker,
            your device becomes a factor that can satisfy MFA (Multi-factor authentication).
            This factor would become mandatory
            if a tenant's admin enables a corresponding Conditional Access (CA) policy.
            The broker's presence allows Microsoft identity platform
            to have higher confidence that the tokens are being issued to your device,
            and that is more secure.

            An additional benefit of broker is,
            it runs as a long-lived process with your device's OS,
            and maintains its own cache,
            so that your broker-enabled apps (even a CLI)
            could automatically SSO from a previously established signed-in session.

            **You shall only enable broker when your app:**

            1. is running on supported platforms,
               and already registered their corresponding redirect_uri

               * ``ms-appx-web://Microsoft.AAD.BrokerPlugin/your_client_id``
                 if your app is expected to run on Windows 10+
               * ``msauth.com.msauth.unsignedapp://auth``
                 if your app is expected to run on Mac

            2. installed broker dependency,
               e.g. ``pip install msal[broker]>=1.31,<2``.

            3. tested with ``acquire_token_interactive()`` and ``acquire_token_silent()``.

            **The fallback behaviors of MSAL Python's broker support**

            MSAL will either error out, or silently fallback to non-broker flows.

            1. MSAL will ignore the `enable_broker_...` and bypass broker
               on those auth flows that are known to be NOT supported by broker.
               This includes ADFS, B2C, etc..
               For other "could-use-broker" scenarios, please see below.
            2. MSAL errors out when app developer opted-in to use broker
               but a direct dependency "mid-tier" package is not installed.
               Error message guides app developer to declare the correct dependency
               ``msal[broker]``.
               We error out here because the error is actionable to app developers.
            3. MSAL silently "deactivates" the broker and fallback to non-broker,
               when opted-in, dependency installed yet failed to initialize.
               We anticipate this would happen on a device whose OS is too old
               or the underlying broker component is somehow unavailable.
               There is not much an app developer or the end user can do here.
               Eventually, the conditional access policy shall
               force the user to switch to a different device.
            4. MSAL errors out when broker is opted in, installed, initialized,
               but subsequent token request(s) failed.

        :param boolean enable_broker_on_windows:
            This setting is only effective if your app is running on Windows 10+.
            This parameter defaults to None, which means MSAL will not utilize a broker.

            New in MSAL Python 1.25.0.

        :param boolean enable_broker_on_mac:
            This setting is only effective if your app is running on Mac.
            This parameter defaults to None, which means MSAL will not utilize a broker.

            New in MSAL Python 1.31.0.
        """
        if client_credential is not None:
            raise ValueError("Public Client should not possess credentials")
        # Using kwargs notation for now. We will switch to keyword-only arguments.
        enable_broker_on_windows = kwargs.pop("enable_broker_on_windows", False)
        enable_broker_on_mac = kwargs.pop("enable_broker_on_mac", False)
        self._enable_broker = bool(
            enable_broker_on_windows and sys.platform == "win32"
            or enable_broker_on_mac and sys.platform == "darwin")
        super(PublicClientApplication, self).__init__(
            client_id, client_credential=None, **kwargs)

    def acquire_token_interactive(
            self,
            scopes,  # type: list[str]
            prompt=None,
            login_hint=None,  # type: Optional[str]
            domain_hint=None,  # type: Optional[str]
            claims_challenge=None,
            timeout=None,
            port=None,
            extra_scopes_to_consent=None,
            max_age=None,
            parent_window_handle=None,
            on_before_launching_ui=None,
            auth_scheme=None,
            **kwargs):
        """Acquire token interactively i.e. via a local browser.

        Prerequisite: In Azure Portal, configure the Redirect URI of your
        "Mobile and Desktop application" as ``http://localhost``.
        If you opts in to use broker during ``PublicClientApplication`` creation,
        your app also need this Redirect URI:
        ``ms-appx-web://Microsoft.AAD.BrokerPlugin/YOUR_CLIENT_ID``

        :param list scopes:
            It is a list of case-sensitive strings.
        :param str prompt:
            By default, no prompt value will be sent, not even string ``"none"``.
            You will have to specify a value explicitly.
            Its valid values are the constants defined in
            :class:`Prompt <msal.Prompt>`.
        :param str login_hint:
            Optional. Identifier of the user. Generally a User Principal Name (UPN).
        :param domain_hint:
            Can be one of "consumers" or "organizations" or your tenant domain "contoso.com".
            If included, it will skip the email-based discovery process that user goes
            through on the sign-in page, leading to a slightly more streamlined user experience.
            More information on possible values available in
            `Auth Code Flow doc <https://docs.microsoft.com/en-us/azure/active-directory/develop/v2-oauth2-auth-code-flow#request-an-authorization-code>`_ and
            `domain_hint doc <https://docs.microsoft.com/en-us/openspecs/windows_protocols/ms-oapx/86fb452d-e34a-494e-ac61-e526e263b6d8>`_.

        :param claims_challenge:
            The claims_challenge parameter requests specific claims requested by the resource provider
            in the form of a claims_challenge directive in the www-authenticate header to be
            returned from the UserInfo Endpoint and/or in the ID Token and/or Access Token.
            It is a string of a JSON object which contains lists of claims being requested from these locations.

        :param int timeout:
            This method will block the current thread.
            This parameter specifies the timeout value in seconds.
            Default value ``None`` means wait indefinitely.

        :param int port:
            The port to be used to listen to an incoming auth response.
            By default we will use a system-allocated port.
            (The rest of the redirect_uri is hard coded as ``http://localhost``.)

        :param list extra_scopes_to_consent:
            "Extra scopes to consent" is a concept only available in Microsoft Entra.
            It refers to other resources you might want to prompt to consent for,
            in the same interaction, but for which you won't get back a
            token for in this particular operation.

        :param int max_age:
            OPTIONAL. Maximum Authentication Age.
            Specifies the allowable elapsed time in seconds
            since the last time the End-User was actively authenticated.
            If the elapsed time is greater than this value,
            Microsoft identity platform will actively re-authenticate the End-User.

            MSAL Python will also automatically validate the auth_time in ID token.

            New in version 1.15.

        :param int parent_window_handle:
            OPTIONAL.

            * If your app does not opt in to use broker,
              you do not need to provide a ``parent_window_handle`` here.

            * If your app opts in to use broker,
              ``parent_window_handle`` is required.

              - If your app is a GUI app running on Windows or Mac system,
                you are required to also provide its window handle,
                so that the sign-in window will pop up on top of your window.
              - If your app is a console app running on Windows or Mac system,
                you can use a placeholder
                ``PublicClientApplication.CONSOLE_WINDOW_HANDLE``.

            Most Python scripts are console apps.

            New in version 1.20.0.

        :param function on_before_launching_ui:
            A callback with the form of
            ``lambda ui="xyz", **kwargs: print("A {} will be launched".format(ui))``,
            where ``ui`` will be either "browser" or "broker".
            You can use it to inform your end user to expect a pop-up window.

            New in version 1.20.0.

        :param object auth_scheme:
            You can provide an ``msal.auth_scheme.PopAuthScheme`` object
            so that MSAL will get a Proof-of-Possession (POP) token for you.

            New in version 1.26.0.

        :return:
            - A dict containing no "error" key,
              and typically contains an "access_token" key.
            - A dict containing an "error" key, when token refresh failed.
        """
        data = kwargs.pop("data", {})
        enable_msa_passthrough = kwargs.pop(  # MUST remove it from kwargs
            "enable_msa_passthrough",  # Keep it as a hidden param, for now.
                # OPTIONAL. MSA-Passthrough is a legacy configuration,
                # needed by a small amount of Microsoft first-party apps,
                # which would login MSA accounts via ".../organizations" authority.
                # If you app belongs to this category, AND you are enabling broker,
                # you would want to enable this flag. Default value is False.
                # More background of MSA-PT is available from this internal docs:
                # https://microsoft.sharepoint.com/:w:/t/Identity-DevEx/EatIUauX3c9Ctw1l7AQ6iM8B5CeBZxc58eoQCE0IuZ0VFw?e=tgc3jP&CID=39c853be-76ea-79d7-ee73-f1b2706ede05
            False
            ) and data.get("token_type") != "ssh-cert"  # Work around a known issue as of PyMsalRuntime 0.8
        self._validate_ssh_cert_input_data(data)
        if not on_before_launching_ui:
            on_before_launching_ui = lambda **kwargs: None
        if _is_running_in_cloud_shell() and prompt == "none":
            # Note: _acquire_token_by_cloud_shell() is always silent,
            #       so we would not fire on_before_launching_ui()
            return self._acquire_token_by_cloud_shell(scopes, data=data)
        claims = _merge_claims_challenge_and_capabilities(
            self._client_capabilities, claims_challenge)
        if self._enable_broker:
            if parent_window_handle is None:
                raise ValueError(
                    "parent_window_handle is required when you opted into using broker. "
                    "You need to provide the window handle of your GUI application, "
                    "or use msal.PublicClientApplication.CONSOLE_WINDOW_HANDLE "
                    "when and only when your application is a console app.")
            if extra_scopes_to_consent:
                logger.warning(
                    "Ignoring parameter extra_scopes_to_consent, "
                    "which is not supported by broker")
            response = self._acquire_token_interactive_via_broker(
                scopes,
                parent_window_handle,
                enable_msa_passthrough,
                claims,
                data,
                on_before_launching_ui,
                auth_scheme,
                prompt=prompt,
                login_hint=login_hint,
                max_age=max_age,
                )
            return self._process_broker_response(response, scopes, data)

        if auth_scheme:
            raise ValueError(self._AUTH_SCHEME_UNSUPPORTED)
        on_before_launching_ui(ui="browser")
        telemetry_context = self._build_telemetry_context(
            self.ACQUIRE_TOKEN_INTERACTIVE)
        response = _clean_up(self.client.obtain_token_by_browser(
            scope=self._decorate_scope(scopes) if scopes else None,
            extra_scope_to_consent=extra_scopes_to_consent,
            redirect_uri="http://localhost:{port}".format(
                # Hardcode the host, for now. AAD portal rejects 127.0.0.1 anyway
                port=port or 0),
            prompt=prompt,
            login_hint=login_hint,
            max_age=max_age,
            timeout=timeout,
            auth_params={
                "claims": claims,
                "domain_hint": domain_hint,
                },
            data=dict(data, claims=claims),
            headers=telemetry_context.generate_headers(),
            browser_name=_preferred_browser(),
            **kwargs))
        if "access_token" in response:
            response[self._TOKEN_SOURCE] = self._TOKEN_SOURCE_IDP
        telemetry_context.update_telemetry(response)
        return response

    def _acquire_token_interactive_via_broker(
            self,
            scopes,  # type: list[str]
            parent_window_handle,  # type: int
            enable_msa_passthrough,  # type: boolean
            claims,  # type: str
            data,  # type: dict
            on_before_launching_ui,  # type: callable
            auth_scheme,  # type: object
            prompt=None,
            login_hint=None,  # type: Optional[str]
            max_age=None,
            **kwargs):
        from .broker import _signin_interactively, _signin_silently, _acquire_token_silently
        if "welcome_template" in kwargs:
            logger.debug(kwargs["welcome_template"])  # Experimental
        authority = "https://{}/{}".format(
            self.authority.instance, self.authority.tenant)
        validate_authority = "no" if (
            self.authority._is_known_to_developer
            or self._instance_discovery is False) else None
        # Calls different broker methods to mimic the OIDC behaviors
        if login_hint and prompt != "select_account":  # OIDC prompts when the user did not sign in
            accounts = self.get_accounts(username=login_hint)
            if len(accounts) == 1:  # Unambiguously proceed with this account
                logger.debug("Calling broker._acquire_token_silently()")
                response = _acquire_token_silently(  # When it works, it bypasses prompt
                    authority,
                    self.client_id,
                    accounts[0]["local_account_id"],
                    scopes,
                    claims=claims,
                    auth_scheme=auth_scheme,
                    **data)
                if response and "error" not in response:
                    return response
        # login_hint undecisive or not exists
        if prompt == "none" or not prompt:  # Must/Can attempt _signin_silently()
            logger.debug("Calling broker._signin_silently()")
            response = _signin_silently(  # Unlike OIDC, it doesn't honor login_hint
                authority, self.client_id, scopes,
                validateAuthority=validate_authority,
                claims=claims,
                max_age=max_age,
                enable_msa_pt=enable_msa_passthrough,
                auth_scheme=auth_scheme,
                **data)
            is_wrong_account = bool(
                # _signin_silently() only gets tokens for default account,
                # but this seems to have been fixed in PyMsalRuntime 0.11.2
                "access_token" in response and login_hint
                and response.get("id_token_claims", {}) != login_hint)
            wrong_account_error_message = (
                'prompt="none" will not work for login_hint="non-default-user"')
            if is_wrong_account:
                logger.debug(wrong_account_error_message)
            if prompt == "none":
                return response if not is_wrong_account else {
                        "error": "broker_error",
                        "error_description": wrong_account_error_message,
                    }
            else:
                assert bool(prompt) is False
                from pymsalruntime import Response_Status
                recoverable_errors = frozenset([
                    Response_Status.Status_AccountUnusable,
                    Response_Status.Status_InteractionRequired,
                    ])
                if is_wrong_account or "error" in response and response.get(
                        "_broker_status") in recoverable_errors:
                    pass  # It will fall back to the _signin_interactively()
                else:
                    return response

        logger.debug("Falls back to broker._signin_interactively()")
        on_before_launching_ui(ui="broker")
        return _signin_interactively(
            authority, self.client_id, scopes,
            None if parent_window_handle is self.CONSOLE_WINDOW_HANDLE
                else parent_window_handle,
            validateAuthority=validate_authority,
            login_hint=login_hint,
            prompt=prompt,
            claims=claims,
            max_age=max_age,
            enable_msa_pt=enable_msa_passthrough,
            auth_scheme=auth_scheme,
            **data)

    def initiate_device_flow(self, scopes=None, **kwargs):
        """Initiate a Device Flow instance,
        which will be used in :func:`~acquire_token_by_device_flow`.

        :param list[str] scopes:
            Scopes requested to access a protected API (a resource).
        :return: A dict representing a newly created Device Flow object.

            - A successful response would contain "user_code" key, among others
            - an error response would contain some other readable key/value pairs.
        """
        correlation_id = msal.telemetry._get_new_correlation_id()
        flow = self.client.initiate_device_flow(
            scope=self._decorate_scope(scopes or []),
            headers={msal.telemetry.CLIENT_REQUEST_ID: correlation_id},
            **kwargs)
        flow[self.DEVICE_FLOW_CORRELATION_ID] = correlation_id
        return flow

    def acquire_token_by_device_flow(self, flow, claims_challenge=None, **kwargs):
        """Obtain token by a device flow object, with customizable polling effect.

        :param dict flow:
            A dict previously generated by :func:`~initiate_device_flow`.
            By default, this method's polling effect  will block current thread.
            You can abort the polling loop at any time,
            by changing the value of the flow's "expires_at" key to 0.
        :param claims_challenge:
            The claims_challenge parameter requests specific claims requested by the resource provider
            in the form of a claims_challenge directive in the www-authenticate header to be
            returned from the UserInfo Endpoint and/or in the ID Token and/or Access Token.
            It is a string of a JSON object which contains lists of claims being requested from these locations.

        :return: A dict representing the json response from Microsoft Entra:

            - A successful response would contain "access_token" key,
            - an error response would contain "error" and usually "error_description".
        """
        telemetry_context = self._build_telemetry_context(
            self.ACQUIRE_TOKEN_BY_DEVICE_FLOW_ID,
            correlation_id=flow.get(self.DEVICE_FLOW_CORRELATION_ID))
        response = _clean_up(self.client.obtain_token_by_device_flow(
            flow,
            data=dict(
                kwargs.pop("data", {}),
                code=flow["device_code"],  # 2018-10-4 Hack:
                    # during transition period,
                    # service seemingly need both device_code and code parameter.
                claims=_merge_claims_challenge_and_capabilities(
                    self._client_capabilities, claims_challenge),
                ),
            headers=telemetry_context.generate_headers(),
            **kwargs))
        if "access_token" in response:
            response[self._TOKEN_SOURCE] = self._TOKEN_SOURCE_IDP
        telemetry_context.update_telemetry(response)
        return response

    def acquire_token_integrated_windows_auth(self, username, scopes="openid", **kwargs):
        """Gets a token for a given resource via Integrated Windows Authentication (IWA).

        :param str username: Typically a UPN in the form of an email address.
        :param str scopes: Scopes requested to access a protected API (a resource).

        :return: A dict representing the json response from AAD:

            - A successful response would contain "access_token" key,
            - an error response would contain "error" and usually "error_description".
        """
        telemetry_context = self._build_telemetry_context(
            self.ACQUIRE_TOKEN_INTEGRATED_WINDOWS_AUTH_ID)
        headers = telemetry_context.generate_headers()
        user_realm_result = self.authority.user_realm_discovery(
            username,
            correlation_id=headers[msal.telemetry.CLIENT_REQUEST_ID]
        )
        if user_realm_result.get("account_type") != "Federated":
            raise ValueError("Server returned an unknown account type: %s" % user_realm_result.get("account_type"))
        response = _clean_up(self._acquire_token_by_iwa_federated(user_realm_result, username, scopes, **kwargs))
        if response is None:  # Either ADFS or not federated
            raise ValueError("Integrated Windows Authentication failed for this user: %s", username)
        if "access_token" in response:
            response[self._TOKEN_SOURCE] = self._TOKEN_SOURCE_IDP
        telemetry_context.update_telemetry(response)
        return response

    def _acquire_token_by_iwa_federated(
            self, user_realm_result, username, scopes="openid", **kwargs):
        wstrust_endpoint = {}
        if user_realm_result.get("federation_metadata_url"):
            mex_endpoint = user_realm_result.get("federation_metadata_url")
            logger.debug(
                "Attempting mex at: %(mex_endpoint)s",
                {"mex_endpoint": mex_endpoint})
            wstrust_endpoint = mex_send_request_iwa(mex_endpoint, self.http_client)
            if wstrust_endpoint is None:
                raise ValueError("Unable to find wstrust endpoint from MEX. "
                                 "This typically happens when attempting MSA accounts. "
                                 "More details available here. "
                                 "https://github.com/AzureAD/microsoft-authentication-library-for-python/wiki/Username-Password-Authentication")
        logger.debug("wstrust_endpoint = %s", wstrust_endpoint)
        wstrust_result = wst_send_request(
            None, None,
            user_realm_result.get("cloud_audience_urn", "urn:federation:MicrosoftOnline"),
            wstrust_endpoint.get("address",
                                 # Fallback to an AAD supplied endpoint
                                 user_realm_result.get("federation_active_auth_url")),
            wstrust_endpoint.get("action"), self.http_client)
        if not ("token" in wstrust_result and "type" in wstrust_result):
            raise RuntimeError("Unsuccessful RSTR. %s" % wstrust_result)
        GRANT_TYPE_SAML1_1 = 'urn:ietf:params:oauth:grant-type:saml1_1-bearer'
        grant_type = {
            SAML_TOKEN_TYPE_V1: GRANT_TYPE_SAML1_1,
            SAML_TOKEN_TYPE_V2: self.client.GRANT_TYPE_SAML2,
            WSS_SAML_TOKEN_PROFILE_V1_1: GRANT_TYPE_SAML1_1,
            WSS_SAML_TOKEN_PROFILE_V2: self.client.GRANT_TYPE_SAML2
        }.get(wstrust_result.get("type"))
        if not grant_type:
            raise RuntimeError(
                "RSTR returned unknown token type: %s", wstrust_result.get("type"))
        self.client.grant_assertion_encoders.setdefault(  # Register a non-standard type
            grant_type, self.client.encode_saml_assertion)
        return self.client.obtain_token_by_assertion(
            wstrust_result["token"], grant_type, scope=scopes,
            on_obtaining_tokens=lambda event: self.token_cache.add(dict(
                event,
                environment=self.authority.instance,
                username=username,  # Useful in case IDT contains no such info
            )),
            **kwargs)

class ConfidentialClientApplication(ClientApplication):  # server-side web app
    """Same as :func:`ClientApplication.__init__`,
    except that ``allow_broker`` parameter shall remain ``None``.
    """

    def acquire_token_for_client(self, scopes, claims_challenge=None, **kwargs):
        """Acquires token for the current confidential client, not for an end user.

        Since MSAL Python 1.23, it will automatically look for token from cache,
        and only send request to Identity Provider when cache misses.

        :param list[str] scopes: (Required)
            Scopes requested to access a protected API (a resource).
        :param claims_challenge:
            The claims_challenge parameter requests specific claims requested by the resource provider
            in the form of a claims_challenge directive in the www-authenticate header to be
            returned from the UserInfo Endpoint and/or in the ID Token and/or Access Token.
            It is a string of a JSON object which contains lists of claims being requested from these locations.

        :return: A dict representing the json response from Microsoft Entra:

            - A successful response would contain "access_token" key,
            - an error response would contain "error" and usually "error_description".
        """
        if kwargs.get("force_refresh"):
            raise ValueError(  # We choose to disallow force_refresh
                "Historically, this method does not support force_refresh behavior. "
            )
        return _clean_up(self._acquire_token_silent_with_error(
            scopes, None, claims_challenge=claims_challenge, **kwargs))

    def _acquire_token_for_client(
        self,
        scopes,
        refresh_reason,
        claims_challenge=None,
        **kwargs
    ):
        if self.authority.tenant.lower() in ["common", "organizations"]:
            warnings.warn(
                "Using /common or /organizations authority "
                "in acquire_token_for_client() is unreliable. "
                "Please use a specific tenant instead.", DeprecationWarning)
        self._validate_ssh_cert_input_data(kwargs.get("data", {}))
        telemetry_context = self._build_telemetry_context(
            self.ACQUIRE_TOKEN_FOR_CLIENT_ID, refresh_reason=refresh_reason)
        client = self._regional_client or self.client
        response = client.obtain_token_for_client(
            scope=scopes,  # This grant flow requires no scope decoration
            headers=telemetry_context.generate_headers(),
            data=dict(
                kwargs.pop("data", {}),
                claims=_merge_claims_challenge_and_capabilities(
                    self._client_capabilities, claims_challenge)),
            **kwargs)
        telemetry_context.update_telemetry(response)
        return response

    def remove_tokens_for_client(self):
        """Remove all tokens that were previously acquired via
        :func:`~acquire_token_for_client()` for the current client."""
        for env in [self.authority.instance] + self._get_authority_aliases(
                self.authority.instance):
            for at in list(self.token_cache.search(  # Remove ATs from a snapshot
                TokenCache.CredentialType.ACCESS_TOKEN, query={
                "client_id": self.client_id,
                "environment": env,
                "home_account_id": None,  # These are mostly app-only tokens
            })):
                self.token_cache.remove_at(at)
        # acquire_token_for_client() obtains no RTs, so we have no RT to remove

    def acquire_token_on_behalf_of(self, user_assertion, scopes, claims_challenge=None, **kwargs):
        """Acquires token using on-behalf-of (OBO) flow.

        The current app is a middle-tier service which was called with a token
        representing an end user.
        The current app can use such token (a.k.a. a user assertion) to request
        another token to access downstream web API, on behalf of that user.
        See `detail docs here <https://docs.microsoft.com/en-us/azure/active-directory/develop/v2-oauth2-on-behalf-of-flow>`_ .

        The current middle-tier app has no user interaction to obtain consent.
        See how to gain consent upfront for your middle-tier app from this article.
        https://docs.microsoft.com/en-us/azure/active-directory/develop/v2-oauth2-on-behalf-of-flow#gaining-consent-for-the-middle-tier-application

        :param str user_assertion: The incoming token already received by this app
        :param list[str] scopes: Scopes required by downstream API (a resource).
        :param claims_challenge:
            The claims_challenge parameter requests specific claims requested by the resource provider
            in the form of a claims_challenge directive in the www-authenticate header to be
            returned from the UserInfo Endpoint and/or in the ID Token and/or Access Token.
            It is a string of a JSON object which contains lists of claims being requested from these locations.

        :return: A dict representing the json response from Microsoft Entra:

            - A successful response would contain "access_token" key,
            - an error response would contain "error" and usually "error_description".
        """
        telemetry_context = self._build_telemetry_context(
            self.ACQUIRE_TOKEN_ON_BEHALF_OF_ID)
        # The implementation is NOT based on Token Exchange (RFC 8693)
        response = _clean_up(self.client.obtain_token_by_assertion(  # bases on assertion RFC 7521
            user_assertion,
            self.client.GRANT_TYPE_JWT,  # IDTs and AAD ATs are all JWTs
            scope=self._decorate_scope(scopes),  # Decoration is used for:
                # 1. Explicitly requesting an RT, without relying on AAD default
                #    behavior, even though it currently still issues an RT.
                # 2. Requesting an IDT (which would otherwise be unavailable)
                #    so that the calling app could use id_token_claims to implement
                #    their own cache mapping, which is likely needed in web apps.
            data=dict(
                kwargs.pop("data", {}),
                requested_token_use="on_behalf_of",
                claims=_merge_claims_challenge_and_capabilities(
                    self._client_capabilities, claims_challenge)),
            headers=telemetry_context.generate_headers(),
                # TBD: Expose a login_hint (or ccs_routing_hint) param for web app
            **kwargs))
        if "access_token" in response:
            response[self._TOKEN_SOURCE] = self._TOKEN_SOURCE_IDP
        telemetry_context.update_telemetry(response)
        return response
