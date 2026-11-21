"""Run Claude in an existing managed container; leave its lifetime to start/stop."""
import argparse
import os
import sys
from .enter import target, exec_command
from .errors import CageError
from .config import ConfigError, resolve


def command(argv=None, tty=None):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--instance')
    parser.add_argument('--app-settings')
    options, arguments = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
    flags = []
    if options.instance is not None:
        flags += ['--instance', options.instance]
    if options.app_settings is not None:
        flags += ['--settings', options.app_settings]
    args = resolve(['status', *flags])
    return exec_command(target(args.instance, 'claude'), ['claude', *arguments], tty=tty)


if __name__ == '__main__':
    try:
        args = command()
        os.execvp(args[0], args)
    except (ConfigError, CageError, OSError) as error:
        print('Error: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
