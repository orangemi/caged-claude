"""The cage's timezone follows the node's exit, or must lie in its country."""
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

from caged.host import session as session_module
from caged.host.errors import CageError
from caged.runtime.network import main as gateway
from caged.shared import zones


class Zones(unittest.TestCase):
    def test_country_of_a_zone(self):
        self.assertEqual(zones.country_of('America/New_York'), 'US')
        self.assertEqual(zones.country_of('Asia/Singapore'), 'SG')
        for name in ('UTC', 'Etc/GMT+5', 'Not/AZone', '../../etc/passwd'):
            with self.subTest(name=name):
                self.assertIsNone(zones.country_of(name))

    def test_located_zone_wins_inside_the_country_else_its_default(self):
        self.assertEqual(zones.for_country('US', 'America/Chicago'), 'America/Chicago')
        self.assertEqual(zones.for_country('US', 'Asia/Tokyo'), 'America/New_York')
        self.assertEqual(zones.for_country('US'), 'America/New_York')
        self.assertEqual(zones.for_country('AU'), 'Australia/Sydney')
        self.assertEqual(zones.for_country('JP'), 'Asia/Tokyo')
        self.assertIsNone(zones.for_country('ZZ'))
        self.assertEqual(zones.examples('US')[0], 'America/New_York')


class Settle(unittest.TestCase):
    def settle(self, configured, egress, returncode=0):
        fake = types.SimpleNamespace(policy={'timezone': configured}, args=types.SimpleNamespace(timezone=configured),
                                     instance=types.SimpleNamespace(network='gateway'))
        writer = MagicMock(return_value=MagicMock(returncode=returncode))
        with patch.object(session_module.engine, 'write_private', writer):
            source = session_module.Session.settle_timezone(fake, egress)
        return fake, source, writer

    def test_unset_follows_the_located_zone_or_the_country_default(self):
        fake, source, writer = self.settle(None, {'ip': '203.0.113.1', 'country': 'US', 'timezone': 'America/Chicago'})
        self.assertEqual((fake.policy['timezone'], fake.args.timezone), ('America/Chicago', 'America/Chicago'))
        self.assertIn('located zone of US', source)
        writer.assert_called_once_with('gateway', '/run/caged/timezone', 'America/Chicago', timeout=15)
        fake, source, _ = self.settle(None, {'ip': '203.0.113.1', 'country': 'JP', 'timezone': None})
        self.assertEqual((fake.policy['timezone'], source), ('Asia/Tokyo', 'from the node: default zone of JP'))

    def test_set_zone_must_lie_in_the_exit_country(self):
        fake, source, _ = self.settle('America/Chicago', {'ip': '203.0.113.1', 'country': 'US', 'timezone': 'America/New_York'})
        self.assertEqual(fake.policy['timezone'], 'America/Chicago')
        self.assertIn('matches the exit in US', source)
        with self.assertRaisesRegex(CageError, r'Asia/Singapore does not match the node: its exit 203.0.113.1 is in US'
                                               r'.*America/New_York.*choose a node in SG'):
            self.settle('Asia/Singapore', {'ip': '203.0.113.1', 'country': 'US', 'timezone': None})

    def test_refused_zone_is_never_handed_to_the_gateway(self):
        writer = MagicMock()
        fake = types.SimpleNamespace(policy={'timezone': 'Asia/Tokyo'}, args=types.SimpleNamespace(timezone='Asia/Tokyo'),
                                     instance=types.SimpleNamespace(network='gateway'))
        with patch.object(session_module.engine, 'write_private', writer), self.assertRaises(CageError):
            session_module.Session.settle_timezone(fake, {'ip': '203.0.113.1', 'country': 'SG'})
        writer.assert_not_called()

    def test_attaching_launcher_adopts_the_gateway_zone(self):
        fake = types.SimpleNamespace(policy={'node': 'n', 'dns_upstream': [], 'controller_port': 1, 'mixed_port': 2,
                                             'browser_ui_port': 3, 'timezone': None},
                                     args=types.SimpleNamespace(timezone=None, browser_ui_port=3, port=4))
        status = {'policy': {**fake.policy, 'timezone': 'Asia/Tokyo'}}
        state = {'Id': 'gateway', 'Config': {'Labels': {'io.caged-claude.session': 's'}},
                 'NetworkSettings': {'Ports': {'3/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '4'}]}}}
        session_module.Session.use_network(fake, status, state)
        self.assertEqual((fake.policy['timezone'], fake.args.timezone), ('Asia/Tokyo', 'Asia/Tokyo'))
        fake.policy['timezone'] = 'Asia/Singapore'
        with self.assertRaisesRegex(CageError, 'uses timezone Asia/Tokyo.*./cage down'):
            session_module.Session.use_network(fake, status, state)


class Gateway(unittest.TestCase):
    def test_monitor_waits_for_a_valid_zone_that_keeps_a_set_one(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(gateway, 'ROOT', Path(folder)):
            self.assertIsNone(gateway.chosen_timezone({'timezone': None}))
            (Path(folder) / 'timezone').write_text('Asia/Tokyo\n')
            self.assertEqual(gateway.chosen_timezone({'timezone': None}), 'Asia/Tokyo')
            self.assertEqual(gateway.chosen_timezone({'timezone': 'Asia/Tokyo'}), 'Asia/Tokyo')
            with self.assertRaisesRegex(RuntimeError, 'other than the configured'):
                gateway.chosen_timezone({'timezone': 'Asia/Singapore'})
            (Path(folder) / 'timezone').write_text('Not/AZone')
            with self.assertRaises(Exception):
                gateway.chosen_timezone({'timezone': None})
            gateway.write_ready({'policy': {'timezone': 'Asia/Tokyo'}})
            self.assertEqual(json.loads((Path(folder) / 'ready.json').read_text())['policy']['timezone'], 'Asia/Tokyo')


if __name__ == '__main__':
    unittest.main()
