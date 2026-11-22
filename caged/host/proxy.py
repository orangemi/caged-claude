"""Host YAML adapter and public IPv4 pinning for a selected node."""
import copy
import ipaddress
import json
import socket
from . import yamlfile
from .errors import CageError


def load_node(path, name=None):
    """List the file's proxies, or return the one named node after validating it."""
    unreadable = 'Cannot read the YAML configuration. No configuration contents were printed.'
    try:
        config = json.loads(json.dumps(yamlfile.load(path)))
    except yamlfile.YamlError as error:
        raise CageError(unreadable if str(error) == 'unreadable' else str(error)) from None
    except (TypeError, ValueError):
        raise CageError(unreadable) from None
    if not isinstance(config, dict):
        raise CageError('Configuration must be a YAML mapping.')
    nodes = [node for node in config.get('proxies') or [] if isinstance(node, dict)]
    if name is None:
        return [{'name': node.get('name'), 'type': node.get('type'), 'udp': node.get('udp')} for node in nodes]
    node = next((node for node in nodes if node.get('name') == name), None)
    if node is None:
        raise CageError('Selected node was not found in the proxy configuration.')
    if node.get('type') != 'anytls':
        raise CageError('This first version supports standalone AnyTLS nodes only.')
    if node.get('dialer-proxy'):
        raise CageError('A chained node must be expanded before it can be used independently.')
    return node


def pin_node(node):
    proxy = copy.deepcopy(node)
    server = str(proxy['server'])
    try:
        address = ipaddress.ip_address(server)
        addresses = [str(address)] if address.version == 4 else []
    except ValueError:
        addresses = sorted({row[4][0] for row in socket.getaddrinfo(
            server, int(proxy['port']), socket.AF_INET, socket.SOCK_STREAM)})
        proxy.setdefault('sni', server)
    addresses = [address for address in addresses if ipaddress.ip_address(address).is_global]
    if not addresses:
        raise CageError('The node did not resolve to a public IPv4 address; refusing to use a fake-IP or local address.')
    proxy['server'] = addresses[0]
    return proxy
