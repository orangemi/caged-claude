"""State directory layout, markers, seeding and refusal to overwrite."""
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from caged.host import stateful
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
        mounts = self.state.claude_mounts('/project/config/claude/fixture')
        self.assertEqual(set(mounts), {'/etc', '/usr', '/var', '/opt', '/srv', '/root',
                                       '/home/claude', '/home/claude/.claude'})
        self.assertEqual(mounts['/usr'], str(self.state.system / 'usr'))
        self.assertEqual(mounts['/home/claude/.claude'], '/project/config/claude/fixture')


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
