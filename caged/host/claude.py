"""Run Claude in an existing managed container; leave its lifetime to start/stop."""
import argparse
import sys
from .config import resolve
from .enter import exec_command, settings_flags, target


def command(argv=None, tty=None):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--instance')
    parser.add_argument('--app-settings')
    options, arguments = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
    args = resolve(['status', *settings_flags(options)])
    return exec_command(target(args.instance, 'claude'), ['claude', *arguments], tty=tty)
