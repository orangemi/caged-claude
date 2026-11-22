"""Host-side state of one instance's Linux environment (docs/stateful-service.md).

Every mutable directory of the Claude container is a bind mount of a host
directory under state/<instance>/, seeded once from the image. Writes land on
host files immediately, so containers stay disposable and the environment
survives Docker itself being reinstalled. This module owns the layout, the
marker that ties a directory to its instance, seeding and the mount list.
"""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import time
import uuid
from . import docker as engine
from .errors import CageError
from .paths import ROOT

LAYOUT = 1
MARKER = '.caged-state'
LOCK = 'lifecycle.lock'
# Persisted system directories; merged-usr makes /bin, /sbin and /lib
# symlinks into /usr, so /usr covers every system binary.
SYSTEM = ('etc', 'usr', 'var', 'opt', 'srv', 'root')
HOME = '/home/claude'
CLAUDE_STATE = HOME + '/.claude'
RUNTIME = '/caged-runtime'
REGISTRY = ROOT / 'config' / 'instances'
# Files Docker generates inside /etc for every container.
DOCKER_MANAGED = ('etc/hosts', 'etc/hostname', 'etc/resolv.conf')


class StateError(CageError):
    pass


class InstanceState:
    def __init__(self, state_dir, instance):
        self.instance = instance
        self.root = Path(state_dir) / instance

    @property
    def system(self):
        return self.root / 'system'

    @property
    def home(self):
        return self.root / 'home'

    @property
    def browser(self):
        return self.root / 'browser'

    @property
    def registry(self):
        return REGISTRY / (self.instance + '.json')

    def marker(self):
        try:
            return json.loads((self.root / MARKER).read_text())
        except FileNotFoundError:
            return None
        except ValueError:
            raise StateError(f'State marker {self.root / MARKER} is unreadable; refusing to touch this environment.') from None

    def registered(self):
        try:
            return json.loads(self.registry.read_text())
        except FileNotFoundError:
            return None

    def validate(self):
        """Refuse to reuse, or silently replace, a missing or foreign state.

        Returns the marker, or None for an instance that was never seeded."""
        marker, registered = self.marker(), self.registered()
        if marker is not None:
            if marker.get('instance') != self.instance or marker.get('layout') != LAYOUT:
                raise StateError(f'{self.root} belongs to another instance or layout; refusing to use it.')
            if registered is not None and registered.get('id') != marker.get('id'):
                raise StateError(f'{self.root} is not the environment registered for {self.instance} '
                                 f'(expected id {registered.get("id")}); refusing to use it.')
            return marker
        if registered is not None:
            raise StateError(f'Environment storage for {self.instance} is unavailable: {self.root} has no state '
                             'marker. Reconnect the disk or fix state.dir; it will not be re-created over.')
        unexpected = [entry.name for entry in self.root.iterdir()
                      if entry.name not in (LOCK, 'browser') and not entry.name.startswith(('.seed-', '.probe-'))] \
            if self.root.is_dir() else []
        if unexpected:
            raise StateError(f'{self.root} has content but no state marker ({", ".join(sorted(unexpected))}); '
                             'refusing to seed over it.')
        return None

    @contextlib.contextmanager
    def locked(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(self.root / LOCK, 'a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def ensure(self, image):
        """Validate the state, seeding it from image on first use."""
        with self.locked():
            if self.validate() is not None:
                return
            self._remove_leftovers(image)
            self._probe(image)
            self._seed(image)

    def prepare_browser(self):
        self.validate()
        self.browser.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.browser, 0o700)

    def claude_mounts(self, credentials):
        """Destination -> source for the Claude container, in mount order."""
        mounts = {'/' + name: str(self.system / name) for name in SYSTEM}
        mounts[HOME] = str(self.home)
        mounts[CLAUDE_STATE] = str(credentials)
        return mounts

    def _run(self, script, image, timeout=900, **environment):
        flags = [item for key, value in environment.items() for item in ('-e', f'{key}={value}')]
        return engine.call(['docker', 'run', '--rm', '--network', 'none', '--user', '0:0', '--entrypoint', '/bin/sh',
                            '--mount', f'type=bind,src={self.root},dst=/state', *flags, image, '-c', script],
                           timeout=timeout)

    def _remove_leftovers(self, image):
        if any(entry.name.startswith(('.seed-', '.probe-')) for entry in self.root.iterdir()):
            # Interrupted seeds hold root-owned files the launcher cannot delete.
            self._run('rm -rf /state/.seed-* /state/.probe-*', image, timeout=300)

    def _probe(self, image):
        """Check that the host filesystem keeps ownership, setuid and links."""
        script = '''set -e
d=/state/.probe-$PROBE
mkdir "$d"
touch "$d/file"
chown 12345:23456 "$d/file"
chmod 4755 "$d/file"
ln "$d/file" "$d/hard"
ln -s file "$d/soft"
result="$(stat -c '%u:%g:%a:%h' "$d/file") $(readlink "$d/soft")"
rm -rf "$d"
[ "$result" = "12345:23456:4755:2 file" ] || { echo "$result"; exit 3; }'''
        try:
            self._run(script, image, timeout=60, PROBE=uuid.uuid4().hex)
        except CageError:
            raise StateError(f'The filesystem holding {self.root} does not keep Linux ownership, setuid bits or '
                             'links, so the environment cannot be persisted there. Use a Linux (e.g. ext4) '
                             'directory for state.dir.') from None

    def _seed(self, image):
        seed_id = uuid.uuid4().hex
        excludes = ' '.join(f'--exclude=./{path}' for path in DOCKER_MANAGED)
        script = f'''set -e
d=/state/.seed-$SEED
mkdir -p "$d/system" "$d/home"
cd / && tar --numeric-owner --xattrs {excludes} -cpf - {' '.join('./' + name for name in SYSTEM)} \\
    | tar -C "$d/system" --numeric-owner --xattrs -xpf -
cd {HOME} && tar --numeric-owner --xattrs -cpf - . | tar -C "$d/home" --numeric-owner --xattrs -xpf -
sync'''
        print(f'Creating the persistent environment in {self.root} (first start only)...', flush=True)
        self._run(script, image, SEED=seed_id)
        staged = self.root / ('.seed-' + seed_id)
        # Renames are atomic; the marker is written last, so an interrupted
        # seed leaves only a .seed-* directory that the next start removes.
        os.rename(staged / 'system', self.system)
        os.rename(staged / 'home', self.home)
        staged.rmdir()
        marker = {'instance': self.instance, 'id': seed_id, 'layout': LAYOUT, 'image': image,
                  'created': time.strftime('%Y-%m-%dT%H:%M:%S%z')}
        write_private(self.root / MARKER, marker)
        REGISTRY.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_private(self.registry, {'state': str(self.root), 'id': seed_id})


def write_private(path, value):
    temporary = path.with_name(path.name + '.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        json.dump(value, handle, indent=2)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    os.rename(temporary, path)


def adopt_legacy_credentials(args, default_instance):
    """Move the pre-stateful config/.claude to the default instance's directory."""
    legacy = ROOT / 'config' / '.claude'
    target = Path(args.claude_config_dir)
    if (args.sources.get('claude_config_dir') == 'default' and args.instance == default_instance
            and legacy.is_dir() and not target.exists()):
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.rename(legacy, target)
        print(f'Moved Claude credentials from {legacy} to {target}.', flush=True)
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
