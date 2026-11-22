"""Non-secret resolved policy, supplied by the launcher to every container/probe."""
import json
import os

POLICY_ENV = 'CAGED_CLAUDE_POLICY'
# The upstream desktop image serves its web UI on both ports inside the
# container. Only the one chosen by browser.ui_scheme is published and allowed
# through the gateway firewall. http is the default: browsers treat
# http://localhost as a secure context, and the UI is only bound to loopback.
BROWSER_HTTP_PORT = 3000
BROWSER_HTTPS_PORT = 3001  # self-signed certificate
BROWSER_UI_PORTS = {'http': BROWSER_HTTP_PORT, 'https': BROWSER_HTTPS_PORT}
APP_USER = 'claude-user'
BROWSER_DNS_POLICIES = {'DnsOverHttpsMode': 'off', 'BuiltInDnsClientEnabled': False}


class PolicyViolation(RuntimeError):
    pass


def policy():
    try:
        result = json.loads(os.environ[POLICY_ENV])
    except (KeyError, ValueError):
        raise RuntimeError('Resolved runtime policy is missing; start containers through ./cage up') from None
    return result


def check_country(country, expected):
    if not country or (expected and country != expected):
        raise PolicyViolation('Exit country did not match the configured policy')
