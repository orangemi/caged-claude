"""Node pinning through HTTPS DNS services, and the host route check."""
from http.client import HTTPMessage
import io
import ssl
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock, patch

from dns import message, rcode, rrset

from caged.host import proxy
from caged.host.errors import CageError
from caged.host.proxy import pin_node, source_resolvers, check_host_routes

CLOUDFLARE, ALIDNS = 'https://1.1.1.1/dns-query', 'https://223.5.5.5/dns-query'


def reply(name, *addresses, status=rcode.NOERROR, cname=None):
    answer = message.make_response(message.make_query(name, 'A', id=0))
    answer.set_rcode(status)
    owner = name
    if cname:
        answer.answer.append(rrset.from_text(name + '.', 60, 'IN', 'CNAME', cname + '.'))
        owner = cname
    if addresses:
        answer.answer.append(rrset.from_text(owner + '.', 60, 'IN', 'A', *addresses))
    return answer


class FakeResolvers:
    """Stands in for HTTPSConnection: each resolver host answers or fails as configured."""

    def __init__(self, answers):
        self.answers, self.connections = answers, []

    def __call__(self, host, port, **kwargs):
        connection = MagicMock()
        connection.host, connection.kwargs = host, kwargs
        self.connections.append(connection)
        outcome = self.answers[host]

        def request(method, path, body, headers):
            connection.sent = (method, path, message.from_wire(body))
            if isinstance(outcome, Exception):
                raise outcome
        connection.request.side_effect = request
        response = connection.getresponse.return_value.__enter__.return_value
        response.headers = HTTPMessage()
        response.headers['Content-Type'] = 'application/dns-message'
        status, answer = (outcome if isinstance(outcome, tuple) else (200, outcome))
        response.status = status
        response.read.return_value = answer.to_wire() if hasattr(answer, 'to_wire') else b'invalid wire data'
        return connection


class NodeResolution(unittest.TestCase):
    node = {'server': 'node.example', 'port': 443}

    def resolve(self, answers, resolvers=(CLOUDFLARE, ALIDNS), node=None, source=()):
        fake = FakeResolvers(answers)
        with patch('caged.host.proxy.HTTPSConnection', side_effect=fake), \
                patch('socket.getaddrinfo', side_effect=AssertionError('system DNS must not be used')):
            return pin_node(node or self.node, list(resolvers), source), fake

    def test_pins_every_public_ipv4_through_cname_with_certificate_verification(self):
        answer = reply('node.example', '198.18.1.56', '93.184.215.14', '93.184.215.13', '9.9.9.9', cname='target.example')
        answer.answer.append(rrset.from_text('unrelated.example.', 60, 'IN', 'A', '1.0.0.1'))
        (pinned, addresses), fake = self.resolve({'1.1.1.1': answer, '223.5.5.5': answer})
        # Numeric order (9.x before 93.x); fake-IP and unrelated records are dropped.
        self.assertEqual(addresses, ['9.9.9.9', '93.184.215.13', '93.184.215.14'])
        self.assertEqual(pinned, {'server': '9.9.9.9', 'port': 443, 'sni': 'node.example'})
        self.assertEqual(self.node['server'], 'node.example')
        connection = next(item for item in fake.connections if item.host == '1.1.1.1')
        self.assertTrue(connection.kwargs['context'].check_hostname)
        self.assertEqual(connection.kwargs['context'].verify_mode, ssl.CERT_REQUIRED)
        method, path, query = connection.sent
        self.assertEqual((method, path), ('POST', '/dns-query'))
        self.assertEqual(query.question, answer.question)
        (pinned, _), _ = self.resolve({'1.1.1.1': answer, '223.5.5.5': answer}, node={**self.node, 'sni': 'custom.example'})
        self.assertEqual(pinned['sni'], 'custom.example')

    def test_caps_pinned_addresses(self):
        answer = reply('node.example', *[f'8.8.8.{index}' for index in range(1, 8)])
        (_, addresses), _ = self.resolve({'1.1.1.1': answer, '223.5.5.5': answer})
        self.assertEqual(len(addresses), proxy.MAX_ADDRESSES)

    def test_next_resolver_answers_when_the_first_fails_and_order_decides_otherwise(self):
        second = reply('node.example', '8.8.4.4')
        (_, addresses), _ = self.resolve({'1.1.1.1': TimeoutError('private detail'), '223.5.5.5': second})
        self.assertEqual(addresses, ['8.8.4.4'])
        (_, addresses), _ = self.resolve({'1.1.1.1': reply('node.example', '8.8.8.8'), '223.5.5.5': second})
        self.assertEqual(addresses, ['8.8.8.8'])
        # A resolver whose answer has no public IPv4 (e.g. fake-IP) does not stop the next one.
        (_, addresses), _ = self.resolve({'1.1.1.1': reply('node.example', '198.18.0.9'), '223.5.5.5': second})
        self.assertEqual(addresses, ['8.8.4.4'])

    def test_literal_node_skips_dns_and_rejects_non_public_ipv4(self):
        node = {**self.node, 'server': '8.8.4.4'}
        (pinned, addresses), fake = self.resolve({}, node=node)
        self.assertEqual((pinned, addresses), (node, ['8.8.4.4']))
        self.assertEqual(fake.connections, [])
        for server in ('198.18.1.56', '192.168.1.3', '224.0.0.1', '2606:4700:4700::1111'):
            with self.subTest(server=server), self.assertRaisesRegex(CageError, 'server field of the proxy YAML'):
                self.resolve({}, node={**node, 'server': server})

    def test_no_public_dns_answer_points_to_node_configuration(self):
        for answer in (reply('node.example', '198.18.1.56'), reply('node.example', status=rcode.NXDOMAIN)):
            with self.subTest(status=answer.rcode()), \
                    self.assertRaisesRegex(CageError, 'no usable public IPv4 address.*--node'):
                self.resolve({'1.1.1.1': answer, '223.5.5.5': answer})

    def test_doh_failure_lists_each_resolver_and_points_to_resolver_configuration(self):
        answers = {'1.1.1.1': TimeoutError('private detail'), '223.5.5.5': ssl.SSLCertVerificationError('private detail')}
        with self.assertRaisesRegex(CageError, '1.1.1.1: could not be reached; 223.5.5.5: could not verify.*--bootstrap-dns') as error:
            self.resolve(answers)
        self.assertNotIn('private detail', str(error.exception))
        for outcome, expected in (((302, None), 'returned HTTP 302'), ((200, None), 'invalid reply'),
                                  (reply('node.example', status=rcode.SERVFAIL), r'could not resolve the node \(SERVFAIL\)')):
            with self.subTest(expected=expected), self.assertRaisesRegex(CageError, expected + '.*--bootstrap-dns'):
                self.resolve({'1.1.1.1': outcome}, resolvers=[CLOUDFLARE])

    def test_source_resolvers_by_hostname_are_found_through_the_literal_ones(self):
        answers = {'1.1.1.1': reply('dns.provider.example', '8.8.8.8'),
                   'dns.provider.example': reply('node.example', '9.9.9.9')}
        fake = FakeResolvers(answers)
        created = []
        with patch('caged.host.proxy.HTTPSConnection', side_effect=fake), \
                patch('socket.create_connection', side_effect=lambda target, *rest: created.append(target)):
            _, addresses = pin_node(self.node, [CLOUDFLARE], ['https://dns.provider.example/dns-query'])
            provider = next(item for item in fake.connections if item.host == 'dns.provider.example')
            provider._create_connection(('dns.provider.example', 443), 5)
        self.assertEqual(addresses, ['9.9.9.9'])
        self.assertEqual(created, [('8.8.8.8', 443)])  # connects to the resolved IP, verifies the name

    def test_source_resolvers_keep_only_safe_remote_doh(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'nodes.yaml'
            path.write_text('''dns:
  proxy-server-nameserver:
    - https://doh.pub/dns-query
    - https://doh.pub/dns-query
    - https://223.5.5.5/dns-query
    - https://1.12.12.12/dns-query#skip-cert-verify=true
    - https://user:pass@dns.example/dns-query
    - https://192.168.1.1/dns-query
    - https://100.100.100.100/dns-query
    - https://router.lan/dns-query
    - tls://8.8.8.8
    - 114.114.114.114
    - system
    - https://dns.google/dns-query
    - https://dns.quad9.net/dns-query
    - https://dns.adguard.com/dns-query
proxies: []
''')
            self.assertEqual(source_resolvers(path), ['https://doh.pub/dns-query', 'https://223.5.5.5/dns-query',
                                                      'https://dns.google/dns-query', 'https://dns.quad9.net/dns-query'])
            path.write_text('proxies: []\n')
            self.assertEqual(source_resolvers(path), [])


class HostRoutes(unittest.TestCase):
    def check(self, routes, mode, system='Darwin'):
        stderr = io.StringIO()
        with patch.object(proxy, 'IN_LAUNCHER', True), redirect_stderr(stderr):
            result = check_host_routes(['203.0.113.1'], mode, {'CAGED_HOST_OS': system, 'CAGED_HOST_ROUTES': routes})
        return result, stderr.getvalue()

    def test_physical_routes_are_quiet(self):
        for system, routes in (('Darwin', '1.1.1.1=en0,223.5.5.5=en0'), ('Linux', '1.1.1.1=eth0,223.5.5.5=wlp2s0')):
            with self.subTest(system=system):
                result, warning = self.check(routes, 'warn', system)
                self.assertEqual((result['tunnels'], warning), ({}, ''))

    def test_tunnel_routes_warn_block_or_are_allowed(self):
        result, warning = self.check('1.1.1.1=utun4,223.5.5.5=en0', 'warn')
        self.assertEqual(result['tunnels'], {'1.1.1.1': 'utun4'})
        self.assertIn('1.1.1.1 via utun4', warning)
        with self.assertRaisesRegex(CageError, 'non-physical interface.*proxy.host_tunnel'):
            self.check('1.1.1.1=Meta', 'block', 'Linux')
        self.assertEqual(self.check('1.1.1.1=utun4', 'allow'), ({}, ''))

    def test_native_launcher_also_checks_the_node_addresses(self):
        with patch.object(proxy, 'IN_LAUNCHER', False), \
                patch('subprocess.run', return_value=MagicMock(stdout='   interface: utun7\n')):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                result = check_host_routes(['203.0.113.1'], 'warn', {'CAGED_HOST_OS': 'Darwin'})
        self.assertEqual(result['tunnels'], {'203.0.113.1': 'utun7'})
