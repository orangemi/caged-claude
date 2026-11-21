"""Read-only, nonce-signed observations; no forwarding or command endpoint."""
import hmac
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import pwd
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit, parse_qs
from caged.shared.policy import APP_USER

from caged.shared.protocol import encoded, digest

BOOTSTRAP = Path('/run/caged-agent.json')


def observation(role):
    options = json.loads(os.environ['CAGED_BROWSER_POLICY'])
    resolvers = [row.split()[1] for row in Path('/etc/resolv.conf').read_text().splitlines()
                 if row.split()[:1] == ['nameserver']]
    account = pwd.getpwnam(APP_USER)
    value = {'timezone': os.environ.get('TZ'), 'locale': os.environ.get('LANG'),
            'username': account.pw_name, 'uid': account.pw_uid, 'gid': account.pw_gid,
            'user_env': os.environ.get('USER'), 'logname': os.environ.get('LOGNAME'),
            'lc_all': os.environ.get('LC_ALL'), 'language': os.environ.get('LANGUAGE'),
            'system_timezone': Path('/etc/timezone').read_text().strip(),
            'localtime': os.path.realpath('/etc/localtime'), 'offset': time.strftime('%z'),
            'localtime_matches': os.path.samefile('/etc/localtime', '/usr/share/zoneinfo/' + options['timezone']),
            'locale_valid': subprocess.run(['locale'], capture_output=True, timeout=2).returncode == 0,
            'resolvers': resolvers,
            'proxy_env_empty': not any(os.environ.get(key) for key in
                ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy'))}
    if role == 'browser':
        value['dns_policies'] = json.loads(Path('/etc/chromium/policies/managed/caged-network.json').read_text())
    return value


def serve(role):
    while not BOOTSTRAP.is_file():
        time.sleep(0.2)
    private = json.loads(BOOTSTRAP.read_text())
    if private['role'] != role:
        raise RuntimeError('Agent role mismatch')
    key = bytes.fromhex(private['key'])
    options = json.loads(os.environ['CAGED_BROWSER_POLICY'])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            url = urlsplit(self.path)
            nonce = parse_qs(url.query).get('nonce', [''])[0]
            if url.path != '/report' or not re.fullmatch('[a-f0-9]{64}', nonce):
                self.send_error(404)
                return
            value = {'role': role, 'nonce': nonce, 'session': private['session'],
                     'policy_digest': digest(options)}
            try:
                value['observation'] = observation(role)
            except Exception:
                value['error'] = 'Environment observation unavailable'
            value['signature'] = hmac.new(key, encoded(value), hashlib.sha256).hexdigest()
            body = encoded(value)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    HTTPServer(('127.0.0.1', options[role + '_probe_port']), Handler).serve_forever()


if __name__ == '__main__':
    serve(sys.argv[1])
