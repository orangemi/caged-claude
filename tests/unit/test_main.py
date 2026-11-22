"""./cage subcommand dispatch, logs targeting and container names."""
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from caged.host import docker as engine, logs, main
from caged.host.errors import CageError
from caged.host.instance import Instance
from caged.host.paths import ROOT, LABEL


class Dispatch(unittest.TestCase):
    def test_lifecycle_subcommands_map_to_launcher_actions(self):
        for name, action in (('up', 'start'), ('down', 'stop'), ('status', 'status'), ('nodes', 'nodes'),
                             ('pause', 'pause'), ('resume', 'resume'), ('build', 'build')):
            with patch.object(main.cli, 'cli') as cli:
                main.main([name, '--instance', 'x'])
            cli.assert_called_once_with([action, '--instance', 'x'])

    def test_checks_are_grouped_and_validated(self):
        with patch.object(main.cli, 'cli') as cli:
            main.main(['check', 'audit', '--port', '3003'])
        cli.assert_called_once_with(['audit', '--port', '3003'])
        for argv in (['check'], ['check', 'start'], ['start'], ['frobnicate'], []):
            with patch.object(main.cli, 'cli') as cli, self.assertRaises(SystemExit) as raised:
                main.main(argv)
            self.assertEqual(raised.exception.code, 2)
            cli.assert_not_called()

    def test_entry_subcommands_replace_the_process_with_their_command(self):
        with patch.dict(main.EXEC, claude=lambda argv: ['docker', 'exec', 'x', 'claude', *argv]), \
                patch.object(main.os, 'execvp') as execvp:
            main.main(['claude', '-p', 'hi'])
        execvp.assert_called_once_with('docker', ['docker', 'exec', 'x', 'claude', '-p', 'hi'])

    def test_entry_errors_are_reported_without_a_traceback(self):
        def broken(argv):
            raise CageError('Claude container is not running.')
        with patch.dict(main.EXEC, shell=broken), self.assertRaises(SystemExit) as raised:
            main.main(['shell'])
        self.assertEqual(raised.exception.code, 1)

    def test_help_and_unknown_commands_need_no_docker_or_python_setup(self):
        result = subprocess.run(['sh', str(ROOT / 'cage'), 'help'], text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        for word in ('init', 'up', 'down', 'claude', 'shell', 'logs', 'check test'):
            self.assertIn(word, result.stdout)


class Logs(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(logs, 'resolve', return_value=SimpleNamespace(instance='fixture'))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.container = {'Id': 'container-id', 'Config': {'Labels': {LABEL: str(ROOT)}}}

    def test_defaults_to_the_gateway_and_forwards_follow_and_tail(self):
        with patch.object(engine, 'inspect', return_value=self.container) as inspect:
            self.assertEqual(logs.command([]), ['docker', 'logs', '--tail', '200', 'container-id'])
            inspect.assert_called_with('fixture-network')
            self.assertEqual(logs.command(['claude', '-f', '-n', 'all']),
                             ['docker', 'logs', '--tail', 'all', '--follow', 'container-id'])
            inspect.assert_called_with('fixture-claude')

    def test_missing_or_unmanaged_containers_are_refused(self):
        with patch.object(engine, 'inspect', return_value=None), self.assertRaisesRegex(CageError, 'does not exist'):
            logs.command(['browser'])
        self.container['Config']['Labels'][LABEL] = '/another-project'
        with patch.object(engine, 'inspect', return_value=self.container), self.assertRaisesRegex(CageError, 'unmanaged'):
            logs.command([])


class Names(unittest.TestCase):
    def test_every_container_carries_its_role(self):
        self.assertEqual(Instance('caged-claude').names,
                         ('caged-claude-network', 'caged-claude-browser', 'caged-claude-claude', 'caged-claude-check'))


if __name__ == '__main__':
    unittest.main()
