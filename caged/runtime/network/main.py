#!/usr/bin/env python3
"""A private network namespace: global mihomo TUN plus an independent kill switch."""
import ipaddress
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time
import argparse
from caged.shared.policy import policy
from .lease import PROBE_MARK, initialize
from .mihomo import NODE, TUN, api, make_config
from .firewall import command, firewall_rules

ROOT = Path('/run/caged')
STOP = False


def stop_requested(_signum, _frame):
    global STOP
    STOP = True


def main():
    os.umask(0o077)
    options = policy()
    ROOT.mkdir(parents=True, exist_ok=True)
    parser = argparse.ArgumentParser()
    parser.add_argument('--bootstrap-file')
    arguments = parser.parse_args()
    if arguments.bootstrap_file:
        bootstrap = Path(arguments.bootstrap_file)
        deadline = time.monotonic() + 25
        while not bootstrap.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        payload = json.loads(bootstrap.read_text())
        bootstrap.unlink()
    else:
        payload = json.loads(sys.stdin.readline(1024 * 1024))
    proxy = payload['proxy']
    address = str(ipaddress.IPv4Address(proxy['server']))
    if proxy.get('type') != 'anytls':
        raise RuntimeError('Unsupported node transport')
    # Establish the deny policy before starting anything that can access the network.
    initialize()
    (ROOT / 'probe-mark').write_text(str(PROBE_MARK))
    command(['iptables-restore', '-w', '5'], data=firewall_rules(address, proxy['port']))
    command(['ip6tables-restore', '-w', '5'], data='''*filter
:INPUT DROP [0:0]
:FORWARD DROP [0:0]
:OUTPUT DROP [0:0]
-A INPUT -i lo -j ACCEPT
-A OUTPUT -o lo -j ACCEPT
COMMIT
''')
    for family in ('v4', 'v6'):
        (ROOT / ('firewall.' + family)).write_text(command(
            ['iptables-save' if family == 'v4' else 'ip6tables-save']))
    secret = secrets.token_urlsafe(32)
    config = make_config(proxy, secret)
    config_file = ROOT / 'config.json'
    config_file.write_text(json.dumps(config))
    (ROOT / 'controller-secret').write_text(secret)
    import hashlib
    (ROOT / 'monitor-private.json').write_text(json.dumps({
        'session': payload.get('session', ''), 'agents': payload.get('agents', {}),
        'config_digest': hashlib.sha256(config_file.read_bytes()).hexdigest(),
    }))
    log = (ROOT / 'mihomo.log').open('w')
    validation = subprocess.run(
        ['/usr/local/bin/mihomo', '-t', '-d', str(ROOT), '-f', str(config_file)],
        stdout=log, stderr=subprocess.STDOUT,
    )
    if validation.returncode:
        raise RuntimeError('mihomo rejected the temporary configuration')
    core = subprocess.Popen(
        ['/usr/local/bin/mihomo', '-d', str(ROOT), '-f', str(config_file)],
        stdout=log, stderr=subprocess.STDOUT,
    )
    (ROOT / 'core.pid').write_text(str(core.pid))
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, stop_requested)
    ready = False
    monitor = None
    try:
        deadline = time.monotonic() + 40
        while not STOP and core.poll() is None and time.monotonic() < deadline:
            try:
                api(secret, '/proxies/GLOBAL', {'name': NODE})
                selected = api(secret, '/proxies/GLOBAL')
                runtime = api(secret, '/configs')
                tun_ready = Path('/sys/class/net/' + TUN).exists()
                if selected['now'] == NODE and runtime['mode'] == 'global' and tun_ready:
                    ready = True
                    break
            except Exception:
                pass
            time.sleep(0.2)
        if not ready:
            raise RuntimeError('Gateway did not become ready; browser was not started')
        (ROOT / 'ready.json').write_text(json.dumps({
            'mode': 'global', 'node': payload['display_name'],
            'dns': '127.0.0.1', 'ipv6': 'blocked', 'kill_switch': True,
            'policy': options,
        }))
        print('Gateway ready: global mode; independent IPv4/IPv6 firewall installed.', flush=True)
        monitor = subprocess.Popen([sys.executable, '-u', '-m', 'caged.runtime.network.monitor'])
        (ROOT / 'monitor.pid').write_text(str(monitor.pid))
        while not STOP and core.poll() is None:
            if monitor.poll() is not None:
                from .lease import revoke
                revoke()
                monitor = subprocess.Popen([sys.executable, '-u', '-m', 'caged.runtime.network.monitor'])
                (ROOT / 'monitor.pid').write_text(str(monitor.pid))
            time.sleep(0.2)
    finally:
        (ROOT / 'ready.json').unlink(missing_ok=True)
        if monitor is not None:
            monitor.terminate()
            try:
                monitor.wait(timeout=3)
            except subprocess.TimeoutExpired:
                monitor.kill()
                monitor.wait()
        if core.poll() is None:
            core.terminate()
            try:
                core.wait(timeout=8)
            except subprocess.TimeoutExpired:
                core.kill()
                core.wait()
        log.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'Gateway stopped: {error}', file=sys.stderr)
        sys.exit(1)
