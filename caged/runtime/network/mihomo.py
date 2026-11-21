"""Generate the private proxy config and access the authenticated controller."""
import json
import urllib.request
from urllib.parse import urlsplit
from caged.shared.policy import policy
from .lease import CORE_MARK

NODE = "CAGED_NODE"
TUN = "caged0"


def make_config(proxy, secret):
    options = policy()
    return {
        'mode': 'global',
        'log-level': 'warning',
        'ipv6': False,
        'find-process-mode': 'off',
        'geo-auto-update': False,
        'interface-name': 'eth0',
        'routing-mark': CORE_MARK,
        'external-controller': f'127.0.0.1:{options["controller_port"]}',
        'secret': secret,
        'mixed-port': options['mixed_port'],
        'authentication': ['caged:' + secret],
        'skip-auth-prefixes': [],
        'bind-address': '127.0.0.1',
        'allow-lan': False,
        'profile': {'store-selected': False, 'store-fake-ip': False},
        'proxies': [{**proxy, 'name': NODE}],
        'proxy-groups': [],
        'rules': [],
        'dns': {
            'enable': True,
            'listen': '127.0.0.1:53',
            'ipv6': False,
            'enhanced-mode': 'fake-ip',
            'fake-ip-range': '198.18.0.1/16',
            'fake-ip-filter': [],
            'use-hosts': False,
            'use-system-hosts': False,
            'nameserver': [options['dns_upstream'] + '#' + NODE],
            'default-nameserver': [urlsplit(options['dns_upstream']).hostname],
        },
        'tun': {
            'enable': True,
            'device': TUN,
            'stack': 'gvisor',
            'auto-route': True,
            'auto-redirect': False,
            'auto-detect-interface': False,
            'strict-route': True,
            'dns-hijack': ['any:53', 'tcp://any:53'],
            'mtu': 1500,
        },
    }


def api(secret, path, data=None, method=None):
    request = urllib.request.Request(
        f'http://127.0.0.1:{policy()["controller_port"]}' + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={'Authorization': 'Bearer ' + secret, 'Content-Type': 'application/json'},
        method=method or ('PUT' if data is not None else 'GET'),
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=2) as response:
        raw = response.read()
        return json.loads(raw) if raw else None
