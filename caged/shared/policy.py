"""Non-secret resolved policy, supplied by the launcher to every container/probe."""
import json
import os

POLICY_ENV = 'CAGED_BROWSER_POLICY'
BROWSER_HTTPS_PORT = 3001  # The upstream desktop image's HTTPS service port.
APP_USER = 'claude-user'
BROWSER_DNS_POLICIES = {'DnsOverHttpsMode': 'off', 'BuiltInDnsClientEnabled': False}


class PolicyViolation(RuntimeError):
    pass


def policy():
    try:
        result = json.loads(os.environ[POLICY_ENV])
    except (KeyError, ValueError):
        raise RuntimeError('Resolved runtime policy is missing; start containers through ./start') from None
    return result


def check_country(country, expected):
    if not country or (expected and country != expected):
        raise PolicyViolation('Exit country did not match the configured policy')
