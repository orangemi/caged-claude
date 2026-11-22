"""Open Bash, or run a command, inside an already-running managed container."""
import argparse
import sys
from .config import resolve
from .enter import exec_command, settings_flags, target


def command(argv=None, tty=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    separator = argv.index('--') if '--' in argv else len(argv)
    arguments = argv[separator + 1:]
    parser = argparse.ArgumentParser(prog='./cage shell', description=__doc__,
                                     epilog='Use -- before a command, for example: ./cage shell -- id')
    parser.add_argument('container', nargs='?', choices=('claude', 'browser', 'network'), default='claude')
    parser.add_argument('--root', action='store_true', help='use root inside the container')
    parser.add_argument('--instance', help='override the configured instance')
    parser.add_argument('--app-settings', help='use another application YAML')
    options = parser.parse_args(argv[:separator])
    args = resolve(['status', *settings_flags(options)])
    current = target(args.instance, options.container)
    default_shell = ['sh'] if options.container == 'network' else ['bash', '-l']
    return exec_command(current, arguments or default_shell,
                        root=options.root or options.container == 'network', tty=tty)
