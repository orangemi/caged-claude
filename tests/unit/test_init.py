"""The Claude container's PID 1 replacement for docker --init."""
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest

INIT = Path(__file__).resolve().parents[2] / 'caged' / 'runtime' / 'init.py'


class Init(unittest.TestCase):
    def run_init(self, *command, **kwargs):
        return subprocess.run([sys.executable, str(INIT), *command], capture_output=True, text=True, timeout=10, **kwargs)

    def test_exit_status_of_the_command_is_kept(self):
        self.assertEqual(self.run_init('sh', '-c', 'exit 3').returncode, 3)
        self.assertEqual(self.run_init('sh', '-c', 'echo ok').stdout, 'ok\n')
        self.assertEqual(self.run_init('/does/not/exist').returncode, 127)

    def test_termination_is_forwarded_to_the_command(self):
        process = subprocess.Popen([sys.executable, str(INIT), 'sh', '-c', 'trap "exit 7" TERM; while :; do sleep 0.05; done'])
        time.sleep(0.5)
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=5), 7)

    def test_a_command_killed_by_a_signal_reports_128_plus_signal(self):
        self.assertEqual(self.run_init('sh', '-c', 'kill -KILL $$').returncode, 128 + signal.SIGKILL)

    def test_orphans_do_not_keep_it_alive_after_the_command_exits(self):
        started = time.monotonic()
        result = subprocess.run([sys.executable, str(INIT), 'sh', '-c', '(sleep 30 &); exit 0'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertLess(time.monotonic() - started, 5)

if __name__ == '__main__':
    unittest.main()
