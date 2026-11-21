"""One instance owns its container identities, startup checks and cleanup."""
import json
import secrets
import socket
import subprocess
import sys
import time
import uuid
from . import docker as engine, images, proxy
from .config import runtime_policy
from .errors import CageError
from .instance import Instance
from .paths import ROOT, LABEL, LAYOUT_LABEL, LAYOUT_VERSION
from caged.shared.policy import POLICY_ENV, BROWSER_HTTPS_PORT, APP_USER

EMPTY_PROXY_ENV = [item for key in (
    'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
    'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy',
) for item in ('-e', key + '=')]


class Session:
    def __init__(self, args, detached=False):
        self.args = args
        self.instance = Instance(args.instance)
        self.detached = detached
        self.policy = runtime_policy(args)
        self.gateway_process = None
        self.check_process = None
        self.browser_process = None
        self.claude_process = None
        self.session_id = uuid.uuid4().hex
        self.network_container_id = None
        self.network_session_id = None
        self.agent_keys = {role: secrets.token_hex(32) for role in ('browser', 'claude')}

    def start_network(self):
        engine.ensure_names_available(self.instance.names)
        source = proxy.load_node(self.args.proxy_file_path, self.args.node)
        selected_proxy = proxy.pin_node(source)
        with socket.socket() as server:
            try:
                server.bind(('127.0.0.1', self.args.port))
            except OSError:
                raise CageError(f'Local port {self.args.port} is already in use; select another with --port.')
        images.build('network')
        command = ['docker', 'run', '--rm', '-i', '--name', self.instance.network,
                   '--label', LABEL + '=' + str(ROOT),
                   '--label', LAYOUT_LABEL + '=' + LAYOUT_VERSION,
                   '--label', 'io.caged-browser.session=' + self.session_id,
                   '--cap-add', 'NET_ADMIN', '--device', '/dev/net/tun',
                   '--read-only', '--tmpfs', '/run/caged:rw,noexec,nosuid,size=32m',
                   '--tmpfs', '/tmp:rw,noexec,nosuid,size=16m',
                   '--dns', '127.0.0.1', '-p', f'127.0.0.1:{self.args.port}:{BROWSER_HTTPS_PORT}',
                   '-e', POLICY_ENV + '=' + json.dumps(self.policy), *EMPTY_PROXY_ENV, images.IMAGES['network']]
        payload = json.dumps({'proxy': selected_proxy, 'display_name': self.args.node,
                              'session': self.session_id, 'agents': self.agent_keys}) + '\n'
        if self.detached:
            command.insert(2, '-d')
            command += ['--bootstrap-file', '/run/caged/bootstrap.json']
            engine.call(command)
            # Credentials cross stdin into private tmpfs, never Docker env/args.
            writer = "import os,sys; os.umask(0o077); p='/run/caged/bootstrap.json'; data=sys.stdin.read(); open(p+'.tmp','w').write(data); os.rename(p+'.tmp',p)"
            result = subprocess.run(['docker', 'exec', '-i', self.instance.network, 'python3', '-c', writer],
                                    input=payload, text=True, capture_output=True, timeout=30)
            if result.returncode:
                raise CageError('Cannot initialize detached gateway; no proxy configuration was printed')
        else:
            self.gateway_process = subprocess.Popen(command, stdin=subprocess.PIPE, text=True)
            self.gateway_process.stdin.write(payload)
            self.gateway_process.stdin.close()
        del selected_proxy, source
        status, state = self.wait_ready()
        self.use_network(status, state)
        egress = self.check_egress()
        print(f'Network ready: {status["node"]}; exit={egress["country"]}; global; DNS through the node; IPv6 blocked.', flush=True)

    def check_egress(self):
        self.policy_snapshot()
        self.exec_probe(self.network_container_id, 'caged.runtime.checks.network', 'dns', timeout=35)
        result = self.exec_probe(self.network_container_id, 'caged.runtime.checks.network', 'egress', timeout=30)
        return json.loads(result.stdout)

    def exec_probe(self, target, module, *arguments, user=None, **kwargs):
        identity = ['--user', user] if user else []
        return engine.docker('exec', *identity, '-e', POLICY_ENV + '=' + json.dumps(self.policy),
                      target, 'python3', '-m', module, *arguments, **kwargs)

    def policy_snapshot(self):
        result = self.exec_probe(self.network_container_id, 'caged.runtime.checks.policy')
        return json.loads(result.stdout)

    def use_network(self, status, state):
        existing = status.get('policy')
        if not existing:
            raise CageError('Gateway has no resolved runtime policy. Restart its launcher with the updated code.')
        for key in ('node', 'dns_upstream', 'controller_port', 'mixed_port'):
            if existing[key] != self.policy[key]:
                raise CageError('Existing gateway differs from the requested configuration: ' + key)
        bindings = state.get('NetworkSettings', {}).get('Ports', {}).get(f'{BROWSER_HTTPS_PORT}/tcp') or []
        local = [binding for binding in bindings if binding['HostIp'] == '127.0.0.1']
        if len(local) != 1:
            raise CageError('Network must publish its browser UI on exactly one IPv4 loopback port.')
        if self.args.port != int(local[0]['HostPort']):
            raise CageError('Existing gateway uses a different UI port; use the same settings/overrides for both launchers.')
        self.network_container_id = state['Id']
        self.network_session_id = state['Config']['Labels'].get('io.caged-browser.session')
        if not self.network_session_id:
            raise CageError('Network session identity is missing.')

    def network_running(self):
        if self.gateway_process is not None and self.gateway_process.poll() is not None:
            return False
        state = engine.inspect(self.network_container_id) if self.network_container_id else None
        return bool(state and state['State']['Running'])

    def start_check(self):
        command = ['docker', 'run', '--rm', '--name', self.instance.check,
                   '--label', LABEL + '=' + str(ROOT),
                   '--label', LAYOUT_LABEL + '=' + LAYOUT_VERSION,
                   '--label', 'io.caged-browser.session=' + self.session_id,
                   '--network', 'container:' + self.network_container_id, '--cap-drop', 'ALL',
                   '-e', POLICY_ENV + '=' + json.dumps(self.policy),
                   *EMPTY_PROXY_ENV, '--entrypoint', 'sleep', images.IMAGES['network'], 'infinity']
        self.check_process = subprocess.Popen(command)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = engine.inspect(self.instance.check)
            if state and state['State']['Running']:
                return
            if self.check_process.poll() is not None:
                raise CageError('Network probe container did not start')
            time.sleep(0.2)
        raise CageError('Network probe startup timed out')

    def verify(self):
        self.start_clients()
        self.start_check()
        result = self.exec_probe(self.instance.check, 'caged.runtime.checks.network', 'verify', timeout=85)
        print(result.stdout.strip(), flush=True)
        # The consumer retains the namespace after the gateway is removed.
        engine.stop_managed(self.instance.network, self.session_id)
        result = self.exec_probe(self.instance.check, 'caged.runtime.checks.network', 'fail-closed', timeout=25)
        print(result.stdout.strip(), flush=True)
        print('PASS: HTTPS, DNS, UDP/STUN, physical-interface block, IPv6 block and proxy-removal kill switch.', flush=True)

    def browser_command(self):
        flags = (f'--lang={self.args.language} --accept-lang={",".join(self.args.languages)} '
                 f'--remote-debugging-address=127.0.0.1 --remote-debugging-port={self.args.debug_port}')
        # Keep the launcher's terminal in normal mode so Ctrl+C always reaches
        # its cleanup handler; the GUI does not need a Docker pseudo-terminal.
        return ['docker', 'run', '--rm', '-i',
                   '--name', self.instance.browser, '--label', LABEL + '=' + str(ROOT),
                   '--label', LAYOUT_LABEL + '=' + LAYOUT_VERSION,
                   '--label', 'io.caged-browser.session=' + self.session_id,
                   '--label', 'io.caged-browser.network-session=' + self.network_session_id,
                   '--network', 'container:' + self.network_container_id,
                   '--cap-drop', 'NET_RAW', '--shm-size=' + self.args.shm_size,
                   *self.common_environment(), *self.workspace_arguments(),
                   '-e', 'CHROME_CLI=' + flags, images.IMAGES['browser']]

    def common_environment(self):
        values = {'PUID': self.args.puid, 'PGID': self.args.pgid, 'TZ': self.args.timezone,
                  'LANG': self.args.locale, 'LC_ALL': self.args.locale,
                  'LANGUAGE': self.args.language.replace('-', '_'), 'USER': APP_USER,
                  'LOGNAME': APP_USER, POLICY_ENV: json.dumps(self.policy)}
        return [item for key, value in values.items() for item in ('-e', f'{key}={value}')] + EMPTY_PROXY_ENV

    def workspace_arguments(self):
        arguments = [item for path in self.args.workspace for item in
                     ('--mount', f'type=bind,src={path},dst={path}')]
        if self.args.workspace:
            arguments += ['--workdir', str(self.args.workspace[0])]
        return arguments

    def expected_mounts(self, role):
        mounts = {str(path): str(path) for path in self.args.workspace} if role in ('browser', 'claude') else {}
        if role == 'claude':
            mounts['/config/.claude'] = str(self.args.claude_config_dir)
        return mounts

    def claude_command(self):
        return ['docker', 'run', '--rm', '--init', '--name', self.instance.claude,
                '--label', LABEL + '=' + str(ROOT),
                '--label', LAYOUT_LABEL + '=' + LAYOUT_VERSION,
                '--label', 'io.caged-browser.session=' + self.session_id,
                '--label', 'io.caged-browser.network-session=' + self.network_session_id,
                '--network', 'container:' + self.network_container_id, '--cap-drop', 'NET_RAW',
                *self.common_environment(), *self.workspace_arguments(),
                '--mount', f'type=bind,src={self.args.claude_config_dir},dst=/config/.claude', images.IMAGES['claude']]

    def start_claude(self):
        if not self.network_running():
            raise CageError('Network is no longer running. Claude was not started.')
        self.args.claude_config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        images.build('claude')
        command = self.claude_command()
        if self.detached:
            command.insert(2, '-d')
            engine.call(command)
        else:
            self.claude_process = subprocess.Popen(command)
        self.enroll_agent(self.instance.claude, 'claude')
        return self.check_claude(settings_only=True)

    def check_claude(self, settings_only=False):
        deadline = time.monotonic() + 50
        last_error = ''
        while time.monotonic() < deadline:
            if not self.network_running():
                raise CageError('Gateway exited while Claude was starting')
            result = self.exec_probe(self.instance.claude, 'caged.runtime.checks.claude', *(['--settings-only'] if settings_only else []), user=f'{self.args.puid}:{self.args.pgid}',
                                     check=False, timeout=45)
            if result.returncode == 0:
                value = json.loads(result.stdout)
                print('Claude check: ' + result.stdout.strip(), flush=True)
                return value
            last_error = (result.stdout or result.stderr).strip()
            state = engine.inspect(self.instance.claude)
            # Foreground diagnostic sessions launch docker with Popen; it may
            # not have created the container yet. Only a completed launch or
            # an already-created, stopped container proves startup failed.
            if state is not None and not state['State']['Running']:
                break
            if self.detached and state is None:
                break
            if self.claude_process is not None and self.claude_process.poll() is not None:
                break
            time.sleep(0.5)
        raise CageError('Claude container check failed: ' + last_error)

    def check_browser(self, settings_only=False):
        deadline = time.monotonic() + 100
        last_error = ''
        while time.monotonic() < deadline:
            if not self.network_running():
                raise CageError('Network exited; closing the browser.')
            result = self.exec_probe(self.instance.network, 'caged.runtime.checks.browser', *(['settings'] if settings_only else []), check=False, timeout=60)
            if result.returncode == 0:
                print('Browser check: ' + result.stdout.strip(), flush=True)
                page = engine.call(['curl', '--noproxy', '*', '-kfsSI', '--max-time', '5',
                             f'https://127.0.0.1:{self.args.port}/'])
                print('Remote browser UI: ' + page.stdout.splitlines()[0], flush=True)
                return json.loads(result.stdout)
            last_error = result.stdout.strip()
            if self.detached:
                current = engine.inspect(self.instance.browser)
                if current is None or not current['State']['Running']:
                    break
            if self.browser_process is not None and self.browser_process.poll() is not None:
                break
            time.sleep(1)
        raise CageError('Actual Chromium check failed: ' + last_error)

    def start_browser(self):
        images.build('browser')
        if not self.network_running():
            raise CageError('Network is no longer running. Browser was not started.')
        command = self.browser_command()
        if self.detached:
            command.insert(2, '-d')
            engine.call(command)
        else:
            self.browser_process = subprocess.Popen(command)
        self.enroll_agent(self.instance.browser, 'browser')
        return self.check_browser(settings_only=True)

    def enroll_agent(self, container, role):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            state = engine.inspect(container)
            if state and state['State']['Running']:
                break
            time.sleep(0.2)
        else:
            raise CageError('Application did not start: ' + role)
        writer = "import os,sys; os.umask(0o077); p='/run/caged-agent.json'; open(p+'.tmp','w').write(sys.stdin.read()); os.rename(p+'.tmp',p)"
        result = subprocess.run(['docker', 'exec', '-i', container, 'python3', '-c', writer],
                                input=json.dumps({'role': role, 'session': self.session_id, 'key': self.agent_keys[role]}),
                                capture_output=True, text=True, timeout=10)
        if result.returncode:
            raise CageError('Cannot enroll private application probe: ' + role)
        if role == 'browser':
            engine.docker('exec', container, 'bash', '/custom-cont-init.d/10-caged-settings', timeout=25)
        engine.docker('exec', '-d', '--user', '0:0', container, 'python3', '-m', 'caged.runtime.agents.environment', role)

    def wait_gate_open(self):
        deadline = time.monotonic() + 100
        last = {}
        while time.monotonic() < deadline:
            if not self.network_running():
                raise CageError('Network exited before outbound authorization')
            result = self.exec_probe(self.instance.network, 'caged.runtime.network.lease', 'status', check=False)
            if result.returncode == 0:
                last = json.loads(result.stdout)
                if last['state'] == 'open' and last['lease_remaining']:
                    return last
                if last['state'] in ('blocked', 'paused'):
                    break
            time.sleep(0.5)
        raise CageError('Network checks did not authorize outbound traffic: ' + last.get('reason', 'unavailable'))

    def start_clients(self):
        self.start_browser()
        self.start_claude()
        self.wait_gate_open()
        return self.check_browser(), self.check_claude()

    def audit(self):
        from .audit import run_audit
        run_audit(self)

    def close(self):
        for name in (self.instance.claude, self.instance.browser, self.instance.check, self.instance.network):
            try:
                engine.stop_managed(name, self.session_id)
            except Exception as error:
                print(f'Cleanup notice for {name}: {type(error).__name__}', file=sys.stderr)
        for process in (self.claude_process, self.browser_process, self.check_process, self.gateway_process):
            if process is not None:
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.terminate()

    def wait_ready(self, timeout=50):
        process, session_id = self.gateway_process, self.session_id
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = engine.inspect(self.instance.network)
            if state is not None:
                labels = state.get('Config', {}).get('Labels', {})
                if labels.get(LABEL) != str(ROOT):
                    raise CageError('The network container is not managed by this project.')
                if session_id and labels.get('io.caged-browser.session') != session_id:
                    raise CageError('The network container belongs to a different launcher.')
                if state['State']['Running']:
                    result = engine.docker('exec', state['Id'], 'cat', '/run/caged/ready.json', check=False)
                    if result.returncode == 0:
                        return json.loads(result.stdout), state
            if process is not None and process.poll() is not None:
                raise CageError('Network exited before readiness. Browser was not started.')
            if process is None and (state is None or not state['State']['Running']):
                raise CageError('Gateway exited before readiness. Run ./start again.')
            time.sleep(0.3)
        raise CageError('Gateway readiness timed out')
