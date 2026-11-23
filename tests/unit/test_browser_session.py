"""Original browser chrome, hidden window buttons, and automatic recovery."""
import configparser
import json
from pathlib import Path
import runpy
import tempfile
import unittest

from caged.host import images
from caged.host.paths import ROOT

BROWSER = ROOT / 'containers' / 'browser'


class BrowserSession(unittest.TestCase):
    def test_image_blocks_desktop_and_selkies_shell_entry_points(self):
        dockerfile = (BROWSER / 'Dockerfile').read_text()
        for setting in ('HARDEN_DESKTOP=true', 'HARDEN_OPENBOX=true',
                        'RESTART_APP=false', 'NO_DECOR=true',
                        'DISABLE_WINDOW_BUTTONS=true',
                        'SELKIES_UI_SIDEBAR_SHOW_APPS=false',
                        'SELKIES_COMMAND_ENABLED=false'):
            self.assertIn(setting, dockerfile)

    def test_session_reopens_a_new_tab_without_kiosk_or_a_crash_screen(self):
        session = (BROWSER / 'session.sh').read_text()
        self.assertIn('while true', session)
        self.assertIn('chrome://newtab', session)
        self.assertIn('--disable-background-mode', session)
        self.assertNotIn('--kiosk', session)
        self.assertNotIn('caged-browser-crash-screen', session)

    def test_persisted_autostart_is_rewritten_on_every_start(self):
        settings = (BROWSER / 'init-settings.sh').read_text()
        self.assertIn('/usr/local/bin/caged-browser-session', settings)
        self.assertIn('session_dir/autostart', settings)
        self.assertIn('menu.xml', settings)

    def test_developer_tools_are_not_disabled_by_policy(self):
        self.assertFalse((BROWSER / 'kiosk-policy.json').exists())
        dns = json.loads((BROWSER / 'network-policy.json').read_text())
        self.assertEqual(dns, {'DnsOverHttpsMode': 'off', 'BuiltInDnsClientEnabled': False})

    def test_browser_files_are_part_of_the_image_inputs(self):
        inputs = {path.relative_to(ROOT) for path in images.input_files('browser')}
        self.assertIn(Path('containers/browser/session.sh'), inputs)
        self.assertIn(Path('containers/browser/configure-browser.py'), inputs)
        self.assertNotIn(Path('containers/browser/crash.html'), inputs)

    def test_native_frame_and_gtk_layout_preserve_existing_settings(self):
        configure = runpy.run_path(str(BROWSER / 'configure-browser.py'))['configure_browser']
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            existing = home / '.config/chromium/Profile 1/Preferences'
            existing.parent.mkdir(parents=True)
            preferences = {'browser': {'custom_chrome_frame': False, 'other': 42},
                           'session': {'restore_on_startup': 1}}
            existing.write_text(json.dumps(preferences))
            settings = home / '.config/gtk-3.0/settings.ini'
            settings.parent.mkdir(parents=True)
            settings.write_text('[Settings]\ngtk-theme-name=Adwaita\ngtk-decoration-layout=minimize,maximize,close\n')
            configure(home)
            preferences['browser']['custom_chrome_frame'] = True
            self.assertEqual(json.loads(existing.read_text()), preferences)
            self.assertTrue(json.loads((home / '.config/chromium/Default/Preferences').read_text())
                            ['browser']['custom_chrome_frame'])
            parsed = configparser.ConfigParser()
            parsed.read(settings)
            self.assertEqual(parsed['Settings']['gtk-decoration-layout'], ':')
            self.assertEqual(parsed['Settings']['gtk-theme-name'], 'Adwaita')
            configure(home)
            self.assertEqual(json.loads(existing.read_text()), preferences)
