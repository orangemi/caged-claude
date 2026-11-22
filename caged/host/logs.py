"""Show the Docker logs of one running managed container."""
import argparse
from . import docker as engine
from .config import resolve
from .enter import settings_flags
from .errors import CageError
from .instance import Instance
from .paths import ROOT, LABEL


def command(argv):
    parser = argparse.ArgumentParser(prog='./cage logs', description=__doc__)
    parser.add_argument('container', nargs='?', choices=('network', 'browser', 'claude'), default='network',
                        help='default: network (gateway, checks and permit changes)')
    parser.add_argument('-f', '--follow', action='store_true', help='keep streaming new lines')
    parser.add_argument('-n', '--tail', default='200', help='lines to show from the end, or "all" (default: 200)')
    parser.add_argument('--instance', help='override the configured instance')
    parser.add_argument('--app-settings', help='use another application YAML')
    options = parser.parse_args(argv)
    args = resolve(['status', *settings_flags(options)])
    name = getattr(Instance(args.instance), options.container)
    current = engine.inspect(name)
    if current is None:
        raise CageError(f'{name} does not exist; containers are removed when they stop. Run ./cage up first.')
    if current['Config'].get('Labels', {}).get(LABEL) != str(ROOT):
        raise CageError('Refusing to read logs of an unmanaged container: ' + name)
    return ['docker', 'logs', '--tail', options.tail, *(['--follow'] if options.follow else []), current['Id']]
