"""Generate the private proxy config and access the authenticated controller."""
import json
import urllib.request
from urllib.parse import urlsplit
from caged.shared.policy import policy
from .lease import CORE_MARK

NODE = "CAGED_NODE"
TUN = "caged0"
# Only used to choose among the node's own pinned addresses, through the node.
HEALTH_URL = 'https://www.gstatic.com/generate_204'


def node_proxies(proxy, addresses=None):
    """One proxy named NODE, or a fallback group NODE over one proxy per pinned address."""
    if not addresses or len(addresses) == 1:
        return [{**proxy, 'name': NODE}], []
    members = [{**proxy, 'server': address, 'name': f'{NODE}-{index}'} for index, address in enumerate(addresses, 1)]
    group = {'name': NODE, 'type': 'fallback', 'proxies': [member['name'] for member in members],
             'url': HEALTH_URL, 'interval': 300, 'timeout': 5000, 'lazy': False}
    return members, [group]


def make_config(proxy, secret, addresses=None):
    options = policy()
    upstreams = options['dns_upstream']
    upstreams = [upstreams] if isinstance(upstreams, str) else upstreams
    proxies, groups = node_proxies(proxy, addresses)
    config = {
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
        'proxies': proxies,
        'proxy-groups': groups,
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
            # mihomo queries every upstream at once and keeps the first usable
            # answer; SERVFAIL from one endpoint falls through to the others.
            'nameserver': [upstream + '#' + NODE for upstream in upstreams],
            'default-nameserver': list(dict.fromkeys(urlsplit(upstream).hostname for upstream in upstreams)),
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
    host = options.get('host_bridge_tcp')
    if host:
        # The host bridge is reached directly through eth0, never via the node.
        config['tun']['route-exclude-address'] = [f'{host[0]}/32']
    return config


def api(secret, path, data=None, method=None, timeout=2):
    request = urllib.request.Request(
        f'http://127.0.0.1:{policy()["controller_port"]}' + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={'Authorization': 'Bearer ' + secret, 'Content-Type': 'application/json'},
        method=method or ('PUT' if data is not None else 'GET'),
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        raw = response.read()
        return json.loads(raw) if raw else None
