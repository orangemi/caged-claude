"""Exit address and location through the selected node, from independent services.

Every request uses the same path as the caller's other probes (see network.request);
none carries cookies, credentials or project identifiers."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import ipaddress
import json
import re
from pathlib import Path
import time
from caged.shared.policy import policy, check_country, PolicyViolation
from caged.shared.mainland import is_mainland

# Started in order; the next one starts when the previous fails or takes over STAGGER seconds.
IP_SOURCES = [
    ('icanhazip', 'https://ipv4.icanhazip.com/'),
    ('amazonaws', 'https://checkip.amazonaws.com/'),
    ('akamai', 'https://whatismyip.akamai.com/'),
    ('cloudflare', 'https://1.1.1.1/cdn-cgi/trace'),
    ('ipify', 'https://api.ipify.org/'),
    ('geojs', 'https://ipv4.geojs.io/v1/ip'),
]
# Tried in order; the first answer that names the queried IP and a country wins.
# Its timezone, when it gives one, is where the launcher looks first (zones.py).
LOCATION_SOURCES = [
    ('ipwho.is', 'https://ipwho.is/{ip}?fields=success,ip,country_code,timezone', True),
    ('ip.sb', 'https://api.ip.sb/geoip/{ip}', False),
    ('ipwhois.app', 'https://ipwhois.app/json/{ip}', True),
    ('geojs', 'https://get.geojs.io/v1/ip/geo/{ip}.json', False),
    ('ipapi.co', 'https://ipapi.co/{ip}/json/', False),
]
# Only distinguishes "node unreachable" from "node blocks the IP services".
REACHABILITY = 'https://www.gstatic.com/generate_204'
STAGGER = 1.0
REQUEST_TIMEOUT = 6
# Locations change rarely; cache them so frequent monitoring stays within free quotas.
CACHE = Path('/run/caged/location-cache.json')
CACHE_SECONDS = 6 * 3600


def public_ipv4(text):
    try:
        address = ipaddress.IPv4Address(text.strip())
    except ValueError:
        return None
    return str(address) if address.is_global and not address.is_multicast else None


def parse_ip(name, body):
    if name == 'cloudflare':
        body = next((line[3:] for line in body.splitlines() if line.startswith('ip=')), '')
    return public_ipv4(body)


def describe(error):
    text = str(error)
    return text if text.startswith(('HTTP ', 'timed out', 'no ', 'invalid', 'connection')) else 'connection failed'


def exit_ip(fetch, sources=IP_SOURCES, stagger=STAGGER):
    """(ip, source, {source: reason}) from the first service with a public IPv4."""
    failures = {}

    def ask(name, url):
        status, body = fetch(url, REQUEST_TIMEOUT)
        if status != 200:
            raise RuntimeError(f'HTTP {status}')
        address = parse_ip(name, body)
        if not address:
            raise RuntimeError('no public IPv4 in the response')
        return address

    pool = ThreadPoolExecutor(max_workers=len(sources))
    queue, pending = iter(sources), {}

    def start():
        item = next(queue, None)
        if item:
            pending[pool.submit(ask, *item)] = item[0]
        return item is not None

    try:
        start()
        while pending:
            done, _ = wait(pending, timeout=stagger, return_when=FIRST_COMPLETED)
            if not done:
                start()
                continue
            for future in done:
                name = pending.pop(future)
                try:
                    return future.result(), name, failures
                except Exception as error:
                    failures[name] = describe(error)
                    start()
        return None, None, failures
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def cached(address):
    try:
        entry = json.loads(CACHE.read_text()).get(address)
    except (OSError, ValueError, AttributeError):
        return None
    if entry and 0 <= time.time() - entry['at'] < CACHE_SECONDS:
        return entry['country'], entry.get('timezone'), entry['source'] + ' (cached)'
    return None


def zone_name(value):
    """An IANA name from a service's timezone field: a string or {"id": ...}."""
    if isinstance(value, dict):
        value = value.get('id')
    if isinstance(value, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+){1,2}', value.strip()):
        return value.strip()
    return None


def remember(address, country, timezone, source):
    try:
        data = json.loads(CACHE.read_text()) if CACHE.exists() else {}
        data = {key: value for key, value in data.items() if time.time() - value['at'] < CACHE_SECONDS}
        data[address] = {'country': country, 'timezone': timezone, 'source': source, 'at': time.time()}
        temporary = CACHE.with_suffix('.tmp')
        temporary.write_text(json.dumps(data))
        temporary.replace(CACHE)
    except (OSError, ValueError, KeyError, TypeError):
        pass  # Only the gateway's private tmpfs keeps a cache.


def locate(address, fetch, sources=LOCATION_SOURCES):
    """(country, timezone, source, {source: reason}); country is None when every service failed."""
    hit = cached(address)
    if hit:
        return (*hit, {})
    failures = {}
    for name, template, require_success in sources:
        try:
            status, body = fetch(template.format(ip=address), REQUEST_TIMEOUT)
            if status != 200:
                raise RuntimeError(f'HTTP {status}')
            value = json.loads(body)
            if not isinstance(value, dict):
                raise RuntimeError('invalid response')
            country = str(value.get('country_code') or '').strip().upper()
            if (value.get('error') is True or value.get('bogon') is True
                    or (require_success and value.get('success') is not True)
                    or public_ipv4(str(value.get('ip', ''))) != address):
                raise RuntimeError('invalid response or another IP')
            if len(country) != 2 or not country.isalpha() or country in ('XX', 'ZZ'):
                raise RuntimeError('no valid country')
        except Exception as error:
            failures[name] = describe(error)
            continue
        timezone = zone_name(value.get('timezone'))
        remember(address, country, timezone, name)
        return country, timezone, name, failures
    return None, None, None, failures


def summary(failures):
    return '; '.join(f'{name}: {reason}' for name, reason in failures.items())


def observe(fetch, expected=None):
    """The exit IP and country, or an error that says why they are unavailable."""
    address, source, failures = exit_ip(fetch)
    if not address:
        try:
            reachable = fetch(REACHABILITY, REQUEST_TIMEOUT)[0] == 204
        except Exception:
            reachable = False
        hint = ('the node answers other sites (generate_204), so it probably blocks these services; '
                'choose another node or ask its provider to allow one of them' if reachable else
                'the node itself may be unreachable: check its address, port and credentials, and run '
                './cage down && ./cage up if its IP address changed')
        raise RuntimeError(f'No exit IP service answered through the selected node ({summary(failures)}); {hint}')
    # A mainland range match needs no location evidence.
    if is_mainland(address):
        raise PolicyViolation('Exit IP is in a mainland China range; mainland exits are not allowed')
    country, timezone, located_by, location_failures = locate(address, fetch)
    if not country:
        raise RuntimeError(f'Exit IP measured, but every location service failed ({summary(location_failures)}); '
                           'retry or choose another node')
    check_country(country, expected, address)
    return {'ip': address, 'country': country, 'timezone': timezone, 'ip_source': source, 'location_source': located_by,
            'ip_failures': failures, 'location_failures': location_failures}


def observe_policy(fetch):
    return observe(fetch, policy()['expected_country'])
