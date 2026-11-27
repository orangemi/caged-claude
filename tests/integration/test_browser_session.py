"""Opt-in Docker regression for original browser chrome and new-tab recovery.

Set CAGED_BROWSER_TEST_IMAGE to a built browser image. The existing network
image supplies the production CDP check (override CAGED_BROWSER_TEST_PROBE_IMAGE
if needed). Containers use no proxy, published ports, host mounts or user data.
"""
import json
import os
import subprocess
import time
import unittest
import uuid

from caged.host.config import resolve, runtime_policy
from caged.host.paths import ROOT
from caged.shared.policy import POLICY_ENV


@unittest.skipUnless(os.environ.get('CAGED_BROWSER_TEST_IMAGE'),
                     'Set CAGED_BROWSER_TEST_IMAGE to a built browser image')
class BrowserRecoveryRuntime(unittest.TestCase):
    def docker(self, *args, check=True, timeout=15):
        return subprocess.run(['docker', *args], check=check, capture_output=True,
                              text=True, timeout=timeout)

    def setUp(self):
        self.name = 'caged-browser-test-' + uuid.uuid4().hex[:12]
        self.addCleanup(self.docker, 'rm', '-f', '-v', self.name, check=False)
        self.docker('run', '-d', '--name', self.name, '--network', 'none',
                    '--shm-size=512m', '-e', 'TZ=Asia/Singapore',
                    '-e', 'LC_ALL=en_US.UTF-8', '-e', 'LANG=en_US.UTF-8',
                    '-e', 'CHROME_CLI=--lang=en-US --accept-lang=en-US,en '
                          '--remote-debugging-address=127.0.0.1 --remote-debugging-port=9222',
                    os.environ['CAGED_BROWSER_TEST_IMAGE'])

    def inside(self, *command, check=True):
        return self.docker('exec', '-e', 'DISPLAY=:1', self.name, *command, check=check)

    def windows(self):
        result = self.inside('xdotool', 'search', '--onlyvisible', '--class', 'chromium', check=False)
        self.assertIn(result.returncode, (0, 1), result.stderr)
        return result.stdout.split()

    def pages(self):
        result = self.inside('python3', '-c',
            "import json,urllib.request; print(json.dumps([p for p in json.load(urllib.request.urlopen('http://127.0.0.1:9222/json/list', timeout=1)) if p['type']=='page']))", check=False)
        return json.loads(result.stdout) if result.returncode == 0 else []

    def browser_pid(self):
        return self.inside('pgrep', '-f',
            '^/usr/lib/chromium/chromium --show-component-extension-options', check=False).stdout.strip()

    def wait_for(self, predicate, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.2)
        self.fail('Browser recovery timed out: ' +
                  self.docker('logs', '--tail', '30', self.name, check=False).stdout)

    def assert_usable(self, previous_pid=None):
        self.wait_for(lambda: self.browser_pid() not in ('', previous_pid))
        window = self.wait_for(self.windows)[0]
        pages = self.wait_for(self.pages)
        self.assertEqual(len(pages), 1)
        self.assertTrue(pages[0]['url'].startswith('chrome://new'), pages[0]['url'])
        state = self.inside('xprop', '-id', window, '_NET_WM_STATE').stdout
        self.assertNotIn('_NET_WM_STATE_FULLSCREEN', state)
        extents = self.inside('xprop', '-id', window, '_NET_FRAME_EXTENTS').stdout
        self.assertEqual([int(x) for x in extents.split('=')[1].split(',')], [0, 0, 0, 0])
        gtk = "import gi; gi.require_version('Gtk','3.0'); from gi.repository import Gtk; print(Gtk.Settings.get_default().get_property('gtk-decoration-layout'))"
        self.assertEqual(self.inside('python3', '-c', gtk).stdout.strip(), ':')
        self.inside('xdotool', 'windowactivate', '--sync', window)
        self.inside('xdotool', 'key', '--clearmodifiers', 'ctrl+l')
        self.inside('xdotool', 'type', '--clearmodifiers', '--delay', '0',
                    'data:text/html,<title>Recovered browser works</title><h1>Usable browser</h1>')
        self.inside('xdotool', 'key', 'Return')
        self.wait_for(lambda: 'Recovered browser works' in
                      self.inside('xdotool', 'getwindowname', window).stdout)
        return self.browser_pid()

    def test_last_tab_last_window_and_crash_reopen_a_usable_new_tab(self):
        pid = self.assert_usable()
        options = runtime_policy(resolve(['--settings', str(ROOT / 'config.example/app.yaml'),
                                          '--timezone', 'Asia/Singapore'], environ={}))
        probe = self.docker('run', '--rm', '--network', 'container:' + self.name,
                           '-e', POLICY_ENV + '=' + json.dumps(options), '--entrypoint', 'python3',
                           os.environ.get('CAGED_BROWSER_TEST_PROBE_IMAGE', 'caged-claude-network:local'),
                           '-m', 'caged.runtime.checks.browser', 'settings', timeout=45)
        self.assertEqual(json.loads(probe.stdout)['language'], 'en-US')
        for tool in ('xterm', 'sudo', 'xdg-open'):
            self.assertNotEqual(self.inside('test', '-x', '/usr/bin/' + tool, check=False).returncode, 0)
        self.assertEqual(self.inside('cat',
            '/run/s6/container_environment/SELKIES_COMMAND_ENABLED').stdout.strip(), 'false')

        self.inside('xdotool', 'key', '--clearmodifiers', 'ctrl+t')
        self.wait_for(lambda: len(self.pages()) == 2)
        self.inside('xdotool', 'key', '--clearmodifiers', 'ctrl+n')
        self.wait_for(lambda: len(self.windows()) == 2)
        self.inside('xdotool', 'key', '--clearmodifiers', 'ctrl+shift+w')
        self.wait_for(lambda: len(self.windows()) == 1)
        self.assertEqual(self.browser_pid(), pid)
        self.inside('xdotool', 'windowactivate', '--sync', self.windows()[0])
        self.inside('xdotool', 'key', '--clearmodifiers', 'ctrl+w')
        self.wait_for(lambda: len(self.pages()) == 1)
        self.assertEqual(self.browser_pid(), pid)

        with self.subTest(recovery='last tab'):
            self.inside('xdotool', 'key', '--clearmodifiers', 'ctrl+w')
            pid = self.assert_usable(pid)
        with self.subTest(recovery='last window'):
            self.inside('xdotool', 'key', '--clearmodifiers', 'ctrl+shift+w')
            pid = self.assert_usable(pid)
        with self.subTest(recovery='process crash'):
            self.inside('pkill', '-KILL', '-x', 'chromium')
            self.assert_usable(pid)
