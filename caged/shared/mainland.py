"""Mainland China IPv4 prefixes, applied to observed exit addresses only.

The bundled list aggregates APNIC delegations to CN. A match is enough to
deny an exit; absence from the list still needs country evidence.
Regenerate it with: python3 -m caged.shared.mainland delegated-apnic-latest
"""
import bisect
import ipaddress
from pathlib import Path
import sys

DATA = Path(__file__).with_name('mainland-ipv4.txt')
_ranges = None


def ranges():
    global _ranges
    if _ranges is None:
        rows = []
        for line in DATA.read_text().split():
            network = ipaddress.IPv4Network(line)
            if network.prefixlen < 8:
                raise RuntimeError('Invalid bundled mainland prefix')
            rows.append((int(network.network_address), int(network.broadcast_address)))
        if not rows:
            raise RuntimeError('The bundled mainland prefix list is empty')
        _ranges = sorted(rows)
    return _ranges


def is_mainland(address):
    value = int(ipaddress.IPv4Address(address))
    rows = ranges()
    index = bisect.bisect_right(rows, (value, 1 << 32)) - 1
    return index >= 0 and rows[index][0] <= value <= rows[index][1]


def build(delegated):
    networks = []
    for line in Path(delegated).read_text().splitlines():
        fields = line.strip().split('|')
        if len(fields) >= 7 and fields[1:3] == ['CN', 'ipv4'] and fields[6] in ('allocated', 'assigned'):
            start = ipaddress.IPv4Address(fields[3])
            networks += ipaddress.summarize_address_range(start, start + int(fields[4]) - 1)
    return [str(network) for network in ipaddress.collapse_addresses(networks)]


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('usage: python3 -m caged.shared.mainland delegated-apnic-latest')
    DATA.write_text('\n'.join(build(sys.argv[1])) + '\n')
