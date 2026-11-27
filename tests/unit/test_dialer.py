"""Host dialer bookkeeping and the choice between a direct and a dialed node path."""
import io
import json
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from caged.host import dialer, proxy
from caged.host.errors import CageError
from caged.host.session import Session

NODE = {'type': 'anytls', 'server': '203.0.113.1', 'port': 7210, 'sni': 'node.example'}


class DialerTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        patcher = patch.object(dialer, 'DIR', self.folder)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_status(self, **value):
        (self.folder / 'status.json').write_text(json.dumps({'heartbeat': time.time(), **value}))


class Bookkeeping(DialerTest):
    def test_register_and_unregister_keep_other_instances(self):
        dialer.register('other', ['203.0.113.9'], 443, 'other.example', {'CAGED_HOST_OS': 'Darwin'})
        route, revision = dialer.register('one', ['203.0.113.1', '203.0.113.2'], 7210, 'node.example',
                                          {'CAGED_HOST_OS': 'Darwin'})
        self.assertEqual(route['listen'], f'127.0.0.1:{dialer.port("one")}')
        self.assertEqual(route['targets'], ['203.0.113.1:7210', '203.0.113.2:7210'])
        saved = json.loads((self.folder / 'routes.json').read_text())['instances']
        self.assertEqual((set(saved), saved['one']['revision']), ({'one', 'other'}, revision))
        dialer.unregister('one')
        self.assertEqual(set(json.loads((self.folder / 'routes.json').read_text())['instances']), {'other'})

    def test_linux_uses_a_socket_and_wsl_depends_on_docker(self):
        route, _ = dialer.register('one', ['203.0.113.1'], 443, 'n', {'CAGED_HOST_OS': 'Linux'})
        self.assertEqual(route['listen'], f'unix:{self.folder}/sock/one/dialer.sock')
        self.assertNotIn('relay', route)
        self.assertTrue((self.folder / 'sock' / 'one').is_dir())
        wsl = {'CAGED_HOST_OS': 'Linux', 'CAGED_HOST_WSL': '1'}
        self.assertEqual(dialer.kind({**wsl, 'CAGED_DOCKER_DESKTOP': '1'}), 'tcp')
        self.assertEqual(dialer.kind(wsl), 'relay')
        # Docker Engine in WSL: the Windows dialer listens on loopback, the relay serves the socket.
        route, _ = dialer.register('two', ['203.0.113.1'], 443, 'n', wsl)
        self.assertEqual((route['listen'], route['relay']),
                         (f'127.0.0.1:{dialer.port("two")}', f'unix:{self.folder}/sock/two/dialer.sock'))

    def test_wait_relay_needs_a_listening_relay(self):
        with self.assertRaisesRegex(CageError, 'WSL relay to the Windows dialer is not running'):
            dialer.wait_relay('one', timeout=0.5)
        (self.folder / 'relay-status.json').write_text(json.dumps(
            {'heartbeat': time.time(), 'instances': {'one': {'listen': 'unix:/s', 'listening': True}}}))
        self.assertTrue(dialer.wait_relay('one', timeout=1)['listening'])

    def test_stale_status_means_not_running(self):
        self.assertIsNone(dialer.status())
        (self.folder / 'status.json').write_text(json.dumps({'heartbeat': time.time() - 60}))
        self.assertIsNone(dialer.status())
        self.write_status(tunnel=True, sample_route='utun4')
        self.assertEqual(dialer.tunnel(dialer.status()), 'utun4')

    def test_wait_check_matches_the_revision_and_reports_listen_errors(self):
        self.write_status(instances={'one': {'listen': '127.0.0.1:1', 'check': {'revision': 1}}})
        threading.Timer(0.3, lambda: self.write_status(
            instances={'one': {'listen': '127.0.0.1:1', 'check': {'revision': 2, 'bound': {'ok': True}}}})).start()
        self.assertEqual(dialer.wait_check('one', 2, timeout=5)['bound'], {'ok': True})
        self.write_status(instances={'one': {'listen': '127.0.0.1:1', 'listen_error': 'address in use'}})
        with self.assertRaisesRegex(CageError, 'address in use'):
            dialer.wait_check('one', 3, timeout=2)


class NodePath(DialerTest):
    def session(self, mode):
        args = SimpleNamespace(host_tunnel=mode, instance='one', host_bridge_address='0.250.250.254')
        return SimpleNamespace(args=args, dialer_registered=False, node_path={'kind': 'direct'})

    def choose(self, mode, tunnels=None, check=None, running=True, system='Darwin', env=None, relay=None):
        session = self.session(mode)
        if running:
            self.write_status(interface='en0')
        output, errors = io.StringIO(), io.StringIO()

        def answer(instance, revision, timeout=30):
            return {'revision': revision, 'interface': 'en0', **(check or {})}
        with patch.object(proxy, 'check_host_routes', return_value={'tunnels': tunnels or {}}), \
                patch.object(dialer, 'wait_check', side_effect=answer), \
                patch.object(dialer, 'wait_status', side_effect=dialer.status), \
                patch.object(dialer, 'wait_relay', side_effect=relay or (lambda instance: {'listening': True})), \
                patch.dict('os.environ', {'CAGED_HOST_OS': system, **(env or {})}), \
                redirect_stdout(output), redirect_stderr(errors):
            path = Session.choose_node_path(session, NODE, ['203.0.113.1'], 'node.example')
        return session, path, output.getvalue() + errors.getvalue()

    ok = {'bound': {'ok': True}, 'default': {'ok': False, 'error': 'connection closed during the TLS handshake'},
          'bound_exit': '198.51.100.7', 'default_exit': '203.0.113.50'}

    def test_auto_without_a_tunnel_stays_direct(self):
        session, path, _ = self.choose('auto')
        self.assertEqual((path, session.dialer_registered), ({'kind': 'direct'}, False))
        self.assertFalse((self.folder / 'routes.json').exists())

    def test_auto_with_a_tunnel_uses_the_dialer(self):
        session, path, output = self.choose('auto', {'1.1.1.1': 'utun4'}, self.ok)
        self.assertEqual((path['kind'], path['interface'], session.dialer_registered), ('tcp', 'en0', True))
        self.assertIn('bound to en0, bypassing 1.1.1.1 via utun4', output)
        self.assertIn('198.51.100.7 over en0, 203.0.113.50 through the TUN', output)
        self.assertEqual(Session.gateway_endpoint(SimpleNamespace(args=session.args, node_path=path), NODE,
                                                  ['203.0.113.1'], 'node.example'),
                         ({**NODE, 'server': '0.250.250.254', 'port': dialer.port('one')}, ['0.250.250.254'], []))

    def test_vless_through_the_dialer_keeps_its_tls_name_in_servername(self):
        session, path, _ = self.choose('auto', {'1.1.1.1': 'utun4'}, self.ok)
        vless = {'type': 'vless', 'tls': True, 'server': '203.0.113.1', 'port': 443}
        node, _, _ = Session.gateway_endpoint(SimpleNamespace(args=session.args, node_path=path), vless,
                                              ['203.0.113.1'], 'node.example')
        self.assertEqual((node['server'], node['servername']), ('0.250.250.254', 'node.example'))
        self.assertNotIn('sni', node)
        reality = {**vless, 'servername': 'cover.example'}
        node, _, _ = Session.gateway_endpoint(SimpleNamespace(args=session.args, node_path=path), reality,
                                              ['203.0.113.1'], 'node.example')
        self.assertEqual(node['servername'], 'cover.example')
        route = json.loads((self.folder / 'routes.json').read_text())['instances']['one']
        self.assertEqual(route['sni'], 'node.example')  # NODE (anytls) registered with its sni

    def test_linux_dialer_endpoint_is_a_mounted_socket(self):
        _, path, _ = self.choose('dialer', check=self.ok, system='Linux')
        self.assertEqual((path['kind'], path['socket']), ('unix', '/run/caged-dialer/dialer.sock'))
        node, addresses, mounts = Session.gateway_endpoint(
            SimpleNamespace(args=self.session('dialer').args, node_path=path), {**NODE, 'sni': None}, ['203.0.113.1'], '203.0.113.1')
        self.assertEqual((node['server'], node['interface-name'], node['sni'], addresses), ('127.0.0.1', 'lo', '203.0.113.1', []))
        self.assertEqual(mounts, ['--mount', f'type=bind,src={self.folder}/sock/one,dst=/run/caged-dialer,readonly'])

    def test_wsl_with_docker_engine_relays_through_a_socket_with_mirrored_networking(self):
        wsl = {'CAGED_HOST_WSL': '1', 'CAGED_WSL_NETWORKING': 'mirrored'}
        _, path, _ = self.choose('auto', {'1.1.1.1': 'Mihomo'}, self.ok, system='Linux', env=wsl)
        self.assertEqual((path['kind'], path['socket']), ('relay', '/run/caged-dialer/dialer.sock'))
        node, addresses, mounts = Session.gateway_endpoint(
            SimpleNamespace(args=self.session('auto').args, node_path=path), NODE, ['203.0.113.1'], 'node.example')
        self.assertEqual((node['server'], node['interface-name'], addresses), ('127.0.0.1', 'lo', []))
        self.assertEqual(mounts[1], f'type=bind,src={self.folder}/sock/one,dst=/run/caged-dialer,readonly')

    def test_wsl_auto_uses_a_running_dialer_even_when_no_tunnel_is_seen(self):
        mirrored = {'CAGED_HOST_WSL': '1', 'CAGED_WSL_NETWORKING': 'mirrored'}
        _, path, output = self.choose('auto', {}, self.ok, system='Linux', env=mirrored)
        self.assertEqual(path['kind'], 'relay')
        self.assertIn('bypassing the host routes', output)
        # Without a running dialer, auto stays direct and quiet.
        (self.folder / 'status.json').unlink()
        _, path, output = self.choose('auto', {}, self.ok, running=False, system='Linux', env=mirrored)
        self.assertEqual((path, output), ({'kind': 'direct'}, ''))
        # macOS keeps relying on the visible routes.
        _, path, _ = self.choose('auto', {}, self.ok)
        self.assertEqual(path, {'kind': 'direct'})

    def test_wsl_relay_problems_fall_back_only_in_auto(self):
        nat = {'CAGED_HOST_WSL': '1', 'CAGED_WSL_NETWORKING': 'nat'}
        session, path, output = self.choose('auto', {'1.1.1.1': 'Mihomo'}, self.ok, system='Linux', env=nat)
        self.assertEqual((path, session.dialer_registered), ({'kind': 'direct'}, False))
        self.assertIn('networkingMode=mirrored', output)
        with self.assertRaisesRegex(CageError, 'WSL networking is nat'):
            self.choose('dialer', check=self.ok, system='Linux', env=nat)

        def missing(instance):
            raise CageError('The WSL relay to the Windows dialer is not running; see state/.dialer/relay.log')
        mirrored = {'CAGED_HOST_WSL': '1', 'CAGED_WSL_NETWORKING': 'mirrored'}
        session, path, output = self.choose('auto', {'1.1.1.1': 'Mihomo'}, self.ok, system='Linux', env=mirrored, relay=missing)
        self.assertEqual((path, session.dialer_registered), ({'kind': 'direct'}, False))
        self.assertIn('relay.log', output)
        self.assertEqual(json.loads((self.folder / 'routes.json').read_text())['instances'], {})

    def test_block_and_direct_modes(self):
        with self.assertRaisesRegex(CageError, 'non-physical interface'):
            self.choose('block', {'1.1.1.1': 'utun4'})
        _, path, output = self.choose('direct', {'1.1.1.1': 'utun4'})
        self.assertEqual(path, {'kind': 'direct'})
        self.assertIn('proxy.host_tunnel is direct', output)

    def test_missing_dialer_falls_back_only_in_auto(self):
        _, path, output = self.choose('auto', {'1.1.1.1': 'utun4'}, running=False)
        self.assertEqual(path, {'kind': 'direct'})
        self.assertIn('host dialer is not running', output)
        with self.assertRaisesRegex(CageError, 'host dialer is not running'):
            self.choose('dialer', running=False)

    def test_dialer_detected_tunnel_counts_where_routes_are_invisible(self):
        self.write_status(interface='Ethernet', tunnel=True, sample_route='Meta')
        session = self.session('auto')
        with patch.object(proxy, 'check_host_routes', return_value={'tunnels': {}}), \
                patch.object(dialer, 'wait_check', return_value={'revision': 0, 'interface': 'Ethernet', **self.ok}), \
                patch.dict('os.environ', {'CAGED_HOST_OS': 'Linux', 'CAGED_HOST_WSL': '1', 'CAGED_DOCKER_DESKTOP': '1'}), \
                redirect_stdout(io.StringIO()):
            path = Session.choose_node_path(session, NODE, ['203.0.113.1'], 'node.example')
        self.assertEqual((path['kind'], path['tunnel']), ('tcp', '1.1.1.1 via Meta'))

    def test_unreachable_node(self):
        both_fail = {'bound': {'ok': False, 'error': 'timed out'}, 'default': {'ok': False, 'error': 'timed out'}}
        with self.assertRaisesRegex(CageError, 'not reachable over en0'):
            self.choose('auto', {'1.1.1.1': 'utun4'}, both_fail)
        only_default = {'bound': {'ok': False, 'error': 'timed out'}, 'default': {'ok': True}}
        session, path, output = self.choose('auto', {'1.1.1.1': 'utun4'}, only_default)
        self.assertEqual((path, session.dialer_registered), ({'kind': 'direct'}, False))
        self.assertIn("only through the host's default path", output)
        self.assertEqual(json.loads((self.folder / 'routes.json').read_text())['instances'], {})
        with self.assertRaisesRegex(CageError, 'not reachable'):
            self.choose('dialer', check=only_default)


class ReadOnlyMounts(unittest.TestCase):
    def test_only_a_socket_path_makes_the_gateway_mount_read_only(self):
        session = SimpleNamespace(node_path={'kind': 'relay'}, readonly_mounts=lambda: {'/caged-runtime/caged': 'src'})
        self.assertEqual(Session.readonly_destinations(session, 'gateway'), {'/run/caged-dialer'})
        self.assertEqual(Session.readonly_destinations(session, 'claude'), {'/caged-runtime/caged'})
        self.assertEqual(Session.readonly_destinations(session, 'browser'), set())
        session.node_path = {'kind': 'tcp'}
        self.assertEqual(Session.readonly_destinations(session, 'gateway'), set())
