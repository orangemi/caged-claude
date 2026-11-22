"""Observe clients, renew kernel permits, and recover without restarting them."""
import hashlib
import hmac
import json
import secrets
import subprocess
import sys
import time
import urllib.request
import math
from zoneinfo import ZoneInfo
from datetime import datetime

from caged.runtime.network import lease as gate
from caged.shared.protocol import encoded, digest
from caged.shared.policy import policy, PolicyViolation, APP_USER, BROWSER_DNS_POLICIES


def check_agents(private, options):
    result = {}
    for role in ('browser', 'claude'):
        key = private['agents'].get(role)
        if not key:
            raise RuntimeError('Application probes have not been enrolled')
        nonce = secrets.token_hex(32)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f'http://127.0.0.1:{options[role + "_probe_port"]}/report?nonce={nonce}', timeout=3) as response:
            value = json.loads(response.read(16384))
        signature = value.pop('signature', '')
        expected = hmac.new(bytes.fromhex(key), encoded(value), hashlib.sha256).hexdigest()
        if (not hmac.compare_digest(signature, expected) or value.get('nonce') != nonce
                or value.get('session') != private['session'] or value.get('role') != role
                or value.get('policy_digest') != digest(options)):
            raise PolicyViolation('Application probe identity or policy changed: ' + role)
        if value.get('error'):
            raise RuntimeError('Application environment cannot be observed: ' + role)
        actual = value['observation']
        if role == 'browser' and encoded(actual.get('dns_policies')) != encoded(BROWSER_DNS_POLICIES):
            raise PolicyViolation('Browser managed DNS configuration changed')
        expected_offset = datetime.now(ZoneInfo(options['timezone'])).strftime('%z')
        if (actual['username'] != APP_USER or actual['uid'] != options['puid'] or actual['gid'] != options['pgid']
                or actual['user_env'] != APP_USER or actual['logname'] != APP_USER
                or actual['timezone'] != options['timezone'] or actual['system_timezone'] != options['timezone']
                or not actual['localtime_matches']
                or actual['offset'] != expected_offset or not actual['locale_valid']
                or actual['locale'] != options['locale'] or actual['lc_all'] != options['locale']
                or actual['language'] != options['language'].replace('-', '_')
                or actual['resolvers'] != ['127.0.0.1'] or not actual['proxy_env_empty']):
            raise PolicyViolation('Application identity, timezone, language or DNS changed: ' + role)
        result[role] = actual
    return result


def check(stage):
    from caged.runtime.checks.policy import snapshot
    options = policy()
    private = json.loads((gate.ROOT / 'monitor-private.json').read_text())
    if hashlib.sha256((gate.ROOT / 'config.json').read_bytes()).hexdigest() != private['config_digest']:
        raise PolicyViolation('The running gateway configuration changed; restart with the intended settings')
    try:
        snapshot()
    except RuntimeError as error:
        raise PolicyViolation(str(error)) from None
    clients = check_agents(private, options)
    result = {'clients': list(clients)}
    if stage == 'full':
        from caged.runtime.checks.network import dns_checks, trace
        result.update(dns=dns_checks(), exit=trace())
    return result


def worker(stage):
    try:
        value = {'ok': True, 'evidence': check(stage)}
    except PolicyViolation as error:
        value = {'ok': False, 'kind': 'violation', 'reason': str(error)}
    except Exception as error:
        # Dependencies may include private controller headers. Publish only a
        # fixed diagnostic, never the raw exception or response body.
        value = {'ok': False, 'kind': 'temporary', 'reason': 'Required probe evidence is unavailable (' + type(error).__name__ + ')'}
    value['completed_monotonic'] = time.monotonic()
    print(json.dumps(value))


def sample(stage, timeout):
    try:
        child = subprocess.run([sys.executable, '-m', 'caged.runtime.network.monitor', '--check', stage],
                               capture_output=True, text=True, timeout=timeout)
        if child.returncode:
            return {'ok': False, 'kind': 'temporary', 'reason': 'Probe process failed'}
        value = json.loads(child.stdout)
        age = time.monotonic() - value['completed_monotonic']
        if age < 0 or age > timeout:
            return {'ok': False, 'kind': 'temporary', 'reason': 'Probe evidence expired while supervision was delayed'}
        value['age'] = age
        return value
    except (subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        return {'ok': False, 'kind': 'temporary', 'reason': 'Probe timed out'}


class Monitor:
    def __init__(self, options, clock=time.monotonic, wall=time.time):
        self.options, self.clock, self.wall = options, clock, wall
        self.state = 'checking'
        self.reason = 'Waiting for both application environments and network evidence'
        self.last_success = None
        self.next_check = 0
        self.attempts = 0
        self.evidence = None
        self.started = clock()
        self.ever_opened = False
        self.last_published = None
        # A confirmed violation remains latched across a monitor crash.
        if (gate.ROOT / 'violation').exists():
            self.state, self.reason = 'blocked', 'Confirmed policy violation; explicit ./cage resume is required'

    def close(self, state, reason, recover=False):
        gate.revoke()
        self.state, self.reason = state, reason
        if state == 'blocked':
            (gate.ROOT / 'violation').write_text('blocked')
        if recover:
            self.next_check = self.clock() + self.options['monitor_retry_initial']
        try:
            from caged.runtime.network.mihomo import api
            api((gate.ROOT / 'controller-secret').read_text(), '/connections', method='DELETE')
        except Exception:
            pass  # Kernel revocation already blocks established traffic.

    def publish(self):
        gate.write_state({'state': self.state, 'reason': self.reason, 'last_success': self.last_success,
                          'next_retry': self.wall() + max(0, self.next_check - self.clock())
                                        if self.state in ('closed', 'checking') else None,
                          'exit': (self.evidence or {}).get('exit'), 'attempts': self.attempts})
        public = (self.state, self.reason)
        if public != self.last_published:
            print(json.dumps({'network': self.state, 'reason': self.reason}), flush=True)
            self.last_published = public

    def tick(self, run=sample):
        now = self.clock()
        if (gate.ROOT / 'manual-pause').exists():
            if self.state != 'paused':
                self.close('paused', 'Manually paused; explicit ./cage resume is required')
            self.publish()
            return
        resume = gate.ROOT / 'resume-request'
        if resume.exists():
            resume.unlink()
            (gate.ROOT / 'violation').unlink(missing_ok=True)
            self.close('checking', 'Explicit resume requested; collecting fresh evidence')
            self.next_check, self.attempts = 0, 0
        if self.state in ('paused', 'blocked'):
            self.publish()
            return
        if self.state == 'open' and not gate.remaining():
            self.close('closed', 'Monitoring permit expired', recover=True)
        if self.state == 'open':
            critical = run('critical', self.options['monitor_probe_timeout'])
            if not critical['ok']:
                self.close('blocked' if critical['kind'] == 'violation' else 'closed', critical['reason'],
                           recover=critical['kind'] != 'violation')
        if self.state in ('open', 'checking', 'closed') and now >= self.next_check:
            result = run('full', self.options['monitor_probe_timeout'])
            # Control changes during a slow probe always win over its result.
            if (gate.ROOT / 'manual-pause').exists() or (gate.ROOT / 'resume-request').exists():
                self.publish()
                return
            if result['ok']:
                lifetime = self.options['monitor_lease'] - math.ceil(result.get('age', 0))
                if lifetime <= 0 or not gate.renew(lifetime):
                    self.publish()
                    return
                self.state, self.reason = 'open', 'Current environment, DNS and proxy exit verified'
                self.last_success, self.evidence = self.wall(), result['evidence']
                self.next_check = self.clock() + self.options['monitor_interval']
                self.attempts, self.ever_opened = 0, True
            elif result['kind'] == 'violation':
                self.close('blocked', result['reason'])
            elif self.state == 'open' and gate.remaining():
                self.reason = result['reason'] + '; current permit was not extended'
                self.next_check = self.clock() + self.options['monitor_tick']
            else:
                self.state, self.reason = 'closed', result['reason']
                # Startup enrollment is quick; recovery of a previously
                # authorized runtime uses exponential backoff.
                delay = self.options['monitor_tick'] if not self.ever_opened else min(
                    self.options['monitor_retry_max'], self.options['monitor_retry_initial'] * 2 ** min(self.attempts, 10))
                self.next_check = self.clock() + delay
                self.attempts += 1
        self.publish()


def main():
    options = policy()
    monitor = Monitor(options)
    while True:
        monitor.tick()
        time.sleep(options['monitor_tick'])


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--check':
        worker(sys.argv[2])
    else:
        main()
