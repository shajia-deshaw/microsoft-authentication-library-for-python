#------------------------------------------------------------------------------
#
# Copyright (c) Microsoft Corporation.
# All rights reserved.
#
# This code is licensed under the MIT License.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files(the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and / or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions :
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
#
#------------------------------------------------------------------------------

import uuid
from datetime import datetime, timedelta
import logging

from .mex import Mex
from .wstrust_response import parse_response

logger = logging.getLogger(__name__)

def send_request(
        username, password, cloud_audience_urn, endpoint_address, soap_action, http_client,
        **kwargs):
    iwa = username is None and password is None
    if not endpoint_address:
        raise ValueError("WsTrust endpoint address can not be empty")
    if soap_action is None:
        if '/trust/2005/usernamemixed' in endpoint_address:
            soap_action = Mex.ACTION_2005
        elif '/trust/13/usernamemixed' in endpoint_address:
            soap_action = Mex.ACTION_13
    if soap_action not in (Mex.ACTION_13, Mex.ACTION_2005):
        raise ValueError("Unsupported soap action: %s. "
            "Contact your administrator to check your ADFS's MEX settings." % soap_action)
    data = _build_rst(
        username, password, cloud_audience_urn, endpoint_address, soap_action)
    if iwa:
        # Make request kerberized
        from requests_kerberos import HTTPKerberosAuth, DISABLED
        resp = http_client.post(endpoint_address, data=data, headers={
            'Content-type':'application/soap+xml; charset=utf-8',
            'SOAPAction': soap_action,
        }, auth=HTTPKerberosAuth(mutual_authentication=DISABLED), allow_redirects=True)
    else:
        resp = http_client.post(endpoint_address, data=data, headers={
                'Content-type':'application/soap+xml; charset=utf-8',
                'SOAPAction': soap_action,
                }, **kwargs)
    if resp.status_code >= 400:
        logger.debug("Unsuccessful WsTrust request receives: %s", resp.text)
    # It turns out ADFS uses 5xx status code even with client-side incorrect password error
    # resp.raise_for_status()
    return parse_response(resp.text)


def escape_password(password):
    return (password.replace('&', '&amp;').replace('"', '&quot;')
        .replace("'", '&apos;')  # the only one not provided by cgi.escape(s, True)
        .replace('<', '&lt;').replace('>', '&gt;'))


def wsu_time_format(datetime_obj):
    # WsTrust (http://docs.oasis-open.org/ws-sx/ws-trust/v1.4/ws-trust.html)
    # does not seem to define timestamp format, but we see YYYY-mm-ddTHH:MM:SSZ
    # here (https://www.ibm.com/developerworks/websphere/library/techarticles/1003_chades/1003_chades.html)
    # It avoids the uncertainty of the optional ".ssssss" in datetime.isoformat()
    # https://docs.python.org/2/library/datetime.html#datetime.datetime.isoformat
    return datetime_obj.strftime('%Y-%m-%dT%H:%M:%SZ')


def _build_rst(username, password, cloud_audience_urn, endpoint_address, soap_action):
    iwa = username is None and password is None
    now = datetime.utcnow()
    _security_header = ""
    if not iwa:
        _security_header = """
            <wsse:Security s:mustUnderstand='1'
            xmlns:wsse='http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd'>
                <wsu:Timestamp wsu:Id='_0'>
                    <wsu:Created>{time_now}</wsu:Created>
                    <wsu:Expires>{time_expire}</wsu:Expires>
                </wsu:Timestamp>
                <wsse:UsernameToken wsu:Id='ADALUsernameToken'>
                    <wsse:Username>{username}</wsse:Username>
                    <wsse:Password>{password}</wsse:Password>
                </wsse:UsernameToken>
            </wsse:Security>
        """.format(
            username = username,
            password = escape_password(password),
            time_now=wsu_time_format(now),
            time_expire=wsu_time_format(now + timedelta(minutes=10)),
        )
    return """<s:Envelope xmlns:s='{s}' xmlns:wsa='{wsa}' xmlns:wsu='{wsu}'>
        <s:Header>
            <wsa:Action s:mustUnderstand='1'>{soap_action}</wsa:Action>
            <wsa:MessageID>urn:uuid:{message_id}</wsa:MessageID>
            <wsa:ReplyTo>
            <wsa:Address>http://www.w3.org/2005/08/addressing/anonymous</wsa:Address>
            </wsa:ReplyTo>
            <wsa:To s:mustUnderstand='1'>{endpoint_address}</wsa:To>
            {security_header}
        </s:Header>
        <s:Body>
        <wst:RequestSecurityToken xmlns:wst='{wst}'>
        <wsp:AppliesTo xmlns:wsp='http://schemas.xmlsoap.org/ws/2004/09/policy'>
            <wsa:EndpointReference>
                <wsa:Address>{applies_to}</wsa:Address>
            </wsa:EndpointReference>
        </wsp:AppliesTo>
        <wst:KeyType>{key_type}</wst:KeyType>
            <wst:RequestType>{request_type}</wst:RequestType>
        </wst:RequestSecurityToken>
        </s:Body>
        </s:Envelope>""".format(
            s=Mex.NS["s"], wsu=Mex.NS["wsu"], wsa=Mex.NS["wsa10"],
            soap_action=soap_action, message_id=str(uuid.uuid4()),
            endpoint_address=endpoint_address,
            wst=Mex.NS["wst"] if soap_action == Mex.ACTION_13 else Mex.NS["wst2005"],
            applies_to=cloud_audience_urn,
            key_type='http://docs.oasis-open.org/ws-sx/ws-trust/200512/Bearer'
                if soap_action == Mex.ACTION_13 else
                'http://schemas.xmlsoap.org/ws/2005/05/identity/NoProofKey',
            request_type='http://docs.oasis-open.org/ws-sx/ws-trust/200512/Issue'
                if soap_action == Mex.ACTION_13 else
                'http://schemas.xmlsoap.org/ws/2005/02/trust/Issue',
            security_header=_security_header
        )

