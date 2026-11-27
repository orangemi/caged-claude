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
from . import docker as engine, stateful, ui
from .errors import CageError
from .paths import ROOT
from caged.shared import zones
from caged.shared.policy import check_country, PolicyViolation


CHECKS = [
    ('egress_policy', 'Outbound containment'), ('proxy_transport', 'Terminal probe path'),
    ('dns_transport', 'DNS confinement'), ('guest_settings', 'System time and locale'),
    ('browser_settings', 'Browser time and locale'), ('filesystem_boundary', 'Shared and private files'),
    ('management_boundary', 'Host management ports'), ('fail_closed', 'Fail-closed'),
    ('udp_policy', 'UDP egress'), ('ipv6_policy', 'IPv6 egress'),
    ('exit_consistency', 'Exit policy'),
]
REMEDIES = {
    'egress_policy': 'run ./cage down && ./cage up to rebuild the gateway; do not edit its firewall by hand',
    'proxy_transport': 'check that the node works; choose another node if no exit service answers',
    'dns_transport': 'check the node connection, or list other DoH endpoints in proxy.dns_upstream',
    'guest_settings': 'apply the timezone and language again, then ./cage down && ./cage up',
    'browser_settings': 'restart the instance to load the browser settings; compare the expected and actual values',
    'filesystem_boundary': 'mount only project directories as workspaces, not your home directory or config/',
    'management_boundary': 'turn off LAN access (allow-lan) of the host proxy, or stop host services listening on all interfaces',
    'fail_closed': 'run the check again; if it keeps failing, ./cage down && ./cage up',
    'udp_policy': 'make sure the node allows UDP; without UDP the STUN probe fails',
    'ipv6_policy': 'run ./cage down && ./cage up to rebuild the gateway',
    'exit_consistency': 'choose a node outside mainland China that matches proxy.expected_country and browser.timezone',
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
        raise Unknown('the network probe returned no result')
    value = source.get(key) or {'error': 'missing'}
    if 'error' in value:
        raise (Failed if value.get('violation') else Unknown)(value['error'])
    return value


def need(value, error, what):
    if value is None:
        raise Unknown(f'no {what}: {error}')
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
        raise Failed(f'the cage can open a file in the host-private directory {private_dir}')
    if not observed['shared_read'] or not answered:
        raise Unknown('the read/write round trip through the Claude state directory did not complete')
    return {'shared_round_trip': 'passed', 'private_probe': f'{private_dir} is not visible in the cage'}


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
        raise Unknown('the drill\'s positive control did not connect, so a closed path cannot be told from a dead one')
    leaked = [key for key in ('https_literal_ip', 'udp_stun', 'public_dns') if fault[key]]
    if leaked:
        raise Failed('without the proxy route these still connected directly: ' + ', '.join(leaked))
    if not grew(before, after, 'CAGED_DIRECT'):
        raise Unknown('the drill traffic failed but left no kernel drop count, so the firewall is not proven to have closed it')
    return {'mode': 'running instance: only a dedicated UID loses the proxy route; your connections and the proxy keep running',
            'positive_control': control, 'fault': fault,
            'kernel_direct_denied': after['counters']['CAGED_DIRECT'] - before['counters']['CAGED_DIRECT'],
            'udp_control': 'passed' if control['udp_stun'] else 'failed (the node may not allow UDP); UDP is not counted',
            'browser': 'the browser shares the network namespace and kernel rules with the terminal; its own DoH is off '
                       'by policy. A real browser drill needs a disposable instance: '
                       './cage check audit --instance <new name> --port <free port>'}


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
    return {'mode': 'disposable instance: the gateway container was removed', 'gateway_removed': True,
            'terminal': terminal, 'browser': browser, 'claude': claude}


def node_connection(path):
    if path.get('kind', 'direct') == 'direct':
        return 'the gateway connects to the pinned node addresses directly'
    return (f'through the host dialer ({path["kind"]}) bound to {path.get("interface")}'
            + (f', bypassing the host TUN ({path["tunnel"]})' if path.get('tunnel') else ''))


def run_audit(session, live=False):
    instance, options, args = session.instance, session.policy, session.args
    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'instance': instance.name,
              'mode': 'live' if live else 'disposable',
              'policy': {**options, 'udp': 'proxied', 'ipv6': 'blocked'},
              'checks': [{'id': ident, 'title': title, 'state': 'unknown', 'evidence': 'not checked yet'}
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
                raise CageError('Outbound traffic is not open (' + permit.get('reason', '') + '); see ./cage status')
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
            need(before, before_error, 'policy snapshot before the probes')
            return need(after, after_error, 'policy snapshot after the probes')

        def egress_policy():
            direct = part(network, 'physical_direct')
            counters()
            if not grew(before, after, 'CAGED_DIRECT'):
                raise Unknown('the direct probe on the physical interface failed but left no kernel drop count')
            if not all(states[role] and states[role]['HostConfig']['NetworkMode'] == 'container:' + session.network_container_id
                       for role in ('browser', 'claude')):
                raise Failed('the browser or Claude uses a Docker network other than the gateway\'s')
            return {'firewall': after['ipv4_ipv6_default_deny'], 'direct': direct,
                    'kernel_direct_denied': after['counters']['CAGED_DIRECT'] - before['counters']['CAGED_DIRECT'],
                    'layout': 'the browser, Claude and the unprivileged probe share only the gateway\'s network namespace',
                    'node_connection': node_connection(session.node_path),
                    'node_interface': session.node_path.get('interface')}
        record('egress_policy', egress_policy)

        record('proxy_transport', lambda: part(network, 'tcp'))

        def dns_transport():
            dns = part(network, 'dns')
            counters()
            if not grew(before, after, 'CAGED_DNS', 3):
                raise Unknown('the public DNS bypass probes failed but the kernel drop count is too low')
            return {**dns, 'doh_via_selected_node': after['doh_via_node'],
                    'deny_packets': after['counters']['CAGED_DNS'] - before['counters']['CAGED_DNS']}
        record('dns_transport', dns_transport)

        def guest_settings():
            observed = need(system, system_error, 'system settings from the browser container')
            app = need(claude, claude_error, 'result of the Claude check')
            expected_offset = datetime.now(ZoneInfo(options['timezone'])).strftime('%z')
            if not (observed['tz'] == observed['timezone_file'] == options['timezone']
                    and observed['timezone_link_matches'] and observed['offset'] == expected_offset
                    and observed['lang'] == observed['lc_all'] == options['locale'] and observed['locale_ok']):
                raise Failed('the system timezone or language differs from the settings')
            if not (app['timezone'] == options['timezone'] and app['locale'] == options['locale']
                    and app['language'] == options['language'] and app['offset'] == observed['offset']):
                raise Failed('Claude and the browser differ in timezone or language')
            return {**{key: observed[key] for key in ('tz', 'timezone_file', 'localtime', 'offset', 'lang', 'lc_all')},
                    'claude': {key: app[key] for key in ('timezone', 'locale', 'language', 'offset')}}
        record('guest_settings', guest_settings)

        record('browser_settings', lambda: need(browser, browser_error, 'result of the browser check'))

        def filesystem_boundary():
            observed = need(system, system_error, 'system settings from the browser container')
            for role, state in states.items():
                if state is None:
                    raise Unknown(f'the {role} container is gone')
                if state['HostConfig'].get('Privileged') or state['HostConfig'].get('PidMode'):
                    raise Failed(f'the {role} container is privileged or shares the host PID namespace')
                mounts = state.get('Mounts', [])
                actual = {mount['Destination']: mount['Source'] for mount in mounts if mount['Type'] == 'bind'}
                if actual != session.expected_mounts(role):
                    raise Failed(f'the {role} container mounts a host directory outside the allowed list')
                volumes = {mount['Destination']: mount.get('Name') for mount in mounts if mount['Type'] == 'volume'}
                if volumes != session.expected_volumes(role) or any(mount['Type'] not in ('bind', 'volume') for mount in mounts):
                    raise Failed(f'the {role} container has a volume outside the allowed list')
                # What the cage must not change: the probe code the audit runs inside
                # it, the host bridge keys, the dialer socket directory. Whether the
                # other mounts are writable is a matter of function, not isolation.
                writable = [mount['Destination'] for mount in mounts
                            if mount['RW'] and mount['Destination'] in session.readonly_destinations(role)]
                if writable:
                    raise Failed(f'the {role} container can write mounts that must be read-only: ' + ', '.join(writable))
                if not state['HostConfig']['AutoRemove']:
                    raise Failed(f'the {role} container keeps its temporary data after it exits')
            if not observed['private_paths_absent']:
                raise Failed('the browser can read gateway secrets or a Docker socket')
            return {'workspace': [str(path) for path in args.workspace],
                    'claude_state': str(args.claude_config_dir), 'environment_state': session.state.describe(),
                    'active_probe': filesystem_challenge(session),
                    'scope': 'only the configured workspaces, Claude credentials, the instance state and the '
                             'read-only probe code; no other host mounts, Docker socket or gateway secrets'}
        record('filesystem_boundary', filesystem_boundary)

        def management_boundary():
            boundary = part(network, 'host_boundary')
            observed = need(system, system_error, 'system settings from the browser container')
            counters()
            if not grew(before, after, 'CAGED_PRIVATE'):
                raise Unknown('the host and metadata probes failed but left no kernel drop count')
            if observed['management_http'] != 401 or not observed['no_network_admin_or_raw']:
                raise Failed('the gateway controller answers without its secret, or an app container has network admin rights')
            published = {port: values for port, values in states['gateway']['NetworkSettings']['Ports'].items() if values}
            ui_service = f'{args.browser_ui_port}/tcp'
            if list(published) != [ui_service] or not all(binding['HostIp'] == '127.0.0.1' for binding in published[ui_service]):
                raise Failed('the gateway publishes more than the browser UI, or not only on 127.0.0.1')
            bridge = ('on: ssh host acts on the host as you; this is the one intended way out'
                      if args.host_bridge else 'off')
            return {**boundary, 'controller_without_secret': 401, 'host_bridge': bridge,
                    'published': 'only the browser UI on localhost; the controller and DevTools are not published'}
        record('management_boundary', management_boundary)

        def udp_policy():
            udp = part(network, 'udp')
            return {'policy': 'UDP goes through the node; UDP on the physical interface is dropped', 'stun': udp}
        record('udp_policy', udp_policy)

        def ipv6_policy():
            ipv6 = part(network, 'ipv6')
            counters()
            if ipv6['global_addresses']:
                raise Failed(f'the cage has {ipv6["global_addresses"]} global IPv6 addresses')
            if ipv6['default_routes'] and not grew(before, after, 'IPV6_OUTPUT'):
                raise Unknown('there is an IPv6 default route but the IPv6 probe left no kernel drop count')
            return {**ipv6, 'kernel_policy': 'default deny',
                    'ipv6_denied_packets': after['counters']['IPV6_OUTPUT'] - before['counters']['IPV6_OUTPUT']}
        record('ipv6_policy', ipv6_policy)

        def exit_consistency():
            tcp, udp = part(network, 'tcp'), part(network, 'udp')
            seen = need(browser, browser_error, 'browser exit')
            app = need(claude, claude_error, 'Claude exit')
            for country, address in ((tcp['country'], tcp['ip']), (seen['country'], seen['ip'])):
                check_country(country, options['expected_country'], address)
            if zones.country_of(options['timezone']) != tcp['country']:
                raise Failed(f'timezone {options["timezone"]} is not a zone of the exit country {tcp["country"]}')
            samples = {'terminal': tcp['ip'], 'udp': udp['public_ip'], 'browser': seen['ip'], 'claude': app['tcp']['ip']}
            if len(set(samples.values())) != 1:
                raise Failed('terminal, UDP, browser and Claude exits differ: ' + json.dumps(samples))
            return {'country': tcp['country'], 'expected_country': options['expected_country'],
                    'timezone': options['timezone'],
                    'mainland_china': 'not in a mainland range and not located in CN', 'sampled_ip': tcp['ip'],
                    'samples': list(samples), 'scope': 'these samples only'}
        record('exit_consistency', exit_consistency)

        # Last: the disposable drill removes this audit's gateway.
        record('fail_closed', lambda: live_drill(session) if live else disposable_drill(session))
        if network_error:
            report['error'] = 'network probe: ' + network_error
    except Exception as error:
        report['error'] = str(error)
        raise
    finally:
        report['passed'] = sum(row['state'] == 'passed' for row in report['checks'])
        report['total'] = len(CHECKS)
        saved = None
        try:
            folder = Path(args.state_dir) / instance.name
            if folder.is_dir():
                saved = folder / 'last-audit.json'
                stateful.write_private(saved, report)
        except OSError:
            saved = None
        print_report(report, saved)
    if report['passed'] != report['total']:
        missing = report['total'] - report['passed']
        raise CageError(f'{missing} of {report["total"]} checks did not pass; see the list above.')


# One line per passed check: the evidence a reader looks for first.
SUMMARIES = {
    'egress_policy': lambda e: f'default-deny firewall · {e["kernel_direct_denied"]} direct packets dropped · node '
                               + (f'via the host dialer on {e["node_interface"]}' if e.get('node_interface') else 'direct'),
    'proxy_transport': lambda e: f'exit {e["ip"]} ({e["country"]}) from {e["ip_source"]}',
    'dns_transport': lambda e: f'DoH through the node via {", ".join(e["upstreams"])} · '
                               f'{e["deny_packets"]} public DNS packets dropped',
    'guest_settings': lambda e: f'{e["tz"]} · {e["lang"]} · Claude matches',
    'browser_settings': lambda e: f'{e["timezone"]} · {e["language"]} · exit {e["ip"]}',
    'filesystem_boundary': lambda e: 'only the configured mounts · shared round trip works · host-private file hidden',
    'management_boundary': lambda e: f'{e["literal_targets"]} host and metadata ports unreachable · controller needs '
                                     f'its secret · host bridge {e["host_bridge"].split(":")[0]}',
    'fail_closed': lambda e: (f'{e["kernel_direct_denied"]} packets dropped once a test UID lost the proxy route'
                              if 'kernel_direct_denied' in e else 'gateway removed: terminal, browser and Claude blocked'),
    'udp_policy': lambda e: f'STUN sees {e["stun"]["public_ip"]} through the node',
    'ipv6_policy': lambda e: f'{e["global_addresses"]} global addresses · requests blocked',
    'exit_consistency': lambda e: f'{e["sampled_ip"]} ({e["country"]}, {e["timezone"]}) for terminal, UDP, browser and Claude',
}
STATES = {'passed': 'ok', 'failed': 'fail', 'unknown': 'warn'}


def summary(row):
    if row['state'] != 'passed':
        return str(row['evidence'])
    try:
        return SUMMARIES[row['id']](row['evidence'])
    except (KeyError, TypeError, AttributeError):
        return ''


def print_report(report, saved=None):
    if ui.JSON:
        ui.result(report)
        return
    where = 'running instance' if report['mode'] == 'live' else 'disposable instance'
    ui.line()
    ui.heading(f'Network and isolation audit ({where} {report["instance"]}): '
               f'{report["passed"]}/{report["total"]} passed')
    width = max(len(title) for _, title in CHECKS)
    for row in report['checks']:
        ui.step(STATES[row['state']], row['title'], summary(row),
                [('Fix', row['remedy'])] if row.get('remedy') else (), out=True, width=width)
    if saved:
        ui.line(f'Full report: {ui.short(saved, ROOT)}')
