"""Launcher side of the host bridge (docs/host-bridge.md).

The listener itself must run on the host, not in the launcher container,
so the launcher only prepares the mount and writes a plan file; ./cage then
runs caged/host/bridge.py with the host's python3 to start or stop it.
"""
import ipaddress
import json
import os
from pathlib import Path
import socket
from .errors import CageError
from .paths import ROOT

MOUNT = '/run/caged-host'
HOST_NAME = 'host.docker.internal'


def tcp_endpoint(args, resolver=None):
    """[address, port] for the tcp transport, else None.

    On macOS no Unix socket crosses into Docker's VM, so the cage reaches the
    host listener over TCP: the gateway's only direct, non-node destination."""
    if not args.host_bridge or args.host_bridge_transport != 'tcp':
        return None
    address = args.host_bridge_address
    if address is None:
        try:
            address = (resolver or socket.gethostbyname)(HOST_NAME)
        except OSError:
            raise CageError(f'Cannot resolve {HOST_NAME} for the host bridge; set host_bridge.address '
                            'or host_bridge.enabled: false') from None
    address = str(ipaddress.IPv4Address(address))
    if ipaddress.IPv4Address(address).is_global:
        raise CageError(f'{HOST_NAME} resolved to a public address ({address}); refusing to open it as the host bridge')
    return [address, args.host_bridge_port]


def target(args):
    """What the container's ssh ProxyCommand connects to."""
    endpoint = getattr(args, 'host_bridge_tcp', None)
    return f'tcp:{endpoint[0]}:{endpoint[1]}' if endpoint else MOUNT + '/host.sock'


def bridge_dir(state):
    return state.root / 'bridge'


def config_dir(instance):
    return ROOT / 'config' / 'host-bridge' / instance


def prepare(state):
    folder = bridge_dir(state)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(folder, 0o700)
    return folder


def plan(args, state, action):
    return {'action': action, 'instance': args.instance, 'user': args.host_bridge_user,
            'sshd': args.host_bridge_sshd, 'bridge': str(bridge_dir(state)), 'config': str(config_dir(args.instance)),
            'transport': args.host_bridge_transport, 'port': args.host_bridge_port}


def write_plan(args, state, action, environ=None):
    """Hand ./cage what to do on the host; no-op without a plan path."""
    target = (os.environ if environ is None else environ).get('CAGED_BRIDGE_PLAN')
    if not target:
        return None
    path = Path(target)
    if ROOT.resolve() not in path.resolve().parents:
        raise ValueError('CAGED_BRIDGE_PLAN must be inside the project')
    path.write_text(json.dumps(plan(args, state, action)) + '\n')
    return path


def status(args, state, resolver=None):
    if not args.host_bridge:
        return 'disabled'
    if args.host_bridge_transport == 'tcp':
        try:
            address, port = tcp_endpoint(args, resolver)
            with socket.create_connection((address, port), timeout=2):
                return f'listening (tcp {address}:{port}, direct)'
        except (OSError, CageError):
            return f'stopped (tcp port {args.host_bridge_port})'
    path = bridge_dir(state) / 'host.sock'
    if not path.exists():
        return 'stopped'
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(2)
        try:
            probe.connect(str(path))
        except OSError:
            return 'stopped (stale socket)'
    return 'listening'
