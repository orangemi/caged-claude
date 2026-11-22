"""Container entry respects role, actual UID/GID and network ownership."""
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from caged.host import docker as engine, shell
from caged.host.errors import CageError
from caged.host.paths import ROOT, LABEL


class ContainerEntry(unittest.TestCase):
    def setUp(self):
        self.gateway = {'Id': 'network-id', 'State': {'Running': True},
                        'Config': {'Labels': {LABEL: str(ROOT)}}}
        self.client = {'Id': 'client-id', 'State': {'Running': True},
                       'Config': {'Labels': {LABEL: str(ROOT)}, 'Env': ['PUID=2101', 'PGID=3101']},
                       'HostConfig': {'NetworkMode': 'container:network-id'}}
        self.resolver = patch.object(shell, 'resolve', return_value=SimpleNamespace(instance='fixture'))
        self.resolver.start()
        self.addCleanup(self.resolver.stop)

    def test_bash_uses_the_running_app_identity_and_allocates_a_terminal(self):
        with patch.object(engine, 'inspect', side_effect=[self.client, self.gateway]) as inspect:
            result = shell.command([], tty=True)
        self.assertEqual([call.args[0] for call in inspect.call_args_list], ['fixture-claude', 'fixture-network'])
        self.assertEqual(result, ['docker', 'exec', '-i', '-t', '--user', '2101:3101', 'client-id', 'bash', '-l'])

    def test_browser_role_and_command_arguments_are_forwarded_without_shell_expansion(self):
        arguments = ['printf', '%s', '$(touch /tmp/should-not-run)', '--instance', 'literal-value']
        with patch.object(engine, 'inspect', side_effect=[self.client, self.gateway]) as inspect:
            result = shell.command(['browser', '--', *arguments], tty=False)
        self.assertEqual(inspect.call_args_list[0].args[0], 'fixture-browser')
        self.assertEqual(result[-len(arguments):], arguments)
        self.assertNotIn('-t', result)

    def test_root_entry_sets_the_root_home_only_for_this_exec(self):
        with patch.object(engine, 'inspect', side_effect=[self.client, self.gateway]):
            result = shell.command(['--root', '--', 'id', '-u'], tty=False)
        self.assertIn('0:0', result)
        self.assertIn('HOME=/root', result)
        self.assertIn('USER=root', result)
        self.assertEqual(result[-3:], ['client-id', 'id', '-u'])

    def test_network_uses_its_available_sh_and_does_not_need_app_identity(self):
        with patch.object(engine, 'inspect', return_value=self.gateway) as inspect:
            result = shell.command(['network'], tty=False)
        inspect.assert_called_once_with('fixture-network')
        self.assertIn('0:0', result)
        self.assertEqual(result[-2:], ['network-id', 'sh'])

    def test_an_unmanaged_container_cannot_be_entered(self):
        client = copy.deepcopy(self.client)
        client['Config']['Labels'][LABEL] = '/another-project'
        with patch.object(engine, 'inspect', return_value=client):
            with self.assertRaisesRegex(CageError, 'unmanaged'):
                shell.command([])

    def test_replaced_gateway_cannot_be_used(self):
        self.gateway['Id'] = 'replacement-id'
        with patch.object(engine, 'inspect', side_effect=[self.client, self.gateway]):
            with self.assertRaisesRegex(CageError, 'gateway is unavailable'):
                shell.command([])

    def test_stopped_container_does_not_get_started_as_a_side_effect(self):
        self.client['State']['Running'] = False
        with patch.object(engine, 'inspect', return_value=self.client):
            with self.assertRaisesRegex(CageError, 'not running'):
                shell.command([])


if __name__ == '__main__':
    unittest.main(verbosity=2)
