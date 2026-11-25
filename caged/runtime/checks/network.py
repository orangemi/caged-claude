#!/usr/bin/env python3
"""Network probes run in a separate, unprivileged namespace consumer."""
import argparse
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
from caged.shared.policy import policy, check_country, PolicyViolation


def probe_mark():
    private = Path('/run/caged/probe-mark')
    return int(private.read_text()) if private.is_file() else None


def dns_query(server='127.0.0.1', *, tcp=False, name='example.com', kind=1, timeout=3):
    ident = os.urandom(2)
    question = b''.join(bytes([len(label)]) + label.encode() for label in name.split('.')) + b'\0'
    packet = ident + struct.pack('!HHHHH', 0x100, 1, 0, 0, 0) + question + struct.pack('!HH', kind, 1)
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
    recursive = dns_query(name='caged-' + uuid.uuid4().hex + '.example.com', kind=16, timeout=12)
    if recursive['rcode'] not in (0, 3) or recursive['answers'] != 0 or recursive['authorities'] < 1:
        raise RuntimeError(
            'Could not verify DNS through the selected proxy. Check the node connection and the HTTPS DNS service '
            'set by proxy.dns_upstream.'
        )
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
            'public_dns': {key: 'blocked' for key in bypass}, 'fake_ip': True}


def host_boundary():
    # Use literal addresses: host.docker.internal must not be resolved through
    # fake-IP and accidentally turn into a request to the remote node's LAN.
    routes = Path('/proc/net/route').read_text().splitlines()[1:]
    gateways = [socket.inet_ntoa(bytes.fromhex(row.split()[2])[::-1]) for row in routes
                if row.split()[1] == '00000000' and row.split()[2] != '00000000']
    targets = [('169.254.169.254', 80)]
    targets += [(host, port) for host in gateways for port in (22, 2375, 2376, 7890)]
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda target: connected(*target), targets))
    if not targets or any(results):
        raise RuntimeError('Host/metadata management boundary failed')
    return {'literal_targets': len(targets), 'metadata_and_bridge_management': 'blocked'}


def curl(url, *, interface=None, timeout=20):
    args = ['curl', '--noproxy', '*', '-fsS', '--connect-timeout', '5', '--max-time', str(timeout)]
    if probe_mark() is not None:
        target = urlsplit(url)
        try:
            ipaddress.ip_address(target.hostname)
            address = target.hostname
        except ValueError:
            address = dns_query(name=target.hostname)['addresses'][0]
        # Use an explicitly marked socket, including marked DNS above. libc
        # resolver traffic and the curl CLI cannot reliably carry SO_MARK.
        try:
            if target.scheme != 'https' or target.username or target.password:
                raise RuntimeError('Invalid diagnostic HTTPS target')
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
                    if response.status != 200:
                        raise RuntimeError('Diagnostic endpoint returned non-200 status')
                    return subprocess.CompletedProcess(args, 0, response.read(65536).decode(), '')
        except (OSError, RuntimeError, http.client.HTTPException):
            return subprocess.CompletedProcess(args, 1, '', 'Diagnostic HTTPS request failed')
    if interface:
        args += ['--interface', interface]
    return subprocess.run(args + [url], text=True, capture_output=True)


def trace():
    result = curl('https://www.cloudflare.com/cdn-cgi/trace')
    if result.returncode:
        raise RuntimeError('HTTPS egress probe failed')
    fields = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    check_country(fields.get('loc'), policy()['expected_country'])
    return {'country': fields.get('loc'), 'ip': fields.get('ip'), 'colo': fields.get('colo')}


def stun():
    cookie = 0x2112A442
    transaction = os.urandom(12)
    request = struct.pack('!HHI', 1, 0, cookie) + transaction
    for host, port in [('stun.l.google.com', 19302), ('stun.cloudflare.com', 3478)]:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
                client.settimeout(6)
                client.sendto(request, (host, port))
                data, _ = client.recvfrom(2048)
            if len(data) < 20 or data[8:20] != transaction:
                continue
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
                    return {'public_ip': address, 'server': host}
                position += 4 + ((size + 3) // 4 * 4)
        except (OSError, RuntimeError):
            continue
    raise RuntimeError('UDP/STUN probe failed; UDP support is not yet verified')


def verify():
    dns = dns_checks()
    tcp = trace()
    udp = stun()
    direct = curl('https://1.1.1.1/cdn-cgi/trace', interface='eth0', timeout=4)
    if direct.returncode == 0:
        raise RuntimeError('Physical-interface direct access unexpectedly succeeded')
    ipv6 = subprocess.run(
        ['curl', '--noproxy', '*', '-6', '-fsS', '--max-time', '3',
         'https://[2606:4700:4700::1111]/cdn-cgi/trace'],
        capture_output=True,
    )
    if ipv6.returncode == 0:
        raise RuntimeError('IPv6 direct access unexpectedly succeeded')
    return {'tcp': tcp, 'udp': udp, 'dns': dns, 'host_boundary': host_boundary(),
            'physical_direct': 'blocked', 'ipv6': 'blocked'}


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
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.settimeout(2)
            client.sendto(struct.pack('!HHI', 1, 0, 0x2112A442) + os.urandom(12), ('162.159.207.0', 3478))
            client.recv(2048)
    except OSError:
        pass
    else:
        raise RuntimeError('UDP survived removal of the proxy container')
    return {'default_route_access': 'blocked', 'physical_direct': 'blocked', 'dns': 'blocked', 'udp': 'blocked'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['egress', 'dns', 'verify', 'fail-closed'])
    args = parser.parse_args()
    try:
        actions = {'egress': trace, 'dns': dns_checks, 'verify': verify, 'fail-closed': fail_closed}
        print(json.dumps(actions[args.action]()))
    except Exception as error:
        print(json.dumps({'error': str(error)}))
        raise SystemExit(1)
