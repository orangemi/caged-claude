#!/usr/bin/env python3
"""Observe Claude's actual non-root environment without reading any credentials."""
import json
import os
import pwd
from pathlib import Path
import subprocess
import tempfile
import time
from caged.shared.policy import policy, APP_USER
from caged.runtime.checks.network import dns_checks, trace


def observe(settings_only=False):
    if not Path('/run/caged-claude-ready').is_file():
        raise RuntimeError('Claude environment is still initializing')
    options = policy()
    state = Path(os.environ['CLAUDE_CONFIG_DIR'])
    version = subprocess.run(['claude', '--version'], text=True, capture_output=True, timeout=20)
    if version.returncode:
        raise RuntimeError('Claude Code is not installed correctly')
    if (os.environ.get('TZ') != options['timezone'] or os.environ.get('LANG') != options['locale']
            or os.environ.get('LC_ALL') != options['locale']
            or os.environ.get('LANGUAGE') != options['language'].replace('-', '_')
            or not os.path.samefile('/etc/localtime', '/usr/share/zoneinfo/' + options['timezone'])
            or Path('/etc/timezone').read_text().strip() != options['timezone']):
        raise RuntimeError('Claude timezone/locale differs from the shared browser policy')
    if os.getuid() != int(os.environ['PUID']) or os.getgid() != int(os.environ['PGID']):
        raise RuntimeError('Claude UID/GID differs from browser UID/GID')
    account = pwd.getpwnam(APP_USER)
    if account.pw_uid != os.getuid() or account.pw_gid != os.getgid():
        raise RuntimeError('Claude runtime identity differs from its fixed account')
    if state != Path('/config/.claude') or Path.home() != Path('/config'):
        raise RuntimeError('Claude state is outside its configured persistent home')
    with tempfile.TemporaryFile(dir=state) as check:
        check.write(b'caged-state-write-check')
    dns = None if settings_only else dns_checks()
    return {'version': version.stdout.strip(), 'username': account.pw_name, 'uid': os.getuid(), 'gid': os.getgid(),
            'home': str(Path.home()), 'state': str(state), 'state_writable': True,
            'timezone': options['timezone'], 'locale': options['locale'], 'language': options['language'],
            'offset': time.strftime('%z'), 'dns': dns, 'tcp': None if settings_only else trace()}


if __name__ == '__main__':
    try:
        import sys
        print(json.dumps(observe('--settings-only' in sys.argv)))
    except Exception as error:
        print(json.dumps({'error': str(error)}))
        raise SystemExit(1)
