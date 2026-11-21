"""Dispatch the four host entry points without importing container services."""
import json
import signal
import subprocess
import sys
from . import docker as engine, images, proxy
from .config import ConfigError, resolve
from .errors import CageError
from .instance import Instance
from .paths import ROOT, LABEL, LAYOUT_LABEL, LAYOUT_VERSION
from .session import Session


def gate_command(container, *arguments):
    # Keep status/pause/resume usable for instances created before this layout
    # migration. New containers only use the packaged runtime.
    labels = container['Config'].get('Labels', {})
    program = ['-m', 'caged.runtime.network.lease'] if labels.get(LAYOUT_LABEL) == LAYOUT_VERSION else ['/app/lease_gate.py']
    return ['exec', container['Id'], 'python3', *program, *arguments]


def show_status(instance):
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
        result = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', str(ROOT / 'tests'),
                                 '-t', str(ROOT), '-p', 'test_*.py', '-v'])
        if result.returncode:
            raise CageError('Local network-boundary test failed')
        return
    if args.action == 'stop':
        for name in instance.shutdown_order:
            engine.stop_managed(name)
        return
    if args.action == 'status':
        show_status(instance)
        return
    if args.action in ('pause', 'resume'):
        current = engine.inspect(instance.network)
        if not current or not current['State']['Running'] or current['Config'].get('Labels', {}).get(LABEL) != str(ROOT):
            raise CageError('Managed network container is not running')
        print(engine.docker(*gate_command(current, args.action)).stdout.strip())
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
                print(f'Started network, browser and Claude in daemon mode. Open https://localhost:{args.port}; use ./claude to enter Claude, ./stop to stop.', flush=True)
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


if __name__ == '__main__':
    cli()
