"""Node pinning through the dedicated HTTPS DNS service."""
from http.client import HTTPMessage
import ssl
import unittest
from unittest.mock import patch

from dns import message, rcode, rrset

from caged.host.errors import CageError
from caged.host.proxy import pin_node


class NodeResolution(unittest.TestCase):
    resolver = 'https://1.1.1.1/dns-query'
    node = {'server': 'node.example', 'port': 443}

    def mock_response(self, connection, answer=None, status=200):
        response = connection.return_value.getresponse.return_value.__enter__.return_value
        response.status = status
        response.headers = HTTPMessage()
        response.headers['Content-Type'] = 'application/dns-message'
        response.read.return_value = answer.to_wire() if answer is not None else b'invalid wire data'
        return response

    @patch('caged.host.proxy.HTTPSConnection')
    def test_resolves_cname_and_pins_public_ipv4_with_certificate_verification(self, connection):
        answer = message.make_response(message.make_query(self.node['server'], 'A', id=0))
        answer.answer.extend([
            rrset.from_text('node.example.', 60, 'IN', 'CNAME', 'target.example.'),
            rrset.from_text('target.example.', 60, 'IN', 'A', '198.18.1.56', '93.184.215.14', '93.184.215.13'),
            rrset.from_text('unrelated.example.', 60, 'IN', 'A', '1.0.0.1'),
        ])
        self.mock_response(connection, answer)
        with patch('socket.getaddrinfo', side_effect=AssertionError('system DNS must not be used')):
            self.assertEqual(pin_node(self.node, self.resolver),
                             {'server': '93.184.215.13', 'port': 443, 'sni': 'node.example'})
        self.assertEqual(self.node['server'], 'node.example')
        self.assertEqual(pin_node({**self.node, 'sni': 'custom.example'}, self.resolver)['sni'], 'custom.example')
        self.assertEqual(connection.call_args.args, ('1.1.1.1', None))
        context = connection.call_args.kwargs['context']
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        request = connection.return_value.request
        self.assertEqual(request.call_args.args, ('POST', '/dns-query'))
        self.assertEqual(message.from_wire(request.call_args.kwargs['body']).question, answer.question)

    @patch('caged.host.proxy.HTTPSConnection')
    def test_literal_node_skips_dns_and_rejects_non_public_ipv4(self, connection):
        node = {**self.node, 'server': '8.8.4.4'}
        self.assertEqual(pin_node(node, self.resolver), node)
        for server in ('198.18.1.56', '192.168.1.3', '224.0.0.1', '2606:4700:4700::1111'):
            with self.subTest(server=server), self.assertRaisesRegex(CageError, 'server field of the proxy YAML'):
                pin_node({**node, 'server': server}, self.resolver)
        connection.assert_not_called()

    @patch('caged.host.proxy.HTTPSConnection')
    def test_no_public_dns_answer_points_to_node_configuration(self, connection):
        query = message.make_query(self.node['server'], 'A', id=0)
        for status in (rcode.NOERROR, rcode.NXDOMAIN):
            with self.subTest(status=status):
                answer = message.make_response(query)
                answer.set_rcode(status)
                if status == rcode.NOERROR:
                    answer.answer.append(rrset.from_text('node.example.', 60, 'IN', 'A', '198.18.1.56'))
                self.mock_response(connection, answer)
                with self.assertRaisesRegex(CageError, 'no usable public IPv4 address.*--node'):
                    pin_node(self.node, self.resolver)

    @patch('caged.host.proxy.HTTPSConnection')
    def test_doh_failure_points_to_resolver_configuration(self, connection):
        request = connection.return_value.request
        for failure, expected in [(TimeoutError('private detail'), 'Could not reach'),
                                  (ssl.SSLCertVerificationError('private detail'), 'Could not verify')]:
            with self.subTest(failure=failure), self.assertRaisesRegex(CageError, expected + '.*--bootstrap-dns') as error:
                request.side_effect = failure
                pin_node(self.node, self.resolver)
            self.assertNotIn('private detail', str(error.exception))
        request.side_effect = None

        for status, expected in ((302, 'HTTP 302'), (200, 'invalid reply')):
            with self.subTest(status=status), self.assertRaisesRegex(CageError, expected + '.*--bootstrap-dns'):
                self.mock_response(connection, status=status)
                pin_node(self.node, self.resolver)
