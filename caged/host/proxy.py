"""Host YAML adapter and public IPv4 pinning for a selected node."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import copy
from http.client import HTTPSConnection, HTTPException
import ipaddress
import json
import os
import platform
import re
import socket
import ssl
import subprocess
from urllib.parse import urlsplit
from . import yamlfile
from .errors import CageError
from .paths import IN_LAUNCHER

# The gateway firewall allows each pinned address; mihomo fails over between them.
MAX_ADDRESSES = 4
MAX_SOURCE_RESOLVERS = 4
RESOLVER_HINT = 'Check proxy.bootstrap_dns in app.yaml or choose other HTTPS DNS services with --bootstrap-dns.'
# Interfaces that carry a host's own public traffic without a TUN or VPN in between.
PHYSICAL = {'Darwin': r'en\d+', 'Linux': r'(?:en|eth|wl|ww|em|bond)\w*|br\d+'}


class ResolverError(CageError):
    """One resolver failed; its short reason is combined with the others."""


def load_config(path):
    unreadable = 'Cannot read the YAML configuration. No configuration contents were printed.'
    try:
        config = json.loads(json.dumps(yamlfile.load(path)))
    except yamlfile.YamlError as error:
        raise CageError(unreadable if str(error) == 'unreadable' else str(error)) from None
    except (TypeError, ValueError):
        raise CageError(unreadable) from None
    if not isinstance(config, dict):
        raise CageError('Configuration must be a YAML mapping.')
    return config


def load_node(path, name=None):
    """List the file's proxies, or return the one named node after validating it."""
    config = load_config(path)
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


def safe_resolver(url):
    """A remote HTTPS DoH URL: no credentials, #routing options, or local addresses."""
    try:
        endpoint = urlsplit(url)
        endpoint.port
    except ValueError:
        return False
    host = (endpoint.hostname or '').rstrip('.')
    if (endpoint.scheme != 'https' or not host or len(url) > 2048 or endpoint.port == 0
            or endpoint.username is not None or endpoint.password is not None or endpoint.fragment):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return '.' in host and not host.endswith(('.localhost', '.local', '.lan', '.internal'))
    return address.version == 4 and address.is_global and not address.is_multicast


def source_resolvers(path):
    """The node file's own dns.proxy-server-nameserver DoH entries, as mihomo would use them.

    Unsupported entries (plain DNS, system, local or private hosts, #skip-cert-verify
    and other # options) are ignored rather than blocking the start."""
    dns = load_config(path).get('dns')
    servers = dns.get('proxy-server-nameserver') if isinstance(dns, dict) else None
    result = []
    for item in servers if isinstance(servers, list) else []:
        item = item.strip() if isinstance(item, str) else ''
        if safe_resolver(item) and item not in result:
            result.append(item)
    return result[:MAX_SOURCE_RESOLVERS]


def dns_modules():
    try:
        from dns import exception, message, rcode, rdatatype
    except ImportError:
        raise CageError('Resolving a proxy hostname requires dnspython. Run ./cage normally or install dnspython for CAGED_NATIVE=1.') from None
    return exception, message, rcode, rdatatype


def make_query(server):
    exception, message, _, rdatatype = dns_modules()
    try:
        return message.make_query(server, rdatatype.A, id=0)
    except (exception.DNSException, UnicodeError):
        raise CageError('The selected proxy node has an invalid hostname. Check its server field in the proxy YAML.') from None


def doh_query(resolver, server, address=None):
    """A records for server from one DoH service; [] for NXDOMAIN.

    address connects to a resolver named by hostname; its certificate is still
    verified against that name."""
    exception, message, rcode, _ = dns_modules()
    query = make_query(server)
    endpoint = urlsplit(resolver)
    media_type = 'application/dns-message'
    try:
        # Certificate verification on, and no system proxy settings: http.client ignores them.
        connection = HTTPSConnection(endpoint.hostname, endpoint.port, timeout=5,
                                     context=ssl.create_default_context())
        if address is not None:
            connection._create_connection = lambda target, *rest: socket.create_connection((address, target[1]), *rest)
        with closing(connection):
            connection.request('POST', endpoint.path + ('?' + endpoint.query if endpoint.query else ''),
                               body=query.to_wire(), headers={'Content-Type': media_type, 'Accept': media_type})
            with connection.getresponse() as response:
                if response.status != 200:
                    raise ResolverError(f'returned HTTP {response.status}')
                if response.headers.get_content_type() != media_type:
                    raise ResolverError('returned an unexpected content type')
                wire = response.read(65536)
    except ssl.SSLCertVerificationError:
        raise ResolverError('could not verify its certificate') from None
    except (OSError, HTTPException):
        raise ResolverError('could not be reached') from None
    try:
        if len(wire) > 65535:
            raise exception.FormError()
        answer = message.from_wire(wire, raise_on_truncation=True)
        if not query.is_response(answer) or query.question != answer.question:
            raise exception.FormError()
        status = answer.rcode()
        if status == rcode.NXDOMAIN:
            return []
        if status != rcode.NOERROR:
            raise ResolverError(f'could not resolve the node ({rcode.to_text(status)})')
        records = answer.resolve_chaining().answer
    except exception.DNSException:
        raise ResolverError('returned an invalid reply') from None
    return [ipaddress.IPv4Address(record.address) for record in records] if records else []


def usable(address):
    return address.version == 4 and address.is_global and not address.is_multicast


def lookup(server, resolvers):
    """Ask every resolver at once; the first in order with a public IPv4 answer wins.

    Resolvers named by hostname are themselves found through the IP-literal ones."""
    literal = [item for item in resolvers if re.fullmatch(r'[\d.]+', urlsplit(item).hostname or '')]

    def attempt(resolver):
        host = urlsplit(resolver).hostname
        address = None
        if resolver not in literal:
            found = [found for found in first_answer(host, literal) if usable(found)] if literal else []
            if not found:
                raise ResolverError('could not be found through the IP-address resolvers')
            address = str(found[0])
        return doh_query(resolver, server, address)

    def first_answer(name, items):
        reasons = []
        for item in items:
            try:
                return doh_query(item, name)
            except ResolverError as error:
                reasons.append(error)
        raise reasons[0] if reasons else ResolverError('has no resolver')

    make_query(server)  # An invalid node hostname is a node error, not a resolver one.
    pool = ThreadPoolExecutor(max_workers=len(resolvers))
    futures = [pool.submit(attempt, resolver) for resolver in resolvers]
    pool.shutdown(wait=False)
    reasons, answered = [], False
    for resolver, future in zip(resolvers, futures):
        name = urlsplit(resolver).hostname  # paths may carry a personal resolver ID
        try:
            addresses = future.result()
        except ResolverError as error:
            reasons.append(f'{name}: {error}')
            continue
        answered = True
        public = sorted({address for address in addresses if usable(address)})
        if public:
            return public
        reasons.append(f'{name}: no public IPv4 address in the answer')
    detail = '; '.join(reasons)
    if answered:
        raise CageError(f'The selected proxy node has no usable public IPv4 address ({detail}). '
                        'Check its server field in the proxy YAML or select another node with --node.')
    raise CageError(f'Could not look up the proxy node through any HTTPS DNS service ({detail}). {RESOLVER_HINT}')


def pin_node(node, resolvers, source=()):
    """(node with a fixed public IPv4 server, every pinned address).

    The node file's own proxy-server-nameserver entries are tried before the
    configured bootstrap resolvers. A hostname node keeps its name as the SNI."""
    proxy = copy.deepcopy(node)
    server = str(proxy['server'])
    try:
        address = ipaddress.ip_address(server)
    except ValueError:
        addresses = lookup(server, list(dict.fromkeys([*source, *resolvers])))
        proxy.setdefault('sni', server)
    else:
        if not usable(address):
            raise CageError('The selected proxy node has no usable public IPv4 address. '
                            'Use a public IPv4 or a hostname in the server field of the proxy YAML.')
        addresses = [address]
    addresses = [str(address) for address in addresses[:MAX_ADDRESSES]]
    proxy['server'] = addresses[0]
    return proxy, addresses


def route_interface(address, system):
    command = ['route', '-n', 'get', address] if system == 'Darwin' else ['ip', 'route', 'get', address]
    try:
        output = subprocess.run(command, capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r'interface: (\S+)' if system == 'Darwin' else r'\bdev (\S+)', output)
    return match[1] if match else None


def host_routes(addresses, environ=os.environ):
    """{address: interface} this host uses for public addresses.

    ./cage samples the host before entering the launcher container; a native
    launcher also asks about the pinned node addresses themselves."""
    system = environ.get('CAGED_HOST_OS') or platform.system()
    routes = {}
    for item in environ.get('CAGED_HOST_ROUTES', '').split(','):
        address, _, interface = item.partition('=')
        if address.strip() and interface.strip():
            routes[address.strip()] = interface.strip()
    if not IN_LAUNCHER:
        for address in addresses:
            interface = route_interface(address, system)
            if interface:
                routes[address] = interface
    return system, routes


def check_host_routes(addresses, environ=os.environ):
    """{'routes', 'tunnels'}: public addresses this host sends through a TUN or VPN.

    Containers reach the node through the host's network, so a host TUN such as
    Clash/mihomo would carry the node connection instead of the physical network
    (see dialer.py for how that is avoided)."""
    system, routes = host_routes(addresses, environ)
    pattern = PHYSICAL.get(system)
    tunnels = {address: interface for address, interface in routes.items()
               if pattern and not re.fullmatch(pattern, interface)}
    return {'routes': routes, 'tunnels': tunnels}
