"""Two more services' view of the exit, shown at start for reference only."""
import json
import subprocess
import types
import unittest
from unittest.mock import MagicMock, patch

from caged.host import session as session_module
from caged.runtime.checks import egress

IPINFO = {'ip': '9.9.9.9', 'country': 'US', 'org': 'AS7922 Example Cable'}
IP2LOCATION = {'ip': '9.9.9.9', 'country_code': 'US', 'asn': '7922', 'as': 'Example Cable', 'is_proxy': False}


def fetcher(answers):
    def fetch(url, timeout):
        answer = answers[next(name for name, source in egress.REFERENCE_SOURCES if source == url)]
        if isinstance(answer, Exception):
            raise answer
        return answer
    return fetch


class Reference(unittest.TestCase):
    def test_each_service_reports_its_address_country_and_network(self):
        result = egress.reference(fetcher({'ipinfo.io': (200, json.dumps(IPINFO)),
                                           'ip2location.io': (200, json.dumps(IP2LOCATION))}))
        self.assertEqual(result, {
            'ipinfo.io': {'ip': '9.9.9.9', 'country': 'US', 'network': 'AS7922 Example Cable'},
            'ip2location.io': {'ip': '9.9.9.9', 'country': 'US', 'network': 'AS7922 Example Cable', 'proxy': False}})

    def test_a_failing_service_is_reported_not_raised(self):
        result = egress.reference(fetcher({'ipinfo.io': (429, ''), 'ip2location.io': (200, 'not json')}))
        self.assertEqual(result, {'ipinfo.io': {'error': 'HTTP 429'}, 'ip2location.io': {'error': 'invalid response'}})
        result = egress.reference(fetcher({'ipinfo.io': TimeoutError('private'), 'ip2location.io': (200, '{"ip": "10.0.0.1"}')}))
        self.assertEqual(result, {'ipinfo.io': {'error': 'connection failed'},
                                  'ip2location.io': {'error': 'no public IPv4 in the response'}})


class StartLine(unittest.TestCase):
    def show(self, stdout=None, error=None):
        fake = types.SimpleNamespace(network_container_id='gateway')
        fake.exec_probe = MagicMock(side_effect=error, return_value=MagicMock(stdout=stdout))
        calls = {}
        with patch.object(session_module.ui, 'ok', lambda *a: calls.setdefault('ok', a)), \
                patch.object(session_module.ui, 'warn', lambda *a: calls.setdefault('warn', a)), \
                patch.object(session_module.ui, 'info', lambda *a: calls.setdefault('info', a)):
            session_module.Session.show_reference(fake, '9.9.9.9')
        return calls

    def test_matching_services_confirm_the_exit(self):
        stdout = json.dumps({'ipinfo.io': {'ip': '9.9.9.9', 'country': 'US', 'network': 'AS7922 Example'},
                             'ip2location.io': {'ip': '9.9.9.9', 'country': 'US', 'network': None, 'proxy': True}})
        label, text, details = self.show(stdout)['ok']
        self.assertEqual((label, text), ('Exit check', '2 reference services see 9.9.9.9 too'))
        self.assertEqual(details, [('ipinfo.io', '9.9.9.9 (US, AS7922 Example)'),
                                   ('ip2location.io', '9.9.9.9 (US, flagged as a proxy)')])

    def test_another_address_warns_but_never_stops_the_start(self):
        stdout = json.dumps({'ipinfo.io': {'ip': '1.0.0.9', 'country': 'US', 'network': None},
                             'ip2location.io': {'error': 'timed out'}})
        label, text, details = self.show(stdout)['warn']
        self.assertIn('see 1.0.0.9, not the measured exit 9.9.9.9', text)
        self.assertIn(('ip2location.io', 'timed out'), details)

    def test_no_answer_is_only_information(self):
        for stdout, error in (('{"error": "probe failed"}', None), ('', None),
                              (None, subprocess.TimeoutExpired('docker', 20))):
            with self.subTest(stdout=stdout, error=error):
                self.assertEqual(self.show(stdout, error)['info'][1], 'reference services did not answer')


if __name__ == '__main__':
    unittest.main()
