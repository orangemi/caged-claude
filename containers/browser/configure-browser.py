#!/usr/bin/python3
"""Keep Chromium tabs and its native frame, without window control buttons."""
import configparser
import json
import os
from pathlib import Path
import tempfile


def configure_browser(home):
    root = home / ".config" / "chromium"
    # Undo the previous system-title-bar preference without touching user data.
    paths = set(root.glob('*/Preferences')) | {root / 'Default' / 'Preferences'}
    for path in paths:
        preferences = json.loads(path.read_text()) if path.exists() else {}
        preferences.setdefault('browser', {})['custom_chrome_frame'] = True
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as target:
            temporary = Path(target.name)
            try:
                json.dump(preferences, target)
                target.close()
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)

    # Chromium reads this GTK setting for its client-side frame buttons. An
    # empty left/right layout removes controls without hiding tabs or the URL.
    for version in ('3.0', '4.0'):
        path = home / '.config' / ('gtk-' + version) / 'settings.ini'
        settings = configparser.ConfigParser(interpolation=None)
        if path.exists():
            settings.read(path)
        if not settings.has_section('Settings'):
            settings.add_section('Settings')
        settings.set('Settings', 'gtk-decoration-layout', ':')
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('w') as target:
            settings.write(target)


if __name__ == '__main__':
    configure_browser(Path.home())
