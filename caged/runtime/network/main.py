#!/usr/bin/env python3
"""A private network namespace: global mihomo TUN plus an independent kill switch."""
import ipaddress
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import argparse
from zoneinfo import ZoneInfo
from caged.shared import protocols
from caged.shared.policy import POLICY_ENV, policy
from .lease import PROBE_MARK, initialize
from .mihomo import NODE, TUN, api, make_config
from .firewall import command, firewall_rules

ROOT = Path('/run/caged')
STOP = False


def stop_requested(_signum, _frame):
    global STOP
    STOP = True


def forward_to_unix(path):
    """Serve 127.0.0.1:<free port> by relaying each connection to a Unix socket."""
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(16)

    def relay(source, target):
        try:
            while data := source.recv(65536):
                target.sendall(data)
            target.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    def serve(client):
        with client, socket.socket(socket.AF_UNIX) as upstream:
            try:
                upstream.connect(path)
            except OSError:
                return
            sending = threading.Thread(target=relay, args=(client, upstream), daemon=True)
            sending.start()
            relay(upstream, client)
            sending.join()

    def accept():
        while True:
            client, _ = listener.accept()
            threading.Thread(target=serve, args=(client,), daemon=True).start()
    threading.Thread(target=accept, daemon=True).start()
    return listener.getsockname()[1]


def chosen_timezone(options):
    """The timezone the launcher wrote after comparing it with the measured exit, if any yet."""
    try:
        name = (ROOT / 'timezone').read_text().strip()
    except OSError:
        return None
    ZoneInfo(name)
    if options['timezone'] not in (None, name):
        raise RuntimeError('The launcher chose a timezone other than the configured one')
    return name


def write_ready(value):
    temporary = ROOT / 'ready.json.tmp'
    temporary.write_text(json.dumps(value))
    temporary.replace(ROOT / 'ready.json')


def start_monitor(options):
    environment = {**os.environ, POLICY_ENV: json.dumps(options)}
    monitor = subprocess.Popen([sys.executable, '-u', '-m', 'caged.runtime.network.monitor'], env=environment)
    (ROOT / 'monitor.pid').write_text(str(monitor.pid))
    return monitor


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
    route = payload.get('dialer') or {'kind': 'direct'}
    if route['kind'] == 'unix':
        # The host dialer's socket: mihomo dials a loopback forwarder, nothing leaves eth0.
        addresses = []
        proxy = {**proxy, 'port': forward_to_unix(route['socket'])}
    else:
        addresses = [str(ipaddress.IPv4Address(value)) for value in payload.get('addresses') or [proxy['server']]]
        if not 1 <= len(addresses) <= 4 or proxy['server'] not in addresses:
            raise RuntimeError('Invalid pinned node addresses')
    if protocols.problem(proxy):
        raise RuntimeError('Unsupported node transport')
    # Establish the deny policy before starting anything that can access the network.
    initialize()
    (ROOT / 'probe-mark').write_text(str(PROBE_MARK))
    command(['iptables-restore', '-w', '5'],
            data=firewall_rules(addresses, proxy['port'], options.get('host_bridge_tcp'), options['browser_ui_port']))
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
    # Behind the dialer there is one endpoint; the dialer itself fails over between addresses.
    config = make_config(proxy, secret, addresses if route['kind'] == 'direct' else None)
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
        ready_state = {
            'mode': 'global', 'node': payload['display_name'],
            'dns': '127.0.0.1', 'ipv6': 'blocked', 'kill_switch': True,
            'policy': options, 'pinned': payload.get('pinned') or {'host': None, 'addresses': addresses},
        }
        write_ready(ready_state)
        print('Gateway ready: global mode; independent IPv4/IPv6 firewall installed.', flush=True)
        # Nothing is permitted out before the monitor runs, and it runs only with
        # the timezone the launcher settled from the measured exit (zones.py).
        while not STOP and core.poll() is None:
            if monitor is None:
                timezone = chosen_timezone(options)
                if timezone:
                    options = {**options, 'timezone': timezone}
                    write_ready({**ready_state, 'policy': options})
                    monitor = start_monitor(options)
            elif monitor.poll() is not None:
                from .lease import revoke
                revoke()
                monitor = start_monitor(options)
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
