"""PID 1: run the entrypoint, supervise its probe, reap orphans, forward signals.

Docker's --init cannot be used here: it bind-mounts /sbin/docker-init, and
with merged-usr /sbin is a symlink into /usr, which is the persisted state
mount and hides it. This file lives in the read-only /caged-runtime mount,
so it works for any seeded state that has python3.
"""
import os
from pathlib import Path
import signal
import sys
import time

FORWARDED = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT, signal.SIGUSR1, signal.SIGUSR2)


def spawn(argv, env=None):
    child = os.fork()
    if child == 0:
        try:
            os.execvpe(argv[0], argv, os.environ if env is None else env)
        finally:
            os._exit(127)
    return child


def send(pid, signum):
    if pid is not None:
        try:
            os.kill(pid, signum)
        except ProcessLookupError:
            pass


def main(argv, *, agent=None, ready=None):
    if not argv:
        print('usage: init.py [--agent claude] command [arguments...]', file=sys.stderr)
        return 2
    if agent:
        # /run can survive docker restart. Never observe the old ready marker
        # while the entrypoint is still resetting identity and system settings.
        ready.unlink(missing_ok=True)
    child = spawn(argv)
    probe = None
    next_probe = 0
    stopping = False

    def forward(signum, _frame):
        nonlocal stopping
        send(child, signum)
        if signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
            stopping = True
            send(probe, signum)

    for signum in FORWARDED:
        signal.signal(signum, forward)
    try:
        while True:
            try:
                # Reap exited children before considering another probe.
                pid, status = os.waitpid(-1, os.WNOHANG if agent else 0)
            except ChildProcessError:
                return 0
            if pid == child:
                code = os.waitstatus_to_exitcode(status)
                return code if code >= 0 else 128 - code
            if probe is not None and pid == probe:
                probe = None
                next_probe = time.monotonic() + 1
            if agent and probe is None and not stopping and ready.is_file() and time.monotonic() >= next_probe:
                # Only the root probe gets the immutable runtime import path;
                # leave the user's Python environment unchanged.
                probe = spawn(agent, {**os.environ,
                                      'PYTHONPATH': str(Path(__file__).resolve().parents[2]),
                                      'PYTHONSAFEPATH': '1'})
            if pid == 0:
                time.sleep(0.1)
    finally:
        # Exiting PID 1 ends the container. Also reap the probe when this init
        # is exercised outside a PID namespace, without waiting on other orphans.
        if probe is not None:
            send(probe, signal.SIGKILL)
            os.waitpid(probe, 0)


if __name__ == '__main__':
    arguments = sys.argv[1:]
    if arguments[:2] == ['--agent', 'claude']:
        sys.exit(main(arguments[2:],
                      agent=[sys.executable, '-m', 'caged.runtime.agents.environment', 'claude'],
                      ready=Path('/run/caged-claude-ready')))
    sys.exit(main(arguments))
