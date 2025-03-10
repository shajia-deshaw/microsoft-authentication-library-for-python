﻿#------------------------------------------------------------------------------
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

try:
    from urllib.parse import urlparse
except ImportError:
    from urlparse import urlparse
try:
    from xml.etree import cElementTree as ET
except ImportError:
    from xml.etree import ElementTree as ET
import logging


logger = logging.getLogger(__name__)

def _xpath_of_root(route_to_leaf):
    # Construct an xpath suitable to find a root node which has a specified leaf
    return '/'.join(route_to_leaf + ['..'] * (len(route_to_leaf)-1))


def send_request(mex_endpoint, http_client, **kwargs):
    mex_resp = http_client.get(mex_endpoint, **kwargs)
    mex_resp.raise_for_status()
    try:
        return Mex(mex_resp.text).get_wstrust_username_password_endpoint()
    except ET.ParseError:
        logger.exception(
            "Malformed MEX document: %s, %s", mex_resp.status_code, mex_resp.text)
        raise

def send_request_iwa(mex_endpoint, http_client, **kwargs):
    mex_resp = http_client.get(mex_endpoint, **kwargs)
    mex_resp.raise_for_status()
    try:
        return Mex(mex_resp.text).get_wstrust_iwa_endpoint()
    except ET.ParseError:
        logger.exception(
            "Malformed MEX document: %s, %s", mex_resp.status_code, mex_resp.text)
        raise


class Mex(object):

    NS = {  # Also used by wstrust_*.py
        'wsdl': 'http://schemas.xmlsoap.org/wsdl/',
        'sp': 'http://docs.oasis-open.org/ws-sx/ws-securitypolicy/200702',
        'sp2005': 'http://schemas.xmlsoap.org/ws/2005/07/securitypolicy',
        'wsu': 'http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd',
        'wsa': 'http://www.w3.org/2005/08/addressing',  # Duplicate?
        'wsa10': 'http://www.w3.org/2005/08/addressing',
        'http': 'http://schemas.microsoft.com/ws/06/2004/policy/http',
        'soap12': 'http://schemas.xmlsoap.org/wsdl/soap12/',
        'wsp': 'http://schemas.xmlsoap.org/ws/2004/09/policy',
        's': 'http://www.w3.org/2003/05/soap-envelope',
        'wst': 'http://docs.oasis-open.org/ws-sx/ws-trust/200512',
        'trust': "http://docs.oasis-open.org/ws-sx/ws-trust/200512",  # Duplicate?
        'saml': "urn:oasis:names:tc:SAML:1.0:assertion",
        'wst2005': 'http://schemas.xmlsoap.org/ws/2005/02/trust',  # was named "t"
        }
    ACTION_13 = 'http://docs.oasis-open.org/ws-sx/ws-trust/200512/RST/Issue'
    ACTION_2005 = 'http://schemas.xmlsoap.org/ws/2005/02/trust/RST/Issue'

    def __init__(self, mex_document):
        self.dom = ET.fromstring(mex_document)

    def _get_policy_ids(self, components_to_leaf, binding_xpath):
        id_attr = '{%s}Id' % self.NS['wsu']
        return set(["#{}".format(policy.get(id_attr))
            for policy in self.dom.findall(_xpath_of_root(components_to_leaf), self.NS)
            # If we did not find any binding, this is potentially bad.
            if policy.find(binding_xpath, self.NS) is not None])

    def _get_username_password_policy_ids(self):
        path = ['wsp:Policy', 'wsp:ExactlyOne', 'wsp:All',
            'sp:SignedEncryptedSupportingTokens', 'wsp:Policy',
            'sp:UsernameToken', 'wsp:Policy', 'sp:WssUsernameToken10']
        policies = self._get_policy_ids(path, './/sp:TransportBinding')
        path2005 = ['wsp:Policy', 'wsp:ExactlyOne', 'wsp:All',
            'sp2005:SignedSupportingTokens', 'wsp:Policy',
            'sp2005:UsernameToken', 'wsp:Policy', 'sp2005:WssUsernameToken10']
        policies.update(self._get_policy_ids(path2005, './/sp2005:TransportBinding'))
        return policies

    def _get_iwa_policy_ids(self):
        return self._get_policy_ids(
            ['wsp:Policy', 'wsp:ExactlyOne', 'wsp:All', 'http:NegotiateAuthentication'],
            './/sp2005:TransportBinding')

    def _get_bindings(self):
        bindings = {}  # {binding_name: {"policy_uri": "...", "version": "..."}}
        for binding in self.dom.findall("wsdl:binding", self.NS):
            if (binding.find('soap12:binding', self.NS).get("transport") !=
                    'http://schemas.xmlsoap.org/soap/http'):
                continue
            action = binding.find(
                'wsdl:operation/soap12:operation', self.NS).get("soapAction")
            for pr in binding.findall("wsp:PolicyReference", self.NS):
                bindings[binding.get("name")] = {
                    "policy_uri": pr.get("URI"), "action": action}
        return bindings

    def _get_endpoints(self, bindings, policy_ids):
        endpoints = []
        for port in self.dom.findall('wsdl:service/wsdl:port', self.NS):
            binding_name = port.get("binding").split(':')[-1]  # Should have 2 parts
            binding = bindings.get(binding_name)
            if binding and binding["policy_uri"] in policy_ids:
                address = port.find('wsa10:EndpointReference/wsa10:Address', self.NS)
                if address is not None and address.text.lower().startswith("https://"):
                    endpoints.append(
                        {"address": address.text, "action": binding["action"]})
        return endpoints

    def get_wstrust_iwa_endpoint(self):
        """Returns {"address": "https://...", "action": "the soapAction value"}"""
        endpoints = self._get_endpoints(
            self._get_bindings(), self._get_iwa_policy_ids())
        for e in endpoints:
            if e["action"] == self.ACTION_13:
                return e  # Historically, we prefer ACTION_13 a.k.a. WsTrust13
        return endpoints[0] if endpoints else None
    def get_wstrust_username_password_endpoint(self):
        """Returns {"address": "https://...", "action": "the soapAction value"}"""
        endpoints = self._get_endpoints(
                self._get_bindings(), self._get_username_password_policy_ids())
        for e in endpoints:
            if e["action"] == self.ACTION_13:
                return e  # Historically, we prefer ACTION_13 a.k.a. WsTrust13
        return endpoints[0] if endpoints else None

