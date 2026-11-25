"""Host YAML adapter and public IPv4 pinning for a selected node."""
from contextlib import closing
import copy
from http.client import HTTPSConnection, HTTPException
import ipaddress
import json
import ssl
from urllib.parse import urlsplit
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


def resolve_node_addresses(server, resolver):
    try:
        from dns import exception, message, rcode, rdatatype
    except ImportError:
        raise CageError('Resolving a proxy hostname requires dnspython. Run ./cage normally or install dnspython for CAGED_NATIVE=1.') from None

    try:
        query = message.make_query(server, rdatatype.A, id=0)
    except (exception.DNSException, UnicodeError):
        raise CageError('The selected proxy node has an invalid hostname. Check its server field in the proxy YAML.') from None

    endpoint = urlsplit(resolver)
    media_type = 'application/dns-message'
    hint = 'Check proxy.bootstrap_dns in app.yaml or choose another HTTPS DNS service with --bootstrap-dns.'
    try:
        # Connect to the configured IP with certificate verification and without system proxy settings.
        with closing(HTTPSConnection(endpoint.hostname, endpoint.port, timeout=5,
                                     context=ssl.create_default_context())) as connection:
            connection.request('POST', endpoint.path, body=query.to_wire(),
                               headers={'Content-Type': media_type, 'Accept': media_type})
            with connection.getresponse() as response:
                if response.status != 200:
                    raise CageError(f'The HTTPS DNS service returned HTTP {response.status}. {hint}')
                if response.headers.get_content_type() != media_type:
                    raise CageError(f'The HTTPS DNS service returned an unexpected content type. {hint}')
                wire = response.read(65536)
    except ssl.SSLCertVerificationError:
        raise CageError(f'Could not verify the HTTPS DNS service certificate. {hint}') from None
    except (OSError, HTTPException):
        raise CageError(f'Could not reach the HTTPS DNS service to look up the proxy node. {hint}') from None

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
            raise CageError(f'The HTTPS DNS service could not resolve the proxy node ({rcode.to_text(status)}). {hint}')
        records = answer.resolve_chaining().answer
    except exception.DNSException:
        raise CageError(f'The HTTPS DNS service returned an invalid reply to the proxy node lookup. {hint}') from None
    return [ipaddress.IPv4Address(record.address) for record in records] if records else []


def pin_node(node, resolver):
    proxy = copy.deepcopy(node)
    server = str(proxy['server'])
    try:
        address = ipaddress.ip_address(server)
    except ValueError:
        addresses = resolve_node_addresses(server, resolver)
        proxy.setdefault('sni', server)
        hint = 'Check its server field in the proxy YAML or select another node with --node.'
    else:
        addresses = [address]
        hint = 'Use a public IPv4 or a hostname in the server field of the proxy YAML.'
    public = [str(address) for address in addresses
              if address.version == 4 and address.is_global and not address.is_multicast]
    if not public:
        raise CageError(f'The selected proxy node has no usable public IPv4 address. {hint}')
    proxy['server'] = min(public)
    return proxy
