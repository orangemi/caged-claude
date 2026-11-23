"""Opt-in real application probes: enrollment, crash recovery and docker restart.

Set CAGED_BROWSER_TEST_IMAGE and/or CAGED_CLAUDE_TEST_IMAGE to built images.
Uses isolated containers with no external network, user state or published ports.
"""
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import time
import unittest
import uuid

from caged.host.config import resolve, runtime_policy
from caged.host.docker import PRIVATE_WRITER
from caged.host.paths import ROOT
from caged.shared.policy import BROWSER_DNS_POLICIES, POLICY_ENV
from caged.shared.protocol import digest, encoded


class AgentLifecycle(unittest.TestCase):
    def docker(self, *args, check=True, timeout=20, **kwargs):
        return subprocess.run(['docker', *args], check=check, capture_output=True,
                              text=True, timeout=timeout, **kwargs)

    def inside(self, *command, check=True):
        return self.docker('exec', self.name, *command, check=check)

    def wait_for(self, predicate, timeout=45):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.2)
        self.fail('Probe lifecycle timed out: ' + self.docker('logs', '--tail', '30', self.name, check=False).stdout)

    def probe_pids(self):
        script = ('from pathlib import Path; import json; result=[]\n'
                  'for p in Path("/proc").glob("[0-9]*/cmdline"):\n'
                  ' try:\n'
                  '  args=p.read_bytes().split(b"\\0")\n'
                  f'  if args[1:4]==[b"-m",b"caged.runtime.agents.environment",{self.role.encode()!r}]: result.append(int(p.parent.name))\n'
                  ' except (FileNotFoundError, ProcessLookupError): pass\n'
                  'print(json.dumps(result))')
        result = self.inside('python3', '-c', script, check=False)
        return json.loads(result.stdout) if result.returncode == 0 else []

    def report(self):
        nonce = secrets.token_hex(32)
        url = f'http://127.0.0.1:{self.options[self.role + "_probe_port"]}/report?nonce={nonce}'
        result = self.inside('python3', '-c',
                            'import urllib.request,sys; print(urllib.request.urlopen(sys.argv[1], timeout=2).read().decode())',
                            url, check=False)
        if result.returncode:
            return None
        value = json.loads(result.stdout)
        signature = value.pop('signature')
        self.assertEqual(signature, hmac.new(bytes.fromhex(self.key), encoded(value), hashlib.sha256).hexdigest())
        self.assertEqual(value['nonce'], nonce)
        self.assertEqual(value['role'], self.role)
        self.assertEqual(value['session'], self.name)
        self.assertEqual(value['policy_digest'], digest(self.options))
        self.assertNotIn('error', value)
        actual = value['observation']
        for key, expected in {'username': 'claude-user', 'uid': 1000, 'gid': 1000,
                              'user_env': 'claude-user', 'logname': 'claude-user',
                              'timezone': 'Etc/UTC', 'system_timezone': 'Etc/UTC',
                              'offset': '+0000', 'locale': 'en_US.UTF-8',
                              'lc_all': 'en_US.UTF-8', 'language': 'en_US',
                              'localtime_matches': True, 'locale_valid': True,
                              'resolvers': ['127.0.0.1'], 'proxy_env_empty': True}.items():
            self.assertEqual(actual[key], expected, key)
        if self.role == 'browser':
            self.assertEqual(actual['dns_policies'], BROWSER_DNS_POLICIES)
        return value

    def exercise(self, role, image):
        self.role = role
        self.name = 'caged-agent-test-' + uuid.uuid4().hex[:12]
        self.key = secrets.token_hex(32)
        self.addCleanup(self.docker, 'rm', '-f', '-v', self.name, check=False)
        self.options = runtime_policy(resolve(['--settings', str(ROOT / 'config.example/app.yaml'),
                                               '--timezone', 'Etc/UTC', '--puid', '1000', '--pgid', '1000'], environ={}))
        values = {'PUID': '1000', 'PGID': '1000', 'TZ': 'Etc/UTC', 'USER': 'claude-user', 'LOGNAME': 'claude-user',
                  'LC_ALL': 'en_US.UTF-8', 'LANG': 'en_US.UTF-8', 'LANGUAGE': 'en_US',
                  POLICY_ENV: json.dumps(self.options)}
        environment = [item for key, value in values.items() for item in ('-e', key + '=' + value)]
        mounts = []
        if role == 'claude':
            mounts = ['--mount', f'type=bind,src={ROOT / "caged"},dst=/caged-runtime/caged,readonly',
                      '--mount', f'type=bind,src={ROOT / "containers/claude/entrypoint.sh"},dst=/caged-runtime/entrypoint.sh,readonly']
        self.docker('run', '-d', '--name', self.name, '--network', 'none', '--dns', '127.0.0.1',
                    '--shm-size=512m', *environment, *mounts, image)
        self.wait_for(self.probe_pids)
        self.assertIsNone(self.report(), 'A probe must wait for private enrollment')
        payload = json.dumps({'role': role, 'session': self.name, 'key': self.key})
        self.docker('exec', '-i', self.name, 'python3', '-c', PRIVATE_WRITER,
                    '/run/caged-agent.json', input=payload)
        self.wait_for(self.report)
        pids = self.probe_pids()
        self.assertEqual(len(pids), 1)
        self.inside('kill', '-KILL', str(pids[0]))
        self.wait_for(lambda: self.probe_pids() and self.probe_pids() != pids)
        self.wait_for(self.report)
        self.assertEqual(len(self.probe_pids()), 1, 'Only the container supervisor should start probes')
        self.docker('restart', '-t', '3', self.name, timeout=45)
        self.wait_for(self.report)
        self.assertEqual(len(self.probe_pids()), 1)
        script = 'from pathlib import Path; s=Path("/run/caged-agent.json").stat(); print(s.st_uid, oct(s.st_mode & 0o777))'
        self.assertEqual(self.inside('python3', '-c', script).stdout.strip(), '0 0o600')

    @unittest.skipUnless(os.environ.get('CAGED_BROWSER_TEST_IMAGE'), 'Set CAGED_BROWSER_TEST_IMAGE to a built browser image')
    def test_browser_probe_recovers_after_crash_and_container_restart(self):
        self.exercise('browser', os.environ['CAGED_BROWSER_TEST_IMAGE'])

    @unittest.skipUnless(os.environ.get('CAGED_CLAUDE_TEST_IMAGE'), 'Set CAGED_CLAUDE_TEST_IMAGE to a built Claude image')
    def test_claude_probe_recovers_after_crash_and_container_restart(self):
        self.exercise('claude', os.environ['CAGED_CLAUDE_TEST_IMAGE'])
