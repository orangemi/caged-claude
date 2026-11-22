"""Host bridge: host-side listener, socket relay, plans and launcher wiring."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from caged.host import bridge, hostbridge, stateful
from caged.host.paths import ROOT
from caged.runtime import hostpipe

HAVE_OPENSSH = all(shutil.which(tool) for tool in ('ssh', 'ssh-keygen')) and \
    any(os.access(path, os.X_OK) for path in ('/usr/sbin/sshd', shutil.which('sshd') or ''))


def plan_for(folder, user='alice'):
    return bridge.Plan({'instance': 'fixture', 'user': user,
                        'bridge': str(folder / 'state' / 'bridge'), 'config': str(folder / 'config')})


class Listener(unittest.TestCase):
    def test_sshd_config_is_user_only_key_only_and_listens_nowhere(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan = plan_for(Path(temporary))
            config = bridge.sshd_config(plan)
        for line in ('AllowUsers alice', 'PasswordAuthentication no', 'KbdInteractiveAuthentication no',
                     'UsePAM no', 'PermitRootLogin no', 'PermitTTY yes', 'AllowTcpForwarding yes',
                     'AllowAgentForwarding no', 'X11Forwarding no', 'Subsystem sftp internal-sftp', 'PidFile none'):
            self.assertIn(line + '\n', config)
        self.assertNotIn('ListenAddress', config)
        self.assertNotIn('Port ', config)

    def test_relative_plan_paths_and_root_are_refused(self):
        with self.assertRaises(bridge.BridgeError):
            bridge.Plan({'instance': 'x', 'user': 'u', 'bridge': 'relative', 'config': '/abs'})
        with tempfile.TemporaryDirectory() as temporary, patch.object(bridge.os, 'geteuid', return_value=0):
            with self.assertRaisesRegex(bridge.BridgeError, 'root'):
                bridge.start(plan_for(Path(temporary)))

    def test_missing_sshd_explains_how_to_fix_or_disable(self):
        with patch.object(bridge.shutil, 'which', return_value=None), patch.object(bridge.os, 'access', return_value=False):
            with self.assertRaisesRegex(bridge.BridgeError, 'host_bridge.enabled: false'):
                bridge.find_sshd(None)

    @unittest.skipUnless(HAVE_OPENSSH and os.geteuid() != 0, 'needs OpenSSH and a non-root user')
    def test_ssh_over_the_socket_runs_commands_as_the_host_user(self):
        # sshd's StrictModes rejects world-writable parents such as /tmp.
        folder = Path(tempfile.mkdtemp(prefix='.caged-bridge-test-', dir=Path.home()))
        try:
            os.chmod(folder, 0o700)
            data = {'instance': 'fixture', 'user': subprocess.run(['id', '-un'], capture_output=True, text=True).stdout.strip(),
                    'bridge': str(folder / 'bridge'), 'config': str(folder / 'config')}
            (folder / 'plan.json').write_text(json.dumps(data))
            started = subprocess.run([sys.executable, str(Path(bridge.__file__)), 'start', str(folder / 'plan.json')],
                                     capture_output=True, text=True, timeout=30)
            self.assertEqual(started.returncode, 0, started.stderr)
            plan = bridge.load(folder / 'plan.json')
            self.assertEqual(plan.socket.stat().st_mode & 0o777, 0o600)
            self.assertTrue(plan.authorized_keys.read_text().startswith('restrict,pty,port-forwarding ssh-ed25519 '))
            options = ['-o', 'BatchMode=yes', '-o', 'HostKeyAlias=caged-host', '-o', 'IdentitiesOnly=yes',
                       '-o', f'UserKnownHostsFile={plan.known_hosts}', '-o', 'StrictHostKeyChecking=yes',
                       '-i', str(plan.client_key), '-o',
                       f'ProxyCommand={sys.executable} {Path(hostpipe.__file__)} {plan.socket}', '-l', data['user'], 'caged-host']
            result = subprocess.run(['ssh', *options, 'id -un; exit 7'], capture_output=True, text=True, timeout=30)
            self.assertEqual((result.returncode, result.stdout.strip()), (7, data['user']), result.stderr)
            subprocess.run([sys.executable, str(Path(bridge.__file__)), 'stop', str(folder / 'plan.json')], timeout=30)
            self.assertFalse(plan.socket.exists())
            self.assertFalse(plan.pidfile.exists())
        finally:
            subprocess.run([sys.executable, str(Path(bridge.__file__)), 'stop', str(folder / 'plan.json')],
                           capture_output=True, timeout=30)
            shutil.rmtree(folder, ignore_errors=True)


class Relay(unittest.TestCase):
    def test_bytes_flow_both_ways_and_eof_is_half_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = os.path.join(temporary, 's')
            server = socket.socket(socket.AF_UNIX)
            server.bind(path)
            server.listen(1)
            received = []

            def serve():
                connection, _ = server.accept()
                while chunk := connection.recv(65536):
                    received.append(chunk)
                connection.sendall(b'reply:' + b''.join(received))
                connection.close()
            thread = threading.Thread(target=serve)
            thread.start()
            up_read, up_write = os.pipe()
            down_read, down_write = os.pipe()
            output = bytearray()

            def feed():  # more than a pipe buffer, so it must stream
                view = memoryview(b'x' * 200000)
                while view:
                    view = view[os.write(up_write, view):]
                os.close(up_write)

            def drain():
                while chunk := os.read(down_read, 65536):
                    output.extend(chunk)
            workers = [threading.Thread(target=feed), threading.Thread(target=drain)]
            for worker in workers:
                worker.start()
            code = hostpipe.relay(path, up_read, down_write)
            os.close(down_write)
            for worker in workers + [thread]:
                worker.join(5)
            server.close()
        self.assertEqual(code, 0)
        self.assertEqual(bytes(output), b'reply:' + b'x' * 200000)

    def test_a_missing_socket_says_how_to_check(self):
        with tempfile.TemporaryDirectory() as temporary, patch('sys.stderr') as stderr:
            self.assertEqual(hostpipe.relay(os.path.join(temporary, 'absent')), 255)
        self.assertIn('./cage status', ''.join(call.args[0] for call in stderr.write.call_args_list))


class LauncherSide(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = stateful.InstanceState(Path(self.temp.name) / 'state', 'fixture')
        self.args = SimpleNamespace(instance='fixture', host_bridge=True, host_bridge_user='alice', host_bridge_sshd=None)

    def test_plan_names_the_host_side_paths(self):
        plan = hostbridge.plan(self.args, self.state, 'start')
        self.assertEqual(plan['bridge'], str(self.state.root / 'bridge'))
        self.assertEqual(plan['config'], str(ROOT / 'config' / 'host-bridge' / 'fixture'))
        bridge.Plan(plan)  # what the host side accepts

    def test_plan_is_written_only_inside_the_project(self):
        self.assertIsNone(hostbridge.write_plan(self.args, self.state, 'stop', environ={}))
        with self.assertRaises(ValueError):
            hostbridge.write_plan(self.args, self.state, 'stop', environ={'CAGED_BRIDGE_PLAN': '/tmp/elsewhere.json'})
        target = ROOT / 'state' / '.plans'
        target.mkdir(parents=True, exist_ok=True)
        path = target / ('unit-' + str(os.getpid()) + '.json')
        try:
            hostbridge.write_plan(self.args, self.state, 'start', environ={'CAGED_BRIDGE_PLAN': str(path)})
            self.assertEqual(json.loads(path.read_text())['action'], 'start')
        finally:
            path.unlink(missing_ok=True)

    def test_status_reflects_the_socket(self):
        self.assertEqual(hostbridge.status(SimpleNamespace(host_bridge=False), self.state), 'disabled')
        self.assertEqual(hostbridge.status(self.args, self.state), 'stopped')
        folder = hostbridge.prepare(self.state)
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(folder / 'host.sock'))
            server.listen(1)
            self.assertEqual(hostbridge.status(self.args, self.state), 'listening')
        self.assertEqual(hostbridge.status(self.args, self.state), 'stopped (stale socket)')

    def test_bridge_directory_does_not_count_as_unmarked_state(self):
        hostbridge.prepare(self.state)
        self.assertIsNone(self.state.validate())


if __name__ == '__main__':
    unittest.main()
