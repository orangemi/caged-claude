"""Node protocols the cage can carry (docs/configuration.md).

The gateway firewall allows only TCP to the pinned node address, and the host
dialer relays TCP after a TLS handshake with the node. So a node must speak
TLS over TCP; mihomo handles the protocol itself, end to end.
"""
# The field mihomo reads the TLS server name from, per supported type.
TLS_NAME = {'anytls': 'sni', 'trojan': 'sni', 'vless': 'servername', 'vmess': 'servername'}
# TLS is optional for these; the cage needs it on.
TLS_FLAG = {'vless', 'vmess'}
TRANSPORTS = ('tcp', 'ws', 'grpc')
UDP_TYPES = {'hysteria', 'hysteria2', 'tuic', 'wireguard'}


def check(node):
    """(short reason, message) when the cage cannot carry this node, else None."""
    kind = node.get('type')
    if kind in UDP_TYPES:
        return 'needs UDP to the node', (
            f'{kind} nodes need UDP to the node, but the gateway firewall and the host dialer carry only TCP. '
            'Choose a TCP node: ' + ', '.join(TLS_NAME) + '.')
    if kind not in TLS_NAME:
        return 'protocol not supported yet', (
            f'{kind} nodes are not supported yet; supported: ' + ', '.join(TLS_NAME) + ' over TLS.')
    if kind in TLS_FLAG and node.get('tls') is not True:
        return 'no TLS', (f'This {kind} node does not use TLS (tls: true, which Reality also needs); '
                          'the cage needs TLS to the node.')
    network = node.get('network') or 'tcp'
    if network not in TRANSPORTS:
        return f'{network} transport', (
            f'The {network} transport is not supported yet; supported: ' + ', '.join(TRANSPORTS) + '.')
    if node.get('dialer-proxy'):
        return 'chained node', 'A chained node must be expanded before it can be used independently.'
    return None


def problem(node):
    """Why the cage cannot carry this node, or None."""
    found = check(node)
    return found[1] if found else None


def tls_name(node):
    return node.get(TLS_NAME.get(node.get('type'), 'sni'))


def keep_name(node, host):
    """The node once its server becomes an address: TLS (and a WebSocket Host header)
    still name the original host. Explicit values are kept."""
    node = {**node}
    field = TLS_NAME.get(node.get('type'), 'sni')
    if not node.get(field):
        node[field] = host
    if node.get('network') == 'ws':
        options = {**(node.get('ws-opts') or {})}
        headers = {**(options.get('headers') or {})}
        if not any(key.lower() == 'host' for key in headers):
            headers['Host'] = host
        node['ws-opts'] = {**options, 'headers': headers}
    return node
