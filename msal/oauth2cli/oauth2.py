"""This OAuth2 client implementation aims to be spec-compliant, and generic."""
# OAuth2 spec https://tools.ietf.org/html/rfc6749

import json
try:
    from urllib.parse import urlencode, parse_qs, quote_plus, urlparse, urlunparse
except ImportError:
    from urlparse import parse_qs, urlparse, urlunparse
    from urllib import urlencode, quote_plus
import logging
import warnings
import time
import base64
import sys
import functools
import random
import string
import hashlib

from .authcode import AuthCodeReceiver as _AuthCodeReceiver

try:
    PermissionError  # Available in Python 3
except:
    from socket import error as PermissionError  # Workaround for Python 2


string_types = (str,) if sys.version_info[0] >= 3 else (basestring, )


class BrowserInteractionTimeoutError(RuntimeError):
    pass

class BaseClient(object):
    # This low-level interface works. Yet you'll find its sub-class
    # more friendly to remind you what parameters are needed in each scenario.
    # More on Client Types at https://tools.ietf.org/html/rfc6749#section-2.1

    @staticmethod
    def encode_saml_assertion(assertion):
        return base64.urlsafe_b64encode(assertion).rstrip(b'=')  # Per RFC 7522

    CLIENT_ASSERTION_TYPE_JWT = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
    CLIENT_ASSERTION_TYPE_SAML2 = "urn:ietf:params:oauth:client-assertion-type:saml2-bearer"
    client_assertion_encoders = {CLIENT_ASSERTION_TYPE_SAML2: encode_saml_assertion}

    @property
    def session(self):
        warnings.warn("Will be gone in next major release", DeprecationWarning)
        return self._http_client

    @session.setter
    def session(self, value):
        warnings.warn("Will be gone in next major release", DeprecationWarning)
        self._http_client = value


    def __init__(
            self,
            server_configuration,  # type: dict
            client_id,  # type: str
            http_client=None,  # We insert it here to match the upcoming async API
            client_secret=None,  # type: Optional[str]
            client_assertion=None,  # type: Union[bytes, callable, None]
            client_assertion_type=None,  # type: Optional[str]
            default_headers=None,  # type: Optional[dict]
            default_body=None,  # type: Optional[dict]
            verify=None,  # type: Union[str, True, False, None]
            proxies=None,  # type: Optional[dict]
            timeout=None,  # type: Union[tuple, float, None]
            ):
        """Initialize a client object to talk all the OAuth2 grants to the server.

        Args:
            server_configuration (dict):
                It contains the configuration (i.e. metadata) of the auth server.
                The actual content typically contains keys like
                "authorization_endpoint", "token_endpoint", etc..
                Based on RFC 8414 (https://tools.ietf.org/html/rfc8414),
                you can probably fetch it online from either
                https://example.com/.../.well-known/oauth-authorization-server
                or
                https://example.com/.../.well-known/openid-configuration
            client_id (str): The client's id, issued by the authorization server

            http_client (http.HttpClient):
                Your implementation of abstract class :class:`http.HttpClient`.
                Defaults to a requests session instance.

                There is no session-wide `timeout` parameter defined here.
                Timeout behavior is determined by the actual http client you use.
                If you happen to use Requests, it disallows session-wide timeout
                (https://github.com/psf/requests/issues/3341). The workaround is:

                    s = requests.Session()
                    s.request = functools.partial(s.request, timeout=3)

                and then feed that patched session instance to this class.

            client_secret (str):  Triggers HTTP AUTH for Confidential Client
            client_assertion (bytes, callable):
                The client assertion to authenticate this client, per RFC 7521.
                It can be a raw SAML2 assertion (we will base64 encode it for you),
                or a raw JWT assertion in bytes (which we will relay to http layer).
                It can also be a callable (recommended),
                so that we will do lazy creation of an assertion.
            client_assertion_type (str):
                The type of your :attr:`client_assertion` parameter.
                It is typically the value of :attr:`CLIENT_ASSERTION_TYPE_SAML2` or
                :attr:`CLIENT_ASSERTION_TYPE_JWT`, the only two defined in RFC 7521.
            default_headers (dict):
                A dict to be sent in each request header.
                It is not required by OAuth2 specs, but you may use it for telemetry.
            default_body (dict):
                A dict to be sent in each token request body. For example,
                you could choose to set this as {"client_secret": "your secret"}
                if your authorization server wants it to be in the request body
                (rather than in the request header).

            verify (boolean):
                It will be passed to the
                `verify parameter in the underlying requests library
                <http://docs.python-requests.org/en/v2.9.1/user/advanced/#ssl-cert-verification>`_.
                When leaving it with default value (None), we will use True instead.

                This does not apply if you have chosen to pass your own Http client.

            proxies (dict):
                It will be passed to the
                `proxies parameter in the underlying requests library
                <http://docs.python-requests.org/en/v2.9.1/user/advanced/#proxies>`_.

                This does not apply if you have chosen to pass your own Http client.

            timeout (object):
                It will be passed to the
                `timeout parameter in the underlying requests library
                <http://docs.python-requests.org/en/v2.9.1/user/advanced/#timeouts>`_.

                This does not apply if you have chosen to pass your own Http client.

        """
        if not server_configuration:
            raise ValueError("Missing input parameter server_configuration")
        # Generally we should have client_id, but we tolerate its absence
        self.configuration = server_configuration
        self.client_id = client_id
        self.client_secret = client_secret
        self.client_assertion = client_assertion
        self.default_headers = default_headers or {}
        self.default_body = default_body or {}
        if client_assertion_type is not None:
            self.default_body["client_assertion_type"] = client_assertion_type
        self.logger = logging.getLogger(__name__)
        if http_client:
            if verify is not None or proxies is not None or timeout is not None:
                raise ValueError(
                    "verify, proxies, or timeout is not allowed "
                    "when http_client is in use")
            self._http_client = http_client
        else:
            import requests  # Lazy loading

            self._http_client = requests.Session()
            self._http_client.verify = True if verify is None else verify
            self._http_client.proxies = proxies
            self._http_client.request = functools.partial(
                # A workaround for requests not supporting session-wide timeout
                self._http_client.request, timeout=timeout)

    def _build_auth_request_params(self, response_type, **kwargs):
        # response_type is a string defined in
        #   https://tools.ietf.org/html/rfc6749#section-3.1.1
        # or it can be a space-delimited string as defined in
        #   https://tools.ietf.org/html/rfc6749#section-8.4
        response_type = self._stringify(response_type)

        params = {'client_id': self.client_id, 'response_type': response_type}
        params.update(kwargs)  # Note: None values will override params
        params = {k: v for k, v in params.items() if v is not None}  # clean up
        if params.get('scope'):
            params['scope'] = self._stringify(params['scope'])
        return params  # A dict suitable to be used in http request

    def _obtain_token(  # The verb "obtain" is influenced by OAUTH2 RFC 6749
            self, grant_type,
            params=None,  # a dict to be sent as query string to the endpoint
            data=None,  # All relevant data, which will go into the http body
            headers=None,  # a dict to be sent as request headers
            post=None,  # A callable to replace requests.post(), for testing.
                        # Such as: lambda url, **kwargs:
                        #   Mock(status_code=200, text='{}')
            **kwargs  # Relay all extra parameters to underlying requests
            ):  # Returns the json object came from the OAUTH2 response
        _data = {'client_id': self.client_id, 'grant_type': grant_type}

        if self.default_body.get("client_assertion_type") and self.client_assertion:
            # See https://tools.ietf.org/html/rfc7521#section-4.2
            encoder = self.client_assertion_encoders.get(
                    self.default_body["client_assertion_type"], lambda a: a)
            _data["client_assertion"] = encoder(
                self.client_assertion()  # Do lazy on-the-fly computation
                if callable(self.client_assertion) else self.client_assertion
                )   # The type is bytes, which is preferable. See also:
                    # https://github.com/psf/requests/issues/4503#issuecomment-455001070

        _data.update(self.default_body)  # It may contain authen parameters
        _data.update(data or {})  # So the content in data param prevails
        _data = {k: v for k, v in _data.items() if v}  # Clean up None values

        if _data.get('scope'):
            _data['scope'] = self._stringify(_data['scope'])

        _headers = {'Accept': 'application/json'}
        _headers.update(self.default_headers)
        _headers.update(headers or {})

        # Quoted from https://tools.ietf.org/html/rfc6749#section-2.3.1
        # Clients in possession of a client password MAY use the HTTP Basic
        # authentication.
        # Alternatively, (but NOT RECOMMENDED,)
        # the authorization server MAY support including the
        # client credentials in the request-body using the following
        # parameters: client_id, client_secret.
        if self.client_secret and self.client_id:
            _headers["Authorization"] = "Basic " + base64.b64encode("{}:{}".format(
                # Per https://tools.ietf.org/html/rfc6749#section-2.3.1
                # client_id and client_secret needs to be encoded by
                # "application/x-www-form-urlencoded"
                # https://www.w3.org/TR/html401/interact/forms.html#h-17.13.4.1
                # BEFORE they are fed into HTTP Basic Authentication
                quote_plus(self.client_id), quote_plus(self.client_secret)
                ).encode("ascii")).decode("ascii")

        if "token_endpoint" not in self.configuration:
            raise ValueError("token_endpoint not found in configuration")
        resp = (post or self._http_client.post)(
            self.configuration["token_endpoint"],
            headers=_headers, params=params, data=_data,
            **kwargs)
        if resp.status_code >= 500:
            resp.raise_for_status()  # TODO: Will probably retry here
        try:
            # The spec (https://tools.ietf.org/html/rfc6749#section-5.2) says
            # even an error response will be a valid json structure,
            # so we simply return it here, without needing to invent an exception.
            return json.loads(resp.text)
        except ValueError:
            self.logger.exception(
                    "Token response is not in json format: %s", resp.text)
            raise

    def obtain_token_by_refresh_token(self, refresh_token, scope=None, **kwargs):
        # type: (str, Union[str, list, set, tuple]) -> dict
        """Obtain an access token via a refresh token.

        :param refresh_token: The refresh token issued to the client
        :param scope: If omitted, is treated as equal to the scope originally
            granted by the resource owner,
            according to https://tools.ietf.org/html/rfc6749#section-6
        """
        assert isinstance(refresh_token, string_types)
        data = kwargs.pop('data', {})
        data.update(refresh_token=refresh_token, scope=scope)
        return self._obtain_token("refresh_token", data=data, **kwargs)

    def _stringify(self, sequence):
        if isinstance(sequence, (list, set, tuple)):
            return ' '.join(sorted(sequence))  # normalizing it, ascendingly
        return sequence  # as-is


def _scope_set(scope):
    assert scope is None or isinstance(scope, (list, set, tuple))
    return set(scope) if scope else set([])


def _generate_pkce_code_verifier(length=43):
    assert 43 <= length <= 128
    verifier = "".join(  # https://tools.ietf.org/html/rfc7636#section-4.1
        random.sample(string.ascii_letters + string.digits + "-._~", length))
    code_challenge = (
        # https://tools.ietf.org/html/rfc7636#section-4.2
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"="))  # Required by https://tools.ietf.org/html/rfc7636#section-3
    return {
        "code_verifier": verifier,
        "transformation": "S256",  # In Python, sha256 is always available
        "code_challenge": code_challenge,
        }


class Client(BaseClient):  # We choose to implement all 4 grants in 1 class
    """This is the main API for oauth2 client.

    Its methods define and document parameters mentioned in OAUTH2 RFC 6749.
    """
    DEVICE_FLOW = {  # consts for device flow, that can be customized by sub-class
        "GRANT_TYPE": "urn:ietf:params:oauth:grant-type:device_code",
        "DEVICE_CODE": "device_code",
        }
    DEVICE_FLOW_RETRIABLE_ERRORS = ("authorization_pending", "slow_down")
    GRANT_TYPE_SAML1_1 = 'urn:ietf:params:oauth:grant-type:saml1_1-bearer'
    GRANT_TYPE_SAML2 = "urn:ietf:params:oauth:grant-type:saml2-bearer"  # RFC7522
    GRANT_TYPE_JWT = "urn:ietf:params:oauth:grant-type:jwt-bearer"  # RFC7523
    grant_assertion_encoders = {GRANT_TYPE_SAML2: BaseClient.encode_saml_assertion}


    def initiate_device_flow(self, scope=None, **kwargs):
        # type: (list, **dict) -> dict
        # The naming of this method is following the wording of this specs
        # https://tools.ietf.org/html/draft-ietf-oauth-device-flow-12#section-3.1
        """Initiate a device flow.

        Returns the data defined in Device Flow specs.
        https://tools.ietf.org/html/draft-ietf-oauth-device-flow-12#section-3.2

        You should then orchestrate the User Interaction as defined in here
        https://tools.ietf.org/html/draft-ietf-oauth-device-flow-12#section-3.3

        And possibly here
        https://tools.ietf.org/html/draft-ietf-oauth-device-flow-12#section-3.3.1
        """
        DAE = "device_authorization_endpoint"
        if not self.configuration.get(DAE):
            raise ValueError("You need to provide device authorization endpoint")
        resp = self._http_client.post(self.configuration[DAE],
            data={"client_id": self.client_id, "scope": self._stringify(scope or [])},
            headers=dict(self.default_headers, **kwargs.pop("headers", {})),
            **kwargs)
        flow = json.loads(resp.text)
        flow["interval"] = int(flow.get("interval", 5))  # Some IdP returns string
        flow["expires_in"] = int(flow.get("expires_in", 1800))
        flow["expires_at"] = time.time() + flow["expires_in"]  # We invent this
        return flow

    def _obtain_token_by_device_flow(self, flow, **kwargs):
        # type: (dict, **dict) -> dict
        # This method updates flow during each run. And it is non-blocking.
        now = time.time()
        skew = 1
        if flow.get("latest_attempt_at", 0) + flow.get("interval", 5) - skew > now:
            warnings.warn('Attempted too soon. Please do time.sleep(flow["interval"])')
        data = kwargs.pop("data", {})
        data.update({
            "client_id": self.client_id,
            self.DEVICE_FLOW["DEVICE_CODE"]: flow["device_code"],
            })
        result = self._obtain_token(
            self.DEVICE_FLOW["GRANT_TYPE"], data=data, **kwargs)
        if result.get("error") == "slow_down":
            # Respecting https://tools.ietf.org/html/draft-ietf-oauth-device-flow-12#section-3.5
            flow["interval"] = flow.get("interval", 5) + 5
        flow["latest_attempt_at"] = now
        return result

    def obtain_token_by_device_flow(self,
            flow,
            exit_condition=lambda flow: flow.get("expires_at", 0) < time.time(),
            **kwargs):
        # type: (dict, Callable) -> dict
        """Obtain token by a device flow object, with customizable polling effect.

        Args:
            flow (dict):
                An object previously generated by initiate_device_flow(...).
                Its content WILL BE CHANGED by this method during each run.
                We share this object with you, so that you could implement
                your own loop, should you choose to do so.

            exit_condition (Callable):
                This method implements a loop to provide polling effect.
                The loop's exit condition is calculated by this callback.

                The default callback makes the loop run until the flow expires.
                Therefore, one of the ways to exit the polling early,
                is to change the flow["expires_at"] to a small number such as 0.

                In case you are doing async programming, you may want to
                completely turn off the loop. You can do so by using a callback as:

                    exit_condition = lambda flow: True

                to make the loop run only once, i.e. no polling, hence non-block.
        """
        while True:
            result = self._obtain_token_by_device_flow(flow, **kwargs)
            if result.get("error") not in self.DEVICE_FLOW_RETRIABLE_ERRORS:
                return result
            for i in range(flow.get("interval", 5)):  # Wait interval seconds
                if exit_condition(flow):
                    return result
                time.sleep(1)  # Shorten each round, to make exit more responsive

    def _build_auth_request_uri(
            self,
            response_type, redirect_uri=None, scope=None, state=None, **kwargs):
        if "authorization_endpoint" not in self.configuration:
            raise ValueError("authorization_endpoint not found in configuration")
        authorization_endpoint = self.configuration["authorization_endpoint"]
        params = self._build_auth_request_params(
            response_type, redirect_uri=redirect_uri, scope=scope, state=state,
            **kwargs)
        sep = '&' if '?' in authorization_endpoint else '?'
        return "%s%s%s" % (authorization_endpoint, sep, urlencode(params))

    def build_auth_request_uri(
            self,
            response_type, redirect_uri=None, scope=None, state=None, **kwargs):
        # This method could be named build_authorization_request_uri() instead,
        # but then there would be a build_authentication_request_uri() in the OIDC
        # subclass doing almost the same thing. So we use a loose term "auth" here.
        """Generate an authorization uri to be visited by resource owner.

        Parameters are the same as another method :func:`initiate_auth_code_flow()`,
        whose functionality is a superset of this method.

        :return: The auth uri as a string.
        """
        warnings.warn("Use initiate_auth_code_flow() instead. ", DeprecationWarning)
        return self._build_auth_request_uri(
            response_type, redirect_uri=redirect_uri, scope=scope, state=state,
            **kwargs)

    def initiate_auth_code_flow(
        # The name is influenced by OIDC
        # https://openid.net/specs/openid-connect-core-1_0.html#CodeFlowAuth
            self,
            scope=None, redirect_uri=None, state=None,
            **kwargs):
        """Initiate an auth code flow.

        Later when the response reaches your redirect_uri,
        you can use :func:`~obtain_token_by_auth_code_flow()`
        to complete the authentication/authorization.

        This method also provides PKCE protection automatically.

        :param list scope:
            It is a list of case-sensitive strings.
            Some ID provider can accept empty string to represent default scope.
        :param str redirect_uri:
            Optional. If not specified, server will use the pre-registered one.
        :param str state:
            An opaque value used by the client to
            maintain state between the request and callback.
            If absent, this library will automatically generate one internally.
        :param kwargs: Other parameters, typically defined in OpenID Connect.

        :return:
            The auth code flow. It is a dict in this form::

                {
                    "auth_uri": "https://...",  // Guide user to visit this
                    "state": "...",  // You may choose to verify it by yourself,
                                     // or just let obtain_token_by_auth_code_flow()
                                     // do that for you.
                    "...": "...",  // Everything else are reserved and internal
                }

            The caller is expected to::

            1. somehow store this content, typically inside the current session,
            2. guide the end user (i.e. resource owner) to visit that auth_uri,
            3. and then relay this dict and subsequent auth response to
               :func:`~obtain_token_by_auth_code_flow()`.
        """
        response_type = kwargs.pop("response_type", "code")  # Auth Code flow
            # Must be "code" when you are using Authorization Code Grant.
            # The "token" for Implicit Grant is not applicable thus not allowed.
            # It could theoretically be other
            # (possibly space-delimited) strings as registered extension value.
            # See https://tools.ietf.org/html/rfc6749#section-3.1.1
        if "token" in response_type:
            # Implicit grant would cause auth response coming back in #fragment,
            # but fragment won't reach a web service.
            raise ValueError('response_type="token ..." is not allowed')
        pkce = _generate_pkce_code_verifier()
        flow = {  # These data are required by obtain_token_by_auth_code_flow()
            "state": state or "".join(random.sample(string.ascii_letters, 16)),
            "redirect_uri": redirect_uri,
            "scope": scope,
            }
        auth_uri = self._build_auth_request_uri(
            response_type,
            code_challenge=pkce["code_challenge"],
            code_challenge_method=pkce["transformation"],
            **dict(flow, **kwargs))
        flow["auth_uri"] = auth_uri
        flow["code_verifier"] = pkce["code_verifier"]
        return flow

    def obtain_token_by_auth_code_flow(
            self,
            auth_code_flow,
            auth_response,
            scope=None,
            **kwargs):
        """With the auth_response being redirected back,
        validate it against auth_code_flow, and then obtain tokens.

        Internally, it implements PKCE to mitigate the auth code interception attack.

        :param dict auth_code_flow:
            The same dict returned by :func:`~initiate_auth_code_flow()`.
        :param dict auth_response:
            A dict based on query string received from auth server.

        :param scope:
            You don't usually need to use scope parameter here.
            Some Identity Provider allows you to provide
            a subset of what you specified during :func:`~initiate_auth_code_flow`.
        :type scope: collections.Iterable[str]

        :return:
            * A dict containing "access_token" and/or "id_token", among others,
              depends on what scope was used.
              (See https://tools.ietf.org/html/rfc6749#section-5.1)
            * A dict containing "error", optionally "error_description", "error_uri".
              (It is either `this <https://tools.ietf.org/html/rfc6749#section-4.1.2.1>`_
              or `that <https://tools.ietf.org/html/rfc6749#section-5.2>`_
            * Most client-side data error would result in ValueError exception.
              So the usage pattern could be without any protocol details::

                def authorize():  # A controller in a web app
                    try:
                        result = client.obtain_token_by_auth_code_flow(
                            session.get("flow", {}), auth_resp)
                        if "error" in result:
                            return render_template("error.html", result)
                        store_tokens()
                    except ValueError:  # Usually caused by CSRF
                        pass  # Simply ignore them
                    return redirect(url_for("index"))
        """
        assert isinstance(auth_code_flow, dict) and isinstance(auth_response, dict)
            # This is app developer's error which we do NOT want to map to ValueError
        if not auth_code_flow.get("state"):
            # initiate_auth_code_flow() already guarantees a state to be available.
            # This check will also allow a web app to blindly call this method with
            # obtain_token_by_auth_code_flow(session.get("flow", {}), auth_resp)
            # which further simplifies their usage.
            raise ValueError("state missing from auth_code_flow")
        if auth_code_flow.get("state") != auth_response.get("state"):
            raise ValueError("state mismatch: {} vs {}".format(
                auth_code_flow.get("state"), auth_response.get("state")))
        if scope and set(scope) - set(auth_code_flow.get("scope", [])):
            raise ValueError(
                "scope must be None or a subset of %s" % auth_code_flow.get("scope"))
        if auth_response.get("code"):  # i.e. the first leg was successful
            return self._obtain_token_by_authorization_code(
                auth_response["code"],
                redirect_uri=auth_code_flow.get("redirect_uri"),
                    # Required, if "redirect_uri" parameter was included in the
                    # authorization request, and their values MUST be identical.
                scope=scope or auth_code_flow.get("scope"),
                    # It is both unnecessary and harmless, per RFC 6749.
                    # We use the same scope already used in auth request uri,
                    # thus token cache can know what scope the tokens are for.
                data=dict(  # Extract and update the data
                    kwargs.pop("data", {}),
                    code_verifier=auth_code_flow["code_verifier"],
                    ),
                **kwargs)
        if auth_response.get("error"):  # It means the first leg encountered error
            # Here we do NOT return original auth_response as-is, to prevent a
            # potential {..., "access_token": "attacker's AT"} input being leaked
            error = {"error": auth_response["error"]}
            if auth_response.get("error_description"):
                error["error_description"] = auth_response["error_description"]
            if auth_response.get("error_uri"):
                error["error_uri"] = auth_response["error_uri"]
            return error
        raise ValueError('auth_response must contain either "code" or "error"')

    def obtain_token_by_browser(
        # Name influenced by RFC 8252: "native apps should (use) ... user's browser"
            self,
            redirect_uri=None,
            auth_code_receiver=None,
            **kwargs):
        """A native app can use this method to obtain token via a local browser.

        Internally, it implements PKCE to mitigate the auth code interception attack.

        :param scope: A list of scopes that you would like to obtain token for.
        :type scope: collections.Iterable[str]

        :param extra_scope_to_consent:
            Some IdP allows you to include more scopes for end user to consent.
            The access token returned by this method will NOT include those scopes,
            but the refresh token would record those extra consent,
            so that your future :func:`~obtain_token_by_refresh_token()` call
            would be able to obtain token for those additional scopes, silently.
        :type scope: collections.Iterable[str]

        :param string redirect_uri:
            The redirect_uri to be sent via auth request to Identity Provider (IdP),
            to indicate where an auth response would come back to.
            Such as ``http://127.0.0.1:0`` (default) or ``http://localhost:1234``.

            If port 0 is specified, this method will choose a system-allocated port,
            then the actual redirect_uri will contain that port.
            To use this behavior, your IdP would need to accept such dynamic port.

            Per HTTP convention, if port number is absent, it would mean port 80,
            although you probably want to specify port 0 in this context.

        :param dict auth_params:
            These parameters will be sent to authorization_endpoint.

        :param int timeout: In seconds. None means wait indefinitely.

        :param str browser_name:
            If you did
            ``webbrowser.register("xyz", None, BackgroundBrowser("/path/to/browser"))``
            beforehand, you can pass in the name "xyz" to use that browser.
            The default value ``None`` means using default browser,
            which is customizable by env var $BROWSER.

        :return: Same as :func:`~obtain_token_by_auth_code_flow()`
        """
        if auth_code_receiver:  # Then caller already knows the listen port
            return self._obtain_token_by_browser(  # Use all input param as-is
                auth_code_receiver, redirect_uri=redirect_uri, **kwargs)
        # Otherwise we will listen on _redirect_uri.port
        _redirect_uri = urlparse(redirect_uri or "http://127.0.0.1:0")
        if not _redirect_uri.hostname:
            raise ValueError("redirect_uri should contain hostname")
        listen_port = (  # Conventionally, port-less uri would mean port 80
            80 if _redirect_uri.port is None else _redirect_uri.port)
        try:
            with _AuthCodeReceiver(port=listen_port) as receiver:
                uri = redirect_uri if _redirect_uri.port != 0 else urlunparse((
                    _redirect_uri.scheme,
                    "{}:{}".format(_redirect_uri.hostname, receiver.get_port()),
                    _redirect_uri.path,
                    _redirect_uri.params,
                    _redirect_uri.query,
                    _redirect_uri.fragment,
                    ))  # It could be slightly different than raw redirect_uri
                self.logger.debug("Using {} as redirect_uri".format(uri))
                return self._obtain_token_by_browser(
                    receiver, redirect_uri=uri, **kwargs)
        except PermissionError:
            raise ValueError(
                "Can't listen on port %s. You may try port 0." % listen_port)

    def _obtain_token_by_browser(
            self,
            auth_code_receiver,
            scope=None,
            extra_scope_to_consent=None,
            redirect_uri=None,
            timeout=None,
            welcome_template=None,
            success_template=None,
            error_template=None,
            auth_params=None,
            auth_uri_callback=None,
            browser_name=None,
            **kwargs):
        # Internally, it calls self.initiate_auth_code_flow() and
        # self.obtain_token_by_auth_code_flow().
        #
        # Parameters are documented in public method obtain_token_by_browser().
        flow = self.initiate_auth_code_flow(
            redirect_uri=redirect_uri,
            scope=_scope_set(scope) | _scope_set(extra_scope_to_consent),
            **(auth_params or {}))
        auth_response = auth_code_receiver.get_auth_response(
            auth_uri=flow["auth_uri"],
            state=flow["state"],  # So receiver can check it early
            timeout=timeout,
            welcome_template=welcome_template,
            success_template=success_template,
            error_template=error_template,
            auth_uri_callback=auth_uri_callback,
            browser_name=browser_name,
            )
        if auth_response is None:
            raise BrowserInteractionTimeoutError("User did not complete the flow in time")
        return self.obtain_token_by_auth_code_flow(
            flow, auth_response, scope=scope, **kwargs)

    @staticmethod
    def parse_auth_response(params, state=None):
        """Parse the authorization response being redirected back.

        :param params: A string or dict of the query string
        :param state: REQUIRED if the state parameter was present in the client
            authorization request. This function will compare it with response.
        """
        warnings.warn(
            "Use obtain_token_by_auth_code_flow() instead", DeprecationWarning)
        if not isinstance(params, dict):
            params = parse_qs(params)
        if params.get('state') != state:
            raise ValueError('state mismatch')
        return params

    def obtain_token_by_authorization_code(
            self, code, redirect_uri=None, scope=None, **kwargs):
        """Get a token via authorization code. a.k.a. Authorization Code Grant.

        This is typically used by a server-side app (Confidential Client),
        but it can also be used by a device-side native app (Public Client).
        See more detail at https://tools.ietf.org/html/rfc6749#section-4.1.3

        You are encouraged to use its higher level method
        :func:`~obtain_token_by_auth_code_flow` instead.

        :param code: The authorization code received from authorization server.
        :param redirect_uri:
            Required, if the "redirect_uri" parameter was included in the
            authorization request, and their values MUST be identical.
        :param scope:
            It is both unnecessary and harmless to use scope here, per RFC 6749.
            We suggest to use the same scope already used in auth request uri,
            so that this library can link the obtained tokens with their scope.
        """
        warnings.warn(
            "Use obtain_token_by_auth_code_flow() instead", DeprecationWarning)
        return self._obtain_token_by_authorization_code(
            code, redirect_uri=redirect_uri, scope=scope, **kwargs)

    def _obtain_token_by_authorization_code(
            self, code, redirect_uri=None, scope=None, **kwargs):
        data = kwargs.pop("data", {})
        data.update(code=code, redirect_uri=redirect_uri)
        if scope:
            data["scope"] = scope
        if not self.client_secret:
            # client_id is required, if the client is not authenticating itself.
            # See https://tools.ietf.org/html/rfc6749#section-4.1.3
            data["client_id"] = self.client_id
        return self._obtain_token("authorization_code", data=data, **kwargs)

    def obtain_token_by_username_password(
            self, username, password, scope=None, **kwargs):
        """The Resource Owner Password Credentials Grant, used by legacy app."""
        data = kwargs.pop("data", {})
        data.update(username=username, password=password, scope=scope)
        return self._obtain_token("password", data=data, **kwargs)

    def obtain_token_for_client(self, scope=None, **kwargs):
        """Obtain token for this client (rather than for an end user),
        a.k.a. the Client Credentials Grant, used by Backend Applications.

        We don't name it obtain_token_by_client_credentials(...) because those
        credentials are typically already provided in class constructor, not here.
        You can still explicitly provide an optional client_secret parameter,
        or you can provide such extra parameters as `default_body` during the
        class initialization.
        """
        data = kwargs.pop("data", {})
        data.update(scope=scope)
        return self._obtain_token("client_credentials", data=data, **kwargs)

    def __init__(self,
            server_configuration, client_id,
            on_obtaining_tokens=lambda event: None,  # event is defined in _obtain_token(...)
            on_removing_rt=lambda token_item: None,
            on_updating_rt=lambda token_item, new_rt: None,
            **kwargs):
        super(Client, self).__init__(server_configuration, client_id, **kwargs)
        self.on_obtaining_tokens = on_obtaining_tokens
        self.on_removing_rt = on_removing_rt
        self.on_updating_rt = on_updating_rt

    def _obtain_token(
            self, grant_type, params=None, data=None,
            also_save_rt=False,
            on_obtaining_tokens=None,
            *args, **kwargs):
        _data = data.copy()  # to prevent side effect
        resp = super(Client, self)._obtain_token(
            grant_type, params, _data, *args, **kwargs)
        if "error" not in resp:
            _resp = resp.copy()
            RT = "refresh_token"
            if grant_type == RT and RT in _resp and not also_save_rt:
                # Then we skip it from on_obtaining_tokens();
                # Leave it to self.obtain_token_by_refresh_token()
                _resp.pop(RT, None)
            if "scope" in _resp:
                scope = _resp["scope"].split()  # It is conceptually a set,
                    # but we represent it as a list which can be persisted to JSON
            else:
                # Note: The scope will generally be absent in authorization grant,
                #       but our obtain_token_by_authorization_code(...) encourages
                #       app developer to still explicitly provide a scope here.
                scope = _data.get("scope")
            (on_obtaining_tokens or self.on_obtaining_tokens)({
                "client_id": self.client_id,
                "scope": scope,
                "token_endpoint": self.configuration["token_endpoint"],
                "grant_type": grant_type,  # can be used to know an IdToken-less
                                           # response is for an app or for a user
                "response": _resp, "params": params, "data": _data,
                })
        return resp

    def obtain_token_by_refresh_token(self, token_item, scope=None,
            rt_getter=lambda token_item: token_item["refresh_token"],
            on_removing_rt=None,
            on_updating_rt=None,
            **kwargs):
        # type: (Union[str, dict], Union[str, list, set, tuple], Callable) -> dict
        """This is an overload which will trigger token storage callbacks.

        :param token_item:
            A refresh token (RT) item, in flexible format. It can be a string,
            or a whatever data structure containing RT string and its metadata,
            in such case the `rt_getter` callable must be able to
            extract the RT string out from the token item data structure.

            Either way, this token_item will be passed into other callbacks as-is.

        :param scope: If omitted, is treated as equal to the scope originally
            granted by the resource owner,
            according to https://tools.ietf.org/html/rfc6749#section-6
        :param rt_getter: A callable to translate the token_item to a raw RT string
        :param on_removing_rt: If absent, fall back to the one defined in initialization

        :param on_updating_rt:
            Default to None, it will fall back to the one defined in initialization.
            This is the most common case.

            As a special case, you can pass in a False,
            then this function will NOT trigger on_updating_rt() for RT UPDATE,
            instead it will allow the RT to be added by on_obtaining_tokens().
            This behavior is useful when you are migrating RTs from elsewhere
            into a token storage managed by this library.
        """
        resp = super(Client, self).obtain_token_by_refresh_token(
            rt_getter(token_item)
                if not isinstance(token_item, string_types) else token_item,
            scope=scope,
            also_save_rt=on_updating_rt is False,
            **kwargs)
        if resp.get('error') == 'invalid_grant':
            (on_removing_rt or self.on_removing_rt)(token_item)  # Discard old RT
        RT = "refresh_token"
        if on_updating_rt is not False and RT in resp:
            (on_updating_rt or self.on_updating_rt)(token_item, resp[RT])
        return resp

    def obtain_token_by_assertion(
            self, assertion, grant_type, scope=None, **kwargs):
        # type: (bytes, Union[str, None], Union[str, list, set, tuple]) -> dict
        """This method implements Assertion Framework for OAuth2 (RFC 7521).
        See details at https://tools.ietf.org/html/rfc7521#section-4.1

        :param assertion:
            The assertion bytes can be a raw SAML2 assertion, or a JWT assertion.
        :param grant_type:
            It is typically either the value of :attr:`GRANT_TYPE_SAML2`,
            or :attr:`GRANT_TYPE_JWT`, the only two profiles defined in RFC 7521.
        :param scope: Optional. It must be a subset of previously granted scopes.
        """
        encoder = self.grant_assertion_encoders.get(grant_type, lambda a: a)
        data = kwargs.pop("data", {})
        data.update(scope=scope, assertion=encoder(assertion))
        return self._obtain_token(grant_type, data=data, **kwargs)

