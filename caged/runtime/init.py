"""PID 1 of the Claude container: run the entrypoint, reap orphans, forward signals.

Docker's --init cannot be used here: it bind-mounts /sbin/docker-init, and
with merged-usr /sbin is a symlink into /usr, which is the persisted state
mount and hides it. This file lives in the read-only /caged-runtime mount,
so it works for any seeded state that has python3.
"""
import os
import signal
import sys

FORWARDED = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT, signal.SIGUSR1, signal.SIGUSR2)


def main(argv):
    if not argv:
        print('usage: init.py command [arguments...]', file=sys.stderr)
        return 2
    child = os.fork()
    if child == 0:
        try:
            os.execvp(argv[0], argv)
        finally:
            os._exit(127)

    def forward(signum, _frame):
        try:
            os.kill(child, signum)
        except ProcessLookupError:
            pass

    for signum in FORWARDED:
        signal.signal(signum, forward)
    while True:
        try:
            pid, status = os.wait()  # reaps every child, including re-parented orphans
        except ChildProcessError:
            return 0
        if pid == child:
            code = os.waitstatus_to_exitcode(status)
            return code if code >= 0 else 128 - code


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
