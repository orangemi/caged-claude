"""Launcher side of the host bridge (docs/host-bridge.md).

The listener itself must run on the host, not in the launcher container,
so the launcher only prepares the mount and writes a plan file; ./cage then
runs caged/host/bridge.py with the host's python3 to start or stop it.
"""
import json
import os
from pathlib import Path
import socket
from .paths import ROOT

MOUNT = '/run/caged-host'


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
            'sshd': args.host_bridge_sshd, 'bridge': str(bridge_dir(state)), 'config': str(config_dir(args.instance))}


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


def status(args, state):
    if not args.host_bridge:
        return 'disabled'
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
