"""Validate a managed target and build exec arguments from its actual identity."""
import sys
from . import docker as engine
from .errors import CageError
from .instance import Instance
from .paths import ROOT, LABEL


def target(instance, role):
    names = Instance(instance)
    name = {'claude': names.claude, 'browser': names.browser, 'network': names.network}[role]
    current = engine.inspect(name)
    if not current or not current['State']['Running']:
        raise CageError(role.capitalize() + ' container is not running. Run ./start first.')
    if current['Config'].get('Labels', {}).get(LABEL) != str(ROOT):
        raise CageError('Refusing to enter an unmanaged ' + role.capitalize() + ' container')
    if role != 'network':
        gateway = engine.inspect(names.network)
        if not gateway or not gateway['State']['Running'] or current['HostConfig']['NetworkMode'] != 'container:' + gateway['Id']:
            raise CageError('The gateway is unavailable or was replaced. Run ./status to inspect this instance.')
        if gateway['Config'].get('Labels', {}).get(LABEL) != str(ROOT):
            raise CageError('Gateway is not managed by this project')
    return current


def exec_command(current, arguments, *, root=False, tty=None):
    env = dict(item.split('=', 1) for item in current['Config'].get('Env', []) if '=' in item)
    result = ['docker', 'exec', '-i']
    use_tty = sys.stdin.isatty() and sys.stdout.isatty() if tty is None else tty
    if use_tty:
        result.append('-t')
    if root:
        result += ['--user', '0:0', '-e', 'HOME=/root', '-e', 'USER=root', '-e', 'LOGNAME=root']
    else:
        if 'PUID' not in env or 'PGID' not in env:
            raise CageError('Application runtime UID/GID is missing')
        result += ['--user', env['PUID'] + ':' + env['PGID']]
    return [*result, current['Id'], *arguments]
