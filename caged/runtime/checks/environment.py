"""Collect explicit audit evidence inside the browser container."""
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request
from caged.shared.policy import policy


def observe():
    options = policy()
    private_paths = ('/var/run/docker.sock', '/run/docker.sock',
                     '/run/caged/controller-secret', '/run/caged/config.json')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f'http://127.0.0.1:{options["controller_port"]}/configs', timeout=3) as response:
            status = response.status
    except urllib.error.HTTPError as error:
        status = error.code
    resolvers = [line.split()[1] for line in Path('/etc/resolv.conf').read_text().splitlines()
                 if line.split()[:1] == ['nameserver']]
    capabilities = int(next(row.split()[1] for row in Path('/proc/self/status').read_text().splitlines()
                            if row.startswith('CapBnd:')), 16)
    return {
        'tz': os.environ.get('TZ'),
        'timezone_file': Path('/etc/timezone').read_text().strip(),
        'localtime': os.path.realpath('/etc/localtime'),
        'offset': time.strftime('%z'),
        'lang': os.environ.get('LANG'),
        'timezone_link_matches': os.path.samefile('/etc/localtime', '/usr/share/zoneinfo/' + options['timezone']),
        'lc_all': os.environ.get('LC_ALL'),
        'locale_ok': subprocess.run(['locale'], capture_output=True).returncode == 0,
        'resolvers': resolvers,
        'management_http': status,
        'private_paths_absent': all(not Path(path).exists() for path in private_paths),
        'no_network_admin_or_raw': not capabilities & ((1 << 12) | (1 << 13)),
        'proxy_env_empty': not any(os.environ.get(key) for key in (
            'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy')),
    }


if __name__ == '__main__':
    print(json.dumps(observe()))
