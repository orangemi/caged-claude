"""Open Bash, or run a command, inside an already-running managed container."""
import argparse
import os
import sys
from .config import ConfigError, resolve
from .enter import target, exec_command
from .errors import CageError


def command(argv=None, tty=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    separator = argv.index('--') if '--' in argv else len(argv)
    arguments = argv[separator + 1:]
    parser = argparse.ArgumentParser(prog='./shell', description=__doc__,
                                     epilog='Use -- before a command, for example: ./shell -- id')
    parser.add_argument('container', nargs='?', choices=('claude', 'browser', 'network'), default='claude')
    parser.add_argument('--root', action='store_true', help='use root inside the container')
    parser.add_argument('--instance', help='override the configured instance')
    parser.add_argument('--app-settings', help='use another application YAML')
    options = parser.parse_args(argv[:separator])
    flags = []
    if options.instance is not None:
        flags += ['--instance', options.instance]
    if options.app_settings is not None:
        flags += ['--settings', options.app_settings]
    args = resolve(['status', *flags])
    current = target(args.instance, options.container)
    default_shell = ['sh'] if options.container == 'network' else ['bash', '-l']
    return exec_command(current, arguments or default_shell,
                        root=options.root or options.container == 'network', tty=tty)


if __name__ == '__main__':
    try:
        arguments = command()
        os.execvp(arguments[0], arguments)
    except (ConfigError, CageError, OSError) as error:
        print('Error: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
