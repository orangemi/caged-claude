"""Adapt the upstream desktop's account and service references at image build."""
from pathlib import Path
import pwd
import re
import subprocess
from caged.shared.policy import APP_USER

old = pwd.getpwnam('abc')
subprocess.run(['usermod', '-l', APP_USER, '-c', APP_USER, old.pw_name], check=True)
subprocess.run(['groupmod', '-n', APP_USER, 'abc'], check=True)

# These are upstream-owned service scripts and desktop defaults, not arbitrary
# libraries: replacing Python's unrelated `abc` module would corrupt programs.
pattern = re.compile(r'(?<![\w-])abc(?![\w-])')
roots = [Path('/etc/s6-overlay/s6-rc.d'), Path('/defaults'), Path('/etc/sudoers.d')]
paths = [Path('/etc/sudoers')]
for root in roots:
    paths.extend(root.rglob('*'))
changed = 0
for path in paths:
    if path.is_symlink() or not path.is_file():
        continue
    try:
        original = path.read_text()
    except UnicodeError:
        continue
    updated = pattern.sub(APP_USER, original)
    if updated != original:
        path.write_text(updated)
        changed += 1
for directory in ('/etc/crontabs', '/var/spool/cron/crontabs'):
    old_path = Path(directory) / 'abc'
    if old_path.exists():
        old_path.rename(old_path.with_name(APP_USER))

for service in ('init-adduser', 'svc-de', 'svc-selkies'):
    source = Path('/etc/s6-overlay/s6-rc.d') / service / 'run'
    assert APP_USER in source.read_text() and not pattern.search(source.read_text()), service
assert pwd.getpwnam(APP_USER).pw_dir == old.pw_dir
print(f'Adapted {changed} desktop files for {APP_USER}; runtime UID/GID remain configurable')
