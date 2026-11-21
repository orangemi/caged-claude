#!/usr/bin/env python3
"""Privileged policy evidence, without returning node credentials or API secrets."""
import json
import re
import subprocess
import socket
from pathlib import Path
from caged.runtime.network.mihomo import api, NODE
from caged.shared.policy import policy


def snapshot():
    root = Path('/run/caged')
    config = json.loads((root / 'config.json').read_text())
    secret = (root / 'controller-secret').read_text()
    runtime = api(secret, '/configs')
    selected = api(secret, '/proxies/GLOBAL')
    if runtime['mode'] != 'global' or selected['now'] != NODE:
        raise RuntimeError('Runtime proxy selection is no longer the fixed global node')
    if config['dns']['nameserver'] != [policy()['dns_upstream'] + '#' + NODE]:
        raise RuntimeError('DNS upstream is not explicitly pinned to the selected node')
    with socket.create_connection(('127.0.0.1', policy()['mixed_port']), timeout=2) as proxy:
        proxy.sendall(b'CONNECT 1.1.1.1:443 HTTP/1.1\r\nHost: 1.1.1.1:443\r\n\r\n')
        if proxy.recv(128).split()[:2] != [b'HTTP/1.1', b'407']:
            raise RuntimeError('Unauthenticated mixed-proxy requests are not rejected')
    saved = subprocess.check_output(['iptables-save', '-c'], text=True)
    # Check the entire installed filter, ignoring only packet counters.
    actual = re.sub(r'\[\d+:\d+\]', '[0:0]', saved)
    if any(row not in actual for row in (':INPUT DROP [0:0]', ':FORWARD DROP [0:0]', ':OUTPUT DROP [0:0]')):
        raise RuntimeError('Default-deny IPv4 policy is missing')
    counters = {}
    for chain in ('CAGED_DNS', 'CAGED_PRIVATE', 'CAGED_DIRECT'):
        match = re.search(r'^\[(\d+):\d+\] -A ' + chain + r' -j DROP$', saved, re.M)
        if not match:
            raise RuntimeError('Required deny chain is missing: ' + chain)
        counters[chain] = int(match[1])
    # Compare with the canonical rules saved immediately after installation.
    template = (root / 'firewall.v4').read_text()
    current = subprocess.check_output(['iptables-save'], text=True)
    canonical = lambda value: [re.sub(r'\[\d+:\d+\]', '[0:0]', row)
                                for row in value.splitlines() if row.startswith((':', '-A'))]
    if canonical(current) != canonical(template):
        raise RuntimeError('IPv4 firewall changed after installation')
    v6 = subprocess.check_output(['ip6tables-save'], text=True)
    if canonical(v6) != canonical((root / 'firewall.v6').read_text()):
        raise RuntimeError('IPv6 firewall changed after installation')
    return {'global_node': True, 'doh_via_node': True, 'ipv4_ipv6_default_deny': True,
            'counters': counters}


if __name__ == '__main__':
    try:
        print(json.dumps(snapshot()))
    except Exception as error:
        print(json.dumps({'error': str(error)}))
        raise SystemExit(1)
