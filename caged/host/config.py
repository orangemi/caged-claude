"""Resolve CLI > environment > YAML > defaults before creating containers."""
import argparse
import getpass
import ipaddress
import json
import os
from pathlib import Path
import re
import zlib
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import yamlfile
from .paths import ROOT
DEFAULT_SETTINGS = ROOT / 'config' / 'app.yaml'
PREFIX = 'CAGED_CLAUDE_'
# Optional operational defaults; node, timezone and policy choices stay explicit.
DEFAULTS = {
    'proxy_file_path': 'mihomo.yaml',
    'instance': 'caged-claude',
    'dns_upstream': 'https://1.1.1.1/dns-query',
    'locale': 'en_US.UTF-8',
    'language': 'en-US',
    'puid': 'host',
    'pgid': 'host',
    'shm_size': '1g',
    'port': 3002,
    'controller_port': 9090,
    'mixed_port': 17890,
    'debug_port': 9222,
    'workspace': [],
    # None means config/claude/<instance>; resolved once the instance is known.
    'claude_config_dir': None,
    'claude_sudo': True,
    'state_dir': str(ROOT / 'state'),
    'host_bridge': True,
    'host_bridge_user': None,  # None: the user running ./cage (CAGED_HOST_USER)
    'host_bridge_sshd': None,  # None: found on the host's PATH
    'host_bridge_transport': 'auto',  # unix on Linux/WSL, tcp on macOS (no socket crosses its VM)
    'host_bridge_port': None,  # tcp only; None: derived from the instance name
    'host_bridge_address': None,  # tcp only; None: host.docker.internal, resolved at start
    'browser_probe_port': 19981,
    'claude_probe_port': 19982,
    'monitor_tick': 5,
    'monitor_interval': 30,
    'monitor_lease': 90,
    'monitor_probe_timeout': 20,
    'monitor_retry_initial': 15,
    'monitor_retry_max': 300,
}
MISSING = object()
# Schema/flag mappings; argparse itself does not override the resolution order.
FIELDS = {
    'node': ('proxy.node', 'NODE'),
    'proxy_file_path': ('proxy.file_path', 'PROXY_FILE_PATH'),
    'expected_country': ('proxy.expected_country', 'EXPECTED_COUNTRY'),
    'dns_upstream': ('proxy.dns_upstream', 'DNS_UPSTREAM'),
    'timezone': ('browser.timezone', 'TIMEZONE'),
    'locale': ('browser.locale', 'LOCALE'),
    'language': ('browser.language', 'LANGUAGE'),
    'puid': ('browser.puid', 'PUID'),
    'pgid': ('browser.pgid', 'PGID'),
    'shm_size': ('browser.shm_size', 'SHM_SIZE'),
    'port': ('ports.ui', 'PORT'),
    'controller_port': ('ports.controller', 'CONTROLLER_PORT'),
    'mixed_port': ('ports.mixed', 'MIXED_PORT'),
    'debug_port': ('ports.debug', 'DEBUG_PORT'),
    'instance': ('instance', 'INSTANCE'),
    'workspace': ('workspace', 'WORKSPACE'),
    'claude_config_dir': ('claude.config_dir', 'CLAUDE_CONFIG_DIR'),
    'claude_sudo': ('claude.sudo', 'CLAUDE_SUDO'),
    'state_dir': ('state.dir', 'STATE_DIR'),
    'host_bridge': ('host_bridge.enabled', 'HOST_BRIDGE'),
    'host_bridge_user': ('host_bridge.user', 'HOST_BRIDGE_USER'),
    'host_bridge_sshd': ('host_bridge.sshd', 'HOST_BRIDGE_SSHD'),
    'host_bridge_transport': ('host_bridge.transport', 'HOST_BRIDGE_TRANSPORT'),
    'host_bridge_port': ('host_bridge.port', 'HOST_BRIDGE_PORT'),
    'host_bridge_address': ('host_bridge.address', 'HOST_BRIDGE_ADDRESS'),
    'browser_probe_port': ('ports.browser_probe', 'BROWSER_PROBE_PORT'),
    'claude_probe_port': ('ports.claude_probe', 'CLAUDE_PROBE_PORT'),
    'monitor_tick': ('monitor.tick', 'MONITOR_TICK'),
    'monitor_interval': ('monitor.interval', 'MONITOR_INTERVAL'),
    'monitor_lease': ('monitor.lease', 'MONITOR_LEASE'),
    'monitor_probe_timeout': ('monitor.probe_timeout', 'MONITOR_PROBE_TIMEOUT'),
    'monitor_retry_initial': ('monitor.retry_initial', 'MONITOR_RETRY_INITIAL'),
    'monitor_retry_max': ('monitor.retry_max', 'MONITOR_RETRY_MAX'),
}
ACTIONS = ['start', 'test', 'verify', 'smoke', 'audit', 'nodes', 'build', 'stop', 'status', 'pause', 'resume']


class ConfigError(ValueError):
    pass


def read_yaml(path):
    try:
        data = yamlfile.load(path)
        # Round-trip through JSON so only JSON-compatible values (no dates) pass.
        return json.loads(json.dumps(data))
    except yamlfile.YamlError as error:
        if str(error) != 'unreadable':
            raise ConfigError(str(error)) from None
    except (TypeError, ValueError):
        pass
    raise ConfigError(f'Cannot read configuration {path}. Copy config.example/app.yaml to config/app.yaml and check the YAML.')


def lookup(data, dotted):
    for key in dotted.split('.'):
        if not isinstance(data, dict) or key not in data:
            return MISSING
        data = data[key]
    return data


def integer(value, key, minimum, maximum):
    if isinstance(value, bool) or not re.fullmatch(r'\d+', str(value)):
        raise ConfigError(key + ' must be an integer')
    value = int(value)
    if not minimum <= value <= maximum:
        raise ConfigError(f'{key} must be between {minimum} and {maximum}')
    return value


def boolean(value, key):
    if isinstance(value, str) and value.strip().lower() in ('true', 'yes', 'on', '1'):
        return True
    if isinstance(value, str) and value.strip().lower() in ('false', 'no', 'off', '0'):
        return False
    if not isinstance(value, bool):
        raise ConfigError(key + ' must be true or false')
    return value


def resolve(argv=None, environ=None):
    env = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description='Run browser and Claude containers behind a fail-closed proxy gateway.')
    parser.add_argument('action', nargs='?', default='start', choices=ACTIONS)
    parser.add_argument('--settings', help='application YAML; env: CAGED_CLAUDE_SETTINGS')
    for name, (key, variable) in FIELDS.items():
        flags = ['--' + name.replace('_', '-')]
        if name == 'proxy_file_path':
            flags += ['--config', '--proxy-config']
        extra = {'action': 'append'} if name == 'workspace' else {}
        parser.add_argument(*flags, dest=name, default=argparse.SUPPRESS, **extra,
                            help=f'{key}; env: {PREFIX}{variable}')
    args = parser.parse_args(argv)
    args.settings = Path(args.settings if args.settings is not None else env.get(PREFIX + 'SETTINGS', DEFAULT_SETTINGS)).expanduser().resolve()
    data = read_yaml(args.settings)
    allowed = {}
    for key, _ in FIELDS.values():
        parts = key.split('.')
        allowed.setdefault(parts[0], set()).update(parts[1:])
    allowed['proxy'].add('config')  # Legacy YAML alias; file_path takes precedence.
    if not isinstance(data, dict) or set(data) - set(allowed):
        raise ConfigError('Unknown application YAML section; supported: proxy, browser, ports, instance, workspace, claude, state, host_bridge, monitor')
    for section, keys in allowed.items():
        if keys and section in data and (not isinstance(data[section], dict) or set(data[section]) - keys):
            raise ConfigError('Unknown or invalid application configuration section: ' + section)
    args.sources = {}
    for name, (key, variable) in FIELDS.items():
        if hasattr(args, name):
            value, source = getattr(args, name), 'cli'
        elif PREFIX + variable in env:
            value, source = env[PREFIX + variable], 'env'
        elif name == 'proxy_file_path' and PREFIX + 'PROXY_CONFIG' in env:
            value, source = env[PREFIX + 'PROXY_CONFIG'], 'env'
        else:
            value, source = lookup(data, key), 'config'
            if name == 'proxy_file_path' and value is MISSING:
                value = lookup(data, 'proxy.config')
            if value is MISSING:
                if name not in DEFAULTS:
                    raise ConfigError('Missing configuration key: ' + key)
                value, source = DEFAULTS[name], 'default'
        setattr(args, name, value)
        args.sources[name] = source
    for name in ('node', 'timezone', 'locale', 'language', 'instance', 'dns_upstream', 'shm_size'):
        value = getattr(args, name)
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(name + ' must be a non-empty string')
        setattr(args, name, value.strip())
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,47}', args.instance):
        raise ConfigError('instance must be a Docker name of 1–48 characters')
    try:
        ZoneInfo(args.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError('Unknown timezone: ' + args.timezone) from None
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.@-]{0,63}', args.locale):
        raise ConfigError('Invalid locale name')
    if not re.fullmatch(r'[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*', args.language):
        raise ConfigError('language must be a language tag such as en-US or de-DE')
    pieces = args.language.split('-')
    args.language = '-'.join([pieces[0].lower()] + [
        part.upper() if len(part) == 2 and part.isalpha() else
        part.title() if len(part) == 4 and part.isalpha() else part.lower()
        for part in pieces[1:]])
    args.languages = list(dict.fromkeys([args.language, args.language.split('-')[0]]))
    if args.expected_country is None or str(args.expected_country).strip().lower() in ('', 'any'):
        args.expected_country = None
    elif re.fullmatch(r'[A-Za-z]{2}', str(args.expected_country)):
        args.expected_country = str(args.expected_country).upper()
    else:
        raise ConfigError('expected_country must be a two-letter country code, null, or any')
    for name in ('port', 'controller_port', 'mixed_port', 'debug_port', 'browser_probe_port', 'claude_probe_port'):
        setattr(args, name, integer(getattr(args, name), name, 1, 65535))
    internal = [args.controller_port, args.mixed_port, args.debug_port, args.browser_probe_port, args.claude_probe_port, 53, 3001]
    if len(set(internal)) != len(internal):
        raise ConfigError('Controller, mixed and debug ports must be distinct and cannot use DNS 53 or browser HTTPS 3001')
    for name in ('monitor_tick', 'monitor_interval', 'monitor_lease', 'monitor_probe_timeout', 'monitor_retry_initial', 'monitor_retry_max'):
        setattr(args, name, integer(getattr(args, name), name, 1, 3600))
    if args.monitor_interval < args.monitor_tick or args.monitor_lease < args.monitor_interval + args.monitor_probe_timeout + 2 * args.monitor_tick:
        raise ConfigError('Monitor lease must cover interval + probe_timeout + two ticks; interval must cover a tick')
    if args.monitor_retry_max < args.monitor_retry_initial:
        raise ConfigError('Monitor retry_max must cover retry_initial')
    # The launcher container may run as root; the wrapper passes the caller's identity.
    for name, variable, getter in (('puid', 'CAGED_HOST_UID', os.getuid), ('pgid', 'CAGED_HOST_GID', os.getgid)):
        value = getattr(args, name)
        if value == 'host':
            value = integer(env[variable], variable, 0, 2147483647) if variable in env else getter()
        else:
            value = integer(value, name, 0, 2147483647)
        setattr(args, name, value)
    if not re.fullmatch(r'[1-9][0-9]*[kKmMgG]', args.shm_size):
        raise ConfigError('shm_size must be a positive size such as 512m or 1g')
    if not isinstance(args.proxy_file_path, (str, Path)) or not str(args.proxy_file_path).strip():
        raise ConfigError('proxy.file_path must be a file path')
    proxy_path = Path(args.proxy_file_path).expanduser()
    if not proxy_path.is_absolute():
        base = args.settings.parent if args.sources['proxy_file_path'] in ('config', 'default') else Path.cwd()
        proxy_path = base / proxy_path
    args.proxy_file_path = proxy_path.resolve()
    value = args.workspace
    if isinstance(value, str):
        if value.lstrip().startswith('['):
            try:
                value = json.loads(value)
            except ValueError:
                raise ConfigError('WORKSPACE must be a path or a JSON array of paths') from None
        else:
            value = [value]
    if value is None:
        value = []
    if not isinstance(value, list):
        raise ConfigError('workspace must be a path or a list of paths')
    base = args.settings.parent if args.sources['workspace'] == 'config' else Path.cwd()
    args.workspace = []
    # Container system paths, including the persisted state mounts: a
    # workspace there would hide them or plant mount points in the state.
    protected = ['/config', '/proc', '/sys', '/dev', '/run', '/etc', '/usr', '/bin', '/sbin', '/lib', '/lib64',
                 '/var', '/opt', '/srv', '/root', '/home/claude', '/caged-runtime']
    for item in value:
        if not isinstance(item, str) or not item.strip() or any(char in item for char in (',', ':', '\n', '\r')):
            raise ConfigError('Workspace paths must be nonempty and cannot contain comma, colon or newlines')
        folder = Path(item).expanduser()
        folder = (folder if folder.is_absolute() else base / folder).resolve()
        if not folder.is_dir():
            raise ConfigError('Workspace directory does not exist: ' + str(folder))
        if any(folder == Path(p) or Path(p) in folder.parents or folder in Path(p).parents for p in protected):
            raise ConfigError('Workspace would mask a container system directory: ' + str(folder))
        if folder not in args.workspace:
            args.workspace.append(folder)
    if args.claude_config_dir is None:
        args.claude_config_dir = ROOT / 'config' / 'claude' / args.instance
    if not isinstance(args.claude_config_dir, (str, Path)) or not str(args.claude_config_dir).strip():
        raise ConfigError('claude.config_dir must be a directory path')
    state = Path(args.claude_config_dir).expanduser()
    state_base = args.settings.parent if args.sources['claude_config_dir'] == 'config' else Path.cwd()
    state = (state if state.is_absolute() else state_base / state).resolve()
    private_root = (ROOT / 'config').resolve()
    if private_root not in state.parents or any(char in str(state) for char in (',', ':', '\n', '\r')):
        raise ConfigError('Claude state must be a subdirectory of the project config/ directory')
    args.claude_config_dir = state
    args.claude_sudo = boolean(args.claude_sudo, 'claude.sudo')
    args.host_bridge = boolean(args.host_bridge, 'host_bridge.enabled')
    if args.host_bridge_user is None:
        args.host_bridge_user = env.get('CAGED_HOST_USER') or getpass.getuser()
    if not isinstance(args.host_bridge_user, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}', args.host_bridge_user):
        raise ConfigError('host_bridge.user must be a user name')
    if args.host_bridge_sshd is not None and (not isinstance(args.host_bridge_sshd, str)
                                              or not args.host_bridge_sshd.startswith('/')):
        raise ConfigError('host_bridge.sshd must be an absolute path')
    transport = args.host_bridge_transport
    if transport not in ('auto', 'unix', 'tcp'):
        raise ConfigError('host_bridge.transport must be auto, unix or tcp')
    if transport == 'auto':
        # A Unix socket cannot cross between macOS and Docker's Linux VM.
        transport = 'tcp' if env.get('CAGED_HOST_OS', os.uname().sysname) == 'Darwin' else 'unix'
    args.host_bridge_transport = transport
    if args.host_bridge_port is None:
        args.host_bridge_port = 41000 + zlib.crc32(args.instance.encode()) % 1000
    args.host_bridge_port = integer(args.host_bridge_port, 'host_bridge.port', 1024, 65535)
    if args.host_bridge_address is not None:
        try:
            args.host_bridge_address = str(ipaddress.IPv4Address(str(args.host_bridge_address)))
        except ValueError:
            raise ConfigError('host_bridge.address must be an IPv4 address') from None
    if not isinstance(args.state_dir, (str, Path)) or not str(args.state_dir).strip():
        raise ConfigError('state.dir must be a directory path')
    folder = Path(args.state_dir).expanduser()
    base = args.settings.parent if args.sources['state_dir'] == 'config' else Path.cwd()
    folder = (folder if folder.is_absolute() else base / folder).resolve()
    if any(char in str(folder) for char in (',', ':', '\n', '\r')):
        raise ConfigError('state.dir cannot contain comma, colon or newlines')
    args.state_dir = folder
    try:
        url = urlsplit(args.dns_upstream)
        address = ipaddress.ip_address(url.hostname or '')
        if (url.scheme != 'https' or address.version != 4 or not address.is_global
                or url.username or url.password or url.fragment or url.query or not url.path
                or url.port in (53, 853)):
            raise ValueError()
    except ValueError:
        raise ConfigError('dns_upstream must be an HTTPS DoH URL with a public IPv4 host, path, and no credentials/query/fragment') from None
    return args


def runtime_policy(args):
    names = ('node', 'expected_country', 'dns_upstream', 'timezone', 'locale', 'language',
             'languages', 'controller_port', 'mixed_port', 'debug_port', 'browser_probe_port', 'claude_probe_port',
             'monitor_tick', 'monitor_interval', 'monitor_lease', 'monitor_probe_timeout',
             'monitor_retry_initial', 'monitor_retry_max', 'puid', 'pgid')
    result = {name: getattr(args, name) for name in names}
    # [address, port] of the host bridge's TCP listener, the gateway's only
    # direct (non-node) destination; None unless the tcp transport is active.
    result['host_bridge_tcp'] = getattr(args, 'host_bridge_tcp', None)
    return result
