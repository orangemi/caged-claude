"""Configuration precedence, validation, and propagation without external services."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from caged.host import docker as engine, proxy
from caged.host.session import Session
from caged.host.paths import ROOT, LABEL
from caged.host.config import ConfigError, read_yaml, resolve, runtime_policy
from caged.shared.policy import POLICY_ENV, check_country
from unittest.mock import patch


class Settings(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name).resolve()
        self.path = self.folder / 'app.yaml'
        self.data = read_yaml(ROOT / 'config.example/app.yaml')
        self.data['proxy'].update(file_path='mihomo.yaml', node='European fixture', expected_country='DE',
                                  dns_upstream='https://9.9.9.9/dns-query')
        self.data['browser'].update(timezone='Europe/Berlin', locale='de_DE.UTF-8', language='de-DE',
                                    puid='host', pgid='host', shm_size='1g')
        self.data['ports'] = {'ui': 4321, 'controller': 19090, 'mixed': 19890, 'debug': 19222}
        self.data['instance'] = 'caged-browser'
        self.write()

    def tearDown(self):
        self.temp.cleanup()

    def write(self):
        self.path.write_text(json.dumps(self.data))  # JSON is also valid YAML.

    def parse(self, *flags, env=None):
        return resolve(['--settings', str(self.path), *flags], environ={} if env is None else env)

    def test_configuration_drives_non_default_country_locale_node_and_ports(self):
        args = self.parse()
        self.assertEqual((args.node, args.expected_country, args.timezone, args.locale, args.language),
                         ('European fixture', 'DE', 'Europe/Berlin', 'de_DE.UTF-8', 'de-DE'))
        self.assertEqual(args.languages, ['de-DE', 'de'])
        self.assertEqual((args.port, args.controller_port, args.mixed_port, args.debug_port),
                         (4321, 19090, 19890, 19222))
        self.assertEqual(self.parse('--language', 'JA-jp').languages, ['ja-JP', 'ja'])

    def test_optional_fields_and_entire_ports_section_can_be_omitted(self):
        del self.data['proxy']['file_path']
        del self.data['instance']
        del self.data['proxy']['dns_upstream']
        for key in ('locale', 'language', 'puid', 'pgid', 'shm_size'):
            del self.data['browser'][key]
        del self.data['ports']
        self.write()
        args = self.parse()
        self.assertEqual(args.dns_upstream, 'https://1.1.1.1/dns-query')
        self.assertEqual((args.locale, args.language, args.languages), ('en_US.UTF-8', 'en-US', ['en-US', 'en']))
        self.assertEqual((args.puid, args.pgid, args.shm_size), (os.getuid(), os.getgid(), '1g'))
        self.assertEqual((args.port, args.controller_port, args.mixed_port, args.debug_port), (3002, 9090, 17890, 9222))
        self.assertEqual((args.node, args.timezone), ('European fixture', 'Europe/Berlin'))
        self.assertEqual(args.sources['port'], 'default')
        self.assertEqual(args.proxy_file_path, self.folder / 'mihomo.yaml')
        self.assertEqual(args.instance, 'caged-browser')
        for key in ('proxy_file_path', 'instance'):
            self.assertEqual(args.sources[key], 'default')

    def test_config_environment_and_cli_can_override_omitted_default_fields(self):
        self.data['ports'] = {'ui': 4567}
        del self.data['browser']['locale']
        del self.data['browser']['language']
        self.write()
        args = self.parse(env={'CAGED_BROWSER_LANGUAGE': 'fr-CA', 'CAGED_BROWSER_PORT': '5678'})
        self.assertEqual((args.locale, args.language, args.port, args.controller_port),
                         ('en_US.UTF-8', 'fr-CA', 5678, 9090))
        args = self.parse('--language', 'ja-JP', '--port', '6789',
                          env={'CAGED_BROWSER_LANGUAGE': 'invalid environment value', 'CAGED_BROWSER_PORT': 'invalid'})
        self.assertEqual((args.language, args.port), ('ja-JP', 6789))

    def test_explicit_invalid_optional_values_do_not_silently_use_defaults(self):
        self.data['browser']['locale'] = None
        self.write()
        with self.assertRaisesRegex(ConfigError, 'locale'):
            self.parse()

    def test_cli_overrides_environment_which_overrides_file(self):
        env = {'CAGED_BROWSER_NODE': 'environment node', 'CAGED_BROWSER_PORT': '5432',
               'CAGED_BROWSER_TIMEZONE': 'America/New_York', 'CAGED_BROWSER_LANGUAGE': 'fr-CA'}
        args = self.parse(env=env)
        self.assertEqual((args.node, args.port, args.timezone, args.languages),
                         ('environment node', 5432, 'America/New_York', ['fr-CA', 'fr']))
        args = self.parse('--node', 'CLI node', '--port', '6543', '--timezone', 'UTC', env=env)
        self.assertEqual((args.node, args.port, args.timezone), ('CLI node', 6543, 'UTC'))

    def test_overridden_invalid_lower_priority_values_do_not_fail(self):
        self.data['ports']['ui'] = 'invalid configuration value'
        self.write()
        self.assertEqual(self.parse(env={'CAGED_BROWSER_PORT': '5432'}).port, 5432)
        self.assertEqual(self.parse('--port', '6543', env={'CAGED_BROWSER_PORT': 'invalid environment value'}).port, 6543)

    def test_cli_selects_settings_file_over_environment(self):
        other = self.folder / 'other.yaml'
        changed = copy.deepcopy(self.data)
        changed['instance'] = 'environment-file'
        other.write_text(json.dumps(changed))
        args = resolve([], environ={'CAGED_BROWSER_SETTINGS': str(other)})
        self.assertEqual(args.instance, 'environment-file')
        self.assertEqual(self.parse(env={'CAGED_BROWSER_SETTINGS': str(other)}).instance, self.data['instance'])

    def test_proxy_paths_are_relative_to_yaml_but_cli_and_env_paths_use_cwd(self):
        self.data['proxy']['file_path'] = 'nodes/custom.yaml'
        self.write()
        self.assertEqual(self.parse().proxy_file_path, self.folder / 'nodes/custom.yaml')
        env = {'CAGED_BROWSER_PROXY_FILE_PATH': 'env.yaml'}
        args = self.parse(env=env)
        self.assertEqual(args.proxy_file_path, Path.cwd() / 'env.yaml')
        self.assertEqual(args.sources['proxy_file_path'], 'env')
        args = self.parse('--proxy-file-path', 'explicit.yaml', env=env)
        self.assertEqual(args.proxy_file_path, Path.cwd() / 'explicit.yaml')
        self.assertEqual(args.sources['proxy_file_path'], 'cli')

    def test_legacy_proxy_file_aliases_remain_compatible(self):
        del self.data['proxy']['file_path']
        self.data['proxy']['config'] = 'legacy.yaml'
        self.write()
        self.assertEqual(self.parse().proxy_file_path, self.folder / 'legacy.yaml')
        self.data['proxy']['file_path'] = 'canonical.yaml'
        self.write()
        self.assertEqual(self.parse().proxy_file_path, self.folder / 'canonical.yaml')
        env = {'CAGED_BROWSER_PROXY_CONFIG': 'legacy-env.yaml'}
        self.assertEqual(self.parse(env=env).proxy_file_path, Path.cwd() / 'legacy-env.yaml')
        env['CAGED_BROWSER_PROXY_FILE_PATH'] = 'canonical-env.yaml'
        self.assertEqual(self.parse(env=env).proxy_file_path, Path.cwd() / 'canonical-env.yaml')
        for flag in ('--config', '--proxy-config'):
            self.assertEqual(self.parse(flag, 'legacy-cli.yaml', env=env).proxy_file_path, Path.cwd() / 'legacy-cli.yaml')

    def test_instance_default_can_be_overridden_by_environment_and_cli(self):
        del self.data['instance']
        self.write()
        self.assertEqual(self.parse().instance, 'caged-browser')
        env = {'CAGED_BROWSER_INSTANCE': 'from-env'}
        self.assertEqual(self.parse(env=env).instance, 'from-env')
        self.assertEqual(self.parse('--instance', 'from-cli', env=env).instance, 'from-cli')

    def test_invalid_proxy_file_values_are_not_replaced_by_the_default(self):
        for value in (None, '', False):
            self.data['proxy']['file_path'] = value
            self.write()
            with self.assertRaisesRegex(ConfigError, 'proxy.file_path'):
                self.parse()
            self.assertEqual(self.parse('--proxy-file-path', 'valid.yaml').proxy_file_path, Path.cwd() / 'valid.yaml')

    def test_country_can_be_disabled_without_disabling_other_settings(self):
        for value in ('any', '', None):
            self.data['proxy']['expected_country'] = value
            self.write()
            self.assertIsNone(self.parse().expected_country)
        self.assertIsNone(self.parse('--expected-country', 'any').expected_country)
        self.assertEqual(self.parse('--expected-country', 'jp').expected_country, 'JP')

    def test_runtime_country_gate_accepts_configured_regions_or_any(self):
        check_country('DE', 'DE')
        check_country('JP', None)
        with self.assertRaises(RuntimeError):
            check_country('DE', 'JP')
        with self.assertRaises(RuntimeError):
            check_country(None, None)

    def test_host_identity_and_explicit_zero_are_preserved(self):
        args = self.parse('--puid', 'host', '--pgid', 'host')
        self.assertEqual((args.puid, args.pgid), (os.getuid(), os.getgid()))
        self.assertEqual(self.parse('--puid', '0').puid, 0)

    def test_default_identity_uses_the_current_launcher_for_each_resolution(self):
        del self.data['browser']['puid']
        del self.data['browser']['pgid']
        self.write()
        for uid, gid in ((501, 20), (2101, 3101)):
            with patch('caged.host.config.os.getuid', return_value=uid), patch('caged.host.config.os.getgid', return_value=gid):
                args = self.parse()
            self.assertEqual((args.puid, args.pgid), (uid, gid))
            self.assertEqual((args.sources['puid'], args.sources['pgid']), ('default', 'default'))
            session = Session(args)
            session.network_container_id, session.network_session_id = 'fixture', 'fixture'
            for command in (session.browser_command(), session.claude_command()):
                for value in (f'PUID={uid}', f'PGID={gid}', 'USER=claude-user', 'LOGNAME=claude-user'):
                    self.assertIn(value, command)

    def test_explicit_identity_can_override_host_defaults_per_field(self):
        self.data['browser'].update(puid=1000, pgid=1000)
        self.write()
        args = self.parse()
        self.assertEqual((args.puid, args.pgid), (1000, 1000))
        env = {'CAGED_BROWSER_PUID': '2001', 'CAGED_BROWSER_PGID': '2002'}
        self.assertEqual((self.parse(env=env).puid, self.parse(env=env).pgid), (2001, 2002))
        args = self.parse('--puid', '3001', env=env)
        self.assertEqual((args.puid, args.pgid), (3001, 2002))

    def test_invalid_active_values_are_rejected_before_docker(self):
        cases = [('--port', '0'), ('--port', '65536'), ('--timezone', '../etc'),
                 ('--timezone', 'Not/AZone'), ('--language', '--bad-option'),
                 ('--locale', 'bad locale'), ('--puid', '-1'), ('--expected-country', 'wrong'),
                 ('--dns-upstream', 'http://1.1.1.1/dns-query'),
                 ('--dns-upstream', 'https://127.0.0.1/dns-query'),
                 ('--dns-upstream', 'https://1.1.1.1/dns-query#DIRECT')]
        for flag, value in cases:
            with self.subTest(flag=flag, value=value):
                with self.assertRaises(ConfigError):
                    self.parse(flag + '=' + value)
        with self.assertRaises(ConfigError):
            self.parse('--controller-port', '19222')

    def test_missing_and_misspelled_keys_do_not_silently_fall_back(self):
        del self.data['proxy']['node']
        self.write()
        with self.assertRaisesRegex(ConfigError, 'proxy.node'):
            self.parse()
        self.data['proxy']['nodes'] = 'typo'
        self.write()
        with self.assertRaises(ConfigError):
            self.parse()

    def test_browser_command_uses_effective_policy(self):
        args = self.parse('--puid', '501', '--pgid', '20', '--shm-size', '512m')
        session = Session(args)
        session.network_container_id, session.network_session_id = 'fixture-id', 'fixture-session'
        command = session.browser_command()
        for value in ('TZ=Europe/Berlin', 'LANG=de_DE.UTF-8', 'LC_ALL=de_DE.UTF-8',
                      'PUID=501', 'PGID=20', '--shm-size=512m'):
            self.assertIn(value, command)
        flags = next(value for value in command if value.startswith('CHROME_CLI='))
        self.assertIn('--accept-lang=de-DE,de', flags)
        self.assertIn('--remote-debugging-port=19222', flags)
        encoded = next(value for value in command if value.startswith(POLICY_ENV + '='))
        self.assertEqual(json.loads(encoded.split('=', 1)[1]), runtime_policy(args))

    def test_gateway_generates_config_from_the_same_effective_policy(self):
        options = runtime_policy(self.parse())
        result = subprocess.run([sys.executable, '-c',
            "import json;from caged.runtime.network.mihomo import make_config; print(json.dumps(make_config({'type':'anytls','name':'fixture'},'fixture-secret')))"] ,
            cwd=ROOT, env={**os.environ, POLICY_ENV: json.dumps(options)}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads(result.stdout)
        self.assertEqual(config['external-controller'], '127.0.0.1:19090')
        self.assertEqual(config['mixed-port'], 19890)
        self.assertEqual(config['dns']['nameserver'], ['https://9.9.9.9/dns-query#CAGED_NODE'])
        self.assertEqual(config['dns']['default-nameserver'], ['9.9.9.9'])

    def test_nodes_command_lists_other_regions_too(self):
        path = self.folder / 'nodes.yaml'
        path.write_text(json.dumps({'proxies': [
            {'name': 'Berlin', 'type': 'anytls', 'udp': True},
            {'name': 'Tokyo', 'type': 'anytls', 'udp': True},
        ]}))
        self.assertEqual([node['name'] for node in proxy.load_node(path)], ['Berlin', 'Tokyo'])

    def test_workspace_and_state_mounts_and_common_environment_match(self):
        first=self.folder/'first'; second=self.folder/'second'
        first.mkdir(); second.mkdir()
        self.data['workspace']=[str(first)]
        self.write()
        args=self.parse('--workspace',str(second))
        self.assertEqual(args.workspace,[second])
        self.assertEqual(self.parse(env={'CAGED_BROWSER_WORKSPACE':'[]'}).workspace,[])
        self.assertEqual(self.parse(env={'CAGED_BROWSER_WORKSPACE':str(second)}).workspace,[second])
        session=Session(args)
        session.network_container_id,session.network_session_id='fixture-id','fixture-session'
        browser,claude=session.browser_command(),session.claude_command()
        mount=f'type=bind,src={second},dst={second}'
        self.assertIn(mount,browser);self.assertIn(mount,claude)
        self.assertIn(f'type=bind,src={args.claude_config_dir},dst=/config/.claude',claude)
        for value in session.common_environment():
            self.assertIn(value,browser);self.assertIn(value,claude)

    def test_workspace_and_state_paths_are_scoped(self):
        with self.assertRaises(ConfigError):self.parse('--workspace','/does-not-exist')
        with self.assertRaises(ConfigError):self.parse('--workspace','/')
        with self.assertRaises(ConfigError):self.parse('--claude-config-dir',str(Path.home()))
        args=self.parse()
        self.assertEqual(args.workspace,[])
        self.assertEqual(args.claude_config_dir,ROOT/'config/.claude')

    def test_claude_entry_uses_actual_container_identity_and_forwards_cli_flags(self):
        from caged.host.claude import command
        labels = {LABEL: str(ROOT)}
        gateway = {'Id': 'fixture-network', 'State': {'Running': True}, 'Config': {'Labels': labels}}
        client = {'Id': 'fixture-claude', 'State': {'Running': True},
                  'Config': {'Labels': labels, 'Env': ['PUID=1000', 'PGID=1000']},
                  'HostConfig': {'NetworkMode': 'container:fixture-network'}}
        with patch.object(engine, 'inspect', side_effect=[client, gateway]):
            actual = command(['--app-settings', str(self.path), '--model', 'sonnet',
                              '--settings', 'claude-options.json'], tty=False)
        self.assertEqual(actual[-5:], ['claude', '--model', 'sonnet', '--settings', 'claude-options.json'])
        self.assertIn('1000:1000', actual)
        self.assertNotIn('-t', actual)


if __name__ == '__main__':
    unittest.main(verbosity=2)
