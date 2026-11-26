"""Launcher side of the host dialer (dialer/, docs/configuration.md).

A host TUN or VPN (Clash, mihomo, sing-box) captures the gateway's connection to
its node, because containers reach the internet through the host's routing. The
dialer runs on the host itself and connects to the pinned node addresses over
the physical interface instead. ./cage builds and starts it; the launcher only
writes this instance's route into routes.json and reads status.json back.

The gateway then dials the dialer: over TCP to host.docker.internal where no
Unix socket crosses into Docker's VM (macOS, Windows), else over a Unix socket
mounted into the gateway (Linux)."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import time
import zlib
from .errors import CageError
from .paths import ROOT

DIR = ROOT / 'state' / '.dialer'
MOUNT = '/run/caged-dialer'
SOCKET = 'dialer.sock'
FRESH = 5  # seconds since the dialer's last heartbeat


def kind(environ=os.environ):
    """unix on Linux, tcp where the dialer runs outside Docker's Linux VM."""
    system = environ.get('CAGED_HOST_OS') or os.uname().sysname
    return 'unix' if system == 'Linux' and environ.get('CAGED_HOST_WSL') != '1' else 'tcp'


def port(instance):
    return 43000 + zlib.crc32(instance.encode()) % 1000


def socket_dir(instance):
    return DIR / 'sock' / instance


def read(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def status():
    """The running dialer's status, or None when it is not running."""
    value = read(DIR / 'status.json')
    if not isinstance(value, dict) or time.time() - value.get('heartbeat', 0) > FRESH:
        return None
    return value


def wait_status(timeout=8):
    deadline = time.monotonic() + timeout
    while True:
        value = status()
        if value or time.monotonic() > deadline:
            return value
        time.sleep(0.25)


@contextmanager
def routes():
    DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (DIR / 'routes.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = read(DIR / 'routes.json') or {}
        data.setdefault('instances', {})
        yield data
        temporary = DIR / 'routes.json.tmp'
        temporary.write_text(json.dumps(data, indent=2) + '\n')
        temporary.replace(DIR / 'routes.json')


def register(instance, addresses, node_port, sni, environ=os.environ):
    """Route this instance to its pinned addresses; returns (route, revision)."""
    revision = time.time_ns()
    if kind(environ) == 'unix':
        folder = socket_dir(instance)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        listen = f'unix:{folder / SOCKET}'
    else:
        listen = f'127.0.0.1:{port(instance)}'
    route = {'listen': listen, 'targets': [f'{address}:{node_port}' for address in addresses],
             'sni': sni, 'revision': revision}
    with routes() as data:
        data['instances'][instance] = route
    return route, revision


def unregister(instance):
    if not (DIR / 'routes.json').exists():
        return
    with routes() as data:
        data['instances'].pop(instance, None)


def wait_check(instance, revision, timeout=30):
    """The dialer's comparison of the bound and the default path to the node."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = status() or {}
        entry = (value.get('instances') or {}).get(instance) or {}
        if entry.get('listen_error'):
            raise CageError(f'The host dialer cannot listen on {entry["listen"]}: {entry["listen_error"]}')
        check = entry.get('check')
        if check and check.get('revision') == revision:
            return check
        time.sleep(0.25)
    raise CageError('The host dialer did not check the node connection in time; see state/.dialer/dialer.log')


def tunnel(value):
    """Interface the host's default path uses for public traffic, if it is not physical."""
    if value and value.get('tunnel'):
        return value.get('sample_route') or 'a tunnel interface'
    return None


def describe(instance):
    value = status()
    if not value:
        return 'not running'
    entry = (value.get('instances') or {}).get(instance)
    interface = value.get('interface') or 'no physical interface'
    host = f'host TUN {value["sample_route"]} bypassed' if value.get('tunnel') else 'no host TUN detected'
    if not entry:
        return f'running (bound to {interface}; {host}); not used by this instance'
    check = entry.get('check') or {}
    bound = check.get('bound') or {}
    result = 'node TLS ok' if bound.get('ok') else 'node TLS failed: ' + bound.get('error', 'pending')
    return (f'used (bound to {interface}; {host}; {result}; '
            f'{entry.get("connections", 0)} connections, {entry.get("failures", 0)} failures)')
