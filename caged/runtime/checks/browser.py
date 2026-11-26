#!/usr/bin/env python3
"""Observe a dedicated diagnostic tab, then close it without touching user tabs."""
import argparse
import json
import socket
import time
import urllib.request
import uuid
import websocket
from caged.shared.policy import policy, check_country, PolicyViolation, BROWSER_DNS_POLICIES
from caged.shared.mainland import is_mainland
from caged.runtime.checks import egress

POLICIES = BROWSER_DNS_POLICIES


def observe(fault=False, settings_only=False):
    options = policy()
    debug_url = f'http://127.0.0.1:{options["debug_port"]}'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(debug_url + '/json/new?about:blank', method='PUT')
    with opener.open(request, timeout=3) as response:
        page = json.load(response)
    client = websocket.create_connection(page['webSocketDebuggerUrl'], timeout=30, suppress_origin=True)
    request_id = 0

    def rpc(method, params=None):
        nonlocal request_id
        request_id += 1
        client.send(json.dumps({'id': request_id, 'method': method, 'params': params or {}}))
        while True:
            message = json.loads(client.recv())
            if message.get('id') == request_id:
                if 'error' in message:
                    raise RuntimeError('Chromium debugging request failed: ' + method)
                return message.get('result', {})

    def evaluate(expression):
        return rpc('Runtime.evaluate', {'expression': expression, 'returnByValue': True}).get('result', {}).get('value')

    try:
        rpc('Network.enable')
        rpc('Network.setCacheDisabled', {'cacheDisabled': True})
        rpc('Network.setBypassServiceWorker', {'bypass': True})
        if fault:
            client.settimeout(8)
            try:
                result = rpc('Page.navigate', {'url': 'https://1.1.1.1/cdn-cgi/trace?caged=' + uuid.uuid4().hex})
                if not result.get('errorText'):
                    raise RuntimeError('Browser navigation survived proxy removal')
            except (socket.timeout, websocket.WebSocketTimeoutException):
                client.settimeout(3)
                rpc('Page.stopLoading')
            return {'fresh_literal_ip_navigation': 'blocked', 'cache_and_service_workers': 'bypassed'}

        rpc('Page.navigate', {'url': 'chrome://policy'})
        deadline = time.monotonic() + 12
        policies = []
        expression = '''(() => {
          const rows = [];
          function walk(root) {
            for (const e of root.querySelectorAll('*')) {
              if (e.tagName === 'POLICY-ROW' && e.policy) rows.push(e.policy);
              if (e.shadowRoot) walk(e.shadowRoot);
            }
          }
          walk(document);
          return rows.filter(p => ['DnsOverHttpsMode','BuiltInDnsClientEnabled'].includes(p.name));
        })()'''
        while time.monotonic() < deadline:
            policies = evaluate(expression) or []
            if len(policies) == len(POLICIES):
                break
            time.sleep(0.25)
        for name, expected in POLICIES.items():
            row = next((row for row in policies if row.get('name') == name), None)
            if row is None or row.get('value') != expected or row.get('error') or row.get('ignored'):
                raise PolicyViolation('Chromium managed policy was not applied: ' + name)

        if settings_only:
            rpc('Page.navigate', {'url': 'about:blank'})
            value = evaluate('({language:navigator.language,languages:navigator.languages,timezone:Intl.DateTimeFormat().resolvedOptions().timeZone})')
            canonical_timezone = evaluate('new Intl.DateTimeFormat(undefined,{timeZone:' + json.dumps(options['timezone']) + '}).resolvedOptions().timeZone')
            if (value['timezone'] != canonical_timezone or value['language'] != options['language']
                    or value['languages'] != options['languages']):
                raise PolicyViolation('Browser runtime settings differ from the launch policy')
            value['dns_policies'] = POLICIES
            return value

        def page_text(url, timeout):
            if rpc('Page.navigate', {'url': url}).get('errorText'):
                return None
            deadline = time.monotonic() + timeout
            origin = json.dumps(url)
            while time.monotonic() < deadline:
                # Until the navigation commits, the previous page is still readable.
                text = evaluate(f"location.origin === new URL({origin}).origin ? (document.body?.innerText || '') : ''")
                if text and text.strip():
                    return text
                time.sleep(0.2)
            return None

        # Positive control for the exact literal-IP origin used by the fault
        # probe. A certificate error must never look like a working kill switch.
        ip, source, failures = None, None, {}
        for name, url in [item for item in egress.IP_SOURCES if item[0] == 'cloudflare'] + [
                item for item in egress.IP_SOURCES if item[0] != 'cloudflare']:
            text = page_text(url + ('&' if '?' in url else '?') + 'caged=' + uuid.uuid4().hex, 12)
            ip = egress.parse_ip(name, text or '')
            if ip:
                source = name
                break
            failures[name] = 'no public IPv4 in the page'
        if not ip:
            raise RuntimeError('No exit IP service answered in the browser (' + egress.summary(failures) + ')')
        # The gateway locates the browser's exit the same way as the terminal's.
        from caged.runtime.checks.network import request
        if is_mainland(ip):
            raise PolicyViolation('Browser exit IP is in a mainland China range; mainland exits are not allowed')
        country, located_by, location_failures = egress.locate(ip, request)
        if not country:
            raise RuntimeError('Browser exit IP measured, but every location service failed ('
                               + egress.summary(location_failures) + ')')
        check_country(country, options['expected_country'], ip)
        value = evaluate('({language:navigator.language, languages:navigator.languages,'
                         'timezone:Intl.DateTimeFormat().resolvedOptions().timeZone})')
        expected_timezone = evaluate('new Intl.DateTimeFormat(undefined, {timeZone:'
                                     + json.dumps(options['timezone']) + '}).resolvedOptions().timeZone')
        if value.get('timezone') != expected_timezone:
            raise RuntimeError('Browser timezone did not match the configured timezone')
        if value.get('language') != options['language'] or value.get('languages') != options['languages']:
            raise RuntimeError('Browser language preferences did not match the configured language')
        value.update(ip=ip, country=country, ip_source=source, location_source=located_by,
                     dns_policies={row['name']: row['value'] for row in policies},
                     literal_ip_positive_control='passed' if source == 'cloudflare' else 'unavailable')
        return value
    finally:
        client.close()
        try:
            opener.open(debug_url + '/json/close/' + page['id'], timeout=3).close()
        except OSError:
            pass


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', nargs='?', choices=['normal', 'fault', 'settings'], default='normal')
    args = parser.parse_args()
    try:
        print(json.dumps(observe(args.action == 'fault', args.action == 'settings')))
    except Exception as error:
        print(json.dumps({'error': str(error)}))
        raise SystemExit(1)
