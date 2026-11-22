"""The browser image is a kiosk: no desktop, terminal, or closable window."""
import json
from pathlib import Path
import unittest
from caged.host import images
from caged.host.paths import ROOT

BROWSER = ROOT / 'containers' / 'browser'


class BrowserKiosk(unittest.TestCase):
    def test_image_locks_down_window_controls_and_selkies_ui(self):
        dockerfile = (BROWSER / 'Dockerfile').read_text()
        for setting in ('HARDEN_DESKTOP=true', 'HARDEN_OPENBOX=true',
                        'RESTART_APP=false', 'NO_DECOR=true',
                        'DISABLE_WINDOW_BUTTONS=true',
                        'SELKIES_UI_SHOW_SIDEBAR="false|locked"',
                        'SELKIES_ENABLE_SHARING=false',
                        'SELKIES_ENABLE_CLIPBOARD="false|locked"',
                        'SELKIES_FILE_TRANSFERS=none'):
            self.assertIn(setting, dockerfile)

    def test_closed_browser_is_replaced_by_the_crash_page(self):
        session = (BROWSER / 'session.sh').read_text()
        self.assertIn('/usr/bin/wrapped-chromium --kiosk', session)
        self.assertIn('file:///usr/share/caged/crash.html', session)
        self.assertIn('while true', session)

    def test_persisted_autostart_is_rewritten_on_every_start(self):
        settings = (BROWSER / 'init-settings.sh').read_text()
        self.assertIn('/usr/local/bin/caged-browser-session', settings)
        self.assertIn('.config/openbox', settings)
        self.assertIn('session_dir/autostart', settings)
        self.assertIn('menu.xml', settings)

    def test_developer_tools_are_not_disabled_by_policy(self):
        # DevTools stay available. The only managed policy is the DNS policy,
        # which the monitor compares exactly.
        self.assertFalse((BROWSER / 'kiosk-policy.json').exists())
        dns = json.loads((BROWSER / 'network-policy.json').read_text())
        self.assertEqual(dns, {'DnsOverHttpsMode': 'off', 'BuiltInDnsClientEnabled': False})

    def test_kiosk_files_are_part_of_the_image_inputs(self):
        inputs = {path.relative_to(ROOT) for path in images.input_files('browser')}
        self.assertIn(Path('containers/browser/session.sh'), inputs)
        self.assertIn(Path('containers/browser/crash.html'), inputs)
        self.assertIn(Path('containers/browser/init-settings.sh'), inputs)
