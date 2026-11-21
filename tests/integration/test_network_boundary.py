#!/usr/bin/env python3
"""Real TCP/UDP/DNS-port isolation against a working local fixture, without credentials."""
import json
import subprocess
import time
import unittest
from caged.host.paths import ROOT
import uuid

from caged.host.config import resolve, runtime_policy
from caged.shared.policy import POLICY_ENV

from caged.host import images
from tests.fixtures import script

IMAGE = images.IMAGES['network']


def docker(*args, check=True, timeout=20):
    result = subprocess.run(['docker', *args], text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(result.stderr or result.stdout)
    return result


def wait_running(name):
    for _ in range(50):
        state = docker('inspect', name, '--format', '{{.State.Running}}', check=False)
        if state.stdout.strip() == 'true':
            return
        time.sleep(0.1)
    raise RuntimeError('Fixture did not start: ' + name)


class NetworkBoundary(unittest.TestCase):
    def test_tcp_udp_and_dns_cannot_escape_after_proxy_removal(self):
        suffix = uuid.uuid4().hex[:8]
        network = 'caged-fixture-' + suffix
        gateway, consumer, sink = [network + ending for ending in ('-net', '-client', '-sink')]
        children, names = [], []
        network_created = False
        try:
            docker('network', 'create', network)
            network_created = True
            child = subprocess.Popen([
                'docker', 'run', '--rm', '--name', sink, '--network', network,
                '--cap-drop', 'ALL', '--entrypoint', 'python3', IMAGE, '-u', '-c', script('boundary_server.py'),
            ])
            children.append(child)
            names.append(sink)
            wait_running(sink)
            target = docker('inspect', sink, '--format', '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}').stdout.strip()
            # Positive control: prove the local services work before testing blocks.
            baseline = docker('run', '--rm', '--network', network, '--cap-drop', 'ALL',
                              '--entrypoint', 'python3', IMAGE, '-c', script('boundary_probe.py'), target)
            self.assertEqual(json.loads(baseline.stdout), dict.fromkeys(('tcp','udp','dns_port','dns_tcp','dot'), True))
            options = resolve(['test', '--settings', str(ROOT / 'config.example/app.yaml')], environ={})
            process = subprocess.Popen([
                'docker', 'run', '--rm', '-i', '--name', gateway, '--network', network,
                '--cap-add', 'NET_ADMIN', '--device', '/dev/net/tun',
                '--read-only', '--tmpfs', '/run/caged:rw,noexec,nosuid,size=32m',
                '--tmpfs', '/tmp:rw,noexec,nosuid,size=16m', '--dns', '127.0.0.1',
                '-e', POLICY_ENV + '=' + json.dumps(runtime_policy(options)), IMAGE,
            ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            children.append(process)
            names.append(gateway)
            process.stdin.write(json.dumps({'display_name': 'non-routable test fixture', 'proxy': {
                'name': 'fixture', 'type': 'anytls', 'server': '203.0.113.1', 'port': 443,
                'password': 'non-secret-fixture', 'sni': 'invalid.example', 'udp': True,
            }}) + '\n')
            process.stdin.close()
            ready = False
            for _ in range(100):
                result = docker('exec', gateway, 'cat', '/run/caged/ready.json', check=False)
                if result.returncode == 0:
                    ready = True
                    break
                if process.poll() is not None:
                    self.fail('Gateway failed: ' + process.stdout.read())
                time.sleep(0.2)
            self.assertTrue(ready, 'Gateway readiness timed out')
            child = subprocess.Popen([
                'docker', 'run', '--rm', '--name', consumer, '--network', 'container:' + gateway,
                '--cap-drop', 'ALL', '--entrypoint', 'sleep', IMAGE, 'infinity',
            ])
            children.append(child)
            names.append(consumer)
            wait_running(consumer)
            before = docker('exec', consumer, 'python3', '-c', script('boundary_probe.py'), target)
            self.assertEqual(json.loads(before.stdout), dict.fromkeys(('tcp','udp','dns_port','dns_tcp','dot'), False))
            docker('stop', '-t', '10', gateway)
            # Kernel firewall survives proxy removal while the consumer owns the namespace.
            after = docker('exec', consumer, 'python3', '-c', script('boundary_probe.py'), target)
            self.assertEqual(json.loads(after.stdout), dict.fromkeys(('tcp','udp','dns_port','dns_tcp','dot'), False))
            result = docker('exec', consumer, 'sh', '-c', 'test ! -e /sys/class/net/caged0', check=False)
            self.assertEqual(result.returncode, 0, 'TUN must be gone for the failure-mode test')
            self.assertNotEqual(docker('inspect', gateway, check=False).returncode, 0)
        finally:
            for name in reversed(names):
                docker('stop', '-t', '5', name, check=False)
            for child in children:
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.terminate()
                if child.stdout:
                    child.stdout.close()
            if network_created:
                docker('network', 'rm', network, check=False)


if __name__ == '__main__':
    unittest.main(verbosity=2)
