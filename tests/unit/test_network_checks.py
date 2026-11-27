"""Exit sampling, mainland ranges, pinned addresses and the live fault drill, without Docker."""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from caged.host import audit
from caged.runtime.checks import egress
from caged.runtime.network import mihomo
from caged.runtime.network.firewall import firewall_rules
from caged.shared import mainland
from caged.shared.policy import POLICY_ENV, PolicyViolation

POLICY = {'controller_port': 9090, 'mixed_port': 17890, 'expected_country': None,
          'dns_upstream': ['https://1.1.1.1/dns-query', 'https://9.9.9.9/dns-query']}


def location(address, country='SG', **extra):
    return 200, json.dumps({'success': True, 'ip': address, 'country_code': country, **extra})


class PinnedAddresses(unittest.TestCase):
    def test_firewall_allows_each_pinned_address_and_nothing_else(self):
        rules = firewall_rules(['203.0.113.1', '203.0.113.2'], 443).splitlines()
        for address in ('203.0.113.1', '203.0.113.2'):
            self.assertEqual(sum(f'-d {address}/32 -p tcp --dport 443' in row for row in rules), 2)
            self.assertEqual(sum(f'-s {address}/32 -p tcp --sport 443' in row for row in rules), 1)
        self.assertEqual(firewall_rules('203.0.113.1', 443), firewall_rules(['203.0.113.1'], 443))
        # Behind the host dialer's mounted socket, nothing may leave eth0 directly.
        self.assertFalse([row for row in firewall_rules([], 443).splitlines() if '-o eth0' in row or '-i eth0' in row])

    def test_several_addresses_become_a_fallback_group_named_like_the_node(self):
        with patch.dict(os.environ, {POLICY_ENV: json.dumps(POLICY)}):
            config = mihomo.make_config({'type': 'anytls', 'server': '203.0.113.1', 'port': 443}, 's',
                                        ['203.0.113.1', '203.0.113.2'])
            single = mihomo.make_config({'type': 'anytls', 'server': '203.0.113.1', 'port': 443}, 's', ['203.0.113.1'])
        self.assertEqual([(proxy['name'], proxy['server']) for proxy in config['proxies']],
                         [('CAGED_NODE-1', '203.0.113.1'), ('CAGED_NODE-2', '203.0.113.2')])
        group, = config['proxy-groups']
        self.assertEqual((group['name'], group['type'], group['proxies']), ('CAGED_NODE', 'fallback', ['CAGED_NODE-1', 'CAGED_NODE-2']))
        self.assertEqual(config['dns']['nameserver'],
                         ['https://1.1.1.1/dns-query#CAGED_NODE', 'https://9.9.9.9/dns-query#CAGED_NODE'])
        self.assertEqual(config['dns']['default-nameserver'], ['1.1.1.1', '9.9.9.9'])
        self.assertEqual(([proxy['name'] for proxy in single['proxies']], single['proxy-groups']), (['CAGED_NODE'], []))


class MainlandRanges(unittest.TestCase):
    def test_bundled_list_matches_known_mainland_and_foreign_addresses(self):
        for address in ('223.5.5.5', '114.114.114.114', '119.29.29.29'):
            self.assertTrue(mainland.is_mainland(address), address)
        for address in ('1.1.1.1', '8.8.8.8', '203.0.113.1', '202.45.84.58'):
            self.assertFalse(mainland.is_mainland(address), address)

    def test_build_collapses_apnic_delegations(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'delegated'
            path.write_text('apnic|CN|ipv4|1.0.1.0|256|20110414|allocated\n'
                            'apnic|CN|ipv4|1.0.2.0|512|20110414|allocated\n'
                            'apnic|HK|ipv4|1.0.8.0|256|20110414|allocated\n'
                            'apnic|CN|ipv6|2001:250::|35|20000426|allocated\n')
            self.assertEqual(mainland.build(path), ['1.0.1.0/24', '1.0.2.0/23'])


class ExitSampling(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(egress, 'CACHE', Path(tempfile.mkdtemp()) / 'cache.json')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_failed_source_starts_the_next_one_immediately(self):
        calls = []

        def fetch(url, timeout):
            calls.append(url)
            if 'icanhazip' in url:
                raise RuntimeError('timed out')
            if 'amazonaws' in url:
                return 200, '198.18.0.1\n'  # fake-IP is not an exit
            return 200, 'ip=8.8.8.8\nloc=US\n' if 'cdn-cgi' in url else '8.8.8.8\n'
        address, source, failures = egress.exit_ip(fetch, stagger=5)
        self.assertEqual((address, source), ('8.8.8.8', 'akamai'))
        self.assertEqual(failures, {'icanhazip': 'timed out', 'amazonaws': 'no public IPv4 in the response'})

    def test_slow_source_is_hedged_by_the_next(self):
        release = threading.Event()

        def fetch(url, timeout):
            if 'icanhazip' in url:
                release.wait(5)
                return 200, '9.9.9.9'
            return 200, '8.8.8.8'
        started = time.monotonic()
        address, source, _ = egress.exit_ip(fetch, stagger=0.05)
        release.set()
        self.assertEqual((address, source), ('8.8.8.8', 'amazonaws'))
        self.assertLess(time.monotonic() - started, 2)

    def test_location_must_name_the_queried_ip_and_a_country(self):
        answers = iter([(200, json.dumps({'success': False, 'ip': '8.8.8.8', 'country_code': 'US'})),
                        location('8.8.4.4'), (429, ''), (200, '[]'), location('8.8.8.8', 'us')])
        country, source, failures = egress.locate('8.8.8.8', lambda url, timeout: next(answers))
        self.assertEqual((country, source), ('US', 'ipapi.co'))
        self.assertEqual(list(failures), ['ipwho.is', 'ip.sb', 'ipwhois.app', 'geojs'])
        self.assertEqual(failures['ipwhois.app'], 'HTTP 429')
        # Cached afterwards: no request at all.
        self.assertEqual(egress.locate('8.8.8.8', None)[:2], ('US', 'ipapi.co (cached)'))

    def test_observe_refuses_mainland_exits_and_explains_missing_evidence(self):
        with self.assertRaisesRegex(PolicyViolation, 'mainland'):
            egress.observe(lambda url, timeout: (200, '223.5.5.5'))
        sources = {url for _, url in egress.IP_SOURCES}
        with self.assertRaisesRegex(PolicyViolation, 'mainland'):
            egress.observe(lambda url, timeout: (200, '8.8.8.8') if url in sources else location('8.8.8.8', 'CN'))

        def offline(url, timeout):
            if url == egress.REACHABILITY:
                return 204, ''
            raise RuntimeError('timed out')
        with self.assertRaisesRegex(RuntimeError, 'probably blocks these services'):
            egress.observe(offline)
        with self.assertRaisesRegex(RuntimeError, r'may be unreachable.*\./cage down && \./cage up'):
            egress.observe(lambda url, timeout: (_ for _ in ()).throw(RuntimeError('connection failed')))

    def test_observe_reports_sources(self):
        def fetch(url, timeout):
            return location('8.8.8.8', 'DE') if '8.8.8.8' in url else (200, '8.8.8.8')
        value = egress.observe(fetch, 'DE')
        self.assertEqual((value['ip'], value['country'], value['ip_source'], value['location_source']),
                         ('8.8.8.8', 'DE', 'icanhazip', 'ipwho.is'))
        with self.assertRaisesRegex(PolicyViolation, 'configured policy'):
            egress.observe(fetch, 'JP')


class FakeDrillSession:
    """Records docker calls; drill results and deny counters are scripted."""

    def __init__(self, control, fault, direct_growth):
        self.network_container_id = 'gateway'
        self.results = iter([control, fault])
        self.snapshots = iter([{'counters': {'CAGED_DIRECT': 10}}, {'counters': {'CAGED_DIRECT': 10 + direct_growth}}])

    def exec_probe(self, target, module, *arguments, **kwargs):
        assert kwargs['user'] == f'{audit.DRILL_UID}:{audit.DRILL_UID}'
        return MagicMock(returncode=0, stdout=json.dumps(next(self.results)))

    def policy_snapshot(self):
        return next(self.snapshots)


class LiveDrill(unittest.TestCase):
    def drill(self, control, fault, growth=2):
        calls = []
        with patch.object(audit.engine, 'docker', side_effect=lambda *args, **kwargs: calls.append(args)):
            try:
                return audit.live_drill(FakeDrillSession(control, fault, growth))
            finally:
                # The route rule is always removed again, even after a failure.
                self.assertEqual([call[4] for call in calls], ['add', 'del'])

    ok = {'https_literal_ip': True, 'udp_stun': True, 'public_dns': False}
    closed = {'https_literal_ip': False, 'udp_stun': False, 'public_dns': False}

    def test_closed_fault_with_kernel_evidence_passes(self):
        result = self.drill(self.ok, self.closed)
        self.assertEqual(result['kernel_direct_denied'], 2)

    def test_leak_fails_and_missing_evidence_is_unknown(self):
        with self.assertRaisesRegex(audit.Failed, 'https_literal_ip'):
            self.drill(self.ok, {**self.closed, 'https_literal_ip': True})
        with self.assertRaisesRegex(audit.Unknown, 'positive control'):
            self.drill(self.closed, self.closed)
        with self.assertRaisesRegex(audit.Unknown, 'kernel drop count'):
            self.drill(self.ok, self.closed, growth=0)

    def test_probe_parts_map_to_states(self):
        self.assertEqual(audit.part({'tcp': {'ip': '8.8.8.8'}}, 'tcp'), {'ip': '8.8.8.8'})
        with self.assertRaises(audit.Failed):
            audit.part({'tcp': {'error': 'leak', 'violation': True}}, 'tcp')
        with self.assertRaises(audit.Unknown):
            audit.part({'tcp': {'error': 'timed out'}}, 'tcp')
        with self.assertRaises(audit.Unknown):
            audit.part(None, 'tcp')


class UnixForwarder(unittest.TestCase):
    def test_loopback_port_relays_to_the_dialer_socket_both_ways(self):
        import socket
        from caged.runtime.network.main import forward_to_unix
        folder = tempfile.mkdtemp(prefix='cg')
        path = os.path.join(folder, 'dialer.sock')
        server = socket.socket(socket.AF_UNIX)
        server.bind(path)
        server.listen(1)
        self.addCleanup(server.close)

        def answer():
            connection, _ = server.accept()
            with connection:
                data = b''
                while chunk := connection.recv(4096):
                    data += chunk
                connection.sendall(b'node:' + data)
        threading.Thread(target=answer, daemon=True).start()
        port = forward_to_unix(path)
        with socket.create_connection(('127.0.0.1', port), timeout=5) as client:
            client.sendall(b'hello')
            client.shutdown(socket.SHUT_WR)
            reply = b''
            while chunk := client.recv(4096):
                reply += chunk
        self.assertEqual(reply, b'node:hello')
