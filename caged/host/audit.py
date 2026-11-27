"""Eleven evidence-based checks, on the running instance or on a new, disposable one.

Each item is passed, failed (a confirmed violation) or unknown (evidence is
missing); one missing probe never hides the evidence for the other items. The
live mode never stops the user's containers: its fault drill reroutes a single
unused UID around the proxy instead of removing the gateway."""
import json
import os
from pathlib import Path
import secrets
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from . import docker as engine, stateful
from .errors import CageError
from .paths import ROOT
from caged.shared.policy import check_country, PolicyViolation


CHECKS = [
    ('egress_policy', '环境外出站约束'), ('proxy_transport', '终端检测通道'),
    ('dns_transport', 'DNS 与解析约束'), ('guest_settings', '系统时区与语言'),
    ('browser_settings', '浏览器时区与语言'), ('filesystem_boundary', '共享文件与宿主私有目录'),
    ('management_boundary', '宿主管理接口'), ('fail_closed', '代理故障关闭'),
    ('udp_policy', 'UDP 出站约束'), ('ipv6_policy', 'IPv6 出站约束'),
    ('exit_consistency', '出口符合已保存规则'),
]
REMEDIES = {
    'egress_policy': '执行 ./cage down && ./cage up 重建网关；不要手动修改网关防火墙',
    'proxy_transport': '确认节点可用；无法连到任何检测服务时更换节点',
    'dns_transport': '检查节点连通性，或在 proxy.dns_upstream 中增加其他 DoH 端点',
    'guest_settings': '重新应用时区与语言后执行 ./cage down && ./cage up',
    'browser_settings': '重启实例以加载浏览器设置；仍失败时查看各字段的期望值与实际值',
    'filesystem_boundary': '只把项目目录配置为 workspace；不要共享主目录或 config/',
    'management_boundary': '关闭宿主代理的局域网监听（allow-lan），或停止对外监听的宿主服务',
    'fail_closed': '重新运行检查；持续失败时执行 ./cage down && ./cage up',
    'udp_policy': '确认节点开启 UDP；不支持 UDP 的节点会让 STUN 探测失败',
    'ipv6_policy': '执行 ./cage down && ./cage up 重建网关',
    'exit_consistency': '选择非中国大陆、且符合 proxy.expected_country 的节点',
}
DRILL_UID = 65533
DRILL_RULE = ['uidrange', f'{DRILL_UID}-{DRILL_UID}', 'lookup', 'main', 'priority', '100']


class Failed(CageError):
    """Confirmed violation."""


class Unknown(CageError):
    """Required evidence is missing."""


def part(source, key):
    """One probe result from network verify, raising when it is missing or failed."""
    if source is None:
        raise Unknown('网络探针没有返回结果')
    value = source.get(key) or {'error': 'missing'}
    if 'error' in value:
        raise (Failed if value.get('violation') else Unknown)(value['error'])
    return value


def need(value, error, what):
    if value is None:
        raise Unknown(f'未取得{what}：{error}')
    return value


def gather(function):
    try:
        return function(), None
    except Exception as error:
        return None, str(error) or type(error).__name__


def grew(before, after, chain, minimum=1):
    if before is None or after is None:
        return False
    return after['counters'][chain] - before['counters'][chain] >= minimum


def probe_json(result):
    value = json.loads(result.stdout or '{}')
    if result.returncode or 'error' in value:
        raise Unknown(value.get('error') or 'probe failed')
    return value


def filesystem_challenge(session):
    """Round-trip a token through the shared Claude state directory, and make sure
    a file in a host-private directory is not visible at its host path."""
    args = session.args
    token, name = secrets.token_hex(32), '.caged-audit-' + secrets.token_hex(16)
    private_dir = ROOT / 'config' if os.access(ROOT / 'config', os.W_OK) else stateful.REGISTRY
    shared = Path(args.claude_config_dir) / name
    reply = shared.with_name(name + '.reply')
    private = private_dir / name
    try:
        shared.write_text(token)
        shared.chmod(0o644)
        private.write_text(token)
        result = session.exec_probe(session.instance.claude, 'caged.runtime.checks.boundary', name, token,
                                    str(private), user=f'{args.puid}:{args.pgid}', check=False, timeout=20)
        observed = probe_json(result)
        answered = reply.is_file() and reply.read_text() == token[::-1]
    finally:
        for path in (shared, reply, private):
            path.unlink(missing_ok=True)
    if observed['private_readable']:
        raise Failed(f'环境内能打开宿主私有目录中的文件：{private_dir}')
    if not observed['shared_read'] or not answered:
        raise Unknown('Claude 状态目录的共享读写往返没有完成')
    return {'shared_round_trip': 'passed', 'private_probe': f'{private_dir} 在环境内不可见'}


def live_drill(session):
    """Proxy failure for one UID only: its traffic loses the TUN route, as when the
    proxy stops, while the user's own connections keep running."""
    gateway = session.network_container_id

    def run():
        return probe_json(session.exec_probe(gateway, 'caged.runtime.checks.network', 'drill',
                                             user=f'{DRILL_UID}:{DRILL_UID}', check=False, timeout=40))
    control = run()
    before = session.policy_snapshot()
    engine.docker('exec', gateway, 'ip', 'rule', 'add', *DRILL_RULE)
    try:
        fault = run()
    finally:
        engine.docker('exec', gateway, 'ip', 'rule', 'del', *DRILL_RULE, check=False)
    after = session.policy_snapshot()
    if not control['https_literal_ip']:
        raise Unknown('故障演练的正向对照没有连通，无法区分“故障关闭”与“本来就不通”')
    leaked = [key for key in ('https_literal_ip', 'udp_stun', 'public_dns') if fault[key]]
    if leaked:
        raise Failed('代理路由缺失时仍能直连：' + ', '.join(leaked))
    if not grew(before, after, 'CAGED_DIRECT'):
        raise Unknown('演练流量失败，但没有留下内核拒绝计数，无法确认由防火墙关闭')
    return {'mode': '运行中实例：仅让一个专用 UID 的流量失去代理路由，用户连接与代理进程保持运行',
            'positive_control': control, 'fault': fault,
            'kernel_direct_denied': after['counters']['CAGED_DIRECT'] - before['counters']['CAGED_DIRECT'],
            'udp_control': '通过' if control['udp_stun'] else '对照未通过（节点可能不支持 UDP），UDP 故障证据不计入',
            'browser': '浏览器与终端共享同一网络命名空间与内核规则；浏览器自带的 DoH 已由受管策略关闭。'
                       '真实浏览器的断网演练需要一次性实例：./cage check audit --instance <新实例名> --port <空闲端口>'}


def disposable_drill(session):
    """Remove this audit's own gateway; the browser and the probe keep the namespace."""
    instance = session.instance
    engine.stop_managed(instance.network, session.session_id)
    terminal = probe_json(session.exec_probe(instance.check, 'caged.runtime.checks.network', 'fail-closed',
                                             check=False, timeout=25))
    browser = probe_json(session.exec_probe(instance.check, 'caged.runtime.checks.browser', 'fault',
                                            check=False, timeout=20))
    claude = probe_json(session.exec_probe(instance.claude, 'caged.runtime.checks.network', 'fail-closed',
                                           user=f'{session.args.puid}:{session.args.pgid}', check=False, timeout=25))
    return {'mode': '一次性实例：真实删除网关容器', 'gateway_removed': True,
            'terminal': terminal, 'browser': browser, 'claude': claude}


def node_connection(path):
    if path.get('kind', 'direct') == 'direct':
        return '网关直接连接固定的节点地址'
    return (f'经宿主拨号器（{path["kind"]}）连接，出站绑定物理网卡 {path.get("interface")}'
            + (f'，绕开宿主 TUN（{path["tunnel"]}）' if path.get('tunnel') else ''))


def run_audit(session, live=False):
    instance, options, args = session.instance, session.policy, session.args
    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'instance': instance.name,
              'mode': 'live' if live else 'disposable',
              'policy': {**options, 'udp': 'proxied', 'ipv6': 'blocked'},
              'checks': [{'id': ident, 'title': title, 'state': 'unknown', 'evidence': '尚未完成检测'}
                         for ident, title in CHECKS]}
    rows = {row['id']: row for row in report['checks']}

    def record(ident, function):
        row = rows[ident]
        try:
            row.update(state='passed', evidence=function())
            row.pop('remedy', None)
        except (Failed, PolicyViolation) as error:
            row.update(state='failed', evidence=str(error), remedy=REMEDIES[ident])
        except Exception as error:
            row.update(state='unknown', evidence=str(error) or type(error).__name__, remedy=REMEDIES[ident])

    try:
        if live:
            session.attach()
            permit = json.loads(session.exec_probe(session.network_container_id, 'caged.runtime.network.lease',
                                                   'status').stdout)
            if permit['state'] != 'open' or not permit['lease_remaining']:
                raise CageError('外网许可当前未开放（' + permit.get('reason', '') + '）；先用 ./cage status 查看原因')
            (browser, browser_error), (claude, claude_error) = gather(session.check_browser), gather(session.check_claude)
        else:
            clients, error = gather(session.start_clients)
            (browser, claude), browser_error, claude_error = clients or (None, None), error, error
        session.start_check()
        before, before_error = gather(session.policy_snapshot)
        network, network_error = gather(lambda: json.loads(session.exec_probe(
            instance.check, 'caged.runtime.checks.network', 'verify', timeout=150).stdout))
        after, after_error = gather(session.policy_snapshot)
        system, system_error = gather(lambda: json.loads(session.exec_probe(
            instance.browser, 'caged.runtime.checks.environment').stdout))
        states = {role: engine.inspect(name) for role, name in
                  (('gateway', instance.network), ('browser', instance.browser), ('claude', instance.claude))}

        def counters():
            need(before, before_error, '检查前的策略快照')
            return need(after, after_error, '检查后的策略快照')

        def egress_policy():
            direct = part(network, 'physical_direct')
            counters()
            if not grew(before, after, 'CAGED_DIRECT'):
                raise Unknown('物理网卡直连探针失败，但没有留下内核拒绝计数')
            if not all(states[role] and states[role]['HostConfig']['NetworkMode'] == 'container:' + session.network_container_id
                       for role in ('browser', 'claude')):
                raise Failed('浏览器或 Claude 使用了网关以外的 Docker 网络')
            return {'firewall': after['ipv4_ipv6_default_deny'], 'direct': direct,
                    'kernel_direct_denied': after['counters']['CAGED_DIRECT'] - before['counters']['CAGED_DIRECT'],
                    'layout': '浏览器、Claude 与无特权探针只共享网关的网络命名空间',
                    'node_connection': node_connection(session.node_path)}
        record('egress_policy', egress_policy)

        record('proxy_transport', lambda: part(network, 'tcp'))

        def dns_transport():
            dns = part(network, 'dns')
            counters()
            if not grew(before, after, 'CAGED_DNS', 3):
                raise Unknown('公共 DNS 绕过探针失败，但内核拒绝计数不足')
            return {**dns, 'doh_via_selected_node': after['doh_via_node'],
                    'deny_packets': after['counters']['CAGED_DNS'] - before['counters']['CAGED_DNS']}
        record('dns_transport', dns_transport)

        def guest_settings():
            observed = need(system, system_error, '浏览器容器的系统设置')
            app = need(claude, claude_error, 'Claude 容器的检查结果')
            expected_offset = datetime.now(ZoneInfo(options['timezone'])).strftime('%z')
            if not (observed['tz'] == observed['timezone_file'] == options['timezone']
                    and observed['timezone_link_matches'] and observed['offset'] == expected_offset
                    and observed['lang'] == observed['lc_all'] == options['locale'] and observed['locale_ok']):
                raise Failed('系统时区或语言与配置不一致')
            if not (app['timezone'] == options['timezone'] and app['locale'] == options['locale']
                    and app['language'] == options['language'] and app['offset'] == observed['offset']):
                raise Failed('Claude 与浏览器环境的时区或语言不一致')
            return {**{key: observed[key] for key in ('tz', 'timezone_file', 'localtime', 'offset', 'lang', 'lc_all')},
                    'claude': {key: app[key] for key in ('timezone', 'locale', 'language', 'offset')}}
        record('guest_settings', guest_settings)

        record('browser_settings', lambda: need(browser, browser_error, '浏览器检查结果'))

        def filesystem_boundary():
            observed = need(system, system_error, '浏览器容器的系统设置')
            for role, state in states.items():
                if state is None:
                    raise Unknown(role + ' 容器已不存在')
                if state['HostConfig'].get('Privileged') or state['HostConfig'].get('PidMode'):
                    raise Failed(role + ' 容器有特权或共享了宿主 PID')
                mounts = state.get('Mounts', [])
                actual = {mount['Destination']: mount['Source'] for mount in mounts if mount['Type'] == 'bind'}
                if actual != session.expected_mounts(role):
                    raise Failed(role + ' 容器挂载了白名单以外的宿主目录')
                volumes = {mount['Destination']: mount.get('Name') for mount in mounts if mount['Type'] == 'volume'}
                if volumes != session.expected_volumes(role) or any(mount['Type'] not in ('bind', 'volume') for mount in mounts):
                    raise Failed(role + ' 容器有白名单以外的卷')
                # What the cage must not change: the probe code the audit runs inside
                # it, the host bridge keys, the dialer socket directory. Whether the
                # other mounts are writable is a matter of function, not isolation.
                writable = [mount['Destination'] for mount in mounts
                            if mount['RW'] and mount['Destination'] in session.readonly_destinations(role)]
                if writable:
                    raise Failed(f'{role} 容器中应只读的挂载可写：' + '、'.join(writable))
                if not state['HostConfig']['AutoRemove']:
                    raise Failed(role + ' 容器退出时不会清理临时数据')
            if not observed['private_paths_absent']:
                raise Failed('浏览器能读到网关密钥或 Docker socket')
            return {'workspace': [str(path) for path in args.workspace],
                    'claude_state': str(args.claude_config_dir), 'environment_state': session.state.describe(),
                    'active_probe': filesystem_challenge(session),
                    'scope': '仅明示的 workspace、Claude 凭证与实例状态目录，以及只读的探针代码；'
                             '无额外宿主挂载、Docker socket 或网关凭证'}
        record('filesystem_boundary', filesystem_boundary)

        def management_boundary():
            boundary = part(network, 'host_boundary')
            observed = need(system, system_error, '浏览器容器的系统设置')
            counters()
            if not grew(before, after, 'CAGED_PRIVATE'):
                raise Unknown('宿主与元数据探针失败，但没有留下内核拒绝计数')
            if observed['management_http'] != 401 or not observed['no_network_admin_or_raw']:
                raise Failed('网关控制接口可匿名访问，或应用容器有网络管理权限')
            published = {port: values for port, values in states['gateway']['NetworkSettings']['Ports'].items() if values}
            ui_service = f'{args.browser_ui_port}/tcp'
            if list(published) != [ui_service] or not all(binding['HostIp'] == '127.0.0.1' for binding in published[ui_service]):
                raise Failed('网关发布了浏览器界面以外的端口，或未只绑定 127.0.0.1')
            bridge = ('开启：环境内可通过 ssh host 以你的身份操作宿主，这是有意保留的唯一通道'
                      if args.host_bridge else '关闭')
            return {**boundary, 'controller_without_secret': 401, 'host_bridge': bridge,
                    'published': '只发布 localhost 浏览器界面；控制器与 DevTools 不发布'}
        record('management_boundary', management_boundary)

        def udp_policy():
            udp = part(network, 'udp')
            return {'policy': 'UDP 经节点转发；物理网卡 UDP 由默认拒绝策略丢弃', 'stun': udp}
        record('udp_policy', udp_policy)

        def ipv6_policy():
            ipv6 = part(network, 'ipv6')
            counters()
            if ipv6['global_addresses']:
                raise Failed(f'环境内有 {ipv6["global_addresses"]} 个全局 IPv6 地址')
            if ipv6['default_routes'] and not grew(before, after, 'IPV6_OUTPUT'):
                raise Unknown('存在 IPv6 默认路由，但 IPv6 探针没有留下内核拒绝计数')
            return {**ipv6, 'kernel_policy': 'default deny',
                    'ipv6_denied_packets': after['counters']['IPV6_OUTPUT'] - before['counters']['IPV6_OUTPUT']}
        record('ipv6_policy', ipv6_policy)

        def exit_consistency():
            tcp, udp = part(network, 'tcp'), part(network, 'udp')
            seen = need(browser, browser_error, '浏览器出口')
            app = need(claude, claude_error, 'Claude 出口')
            for country, address in ((tcp['country'], tcp['ip']), (seen['country'], seen['ip'])):
                check_country(country, options['expected_country'], address)
            samples = {'terminal': tcp['ip'], 'udp': udp['public_ip'], 'browser': seen['ip'], 'claude': app['tcp']['ip']}
            if len(set(samples.values())) != 1:
                raise Failed('终端、UDP、浏览器与 Claude 的出口 IP 不一致：' + json.dumps(samples))
            return {'country': tcp['country'], 'expected_country': options['expected_country'],
                    'mainland_china': '未命中大陆网段，定位也不是 CN', 'sampled_ip': tcp['ip'],
                    'samples': list(samples), 'scope': '仅限本次采样'}
        record('exit_consistency', exit_consistency)

        # Last: the disposable drill removes this audit's gateway.
        record('fail_closed', lambda: live_drill(session) if live else disposable_drill(session))
        if network_error:
            report['error'] = '网络探针：' + network_error
    except Exception as error:
        report['error'] = str(error)
        raise
    finally:
        report['passed'] = sum(row['state'] == 'passed' for row in report['checks'])
        report['total'] = len(CHECKS)
        print_report(report)
        try:
            folder = Path(args.state_dir) / instance.name
            if folder.is_dir():
                stateful.write_private(folder / 'last-audit.json', report)
        except OSError:
            pass
    if report['passed'] != report['total']:
        raise CageError(f'Isolation evidence incomplete: {report["passed"]}/{report["total"]} checks passed')


def print_report(report):
    marks = {'passed': '通过', 'failed': '失败', 'unknown': '未知'}
    print(f'\n网络与隔离检查（{"当前运行实例" if report["mode"] == "live" else "一次性实例"} {report["instance"]}）'
          f'：{report["passed"]}/{report["total"]} 通过', flush=True)
    for row in report['checks']:
        line = f'  [{marks[row["state"]]}] {row["title"]}'
        if row['state'] != 'passed':
            line += f'：{row["evidence"]}'
            if row.get('remedy'):
                line += f'（处理：{row["remedy"]}）'
        print(line, flush=True)
    print('Network and isolation audit: ' + json.dumps(report, ensure_ascii=False), flush=True)
