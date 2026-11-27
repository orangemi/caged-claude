#!/usr/bin/env python3
"""Network probes run in a separate, unprivileged namespace consumer."""
import argparse
import base64
import ipaddress
import json
import os
import socket
import struct
import subprocess
import uuid
import ssl
import http.client
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit
from pathlib import Path
from caged.shared.policy import policy, PolicyViolation
from caged.runtime.checks import egress

RCODES = {0: 'NOERROR', 1: 'FORMERR', 2: 'SERVFAIL', 3: 'NXDOMAIN', 4: 'NOTIMP', 5: 'REFUSED'}
# The live fault drill reroutes only this otherwise unused UID around the proxy.
DRILL_UID = 65533
STUN_TARGET = ('162.159.207.0', 3478)  # stun.cloudflare.com, literal so no lookup is involved


def probe_mark():
    try:
        return int(Path('/run/caged/probe-mark').read_text())
    except (OSError, ValueError):
        return None  # Absent outside the gateway, unreadable to unprivileged UIDs.


def dns_packet(name, kind):
    ident = os.urandom(2)
    question = b''.join(bytes([len(label)]) + label.encode() for label in name.split('.')) + b'\0'
    return ident, ident + struct.pack('!HHHHH', 0x100, 1, 0, 0, 0) + question + struct.pack('!HH', kind, 1)


def dns_query(server='127.0.0.1', *, tcp=False, name='example.com', kind=1, timeout=3):
    ident, packet = dns_packet(name, kind)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM if tcp else socket.SOCK_DGRAM) as client:
        if probe_mark() is not None:
            client.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, probe_mark())
        client.settimeout(timeout)
        client.connect((server, 53))
        if tcp:
            client.sendall(struct.pack('!H', len(packet)) + packet)
            def read(size):
                result = b''
                while len(result) < size:
                    data = client.recv(size - len(result))
                    if not data:
                        raise RuntimeError('Truncated DNS response')
                    result += data
                return result
            data = read(struct.unpack('!H', read(2))[0])
        else:
            client.send(packet)
            data = client.recv(8192)
    if len(data) < 12 or data[:2] != ident:
        raise RuntimeError('Invalid DNS response')
    flags, questions, answers, authorities, _ = struct.unpack('!HHHHH', data[2:12])
    if not flags & 0x8000:
        raise RuntimeError('DNS response flag missing')
    result = {'rcode': flags & 15, 'answers': answers, 'authorities': authorities}
    if kind == 1:
        def skip_name(position):
            for _ in range(128):
                size = data[position]
                if size & 0xc0 == 0xc0:
                    return position + 2
                position += size + 1
                if size == 0:
                    return position
            raise RuntimeError('Invalid DNS name')
        position, addresses = 12, []
        for _ in range(questions):
            position = skip_name(position) + 4
        for _ in range(answers):
            position = skip_name(position)
            record, _, _, size = struct.unpack('!HHIH', data[position:position + 10])
            position += 10
            if record == 1 and size == 4:
                addresses.append(socket.inet_ntoa(data[position:position + size]))
            position += size
        result['addresses'] = addresses
    return result


def connected(host, port, timeout=1.5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def doh_via_node(upstream, secret, timeout=6):
    """One DoH endpoint, tunnelled through the node by the authenticated mixed proxy."""
    target = urlsplit(upstream)
    port = target.port or 443
    _, packet = dns_packet('example.com', 1)
    token = base64.b64encode(f'caged:{secret}'.encode()).decode()
    try:
        with socket.create_connection(('127.0.0.1', policy()['mixed_port']), timeout=timeout) as raw:
            raw.sendall(f'CONNECT {target.hostname}:{port} HTTP/1.1\r\nHost: {target.hostname}:{port}\r\n'
                        f'Proxy-Authorization: Basic {token}\r\n\r\n'.encode())
            head = b''
            while b'\r\n\r\n' not in head and len(head) < 4096:
                data = raw.recv(1)
                if not data:
                    break
                head += data
            if head.split()[1:2] != [b'200']:
                return 'the proxy did not open a tunnel'
            with ssl.create_default_context().wrap_socket(raw, server_hostname=target.hostname) as secure:
                secure.sendall((f'POST {target.path or "/"} HTTP/1.1\r\nHost: {target.hostname}\r\n'
                                'Content-Type: application/dns-message\r\nAccept: application/dns-message\r\n'
                                f'Content-Length: {len(packet)}\r\nConnection: close\r\n\r\n').encode() + packet)
                response = http.client.HTTPResponse(secure)
                response.begin()
                if response.status != 200:
                    return f'HTTP {response.status}'
                data = response.read(65536)
    except socket.timeout:
        return 'timed out through the node'
    except ssl.SSLError:
        return 'TLS failed through the node'
    except (OSError, http.client.HTTPException):
        return 'connection failed through the node'
    if len(data) < 12:
        return 'invalid DNS reply'
    rcode = data[3] & 15
    return 'answered' if rcode == 0 else 'answered ' + RCODES.get(rcode, str(rcode))


def doh_diagnosis():
    """Per-endpoint results; only the gateway can read the mixed-proxy secret."""
    try:
        secret = Path('/run/caged/controller-secret').read_text()
    except OSError:
        return ''
    upstreams = policy()['dns_upstream']
    with ThreadPoolExecutor(max_workers=len(upstreams)) as executor:
        results = list(executor.map(lambda upstream: doh_via_node(upstream, secret), upstreams))
    text = 'DoH endpoints through the node: ' + '; '.join(
        f'{urlsplit(upstream).hostname} {result}' for upstream, result in zip(upstreams, results)) + '. '
    if not any(result.startswith('answered') for result in results):
        text += node_health(secret)
    return text


def node_health(secret):
    """When no endpoint works, tell a dead node apart from blocked DNS services."""
    from urllib.parse import quote
    from caged.runtime.network.mihomo import api, NODE, HEALTH_URL
    try:
        api(secret, f'/proxies/{NODE}/delay?url={quote(HEALTH_URL, safe="")}&timeout=6000', timeout=8)
    except Exception:
        return ('The node itself does not work: the proxy could not open a session to it (see ./cage logs network). '
                'Check the node, and turn off a host TUN/VPN or let it connect to the node directly. ')
    return 'The node works for other sites, so it probably blocks these DoH services. '


def dns_checks():
    resolvers = [line.split()[1] for line in Path('/etc/resolv.conf').read_text().splitlines()
                 if line.split()[:1] == ['nameserver']]
    if resolvers != ['127.0.0.1']:
        raise PolicyViolation('System resolver is not confined to the gateway loopback')
    for tcp in (False, True):
        response = dns_query(tcp=tcp)
        if response['rcode'] != 0 or response['answers'] < 1:
            raise RuntimeError('Local UDP/TCP DNS failed')
    fake_ip = dns_query()['addresses'][0] if probe_mark() is not None else socket.gethostbyname('example.com')
    if ipaddress.ip_address(fake_ip) not in ipaddress.ip_network('198.18.0.0/16'):
        raise PolicyViolation('System lookup bypassed the fake-IP resolver')
    # A records can be fabricated without contacting any upstream. A fresh TXT
    # negative response with an authority record requires recursive DNS.
    try:
        recursive = dns_query(name='caged-' + uuid.uuid4().hex + '.example.com', kind=16, timeout=12)
        detail = (f'rcode={RCODES.get(recursive["rcode"], recursive["rcode"])}, answers={recursive["answers"]}, '
                  f'authorities={recursive["authorities"]}')
    except (OSError, RuntimeError):
        recursive, detail = None, 'no reply within 12s'
    if not recursive or recursive['rcode'] not in (0, 3) or recursive['answers'] != 0 or recursive['authorities'] < 1:
        diagnosis = doh_diagnosis()
        hint = '' if 'node itself does not work' in diagnosis else 'Check the node connection, or list other endpoints in proxy.dns_upstream.'
        raise RuntimeError(f'Could not verify DNS through the selected proxy ({detail}). {diagnosis}{hint}'.strip())
    def public_udp():
        try:
            dns_query('1.1.1.1', timeout=1.5)
            return True
        except (OSError, RuntimeError):
            return False
    with ThreadPoolExecutor(max_workers=3) as executor:
        jobs = [executor.submit(public_udp), executor.submit(connected, '1.1.1.1', 53),
                executor.submit(connected, '1.1.1.1', 853)]
        bypass = dict(zip(('udp53', 'tcp53', 'dot853'), (job.result() for job in jobs)))
    if any(bypass.values()):
        raise PolicyViolation('Public DNS/DoT bypass succeeded')
    return {'resolvers': resolvers, 'local_udp_tcp': 'passed', 'fresh_recursive_txt': recursive,
            'public_dns': {key: 'blocked' for key in bypass}, 'fake_ip': True,
            'upstreams': [urlsplit(upstream).hostname for upstream in policy()['dns_upstream']]}


def host_boundary():
    # Use literal addresses: host.docker.internal must not be resolved through
    # fake-IP and accidentally turn into a request to the remote node's LAN.
    options = policy()
    routes = Path('/proc/net/route').read_text().splitlines()[1:]
    gateways = [socket.inet_ntoa(bytes.fromhex(row.split()[2])[::-1]) for row in routes
                if row.split()[1] == '00000000' and row.split()[2] != '00000000']
    ports = sorted({22, 2375, 2376, *options.get('host_proxy_ports', [7890])})
    hosts = list(dict.fromkeys(gateways))
    bridge = options.get('host_bridge_tcp')
    if bridge and bridge[0] not in hosts:
        hosts.append(bridge[0])  # the host itself on macOS, where its proxy usually listens
    targets = [('169.254.169.254', 80)]
    # The host bridge port is the one intended direct destination.
    targets += [(host, port) for host in hosts for port in ports if not bridge or [host, port] != list(bridge)]
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda target: connected(*target), targets))
    reached = [f'{host}:{port}' for (host, port), result in zip(targets, results) if result]
    if reached:
        raise PolicyViolation('Host or metadata management ports are reachable: ' + ', '.join(reached))
    return {'literal_targets': len(targets), 'hosts': len(hosts), 'ports': ports,
            'metadata_and_bridge_management': 'blocked',
            'host_bridge_exception': f'{bridge[0]}:{bridge[1]}' if bridge else None}


def marked_https(url, timeout, interface=None):
    """(status, body) over an explicitly marked socket, including marked DNS.

    libc resolver traffic and the curl CLI cannot reliably carry SO_MARK."""
    target = urlsplit(url)
    if target.scheme != 'https' or target.username or target.password:
        raise RuntimeError('invalid diagnostic HTTPS target')
    try:
        ipaddress.ip_address(target.hostname)
        address = target.hostname
    except ValueError:
        address = dns_query(name=target.hostname)['addresses'][0]
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as raw:
            raw.settimeout(timeout)
            raw.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, probe_mark())
            if interface:
                raw.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode() + b'\0')
            raw.connect((address, target.port or 443))
            with ssl.create_default_context().wrap_socket(raw, server_hostname=target.hostname) as secure:
                path = (target.path or '/') + ('?' + target.query if target.query else '')
                secure.sendall(f'GET {path} HTTP/1.1\r\nHost: {target.hostname}\r\nConnection: close\r\n\r\n'.encode())
                response = http.client.HTTPResponse(secure)
                response.begin()
                return response.status, response.read(65536).decode(errors='replace')
    except socket.timeout:
        raise RuntimeError('timed out') from None
    except (OSError, http.client.HTTPException):
        raise RuntimeError('connection failed') from None


def request(url, timeout=10):
    """(status, body) for an HTTPS GET through this namespace's normal path."""
    if probe_mark() is not None:
        return marked_https(url, timeout)
    result = subprocess.run(['curl', '--noproxy', '*', '-sS', '--connect-timeout', '5', '--max-time', str(timeout),
                             '-w', '\n%{http_code}', url], text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError('timed out' if result.returncode == 28 else 'connection failed')
    body, _, status = result.stdout.rpartition('\n')
    return int(status), body


def curl(url, *, interface=None, timeout=20):
    args = ['curl', '--noproxy', '*', '-fsS', '--connect-timeout', '5', '--max-time', str(timeout)]
    if probe_mark() is not None:
        try:
            status, body = marked_https(url, timeout, interface)
        except RuntimeError:
            return subprocess.CompletedProcess(args, 1, '', 'Diagnostic HTTPS request failed')
        if status != 200:
            return subprocess.CompletedProcess(args, 1, '', 'Diagnostic endpoint returned non-200 status')
        return subprocess.CompletedProcess(args, 0, body, '')
    if interface:
        args += ['--interface', interface]
    return subprocess.run(args + [url], text=True, capture_output=True)


def trace():
    """Exit IP and country from several independent services (see egress)."""
    return egress.observe_policy(request)


def reference():
    """Two more services' view of the exit, for the start's log only (see egress)."""
    return egress.reference(request)


def stun_request(target, timeout):
    cookie = 0x2112A442
    transaction = os.urandom(12)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        client.settimeout(timeout)
        client.sendto(struct.pack('!HHI', 1, 0, cookie) + transaction, target)
        data, _ = client.recvfrom(2048)
    if len(data) < 20 or data[8:20] != transaction:
        raise RuntimeError('Invalid STUN response')
    position = 20
    while position + 4 <= len(data):
        kind, size = struct.unpack_from('!HH', data, position)
        body = data[position + 4:position + 4 + size]
        if kind in (0x20, 1) and len(body) >= 8 and body[1] == 1:
            number = struct.unpack('!I', body[4:8])[0]
            if kind == 0x20:
                number ^= cookie
            address = str(ipaddress.IPv4Address(number))
            if not ipaddress.ip_address(address).is_global:
                raise RuntimeError('STUN returned a non-public address')
            return address
        position += 4 + ((size + 3) // 4 * 4)
    raise RuntimeError('STUN response lacked an address')


def stun():
    for host, port in [('stun.l.google.com', 19302), ('stun.cloudflare.com', 3478)]:
        try:
            return {'public_ip': stun_request((host, port), 6), 'server': host}
        except (OSError, RuntimeError):
            continue
    raise RuntimeError('UDP/STUN probe failed; UDP support is not yet verified')


def ipv6_state():
    """Global IPv6 addresses and default routes in this namespace."""
    def rows(path):
        try:
            return [row.split() for row in Path(path).read_text().splitlines()]
        except OSError:
            return []  # IPv6 disabled in this namespace
    addresses = [row for row in rows('/proc/net/if_inet6') if len(row) == 6 and row[3] == '00' and row[5] != 'lo']
    defaults = [row for row in rows('/proc/net/ipv6_route')
                if len(row) == 10 and row[0] == '0' * 32 and row[1] == '00' and row[9] != 'lo']
    return {'global_addresses': len(addresses), 'default_routes': len(defaults)}


def ipv6_probe():
    result = subprocess.run(['curl', '--noproxy', '*', '-6', '-fsS', '--max-time', '3',
                             'https://[2606:4700:4700::1111]/cdn-cgi/trace'], capture_output=True)
    if result.returncode == 0:
        raise PolicyViolation('IPv6 direct access unexpectedly succeeded')
    return {'external': 'blocked', **ipv6_state()}


def physical_direct():
    if curl('https://1.1.1.1/cdn-cgi/trace', interface='eth0', timeout=4).returncode == 0:
        raise PolicyViolation('Physical-interface direct access unexpectedly succeeded')
    return {'eth0_literal_ip': 'blocked'}


def part(function):
    """Evidence for one item, or why it is missing; one failure never hides the others."""
    try:
        return function()
    except PolicyViolation as error:
        return {'error': str(error), 'violation': True}
    except Exception as error:
        return {'error': str(error) or type(error).__name__}


def verify():
    return {'dns': part(dns_checks), 'tcp': part(trace), 'udp': part(stun), 'host_boundary': part(host_boundary),
            'physical_direct': part(physical_direct), 'ipv6': part(ipv6_probe)}


def fail_closed():
    # Literal IP avoids making a failed DNS lookup look like a successful kill switch.
    attempts = [
        curl('https://1.1.1.1/cdn-cgi/trace', timeout=5),
        curl('https://1.1.1.1/cdn-cgi/trace', interface='eth0', timeout=4),
    ]
    if any(result.returncode == 0 for result in attempts):
        raise RuntimeError('Direct access survived removal of the proxy container')
    try:
        dns_query(name=uuid.uuid4().hex + '.example.com', kind=16, timeout=2)
    except OSError:
        pass
    else:
        raise RuntimeError('DNS survived removal of the proxy container')
    try:
        stun_request(STUN_TARGET, 2)
    except (OSError, RuntimeError):
        pass
    else:
        raise RuntimeError('UDP survived removal of the proxy container')
    return {'default_route_access': 'blocked', 'physical_direct': 'blocked', 'dns': 'blocked', 'udp': 'blocked'}


def drill():
    """Run as DRILL_UID. Without the drill route rule this is the positive control;
    with it the proxy route is gone for this UID, as when the proxy fails."""
    def works(function):
        try:
            function()
            return True
        except (OSError, RuntimeError):
            return False
    https = curl('https://1.1.1.1/cdn-cgi/trace', timeout=6).returncode == 0
    udp = works(lambda: stun_request(STUN_TARGET, 3))
    dns = works(lambda: dns_query('1.1.1.1', timeout=1.5)) or connected('1.1.1.1', 53)
    return {'uid': os.getuid(), 'https_literal_ip': https, 'udp_stun': udp, 'public_dns': dns}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['egress', 'reference', 'dns', 'verify', 'fail-closed', 'drill'])
    args = parser.parse_args()
    try:
        actions = {'egress': trace, 'reference': reference, 'dns': dns_checks, 'verify': verify, 'fail-closed': fail_closed, 'drill': drill}
        print(json.dumps(actions[args.action]()))
    except Exception as error:
        print(json.dumps({'error': str(error)}))
        raise SystemExit(1)
