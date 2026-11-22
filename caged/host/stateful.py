"""Host-side state of one instance's Linux environment (docs/stateful-service.md).

Every mutable directory of the Claude container is mounted from the
instance's state, seeded once from the image. Writes land on host storage
immediately, so containers stay disposable and the environment survives
Docker itself being reinstalled. Two backends hold the same layout:

- dir: plain directories under state/<instance>/, bind-mounted. Needs a
  Linux filesystem (Linux, WSL ext4).
- image: one ext4 image file, state/<instance>/disk.img, attached to a loop
  device in the Docker VM by a short-lived privileged helper and served as a
  local volume. Works where host files cannot keep Linux ownership (macOS).

This module owns the layout, the marker that ties state to its instance,
seeding and the mount list.
"""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import time
import uuid
from . import docker as engine, images
from .errors import CageError
from .paths import ROOT, LABEL

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
BACKENDS = ('dir', 'image')
DISK = 'disk.img'
# Apparent size of a new image; the file is sparse and grows with use.
DEFAULT_SIZE = '64G'
# Detach every loop device backed by $DISK.
DETACH = 'for d in $(losetup -j "$DISK" | cut -d: -f1); do losetup -d "$d"; done'


def host_os():
    return os.environ.get('CAGED_HOST_OS', os.uname().sysname)


class StateError(CageError):
    pass


class Mount:
    """One container mount: a host path (bind) or part of a volume."""

    def __init__(self, destination, source, volume=False, subpath=None, readonly=False):
        self.destination, self.source, self.volume = destination, str(source), volume
        self.subpath, self.readonly = subpath, readonly

    def argument(self):
        value = f'type={"volume" if self.volume else "bind"},src={self.source},dst={self.destination}'
        if self.subpath:
            value += ',volume-subpath=' + self.subpath
        return value + (',readonly' if self.readonly else '')


def read_marker(root):
    try:
        return json.loads((Path(root) / MARKER).read_text())
    except FileNotFoundError:
        return None
    except ValueError:
        raise StateError(f'State marker {Path(root) / MARKER} is unreadable; refusing to touch this environment.') from None


def open_state(args):
    """The instance's state in the backend it was created with.

    An existing environment keeps its backend whatever the settings say; an
    explicit state.backend that disagrees is refused rather than ignored."""
    root = Path(args.state_dir) / args.instance
    marker = read_marker(root)
    backend = args.state_backend
    if marker is not None:
        backend = marker.get('backend', 'dir')
        if backend not in BACKENDS:
            raise StateError(f'{root} uses an unknown state backend {backend!r}; refusing to use it.')
        if getattr(args, 'state_backend_explicit', False) and args.state_backend != backend:
            raise StateError(f'{root} was created with state.backend {backend}, but {args.state_backend} is '
                             'configured. Remove the setting, or point state.dir elsewhere.')
    if backend == 'image':
        return ImageState(args.state_dir, args.instance, size=getattr(args, 'state_size', DEFAULT_SIZE))
    return InstanceState(args.state_dir, args.instance)


class InstanceState:
    """The dir backend: the layout as plain host directories."""
    backend = 'dir'
    # Entries that may precede the marker (see validate).
    allowed_before_marker = (LOCK, 'browser', 'bridge', 'system', 'home', MARKER + '.tmp')

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
        return read_marker(self.root)

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
            if (marker.get('instance') != self.instance or marker.get('layout') != LAYOUT
                    or marker.get('backend', 'dir') != self.backend):
                raise StateError(f'{self.root} belongs to another instance, layout or backend; refusing to use it.')
            if registered is not None and registered.get('id') != marker.get('id'):
                raise StateError(f'{self.root} is not the environment registered for {self.instance} '
                                 f'(expected id {registered.get("id")}); refusing to use it.')
            return marker
        if registered is not None:
            raise StateError(f'Environment storage for {self.instance} is unavailable: {self.root} has no state '
                             'marker. Reconnect the disk or fix state.dir; it will not be re-created over.')
        # Never registered and never marked: the remaining entries can only be
        # what an interrupted seed left before writing the marker.
        unexpected = [entry.name for entry in self.root.iterdir()
                      if entry.name not in self.allowed_before_marker
                      and not entry.name.startswith(('.seed-', '.probe-'))] if self.root.is_dir() else []
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
            marker = self.validate()
            if marker is not None:
                if self.registered() is None:  # interrupted between marker and registry
                    self._register(marker['id'])
                return
            self._remove_leftovers(image)
            self._probe(image)
            self._seed(image)

    def prepare_browser(self):
        self.validate()
        self.browser.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.browser, 0o700)

    def release(self):
        """Give back what a running environment holds; nothing for directories."""

    def mounts(self, role, credentials=None):
        """The state mounts of a role's container, in mount order."""
        if role == 'browser':
            return [Mount('/config', self.browser)]
        mounts = [Mount('/' + name, self.system / name) for name in SYSTEM]
        return mounts + [Mount(HOME, self.home), Mount(CLAUDE_STATE, credentials)]

    def describe(self):
        return f'directories in {self.root}'

    def _state_mount(self):
        """How the seed container reaches the state root, as /state."""
        return f'type=bind,src={self.root},dst=/state'

    def _run(self, script, image, timeout=900, **environment):
        flags = [item for key, value in environment.items() for item in ('-e', f'{key}={value}')]
        return engine.call(['docker', 'run', '--rm', '--network', 'none', '--user', '0:0', '--entrypoint', '/bin/sh',
                            '--mount', self._state_mount(), *flags, image, '-c', script],
                           timeout=timeout)

    def _remove_leftovers(self, image):
        # Only called for a never-marked, never-registered state (see validate).
        if any(entry.name.startswith(('.seed-', '.probe-')) or entry.name in ('system', 'home', MARKER + '.tmp')
               for entry in self.root.iterdir()):
            # Interrupted seeds hold root-owned files the launcher cannot delete.
            self._run(f'rm -rf /state/.seed-* /state/.probe-* /state/system /state/home /state/{MARKER}.tmp',
                      image, timeout=300)

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
[ "$result" = "12345:23456:4755:2 file" ] || { echo "kept owner:group:mode:links $result, wanted 12345:23456:4755:2 file"; exit 3; }'''
        try:
            self._run(script, image, timeout=60, PROBE=uuid.uuid4().hex)
        except CageError as error:
            hint = ('On macOS, host folders cannot keep Linux ownership; use state.backend: image (the default '
                    'there).' if host_os() == 'Darwin' else 'Use a Linux (e.g. ext4) directory for state.dir, '
                    'or state.backend: image.')
            raise StateError(f'The filesystem holding {self.root} does not keep Linux ownership, setuid bits or '
                             f'links, so the environment cannot be persisted there ({error}). {hint}') from None

    def _seed_script(self):
        excludes = ' '.join(f'--exclude=./{path}' for path in DOCKER_MANAGED)
        # Everything runs as root inside the container: moving a root-owned
        # directory needs write access to the directory itself (its "..").
        # The marker is written last and handed to the launcher's own user.
        return f'''set -e
d=/state/.seed-$SEED
mkdir -p "$d/system" "$d/home"
cd / && tar --numeric-owner --xattrs {excludes} -cpf - {' '.join('./' + name for name in SYSTEM)} \\
    | tar -C "$d/system" --numeric-owner --xattrs -xpf -
cd {HOME} && tar --numeric-owner --xattrs -cpf - . | tar -C "$d/home" --numeric-owner --xattrs -xpf -
sync
mv "$d/system" /state/system
mv "$d/home" /state/home
rmdir "$d"
printf '%s\\n' "$MARKER" > /state/{MARKER}.tmp
chown "$OWNER" /state/{MARKER}.tmp
chmod 600 /state/{MARKER}.tmp
sync
mv /state/{MARKER}.tmp /state/{MARKER}'''

    def _new_marker(self, image, **extra):
        return {'instance': self.instance, 'id': uuid.uuid4().hex, 'layout': LAYOUT, 'backend': self.backend,
                'image': image, 'created': time.strftime('%Y-%m-%dT%H:%M:%S%z'), **extra}

    def _seed(self, image):
        marker = self._new_marker(image)
        print(f'Creating the persistent environment in {self.root} (first start only)...', flush=True)
        self._run(self._seed_script(), image, SEED=marker['id'], MARKER=json.dumps(marker),
                  OWNER=f'{os.getuid()}:{os.getgid()}')
        self._register(marker['id'])

    def _register(self, seed_id):
        REGISTRY.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_private(self.registry, {'state': str(self.root), 'id': seed_id})


class ImageState(InstanceState):
    """The image backend: the same layout inside state/<instance>/disk.img.

    The image is attached to a loop device in the Docker VM and served as a
    Docker local volume; Docker mounts it for the first container that uses
    it and unmounts it after the last one, so a stopped instance leaves a
    clean filesystem in an ordinary host file. A privileged helper container
    only attaches and detaches loop devices; the cage containers themselves
    stay unprivileged."""
    backend = 'image'
    allowed_before_marker = (LOCK, 'browser', 'bridge', DISK + '.tmp')

    def __init__(self, state_dir, instance, size=None):
        super().__init__(state_dir, instance)
        self.size = size or DEFAULT_SIZE

    @property
    def disk(self):
        return self.root / DISK

    @property
    def volume(self):
        return self.instance + '-state'

    def validate(self):
        marker = super().validate()
        if marker is not None and not self.disk.is_file():
            raise StateError(f'Environment storage for {self.instance} is unavailable: {self.disk} is missing. '
                             'Restore the file or fix state.dir; it will not be re-created over.')
        return marker

    def ensure(self, image):
        images.build('storage')
        with self.locked():
            marker = self.validate()
            if marker is None:
                self._remove_leftovers(image)
                marker = self._seed(image)
            elif self.registered() is None:
                self._register(marker['id'])
            self._serve(marker['filesystem'], marker['id'])
            self._run('mkdir -p /state/browser && chmod 700 /state/browser', image, timeout=60)

    def prepare_browser(self):
        """The profile lives in the image; ensure() created it."""

    def release(self):
        """Detach the image once no container uses its volume.

        Called after the instance's containers are gone; a volume still in use
        (another launcher of this instance) is left alone."""
        if not self.disk.is_file():
            return
        with self.locked():
            if not self._drop_volume():
                return
            self._helper(DETACH)

    def mounts(self, role, credentials=None):
        if role == 'browser':
            return [Mount('/config', self.volume, volume=True, subpath='browser')]
        mounts = [Mount('/' + name, self.volume, volume=True, subpath='system/' + name) for name in SYSTEM]
        return mounts + [Mount(HOME, self.volume, volume=True, subpath='home'), Mount(CLAUDE_STATE, credentials)]

    def describe(self):
        return f'ext4 image {self.disk}, volume {self.volume}'

    def _state_mount(self):
        return f'type=volume,src={self.volume},dst=/state'

    def _helper(self, script, privileged=True, timeout=120, disk=None, **environment):
        """Run a script in the storage image, with the state root at its host path.

        Keeping the host path makes the loop devices' backing-file names the
        same whichever launcher attached them."""
        flags = [item for key, value in {'DISK': disk or self.disk, **environment}.items() for item in ('-e', f'{key}={value}')]
        return engine.call(['docker', 'run', '--rm', '--network', 'none', '--user', '0:0',
                            *(['--privileged'] if privileged else []),
                            '--mount', f'type=bind,src={self.root},dst={self.root}', *flags,
                            images.IMAGES['storage'], 'sh', '-c', 'set -e\n' + script], timeout=timeout)

    def _volume_users(self):
        result = engine.docker('ps', '-aq', '--filter', 'volume=' + self.volume, check=False)
        return result.stdout.split()

    def _drop_volume(self):
        """Remove the volume definition (never its data); False while it is in use."""
        if engine.docker('volume', 'inspect', self.volume, check=False).returncode:
            return True
        if self._volume_users():
            return False
        return engine.docker('volume', 'rm', self.volume, check=False).returncode == 0

    def _serve(self, filesystem, state_id, disk=None):
        """Attach the image and (re)define the volume on its loop device.

        Loop device numbers change when the Docker VM restarts, so the volume
        is recreated on every start; only an unused volume is replaced."""
        if not self._drop_volume():
            raise StateError(f'Volume {self.volume} is in use by containers outside this instance '
                             f'({", ".join(self._volume_users())}); stop them and try again.')
        result = self._helper('''uuid=$(tune2fs -l "$DISK" | sed -n 's/^Filesystem UUID: *//p')
if [ "$uuid" != "$UUID" ]; then
    echo "$DISK holds filesystem ${uuid:-unknown}, not this environment's $UUID" >&2
    exit 5
fi
set -- $(losetup -j "$DISK" | cut -d: -f1)
if [ $# -gt 1 ]; then
    echo "$DISK is attached to several loop devices ($*); run ./cage down first" >&2
    exit 4
fi
[ $# -eq 1 ] || set -- "$(losetup -f --show "$DISK")"
echo "$1"''', disk=disk, UUID=filesystem)
        device = result.stdout.strip().splitlines()[-1]
        engine.docker('volume', 'create', '--driver', 'local', '--opt', 'type=ext4', '--opt', 'device=' + device,
                      '--label', f'{LABEL}={ROOT}', '--label', 'io.caged-claude.state=' + state_id, self.volume)

    def _remove_leftovers(self, image):
        # Only an interrupted seed's disk.img.tmp, never a disk.img (see validate).
        if (self.root / (DISK + '.tmp')).exists():
            self._drop_volume()
            self._helper(DETACH + '\nrm -f "$DISK"', disk=self.root / (DISK + '.tmp'))

    def _seed(self, image):
        """Create the filesystem as disk.img.tmp, seed it, then publish it.

        disk.img appears only complete and detached, and the host marker last,
        so an interruption leaves nothing but disk.img.tmp to clean up."""
        filesystem = str(uuid.uuid4())
        marker = self._new_marker(image, filesystem=filesystem)
        print(f'Creating the persistent environment in {self.disk} (first start only)...', flush=True)
        temporary = self.root / (DISK + '.tmp')
        self._helper('''rm -f "$DISK"
truncate -s "$SIZE" "$DISK"
mkfs.ext4 -q -F -m 0 -U "$UUID" "$DISK"
# Hand the file to the launcher's user where the filesystem keeps owners.
chown "$OWNER" "$DISK" 2>/dev/null || true
chmod 600 "$DISK"''', privileged=False, disk=temporary, SIZE=self.size, UUID=filesystem, OWNER=f'{os.getuid()}:{os.getgid()}')
        # Serve the temporary file under the usual volume name while seeding.
        self._serve(filesystem, marker['id'], disk=temporary)
        try:
            self._run(self._seed_script(), image, SEED=marker['id'], MARKER=json.dumps(marker),
                      OWNER=f'{os.getuid()}:{os.getgid()}')
        finally:
            self._drop_volume()
            self._helper(DETACH, disk=temporary)
        os.replace(temporary, self.disk)
        write_private(self.root / MARKER, marker)
        self._register(marker['id'])
        return marker


def write_private(path, value):
    temporary = path.with_name(path.name + '.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        json.dump(value, handle, indent=2)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    os.rename(temporary, path)


def adopt_legacy_credentials(args):
    """Move the pre-stateful config/.claude to the configured instance's directory.

    Only an instance chosen by settings (file, env or default) adopts it, so
    throwaway --instance runs such as audits and tests never take the login."""
    legacy = ROOT / 'config' / '.claude'
    target = Path(args.claude_config_dir)
    if (args.sources.get('claude_config_dir') == 'default' and args.sources.get('instance') != 'cli'
            and legacy.is_dir() and not target.exists()):
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.rename(legacy, target)
        print(f'Moved Claude credentials from {legacy} to {target}.', flush=True)
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
