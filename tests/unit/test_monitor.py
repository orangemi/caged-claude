"""Permit transitions, missing evidence, replay protection and operator intent."""
import copy
import hashlib
import hmac
import json
from pathlib import Path
import sys
import tempfile
import unittest
import types
from unittest.mock import patch

from caged.runtime.network import monitor, lease as gate
from caged.host.config import DEFAULTS, resolve, runtime_policy, ConfigError

GOOD = {'ok': True, 'evidence': {'exit': {'ip': '203.0.113.2', 'country': 'SG'}}}
TEMPORARY = {'ok': False, 'kind': 'temporary', 'reason': 'No current evidence'}
BAD = {'ok': False, 'kind': 'violation', 'reason': 'Wrong exit country'}


class MonitorTransitions(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.clock = 100.0
        self.permit = 0
        self.renewals = []
        self.patches = [patch.object(gate, 'ROOT', self.root),
                        patch.object(gate, 'remaining', lambda: self.permit),
                        patch.object(gate, 'revoke', self.revoke),
                        patch.object(gate, 'renew', self.renew)]
        for item in self.patches:
            item.start()
        self.options = DEFAULTS.copy()
        self.model = self.new_model()

    def new_model(self):
        return monitor.Monitor(self.options, clock=lambda: self.clock, wall=lambda: 1000 + self.clock)

    def revoke(self):
        self.permit = 0

    def renew(self, seconds):
        self.renewals.append(seconds)
        self.permit = seconds
        return True

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.folder.cleanup()

    def tick(self, result=GOOD):
        self.model.tick(run=lambda stage, timeout: copy.deepcopy(result))

    def test_only_successful_fresh_evidence_renews(self):
        self.tick()
        self.assertEqual(self.renewals, [90])
        self.clock += 31
        self.model.tick(run=lambda stage, timeout: GOOD if stage == 'critical' else TEMPORARY)
        self.assertEqual(self.renewals, [90], 'Network timeouts must never extend authorization')
        self.assertEqual(self.model.state, 'open')
        self.permit = 0
        self.tick(TEMPORARY)
        self.assertEqual(self.model.state, 'closed')

    def test_policy_violation_is_latched_across_monitor_restart(self):
        self.tick()
        self.clock += 5
        self.tick(BAD)
        self.assertEqual(self.permit, 0)
        self.assertEqual(self.model.state, 'blocked')
        self.model = self.new_model()
        self.tick()
        self.assertEqual(self.model.state, 'blocked')
        self.assertEqual(self.renewals, [90])
        (self.root / 'resume-request').write_text('resume')
        self.tick()
        self.assertEqual(self.model.state, 'open')

    def test_manual_pause_wins_over_an_inflight_success(self):
        def during_probe(stage, timeout):
            (self.root / 'manual-pause').write_text('pause')
            return GOOD
        self.model.tick(run=during_probe)
        self.assertEqual(self.renewals, [])
        self.tick()
        self.assertEqual(self.model.state, 'paused')

    def test_missing_environment_evidence_closes_without_waiting_for_expiry(self):
        self.tick()
        self.clock += 5
        self.tick(TEMPORARY)
        self.assertEqual(self.model.state, 'closed')
        self.assertEqual(self.permit, 0)
        self.assertEqual(self.model.next_check, self.clock + 15)

    def test_recovery_backoff_and_success_reset(self):
        self.model.ever_opened = True
        self.tick(TEMPORARY)
        self.assertEqual(self.model.next_check, self.clock + 15)
        self.clock += 15
        self.tick(TEMPORARY)
        self.assertEqual(self.model.next_check, self.clock + 30)
        self.clock += 30
        self.tick()
        self.assertEqual(self.model.state, 'open')
        self.assertEqual(self.model.attempts, 0)

    def test_monitor_settings_precedence_and_timing_validation(self):
        args = resolve(['--monitor-lease', '120'], environ={'CAGED_BROWSER_MONITOR_LEASE': '100'})
        self.assertEqual(args.monitor_lease, 120)
        with self.assertRaises(ConfigError):
            resolve(['--monitor-lease', '10'], environ={})
        with self.assertRaises(ConfigError):
            resolve(['--browser-probe-port', '53'], environ={})

    def test_probe_response_cannot_be_replayed_or_forged(self):
        options = runtime_policy(resolve([], environ={}))
        private = {'session': 'this-session', 'agents': {'browser': '01' * 32, 'claude': '02' * 32}}
        response = {'role': 'browser', 'session': 'this-session', 'nonce': 'old-nonce',
                    'policy_digest': monitor.digest(options)}
        response['signature'] = hmac.new(bytes.fromhex(private['agents']['browser']), monitor.encoded(response), hashlib.sha256).hexdigest()
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self, *_): return json.dumps(response).encode()
        with patch.object(monitor.urllib.request, 'build_opener') as opener:
            opener.return_value.open.return_value = Response()
            with self.assertRaises(monitor.PolicyViolation):
                monitor.check_agents(private, options)

    def test_a_success_queued_before_a_monitor_stall_is_rejected(self):
        import subprocess
        response = {**GOOD, 'completed_monotonic': 10}
        child = subprocess.CompletedProcess([], 0, json.dumps(response), '')
        with patch.object(monitor.subprocess, 'run', return_value=child), patch.object(monitor.time, 'monotonic', return_value=100):
            result = monitor.sample('full', 20)
        self.assertFalse(result['ok'])
        self.assertEqual(result['kind'], 'temporary')

    def test_background_network_checks_do_not_require_browser_debugging(self):
        options = runtime_policy(resolve([], environ={}))
        config = b'{"mode":"global"}'
        (self.root / 'config.json').write_bytes(config)
        (self.root / 'monitor-private.json').write_text(json.dumps({
            'config_digest': hashlib.sha256(config).hexdigest()}))
        network = types.ModuleType('caged.runtime.checks.policy')
        network.snapshot = lambda: {'ipv4_ipv6_default_deny': True}
        probe = types.ModuleType('caged.runtime.checks.network')
        probe.dns_checks = lambda: {'local_udp_tcp': 'passed'}
        probe.trace = lambda: {'ip': '203.0.113.2', 'country': 'SG'}
        # A browser import would fail here. CLI network evidence must still
        # authorize a round, while a broken firewall must still be rejected.
        with patch.dict(sys.modules, {'caged.runtime.checks.browser': None, 'caged.runtime.checks.policy': network, 'caged.runtime.checks.network': probe}), \
                patch.object(monitor, 'policy', return_value=options), \
                patch.object(monitor, 'check_agents', return_value={'browser': {}, 'claude': {}}):
            result = monitor.check('full')
            self.assertEqual(result['dns']['local_udp_tcp'], 'passed')
            self.assertEqual(result['exit']['country'], 'SG')
            network.snapshot = lambda: (_ for _ in ()).throw(RuntimeError('Firewall changed'))
            with self.assertRaises(monitor.PolicyViolation):
                monitor.check('critical')

    def test_signed_browser_policy_configuration_is_checked_without_a_page(self):
        options = runtime_policy(resolve([], environ={}))
        private = {'session': 'fixture-session', 'agents': {'browser': '01' * 32, 'claude': '02' * 32}}
        nonce = 'a' * 64
        actual = {'username': monitor.APP_USER, 'uid': options['puid'], 'gid': options['pgid'],
                  'user_env': monitor.APP_USER, 'logname': monitor.APP_USER,
                  'timezone': options['timezone'], 'system_timezone': options['timezone'],
                  'localtime_matches': True,
                  'offset': monitor.datetime.now(monitor.ZoneInfo(options['timezone'])).strftime('%z'),
                  'locale_valid': True, 'locale': options['locale'], 'lc_all': options['locale'],
                  'language': options['language'].replace('-', '_'), 'resolvers': ['127.0.0.1'],
                  'proxy_env_empty': True, 'dns_policies': monitor.BROWSER_DNS_POLICIES.copy()}
        class Response:
            def __init__(self, value): self.value = value
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self, *_): return json.dumps(self.value).encode()
        def response(url, **_):
            role = 'browser' if ':' + str(options['browser_probe_port']) + '/' in url else 'claude'
            value = {'role': role, 'session': private['session'], 'nonce': nonce,
                     'policy_digest': monitor.digest(options), 'observation': actual}
            value['signature'] = hmac.new(bytes.fromhex(private['agents'][role]), monitor.encoded(value), hashlib.sha256).hexdigest()
            return Response(value)
        with patch.object(monitor.secrets, 'token_hex', return_value=nonce), \
                patch.object(monitor.urllib.request, 'build_opener') as opener:
            opener.return_value.open.side_effect = response
            self.assertEqual(set(monitor.check_agents(private, options)), {'browser', 'claude'})
            for invalid in ({**monitor.BROWSER_DNS_POLICIES, 'DnsOverHttpsMode': 'automatic'},
                            {**monitor.BROWSER_DNS_POLICIES, 'BuiltInDnsClientEnabled': 0}):
                actual['dns_policies'] = invalid
                with self.assertRaises(monitor.PolicyViolation):
                    monitor.check_agents(private, options)


if __name__ == '__main__':
    unittest.main(verbosity=2)
