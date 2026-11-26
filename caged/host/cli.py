"""Launcher actions behind ./cage, without importing container services."""
import json
import os
import signal
import subprocess
import sys
from . import dialer, docker as engine, hostbridge, images, proxy, stateful
from .config import ConfigError, resolve
from .errors import CageError
from .instance import Instance
from .paths import ROOT, LABEL, LEGACY_LABEL, IN_LAUNCHER
from .session import Session


def gate_command(container, *arguments):
    return ['exec', container['Id'], 'python3', '-m', 'caged.runtime.network.lease', *arguments]


def warn_legacy():
    """Point at containers this project started under its old caged-browser name."""
    result = engine.docker('ps', '-aq', '--filter', f'label={LEGACY_LABEL}={ROOT}', check=False)
    if result.returncode == 0 and result.stdout.split():
        print('Notice: containers from the old caged-browser layout are still present and may hold the UI port. '
              'Remove them with: docker rm -f ' + ' '.join(result.stdout.split()), file=sys.stderr)


def show_status(instance, args=None):
    gateway = engine.inspect(instance.network)
    network_up = bool(gateway and gateway['State']['Running'])
    for name in instance.names:
        current = engine.inspect(name)
        if name == instance.check and current is None:
            continue
        state = current['State']['Status'] if current else 'absent'
        if name in (instance.browser, instance.claude) and current and current['State']['Running']:
            if not network_up or current['HostConfig']['NetworkMode'] != 'container:' + gateway['Id']:
                state += ' (gateway unavailable; outbound blocked)'
        print(name + ': ' + state)
    if network_up and gateway['Config'].get('Labels', {}).get(LABEL) == str(ROOT):
        result = engine.docker(*gate_command(gateway, 'status'), check=False)
        if result.returncode == 0:
            observed = json.loads(result.stdout)
            print('network permit: ' + observed['state'] + '; remaining=' + str(observed['lease_remaining']) + 's; ' + observed['reason'])
        if args is not None:
            print('node address: ' + pinned_status(gateway, args))
    if args is not None:
        state = stateful.open_state(args)
        print('environment: ' + state.describe())
        print('host bridge: ' + hostbridge.status(args, state))
        print('host dialer: ' + dialer.describe(instance.name))


def pinned_status(gateway, args):
    """Whether the node hostname still resolves to an address the gateway pinned.

    The gateway firewall only allows the addresses pinned at start, so a node that
    moved needs a restart to be re-resolved."""
    result = engine.docker('exec', gateway['Id'], 'cat', '/run/caged/ready.json', check=False)
    try:
        pinned = json.loads(result.stdout).get('pinned') if result.returncode == 0 else None
    except ValueError:
        pinned = None
    if not pinned:
        return 'unknown (gateway started by an older version)'
    addresses = ', '.join(pinned['addresses'])
    if not pinned.get('host'):
        return addresses + ' (fixed in the node file)'
    try:
        current = [str(value) for value in proxy.lookup(
            pinned['host'], list(dict.fromkeys([*proxy.source_resolvers(args.proxy_file_path), *args.bootstrap_dns])))]
    except CageError as error:
        return f'{addresses} (pinned; could not re-check: {error})'
    if set(current) & set(pinned['addresses']):
        return addresses + ' (pinned; still current)'
    return (f'{addresses} pinned, but the node hostname now resolves to {", ".join(current)}. '
            'Run ./cage down && ./cage up to use the new address.')


def main(args):
    instance = Instance(args.instance)
    if args.action == 'nodes':
        for node in proxy.load_node(args.proxy_file_path):
            print(f'{node["name"]}\t{node["type"]}\tUDP={node.get("udp", False)}')
        return
    if args.action == 'build':
        for role in images.IMAGES:
            images.build(role, force=True)
        return
    if args.action == 'test':
        images.build('network')
        environment = dict(os.environ)
        if IN_LAUNCHER:
            # Tests bind-mount their temporary directories into containers; the
            # daemon resolves those paths on the host, so they must live in a
            # directory the launcher shares with it rather than its own /tmp.
            shared = ROOT / 'state' / '.tmp'
            shared.mkdir(parents=True, exist_ok=True, mode=0o700)
            environment['TMPDIR'] = str(shared)
        result = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', str(ROOT / 'tests'),
                                 '-t', str(ROOT), '-p', 'test_*.py', '-v'], env=environment)
        if result.returncode:
            raise CageError('Local network-boundary test failed')
        return
    if args.action == 'stop':
        for name in instance.shutdown_order:
            engine.stop_managed(name)
        dialer.unregister(instance.name)  # an idle dialer exits by itself
        state = stateful.open_state(args)
        # An image is unmounted with the last container; detach it so the file
        # is clean to copy. Directories are flushed now rather than later.
        state.release()
        os.sync()
        hostbridge.write_plan(args, state, 'stop')
        return
    if args.action == 'status':
        warn_legacy()
        show_status(instance, args)
        return
    if args.action in ('pause', 'resume'):
        current = engine.inspect(instance.network)
        if not current or not current['State']['Running'] or current['Config'].get('Labels', {}).get(LABEL) != str(ROOT):
            raise CageError('Managed network container is not running')
        print(engine.docker(*gate_command(current, args.action)).stdout.strip())
        return
    if args.action == 'start':
        warn_legacy()
    if args.action == 'audit':
        gateway = engine.inspect(instance.network)
        if gateway and gateway['State']['Running']:
            # The running instance: only this audit's own probe container is stopped afterwards.
            session = Session(args, detached=True)
            try:
                session.audit(live=True)
            finally:
                session.close()
            return
    session = Session(args, detached=args.action == 'start')
    keep_running = False
    try:
        session.start_network()
        if args.action == 'verify':
            session.verify()
        elif args.action == 'audit':
            session.audit()
        else:
            session.start_clients()
            if args.action == 'start':
                keep_running = True
                # The listener runs on the host; ./cage starts it from this plan.
                hostbridge.write_plan(args, session.state, 'start' if args.host_bridge else 'stop')
                print(f'Started network, browser and Claude in daemon mode. Open {args.ui_scheme}://localhost:{args.port}; use ./cage claude to enter Claude, ./cage down to stop.', flush=True)
    finally:
        if not keep_running:
            session.close()


def cli(argv=None):
    interrupted = False

    def handle_stop(_signum, _frame):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                signal.signal(signum, signal.SIG_IGN)
            raise KeyboardInterrupt

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, handle_stop)
    try:
        main(resolve(argv))
    except KeyboardInterrupt:
        print('\nStopped; containers owned by this launcher have been cleaned up.')
    except (ConfigError, CageError, OSError, subprocess.TimeoutExpired) as error:
        print(f'Error: {error}', file=sys.stderr)
        raise SystemExit(1)

