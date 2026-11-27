"""Dispatch ./cage subcommands. The shell script handles help and init itself."""
import os
import sys
from . import claude, cli, logs, shell, ui
from .config import ConfigError
from .errors import CageError

# Subcommand -> launcher action; arguments after it are passed through.
ACTIONS = {'up': 'start', 'down': 'stop', 'status': 'status', 'nodes': 'nodes',
           'pause': 'pause', 'resume': 'resume', 'build': 'build'}
CHECKS = ('test', 'verify', 'smoke', 'audit')
# Subcommands that replace this process with a docker exec/logs command.
EXEC = {'claude': claude.command, 'shell': shell.command, 'logs': logs.command}


def fail(message):
    ui.error(message)
    raise SystemExit(2)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        fail('missing subcommand; run ./cage help')
    name, rest = argv[0], argv[1:]
    sys.argv[0] = './cage ' + name  # argparse shows this as the program name
    if name in ACTIONS:
        return cli.cli([ACTIONS[name], *rest])
    if name == 'check':
        if not rest or rest[0] not in CHECKS:
            fail('usage: ./cage check {' + ','.join(CHECKS) + '} [options]')
        sys.argv[0] = './cage check ' + rest[0]
        return cli.cli(rest)
    if name in EXEC:
        try:
            arguments = EXEC[name](rest)
            os.execvp(arguments[0], arguments)
            return None  # not reached: execvp replaces this process
        except (ConfigError, CageError, OSError) as error:
            ui.error(error)
            raise SystemExit(1)
    fail(f'unknown subcommand {name!r}; run ./cage help')


if __name__ == '__main__':
    main()
