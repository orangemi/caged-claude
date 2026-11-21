"""Host YAML adapter and public IPv4 pinning for a selected node."""
import copy
import ipaddress
import json
import socket
from . import docker as engine
from .errors import CageError
from .paths import YAML_READER


def load_node(path, name=None):
    result = engine.call(['ruby', str(YAML_READER), str(path),
                   'node' if name else 'list', *([name] if name else [])])
    return json.loads(result.stdout)


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
