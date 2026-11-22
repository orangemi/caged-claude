"""State directory layout, markers, seeding and refusal to overwrite."""
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from caged.host import images, stateful
from caged.host.errors import CageError


class FakeDocker:
    """Stands in for the root containers that probe, seed and clean state."""

    def __init__(self, probe_ok=True, seed_ok=True):
        self.probe_ok, self.seed_ok, self.scripts = probe_ok, seed_ok, []

    def __call__(self, command, **kwargs):
        script = command[-1]
        self.scripts.append(script)
        root = Path(next(item for item in command if item.startswith('type=bind,src=')).split(',')[1][4:])
        environment = dict(command[index + 1].split('=', 1) for index, item in enumerate(command) if item == '-e')
        if 'PROBE' in environment and not self.probe_ok:
            raise CageError('ownership was not kept')
        if 'SEED' in environment:
            # The real script stages, moves system/ and home/ into place, then
            # writes the marker last; an interruption can stop after any step.
            for name in stateful.SYSTEM:
                (root / 'system' / name).mkdir(parents=True)
            if not self.seed_ok:
                raise CageError('seed interrupted')
            (root / 'system' / 'etc' / 'os-release').write_text('ID=debian\n')
            (root / 'home').mkdir()
            (root / 'home' / '.bashrc').write_text('# skel\n')
            (root / stateful.MARKER).write_text(environment['MARKER'] + '\n')
            (root / stateful.MARKER).chmod(0o600)
            self.owner = environment['OWNER']
        if script.startswith('rm -rf'):
            import shutil
            for entry in root.iterdir():
                if entry.name.startswith(('.seed-', '.probe-')) or entry.name in ('system', 'home'):
                    shutil.rmtree(entry)
        return SimpleNamespace(returncode=0, stdout='', stderr='')


class StateDirectory(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        registry = patch.object(stateful, 'REGISTRY', base / 'config' / 'instances')
        registry.start()
        self.addCleanup(registry.stop)
        self.state = stateful.InstanceState(base / 'state', 'fixture')

    def ensure(self, docker):
        with patch.object(stateful.engine, 'call', docker):
            self.state.ensure('fixture-image')

    def test_first_start_seeds_atomically_and_registers_the_instance(self):
        docker = FakeDocker()
        self.ensure(docker)
        self.assertEqual((self.state.system / 'etc' / 'os-release').read_text(), 'ID=debian\n')
        self.assertTrue((self.state.home / '.bashrc').is_file())
        marker = self.state.marker()
        self.assertEqual((marker['instance'], marker['layout'], marker['image']), ('fixture', 1, 'fixture-image'))
        self.assertEqual(self.state.registered()['id'], marker['id'])
        self.assertEqual((self.state.root / stateful.MARKER).stat().st_mode & 0o777, 0o600)
        self.assertEqual(docker.owner, f'{__import__("os").getuid()}:{__import__("os").getgid()}')
        seed = next(script for script in docker.scripts if 'tar' in script)
        self.assertLess(seed.index('mv "$d/home"'), seed.index('.caged-state.tmp'), 'marker is written last')
        for path in stateful.DOCKER_MANAGED:
            self.assertIn('--exclude=./' + path, seed)
        self.assertLess(seed.index('--exclude'), seed.index('-cpf'), 'GNU tar needs excludes before members')

    def test_later_starts_reuse_the_state_without_touching_it(self):
        self.ensure(FakeDocker())
        (self.state.home / 'work.txt').write_text('kept')
        docker = FakeDocker()
        self.ensure(docker)
        self.assertEqual(docker.scripts, [])
        self.assertEqual((self.state.home / 'work.txt').read_text(), 'kept')

    def test_a_marker_without_registry_is_registered_instead_of_reseeded(self):
        self.ensure(FakeDocker())
        identity = self.state.marker()['id']
        self.state.registry.unlink()
        docker = FakeDocker()
        self.ensure(docker)
        self.assertEqual(docker.scripts, [])
        self.assertEqual(self.state.registered()['id'], identity)

    def test_a_registered_but_missing_environment_is_never_reseeded(self):
        self.ensure(FakeDocker())
        (self.state.root / stateful.MARKER).unlink()
        docker = FakeDocker()
        with self.assertRaisesRegex(stateful.StateError, 'unavailable'):
            self.ensure(docker)
        self.assertEqual(docker.scripts, [])

    def test_foreign_or_unmarked_content_is_refused(self):
        self.ensure(FakeDocker())
        marker = json.loads((self.state.root / stateful.MARKER).read_text())
        marker['id'] = 'another'
        (self.state.root / stateful.MARKER).write_text(json.dumps(marker))
        with self.assertRaisesRegex(stateful.StateError, 'not the environment registered'):
            self.state.validate()
        other = stateful.InstanceState(self.state.root.parent, 'unmarked')
        (other.root / 'projects').mkdir(parents=True)
        with self.assertRaisesRegex(stateful.StateError, 'no state marker'):
            other.validate()

    def test_filesystem_without_linux_semantics_is_refused_before_seeding(self):
        docker = FakeDocker(probe_ok=False)
        with self.assertRaisesRegex(stateful.StateError, 'ownership, setuid'):
            self.ensure(docker)
        self.assertFalse(any('tar' in script for script in docker.scripts))
        self.assertIsNone(self.state.marker())

    def test_an_interrupted_seed_is_cleaned_up_and_retried(self):
        with self.assertRaises(CageError):
            self.ensure(FakeDocker(seed_ok=False))
        self.assertIsNone(self.state.marker())
        self.assertTrue(self.state.system.is_dir())
        self.state.validate()  # an unregistered, unmarked system/ is seed debris
        docker = FakeDocker()
        self.ensure(docker)
        self.assertTrue(docker.scripts[0].startswith('rm -rf'))
        self.assertIsNotNone(self.state.marker())

    def test_browser_profile_is_private_and_does_not_count_as_unmarked_state(self):
        self.state.prepare_browser()
        self.assertEqual(self.state.browser.stat().st_mode & 0o777, 0o700)
        self.ensure(FakeDocker())
        self.assertIsNotNone(self.state.marker())

    def test_claude_mounts_cover_the_system_home_and_separate_credentials(self):
        mounts = {mount.destination: mount for mount in self.state.mounts('claude', '/project/config/claude/fixture')}
        self.assertEqual(set(mounts), {'/etc', '/usr', '/var', '/opt', '/srv', '/root',
                                       '/home/claude', '/home/claude/.claude'})
        self.assertEqual(mounts['/usr'].argument(), f'type=bind,src={self.state.system / "usr"},dst=/usr')
        self.assertEqual(mounts['/home/claude/.claude'].source, '/project/config/claude/fixture')
        self.assertEqual([mount.argument() for mount in self.state.mounts('browser')],
                         [f'type=bind,src={self.state.browser},dst=/config'])

    def test_probe_failure_says_what_was_kept_and_points_macos_at_the_image_backend(self):
        def docker(command, **kwargs):
            raise CageError('kept owner:group:mode:links 0:0:4755:2 file')
        with patch.object(stateful.engine, 'call', docker), patch.dict('os.environ', {'CAGED_HOST_OS': 'Darwin'}):
            with self.assertRaisesRegex(stateful.StateError, '0:0:4755:2.*state.backend: image'):
                self.state.ensure('fixture-image')


class FakeImageDocker:
    """Stands in for Docker, the storage helper and loop devices.

    An image file holds only its filesystem UUID; the filesystem's content is
    a directory keyed by that UUID, so it follows the file across renames."""

    def __init__(self, base, users=(), seed_ok=True):
        self.base, self.users, self.seed_ok = Path(base), list(users), seed_ok
        self.volumes, self.loops, self.calls = {}, {}, []

    def content(self, volume):
        return self.base / 'filesystems' / Path(self.loops[self.volumes[volume]]).read_text()

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        environment = dict(command[index + 1].split('=', 1) for index, item in enumerate(command) if item == '-e')
        args, stdout, code = command[1:], '', 0
        if args[:2] == ['volume', 'inspect']:
            code = 0 if args[2] in self.volumes else 1
        elif args[:2] == ['volume', 'rm']:
            self.volumes.pop(args[2])
        elif args[:2] == ['volume', 'create']:
            device = next(item for item in args if item.startswith('device='))[len('device='):]
            self.volumes[args[-1]] = device
        elif args[0] == 'ps':
            stdout = '\n'.join(self.users)
        elif images.IMAGES['storage'] in command:
            script, disk = command[-1], environment['DISK']
            if 'mkfs.ext4' in script:
                Path(disk).write_text(environment['UUID'])
                (self.base / 'filesystems' / environment['UUID']).mkdir(parents=True)
            elif 'losetup -f' in script:
                if Path(disk).read_text() != environment['UUID']:
                    raise CageError(f'{disk} holds filesystem another, not this environment')
                attached = [device for device, path in self.loops.items() if path == disk]
                if not attached:
                    attached = [f'/dev/loop{len(self.loops)}']
                    self.loops[attached[0]] = disk
                stdout = attached[0] + '\n'
            elif 'losetup -d' in script:
                self.loops = {device: path for device, path in self.loops.items() if path != disk}
                if 'rm -f' in script:
                    Path(disk).unlink()
        else:  # seed or profile directory, in the volume
            mount = command[command.index('--mount') + 1]
            self.assert_volume_mount(mount)
            root = self.content(mount.split(',')[1][4:])
            if 'SEED' in environment:
                for name in stateful.SYSTEM:
                    (root / 'system' / name).mkdir(parents=True)
                if not self.seed_ok:
                    raise CageError('seed interrupted')
                (root / 'home').mkdir()
                (root / stateful.MARKER).write_text(environment['MARKER'])
            if 'mkdir -p /state/browser' in command[-1]:
                (root / 'browser').mkdir(exist_ok=True)
        return SimpleNamespace(returncode=code, stdout=stdout, stderr='')

    @staticmethod
    def assert_volume_mount(mount):
        if not mount.startswith('type=volume,src=fixture-state,dst=/state'):
            raise AssertionError('seed must write through the volume: ' + mount)


class ImageBackend(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        for target, value in ((stateful, 'REGISTRY'),):
            patcher = patch.object(target, value, self.base / 'config' / 'instances')
            patcher.start()
            self.addCleanup(patcher.stop)
        build = patch.object(stateful.images, 'build', lambda *args, **kwargs: None)
        build.start()
        self.addCleanup(build.stop)
        self.docker = FakeImageDocker(self.base)
        self.state = stateful.ImageState(self.base / 'state', 'fixture', '64G')

    def run_docker(self, method, *args, docker=None):
        with patch.object(stateful.engine, 'call', docker or self.docker):
            return getattr(self.state, method)(*args)

    def test_first_start_creates_seeds_and_serves_the_image(self):
        self.run_docker('ensure', 'fixture-image')
        marker = self.state.marker()
        self.assertEqual((marker['backend'], marker['instance']), ('image', 'fixture'))
        self.assertEqual(self.state.disk.read_text(), marker['filesystem'])
        self.assertFalse((self.state.root / 'disk.img.tmp').exists())
        self.assertEqual(self.state.registered()['id'], marker['id'])
        content = self.docker.content('fixture-state')
        self.assertTrue((content / 'system' / 'usr').is_dir() and (content / 'home').is_dir())
        self.assertTrue((content / 'browser').is_dir(), 'the browser profile lives in the image too')
        self.assertEqual(list(self.docker.loops.values()), [str(self.state.disk)], 'one loop device, on disk.img')
        helpers = [command for command in self.docker.calls if images.IMAGES['storage'] in command]
        create = next(command for command in helpers if 'mkfs.ext4' in command[-1])
        self.assertNotIn('--privileged', create, 'creating the file needs no privilege')
        self.assertTrue(all('--privileged' in command for command in helpers if 'losetup' in command[-1]))
        self.assertTrue(all('--network' in command and 'none' in command for command in helpers))

    def test_later_starts_reattach_without_reseeding_and_recreate_the_volume(self):
        self.run_docker('ensure', 'fixture-image')
        identity = self.state.marker()['id']
        self.docker.volumes['fixture-state'] = '/dev/loop-stale'  # e.g. after the Docker VM restarted
        self.docker.calls.clear()
        self.run_docker('ensure', 'fixture-image')
        self.assertEqual(self.state.marker()['id'], identity)
        self.assertFalse(any('mkfs.ext4' in command[-1] or 'SEED' in ' '.join(command) for command in self.docker.calls))
        self.assertEqual(self.docker.loops[self.docker.volumes['fixture-state']], str(self.state.disk))

    def test_release_detaches_only_an_unused_volume(self):
        self.run_docker('ensure', 'fixture-image')
        self.docker.users = ['foreign']
        self.run_docker('release')
        self.assertIn('fixture-state', self.docker.volumes)
        self.docker.users = []
        self.run_docker('release')
        self.assertEqual((self.docker.volumes, self.docker.loops), ({}, {}))
        self.assertTrue(self.state.disk.is_file(), 'release never touches the data')

    def test_a_volume_used_by_other_containers_is_not_replaced(self):
        self.run_docker('ensure', 'fixture-image')
        self.docker.users = ['foreign']
        with self.assertRaisesRegex(stateful.StateError, 'in use'):
            self.run_docker('ensure', 'fixture-image')

    def test_a_swapped_or_missing_image_is_refused(self):
        self.run_docker('ensure', 'fixture-image')
        self.run_docker('release')
        self.state.disk.write_text('another-filesystem')
        with self.assertRaisesRegex(CageError, 'holds filesystem'):
            self.run_docker('ensure', 'fixture-image')
        self.state.disk.unlink()
        with self.assertRaisesRegex(stateful.StateError, 'disk.img is missing'):
            self.run_docker('ensure', 'fixture-image')

    def test_an_interrupted_seed_leaves_only_a_temporary_file_that_is_retried(self):
        with self.assertRaises(CageError):
            self.run_docker('ensure', 'fixture-image', docker=FakeImageDocker(self.base, seed_ok=False))
        self.assertIsNone(self.state.marker())
        self.assertFalse(self.state.disk.exists())
        self.assertTrue((self.state.root / 'disk.img.tmp').exists())
        self.run_docker('ensure', 'fixture-image')
        self.assertIsNotNone(self.state.marker())
        self.assertFalse((self.state.root / 'disk.img.tmp').exists())

    def test_an_unmarked_image_is_never_overwritten(self):
        self.state.root.mkdir(parents=True)
        self.state.disk.write_text('copied without its marker')
        with self.assertRaisesRegex(stateful.StateError, 'no state marker \\(disk.img\\)'):
            self.run_docker('ensure', 'fixture-image')
        self.assertEqual(self.state.disk.read_text(), 'copied without its marker')

    def test_mounts_are_volume_subpaths_and_credentials_stay_on_the_host(self):
        mounts = {mount.destination: mount.argument() for mount in self.state.mounts('claude', '/project/config/claude/x')}
        self.assertEqual(mounts['/usr'], 'type=volume,src=fixture-state,dst=/usr,volume-subpath=system/usr')
        self.assertEqual(mounts['/home/claude'], 'type=volume,src=fixture-state,dst=/home/claude,volume-subpath=home')
        self.assertEqual(mounts['/home/claude/.claude'], 'type=bind,src=/project/config/claude/x,dst=/home/claude/.claude')
        self.assertEqual([mount.argument() for mount in self.state.mounts('browser')],
                         ['type=volume,src=fixture-state,dst=/config,volume-subpath=browser'])


class BackendChoice(unittest.TestCase):
    def args(self, folder, backend='dir', explicit=False):
        return SimpleNamespace(state_dir=folder, instance='fixture', state_backend=backend,
                               state_backend_explicit=explicit, state_size='64G')

    def test_an_existing_environment_keeps_its_backend(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            self.assertEqual(stateful.open_state(self.args(folder, 'image')).backend, 'image')
            (folder / 'fixture').mkdir()
            (folder / 'fixture' / stateful.MARKER).write_text(json.dumps({'backend': 'image'}))
            self.assertEqual(stateful.open_state(self.args(folder, 'dir')).backend, 'image', 'auto adopts it')
            with self.assertRaisesRegex(stateful.StateError, 'created with state.backend image'):
                stateful.open_state(self.args(folder, 'dir', explicit=True))
            (folder / 'fixture' / stateful.MARKER).write_text(json.dumps({'instance': 'fixture'}))
            self.assertEqual(stateful.open_state(self.args(folder, 'image')).backend, 'dir', 'old markers are dir')


class LegacyCredentials(unittest.TestCase):
    def test_configured_instance_adopts_config_claude_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'config' / '.claude').mkdir(parents=True)
            (root / 'config' / '.claude' / '.credentials.json').write_text('{}')
            target = root / 'config' / 'claude' / 'caged-claude'
            args = SimpleNamespace(claude_config_dir=target, instance='caged-claude',
                                   sources={'claude_config_dir': 'default', 'instance': 'config'})
            with patch.object(stateful, 'ROOT', root):
                stateful.adopt_legacy_credentials(args)
                self.assertEqual((target / '.credentials.json').read_text(), '{}')
                self.assertFalse((root / 'config' / '.claude').exists())
                other = SimpleNamespace(claude_config_dir=root / 'config' / 'claude' / 'other', instance='other',
                                        sources={'claude_config_dir': 'default', 'instance': 'cli'})
                (root / 'config' / '.claude').mkdir()
                stateful.adopt_legacy_credentials(other)
                self.assertTrue((root / 'config' / '.claude').is_dir(), 'a throwaway --instance must not adopt it')
                self.assertTrue(other.claude_config_dir.is_dir())


if __name__ == '__main__':
    unittest.main()
