"""Eleven evidence-based checks for a new, disposable container session."""
import json
from . import docker as engine, stateful
from .errors import CageError
from caged.shared.policy import BROWSER_HTTPS_PORT
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


CHECKS = [
    ('egress_policy', '环境外出站约束'), ('proxy_transport', '终端检测通道'),
    ('dns_transport', 'DNS 与解析约束'), ('guest_settings', '系统时区与语言'),
    ('browser_settings', '浏览器时区与语言'), ('filesystem_boundary', '共享文件与宿主私有目录'),
    ('management_boundary', '宿主管理接口'), ('fail_closed', '代理故障关闭'),
    ('udp_policy', 'UDP 出站约束'), ('ipv6_policy', 'IPv6 出站约束'),
    ('exit_consistency', '出口符合已保存规则'),
]


def run_audit(session):
    instance = session.instance
    options = session.policy
    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'instance': instance.browser,
              'policy': {**options, 'udp': 'proxied', 'ipv6': 'blocked'},
              'checks': [{'id': ident, 'title': title, 'state': 'unknown', 'evidence': '尚未完成检测'}
                         for ident, title in CHECKS]}
    def passed(ident, evidence):
        item = next(row for row in report['checks'] if row['id'] == ident)
        item.update(state='passed', evidence=evidence)

    def require(condition, message):
        if not condition:
            raise CageError(message)

    try:
        browser, claude = session.start_clients()
        session.start_check()
        before = session.policy_snapshot()
        result = session.exec_probe(instance.check, 'caged.runtime.checks.network', 'verify', timeout=85)
        network = json.loads(result.stdout)
        after = session.policy_snapshot()
        require(after['counters']['CAGED_DIRECT'] > before['counters']['CAGED_DIRECT'],
                'Physical-interface probe lacked kernel deny-counter evidence')
        passed('proxy_transport', network['tcp'])
        require(after['counters']['CAGED_DNS'] >= before['counters']['CAGED_DNS'] + 3,
                'DNS negative probes lacked kernel deny-counter evidence')
        require(after['counters']['CAGED_PRIVATE'] > before['counters']['CAGED_PRIVATE'],
                'Host/metadata negative probes lacked kernel deny-counter evidence')
        passed('dns_transport', {**network['dns'], 'doh_via_selected_node': after['doh_via_node'],
                                  'deny_packets': after['counters']['CAGED_DNS'] - before['counters']['CAGED_DNS']})
        passed('udp_policy', {'policy': 'proxied; physical UDP denied', 'stun': network['udp']})
        passed('ipv6_policy', {'external': network['ipv6'], 'kernel_policy': 'default deny'})

        result = session.exec_probe(instance.browser, 'caged.runtime.checks.environment')
        system = json.loads(result.stdout)
        expected_offset = datetime.now(ZoneInfo(options['timezone'])).strftime('%z')
        require(system['tz'] == system['timezone_file'] == options['timezone']
                and system['timezone_link_matches'] and system['offset'] == expected_offset
                and system['lang'] == system['lc_all'] == options['locale'] and system['locale_ok'],
                'System timezone/locale did not match the requested settings')
        passed('guest_settings', {key: system[key] for key in ('tz', 'timezone_file', 'localtime', 'offset', 'lang', 'lc_all')})
        passed('browser_settings', browser)
        require(claude['timezone'] == options['timezone'] and claude['locale'] == options['locale']
                and claude['language'] == options['language'] and claude['offset'] == system['offset'],
                'Claude and browser environments diverged')
        next(row for row in report['checks'] if row['id'] == 'guest_settings')['evidence']['claude'] = claude
        require(system['resolvers'] == ['127.0.0.1'] and system['proxy_env_empty'],
                'Browser resolver or proxy environment diverged from the isolated network')
        gateway_state, browser_state, claude_state = [engine.inspect(name) for name in (instance.network, instance.browser, instance.claude)]
        require(gateway_state and browser_state and claude_state, 'Audit containers disappeared')
        require(all(state['HostConfig']['NetworkMode'] == 'container:' + session.network_container_id
                    for state in (browser_state, claude_state)), 'A client has another Docker network')
        passed('egress_policy', {'firewall': after['ipv4_ipv6_default_deny'], 'direct': network['physical_direct'],
                                  'layout': 'browser, Claude and unprivileged probe share only the gateway network namespace'})
        for role, state in (('gateway', gateway_state), ('browser', browser_state), ('claude', claude_state)):
            require(not state['HostConfig'].get('Privileged')
                    and not state['HostConfig'].get('PidMode'), 'Unexpected host privilege or PID sharing')
            actual = {mount['Destination']: mount['Source'] for mount in state.get('Mounts', []) if mount['Type'] == 'bind'}
            require(actual == session.expected_mounts(role), 'Unexpected host filesystem mount in ' + role)
            # Only the probe code and entrypoint are read-only; every state mount must be writable.
            require(all(mount['RW'] != mount['Destination'].startswith(stateful.RUNTIME + '/')
                        for mount in state.get('Mounts', []) if mount['Type'] == 'bind'),
                    'A state mount is read-only or the probe runtime is writable')
            require(state['HostConfig']['AutoRemove'], 'Container does not remove its temporary data on exit')
        require(system['private_paths_absent'], 'Browser can read gateway secrets or a Docker socket')
        passed('filesystem_boundary', {'workspace': [str(path) for path in session.args.workspace],
                                      'claude_state': str(session.args.claude_config_dir),
                                      'environment_state': str(session.state.root),
                                      'scope': '仅明示的 workspace、Claude 凭证与实例状态目录，以及只读的探针代码；无额外宿主挂载、Docker socket 或网关凭证'})
        require(system['management_http'] == 401 and system['no_network_admin_or_raw'],
                'Browser can administer the gateway without authentication or has network capabilities')
        ports = gateway_state['NetworkSettings']['Ports']
        published = {port: values for port, values in ports.items() if values}
        ui_service = f'{BROWSER_HTTPS_PORT}/tcp'
        require(list(published) == [ui_service]
                and all(binding['HostIp'] == '127.0.0.1' for binding in published[ui_service]),
                'A management port is publicly published')
        passed('management_boundary', {**network['host_boundary'], 'controller_without_secret': 401,
                                       'published': 'localhost UI only; controller and DevTools not published'})
        require(network['tcp']['country'] == browser['country']
                and (not options['expected_country'] or browser['country'] == options['expected_country'])
                and network['tcp']['ip'] == network['udp']['public_ip'] == browser['ip'],
                'TCP, UDP and browser exit samples did not match the configured policy')
        require(claude['tcp']['ip'] == browser['ip'], 'Claude and browser egress IP samples differ')
        passed('exit_consistency', {'country': browser['country'], 'expected_country': options['expected_country'],
                                    'sampled_tcp_udp_browser_claude_ip': browser['ip'], 'scope': 'current samples only'})

        # This audit owns a new session. Remove only its gateway; both the
        # browser and the unprivileged probe keep the actual namespace alive.
        engine.stop_managed(instance.network, session.session_id)
        result = session.exec_probe(instance.check, 'caged.runtime.checks.network', 'fail-closed', timeout=25)
        terminal_fault = json.loads(result.stdout)
        result = session.exec_probe(instance.check, 'caged.runtime.checks.browser', 'fault', timeout=20)
        browser_fault = json.loads(result.stdout)
        result = session.exec_probe(instance.claude, 'caged.runtime.checks.network', 'fail-closed',
                                    user=f'{session.args.puid}:{session.args.pgid}', timeout=25)
        passed('fail_closed', {'gateway_removed': True, 'terminal': terminal_fault,
                               'browser': browser_fault, 'claude': json.loads(result.stdout)})
    except Exception as error:
        report['error'] = str(error)
        raise
    finally:
        report['passed'] = sum(item['state'] == 'passed' for item in report['checks'])
        report['total'] = len(CHECKS)
        print('Network and isolation audit: ' + json.dumps(report, ensure_ascii=False), flush=True)
    require(report['passed'] == report['total'], 'Incomplete isolation evidence')
