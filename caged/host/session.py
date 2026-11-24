"""One instance owns its container identities, startup checks and cleanup."""
import json
import secrets
import socket
import subprocess
import sys
import time
import uuid
from . import docker as engine, hostbridge, images, proxy, stateful
from .config import runtime_policy
from .errors import CageError
from .instance import Instance
from .paths import ROOT, LABEL, LAYOUT_LABEL, LAYOUT_VERSION, IN_LAUNCHER
from caged.shared.policy import POLICY_ENV, APP_USER

EMPTY_PROXY_ENV = [item for key in (
    'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
    'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy',
) for item in ('-e', key + '=')]


class Session:
    def __init__(self, args, detached=False):
        self.args = args
        self.instance = Instance(args.instance)
        self.detached = detached
        args.host_bridge_tcp = hostbridge.tcp_endpoint(args)
        self.policy = runtime_policy(args)
        self.gateway_process = None
        self.check_process = None
        self.browser_process = None
        self.claude_process = None
        self.session_id = uuid.uuid4().hex
        self.network_container_id = None
        self.network_session_id = None
        self.agent_keys = {role: secrets.token_hex(32) for role in ('browser', 'claude')}
        self.state = stateful.open_state(args)

    def start_network(self):
        engine.ensure_names_available(self.instance.names)
        source = proxy.load_node(self.args.proxy_file_path, self.args.node)
        selected_proxy = proxy.pin_node(source)
        # Inside the launcher this would probe the container's own loopback;
        # there docker run reports a busy host port itself.
        if not IN_LAUNCHER:
            with socket.socket() as server:
                try:
                    server.bind(('127.0.0.1', self.args.port))
                except OSError:
                    raise CageError(f'Local port {self.args.port} is already in use; select another with --port.')
        images.build('network')
        # Both applications share this UTS namespace; a stable hostname keeps
        # the persisted Chromium profile lock valid across restarts.
        command = ['docker', 'run', '--rm', '-i', '--name', self.instance.network, '--hostname', self.instance.name,
                   *self.managed_labels(),
                   '--cap-add', 'NET_ADMIN', '--device', '/dev/net/tun',
                   '--read-only', '--tmpfs', '/run/caged:rw,noexec,nosuid,size=32m',
                   '--tmpfs', '/tmp:rw,noexec,nosuid,size=16m',
                   '--dns', '127.0.0.1', '-p', self.ui_publish(),
                   '-e', POLICY_ENV + '=' + json.dumps(self.policy), *EMPTY_PROXY_ENV, images.IMAGES['network']]
        payload = json.dumps({'proxy': selected_proxy, 'display_name': self.args.node,
                              'session': self.session_id, 'agents': self.agent_keys}) + '\n'
        if self.detached:
            command.insert(2, '-d')
            command += ['--bootstrap-file', '/run/caged/bootstrap.json']
            engine.call(command)
            result = engine.write_private(self.instance.network, '/run/caged/bootstrap.json', payload, timeout=30)
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
        return engine.docker('exec', *identity, *self.runtime_environment(target),
                             '-e', POLICY_ENV + '=' + json.dumps(self.policy),
                             target, 'python3', '-m', module, *arguments, **kwargs)

    def runtime_environment(self, target):
        # The Claude image carries no probe code: it is mounted read-only at
        # /caged-runtime, outside the persisted directories.
        if target == self.instance.claude:
            return ['-e', 'PYTHONPATH=' + stateful.RUNTIME, '-e', 'PYTHONSAFEPATH=1']
        return []

    def policy_snapshot(self):
        result = self.exec_probe(self.network_container_id, 'caged.runtime.checks.policy')
        return json.loads(result.stdout)

    def use_network(self, status, state):
        existing = status.get('policy')
        if not existing:
            raise CageError('Gateway has no resolved runtime policy. Restart its launcher with the updated code.')
        for key in ('node', 'dns_upstream', 'controller_port', 'mixed_port', 'browser_ui_port'):
            if existing[key] != self.policy[key]:
                raise CageError('Existing gateway differs from the requested configuration: ' + key)
        bindings = state.get('NetworkSettings', {}).get('Ports', {}).get(f'{self.args.browser_ui_port}/tcp') or []
        local = [binding for binding in bindings if binding['HostIp'] == '127.0.0.1']
        if len(local) != 1:
            raise CageError('Network must publish its browser UI on exactly one IPv4 loopback port.')
        if self.args.port != int(local[0]['HostPort']):
            raise CageError('Existing gateway uses a different UI port; use the same settings/overrides for both launchers.')
        self.network_container_id = state['Id']
        self.network_session_id = state['Config']['Labels'].get('io.caged-claude.session')
        if not self.network_session_id:
            raise CageError('Network session identity is missing.')

    def network_running(self):
        if self.gateway_process is not None and self.gateway_process.poll() is not None:
            return False
        state = engine.inspect(self.network_container_id) if self.network_container_id else None
        return bool(state and state['State']['Running'])

    def start_check(self):
        command = ['docker', 'run', '--rm', '--name', self.instance.check,
                   *self.managed_labels(),
                   '--network', 'container:' + self.network_container_id, '--cap-drop', 'ALL',
                   '-e', POLICY_ENV + '=' + json.dumps(self.policy),
                   *EMPTY_PROXY_ENV, '--entrypoint', 'sleep', images.IMAGES['network'], 'infinity']
        self.check_process = subprocess.Popen(command)

        def exited():
            if self.check_process.poll() is not None:
                raise CageError('Network probe container did not start')
        if engine.wait_running(self.instance.check, 15, exited) is None:
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
                   '--name', self.instance.browser, *self.managed_labels(client=True),
                   '--network', 'container:' + self.network_container_id,
                   '--cap-drop', 'NET_RAW', '--shm-size=' + self.args.shm_size,
                   *self.common_environment(), *self.workspace_arguments(),
                   *self.state_arguments('browser'),
                   '-e', 'CHROME_CLI=' + flags, images.IMAGES['browser']]

    def managed_labels(self, client=False):
        labels = {LABEL: str(ROOT), LAYOUT_LABEL: LAYOUT_VERSION, 'io.caged-claude.session': self.session_id}
        if client:
            labels['io.caged-claude.network-session'] = self.network_session_id
        return [item for key, value in labels.items() for item in ('--label', f'{key}={value}')]

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

    def runtime_mounts(self):
        """Read-only probe code and entrypoint, kept out of the persisted state."""
        return {stateful.RUNTIME + '/caged': str(ROOT / 'caged'),
                stateful.RUNTIME + '/entrypoint.sh': str(ROOT / 'containers' / 'claude' / 'entrypoint.sh')}

    def readonly_mounts(self):
        """Probe runtime and, when enabled, the host bridge socket directory."""
        mounts = self.runtime_mounts()
        if self.args.host_bridge:
            mounts[hostbridge.MOUNT] = str(hostbridge.bridge_dir(self.state))
        return mounts

    def state_mounts(self, role):
        return self.state.mounts(role, self.args.claude_config_dir) if role in ('browser', 'claude') else []

    def expected_mounts(self, role):
        """Destination -> host path of every bind mount of a role's container."""
        mounts = {str(path): str(path) for path in self.args.workspace} if role in ('browser', 'claude') else {}
        mounts.update({mount.destination: mount.source for mount in self.state_mounts(role) if not mount.volume})
        if role == 'claude':
            mounts.update(self.readonly_mounts())
        return mounts

    def expected_volumes(self, role):
        """Destination -> volume name of every volume mount (image state backend)."""
        return {mount.destination: mount.source for mount in self.state_mounts(role) if mount.volume}

    def state_arguments(self, role):
        return [item for mount in self.state_mounts(role) for item in ('--mount', mount.argument())]

    def claude_state_arguments(self):
        return self.state_arguments('claude') + [
            item for destination, source in self.readonly_mounts().items()
            for item in ('--mount', f'type=bind,src={source},dst={destination},readonly')]

    def claude_command(self):
        # No --init: the image's PID 1 is caged/runtime/init.py (see its docstring).
        return ['docker', 'run', '--rm', '--name', self.instance.claude,
                *self.managed_labels(client=True),
                '--network', 'container:' + self.network_container_id, '--cap-drop', 'NET_RAW',
                *self.common_environment(), *self.workspace_arguments(), *self.claude_state_arguments(),
                '-e', 'CAGED_SUDO=' + ('1' if self.args.claude_sudo else '0'),
                *(['-e', 'CAGED_HOST_USER=' + self.args.host_bridge_user,
                   '-e', 'CAGED_HOST_BRIDGE=' + hostbridge.target(self.args)] if self.args.host_bridge else []),
                images.IMAGES['claude']]

    def prepare_state(self):
        """Credentials, the seeded environment and the browser profile, before either app starts."""
        images.build('claude')  # also the image a new environment is seeded from
        self.state.ensure(images.IMAGES['claude'])
        self.state.prepare_browser()
        with self.state.locked():
            stateful.adopt_legacy_credentials(self.args)
            if self.args.host_bridge:
                hostbridge.prepare(self.state)

    def start_claude(self):
        if not self.network_running():
            raise CageError('Network is no longer running. Claude was not started.')
        images.build('claude')
        command = self.claude_command()
        if self.detached:
            command.insert(2, '-d')
            engine.call(command)
        else:
            self.claude_process = subprocess.Popen(command)
        self.enroll_agent(self.instance.claude, 'claude')
        result = self.check_claude(settings_only=True)
        self.hint_missing_ssh()
        return result

    def hint_missing_ssh(self):
        # States seeded before openssh-client was in the image lack `ssh`.
        if not self.args.host_bridge:
            return
        found = engine.docker('exec', self.instance.claude, 'sh', '-c', 'command -v ssh && command -v rsync', check=False)
        if found.returncode:
            print("Notice: the host bridge needs ssh (and rsync for rsync host:) in the Claude environment; install "
                  "them once with: ./cage shell -- sh -c 'sudo apt-get update && sudo apt-get install -y "
                  "openssh-client rsync'", flush=True)

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
                page = self.fetch_ui_head()
                print('Remote browser UI: ' + page.stdout.splitlines()[0], flush=True)
                return json.loads(result.stdout)
            last_error = (result.stdout or result.stderr).strip()
            if self.detached:
                current = engine.inspect(self.instance.browser)
                if current is None or not current['State']['Running']:
                    break
            if self.browser_process is not None and self.browser_process.poll() is not None:
                break
            time.sleep(1)
        profile_location = (f'{self.state.disk} (inside the image: browser/.config/chromium)'
                            if self.state.backend == 'image'
                            else str(self.state.browser / '.config' / 'chromium'))
        raise CageError(
            'Actual Chromium check failed: ' + last_error
            + '\nIf you manually edited a browser profile, its configuration may be invalid.'
            + '\nBrowser profiles: ' + profile_location
            + '\nRecovery: run ./cage down with the same settings/instance, back up and rename '
              'the damaged profile\'s Preferences file, then run ./cage up with the same settings/instance. '
              'For image storage, mount the image to access the browser directory.'
            + '\nRenaming Preferences resets that profile\'s browser settings. To reset all browser '
              'profiles instead, back up and rename only the .config/chromium directory; '
              'this also resets browser logins, cookies and history.'
            + '\nDo not delete the whole state directory or disk.img: they also contain the Claude environment.')

    def fetch_ui_head(self):
        # The launcher container cannot reach the host loopback port; there,
        # request the UI inside the gateway (use_network verified the publish).
        if IN_LAUNCHER:
            return engine.docker('exec', self.network_container_id, *self.ui_request(self.args.browser_ui_port))
        return engine.call(self.ui_request(self.args.port))

    def ui_publish(self):
        """The only published port: the browser UI, on host loopback."""
        return f'127.0.0.1:{self.args.port}:{self.args.browser_ui_port}'

    def ui_request(self, port):
        # -k only for https: the desktop image's certificate is self-signed.
        insecure = ['-k'] if self.args.ui_scheme == 'https' else []
        return ['curl', '--noproxy', '*', *insecure, '-fsSI', '--max-time', '5',
                f'{self.args.ui_scheme}://127.0.0.1:{port}/']

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
        if engine.wait_running(container, 20) is None:
            raise CageError('Application did not start: ' + role)
        payload = json.dumps({'role': role, 'session': self.session_id, 'key': self.agent_keys[role]})
        result = engine.write_private(container, '/run/caged-agent.json', payload, timeout=10)
        if result.returncode:
            raise CageError('Cannot enroll private application probe: ' + role)
        # The container's supervisor owns the probe process. Enrollment only
        # supplies its identity, so docker restart needs no host-side exec.

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
        self.prepare_state()
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
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
        try:
            self.state.release()  # a no-op while another launcher's containers use the state
        except Exception as error:
            print(f'Cleanup notice for the environment storage: {error}', file=sys.stderr)

    def wait_ready(self, timeout=50):
        process, session_id = self.gateway_process, self.session_id
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = engine.inspect(self.instance.network)
            if state is not None:
                labels = state.get('Config', {}).get('Labels', {})
                if labels.get(LABEL) != str(ROOT):
                    raise CageError('The network container is not managed by this project.')
                if session_id and labels.get('io.caged-claude.session') != session_id:
                    raise CageError('The network container belongs to a different launcher.')
                if state['State']['Running']:
                    result = engine.docker('exec', state['Id'], 'cat', '/run/caged/ready.json', check=False)
                    if result.returncode == 0:
                        return json.loads(result.stdout), state
            if process is not None and process.poll() is not None:
                raise CageError('Network exited before readiness. Browser was not started.')
            if process is None and (state is None or not state['State']['Running']):
                raise CageError('Gateway exited before readiness. Run ./cage up again.')
            time.sleep(0.3)
        raise CageError('Gateway readiness timed out')
