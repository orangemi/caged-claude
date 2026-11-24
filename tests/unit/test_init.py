"""The Claude container's PID 1 replacement for docker --init."""
from pathlib import Path
import json
import os
import signal
import subprocess
import sys
import tempfile
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

    def test_probe_waits_for_fresh_initialization_and_recovers_after_a_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ready, release, pids = (root / name for name in ('ready', 'release', 'pids'))
            ready.write_text('stale from before docker restart')
            command = [sys.executable, '-c',
                       'from pathlib import Path; import time; '
                       f'release=Path({str(release)!r}); ready=Path({str(ready)!r}); '
                       'exec("while not release.exists(): time.sleep(0.05)"); '
                       'ready.write_text("initialized"); time.sleep(60)']
            agent = [sys.executable, '-c',
                     'from pathlib import Path; import os,time; '
                     f'p=Path({str(pids)!r}); '
                     'f=p.open("a"); f.write(str(os.getpid())+"\\n"); f.close(); time.sleep(60)']
            driver = ('from pathlib import Path; from caged.runtime.init import main; import json,sys; '
                      'sys.exit(main(json.loads(sys.argv[1]), agent=json.loads(sys.argv[2]), ready=Path(sys.argv[3])))')
            process = subprocess.Popen([sys.executable, '-c', driver, json.dumps(command), json.dumps(agent), str(ready)],
                                       start_new_session=True)
            try:
                self.wait_for(lambda: not ready.exists())
                time.sleep(0.2)
                self.assertFalse(pids.exists(), 'A stale marker must not start a probe during initialization')
                release.touch()
                self.wait_for(pids.exists)
                first = int(pids.read_text().strip())
                os.kill(first, signal.SIGKILL)
                self.wait_for(lambda: len(pids.read_text().splitlines()) == 2)
                second = int(pids.read_text().splitlines()[1])
                self.assertNotEqual(first, second)
                process.terminate()
                self.assertEqual(process.wait(timeout=5), 128 + signal.SIGTERM)
                with self.assertRaises(ProcessLookupError):
                    os.kill(second, 0)
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)

    def wait_for(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail('Init lifecycle timed out')

if __name__ == '__main__':
    unittest.main()
