"""Build inputs, independent instances and entry points after the layout migration."""
from dataclasses import FrozenInstanceError
from pathlib import Path
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from caged.host import images
from caged.host.config import resolve
from caged.host.paths import ROOT
from caged.host.session import Session


class Layout(unittest.TestCase):
    def test_sessions_do_not_change_another_instances_names(self):
        settings = ['--settings', str(ROOT / 'config.example/app.yaml')]
        first = Session(resolve([*settings, '--instance', 'first'], environ={}))
        expected = first.instance.names
        second = Session(resolve([*settings, '--instance', 'second'], environ={}))
        self.assertEqual(first.instance.names, expected)
        self.assertTrue(set(first.instance.names).isdisjoint(second.instance.names))
        with self.assertRaises(FrozenInstanceError):
            first.instance.name = 'changed'

    def test_hash_tracks_recursive_copy_inputs_but_ignores_unrelated_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shared = root / 'caged/shared/nested'
            shared.mkdir(parents=True)
            source = shared / 'policy.py'
            source.write_text('version = 1')
            container = root / 'containers/network'
            container.mkdir(parents=True)
            (container / 'Dockerfile').write_text('FROM fixture\nCOPY --from=core /core /core\nCOPY caged/shared /opt/caged/shared\n')
            (root / '.dockerignore').write_text('config/\n')
            with patch.object(images, 'ROOT', root):
                initial = images.source_hash('network')
                unrelated = root / 'caged/host'
                unrelated.mkdir()
                (unrelated / 'cli.py').write_text('not copied')
                cache = shared / '__pycache__'
                cache.mkdir()
                (cache / 'policy.pyc').write_bytes(b'cache')
                self.assertEqual(images.source_hash('network'), initial)
                source.write_text('version = 2')
                self.assertNotEqual(images.source_hash('network'), initial)

    def test_image_inputs_exclude_private_config_and_host_programs(self):
        for role in images.IMAGES:
            for path in images.input_files(role):
                relative = path.relative_to(ROOT)
                self.assertNotEqual(relative.parts[0], 'config')
                self.assertNotEqual(relative.parts[:2], ('caged', 'host'))

    def test_entry_point_preserves_caller_paths_and_ignores_a_shadow_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            caller = Path(temporary)
            (caller / 'caged').mkdir()
            (caller / 'caged/__init__.py').write_text('raise RuntimeError("wrong package")')
            (caller / 'nodes.yaml').write_text('proxies:\n  - name: from-caller\n    type: anytls\n    udp: true\n')
            env = {key: value for key, value in os.environ.items() if not key.startswith('CAGED_CLAUDE_')}
            env['CAGED_NATIVE'] = '1'  # exercise the Python entry point, not the launcher container
            result = subprocess.run([str(ROOT / 'cage'), 'nodes', '--settings', str(ROOT / 'config.example/app.yaml'),
                                     '--config', 'nodes.yaml', '--workspace', '.'],
                                    cwd=caller, env=env, text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertRegex(result.stdout, r'(?m)^from-caller\s+anytls\s+UDP$')

    def test_minimal_example_finds_proxy_file_beside_settings_from_another_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = root / 'configuration'
            settings.mkdir()
            (settings / 'app.yaml').write_bytes((ROOT / 'config.example/app.yaml').read_bytes())
            (settings / 'mihomo.yaml').write_text('proxies:\n  - name: beside-settings\n    type: anytls\n    udp: true\n')
            caller = root / 'caller'
            caller.mkdir()
            (caller / 'mihomo.yaml').write_text('proxies:\n  - name: wrong-directory\n    type: anytls\n')
            env = {key: value for key, value in os.environ.items() if not key.startswith('CAGED_CLAUDE_')}
            env['CAGED_NATIVE'] = '1'  # exercise the Python entry point, not the launcher container
            result = subprocess.run([str(ROOT / 'cage'), 'nodes', '--settings', str(settings / 'app.yaml')],
                                    cwd=caller, env=env, text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertRegex(result.stdout, r'(?m)^beside-settings\s+anytls\s+UDP$')
            self.assertNotIn('wrong-directory', result.stdout)
