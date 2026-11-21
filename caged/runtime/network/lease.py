"""Kernel-owned, expiring permits. No monitor process is needed for expiry."""
import json
from pathlib import Path
import subprocess
import time
import fcntl
from contextlib import contextmanager

ROOT = Path('/run/caged')
LEASE = 'caged_permit'
PROBE_MARK = 0x434101
CORE_MARK = 0x434102


@contextmanager
def control_lock():
    with (ROOT / 'control.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def run(*args, data=None):
    result = subprocess.run(args, input=data, capture_output=True, text=True, timeout=5)
    if result.returncode:
        raise RuntimeError('Kernel network permit operation failed: ' + args[0])
    return result.stdout


def initialize():
    run('ipset', 'create', LEASE, 'hash:net', 'family', 'inet', 'timeout', '90', '-exist')
    revoke()


def renew(seconds):
    with control_lock():
        if any((ROOT / name).exists() for name in ('manual-pause', 'resume-request', 'violation')):
            return False
        _renew(seconds)
        return True


def _renew(seconds):
    seconds = int(seconds)
    if not 1 <= seconds <= 3600:
        raise ValueError('Invalid permit lifetime')
    # Two /1 prefixes cover all IPv4 sources. Swap both halves atomically.
    temporary = LEASE + '_next'
    run('ipset', 'create', temporary, 'hash:net', 'family', 'inet', 'timeout', str(seconds), '-exist')
    run('ipset', 'flush', temporary)
    for prefix in ('0.0.0.0/1', '128.0.0.0/1'):
        run('ipset', 'add', temporary, prefix, 'timeout', str(seconds))
    run('ipset', 'swap', temporary, LEASE)
    run('ipset', 'destroy', temporary)


def revoke():
    with control_lock():
        run('ipset', 'flush', LEASE)


def remaining():
    entries = [row.split() for row in run('ipset', 'save', LEASE).splitlines() if row.startswith('add ')]
    if len(entries) != 2:
        return 0
    return min(int(row[row.index('timeout') + 1]) for row in entries)


def write_state(value):
    temporary = ROOT / 'monitor.json.tmp'
    temporary.write_text(json.dumps(value))
    temporary.replace(ROOT / 'monitor.json')


def status():
    try:
        value = json.loads((ROOT / 'monitor.json').read_text())
    except (OSError, ValueError):
        value = {'state': 'starting', 'reason': 'Waiting for runtime checks'}
    value['lease_remaining'] = remaining()
    if (ROOT / 'manual-pause').exists():
        value.update(state='paused', reason='Manually paused')
    if value.get('state') == 'open' and not value['lease_remaining']:
        value.update(state='closed', reason='Monitoring permit expired')
    value['observed_at'] = time.time()
    return value


if __name__ == '__main__':
    import sys
    action = sys.argv[1] if len(sys.argv) > 1 else 'status'
    if action == 'status':
        print(json.dumps(status()))
    elif action in ('pause', 'resume'):
        # Only docker exec/root can write here; no network control API exists.
        with control_lock():
            target = ROOT / 'manual-pause'
            if action == 'pause':
                target.write_text('paused')
            else:
                target.unlink(missing_ok=True)
                (ROOT / 'resume-request').write_text('recheck')
            run('ipset', 'flush', LEASE)
        print(action + ' requested; external traffic remains blocked until checks pass')
    else:
        raise SystemExit('Unsupported gate operation')
