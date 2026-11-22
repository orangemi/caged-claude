"""Static default-deny rules; expiring permits are maintained separately."""
import ipaddress
import subprocess
from caged.shared.policy import BROWSER_HTTPS_PORT
from .lease import LEASE, PROBE_MARK, CORE_MARK
from .mihomo import TUN


def command(args, *, data=None):
    result = subprocess.run(args, input=data, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(f'{args[0]} failed; no upstream configuration was printed')
    return result.stdout


def host_bridge_rules(host):
    """The single direct destination besides the node: the host bridge's TCP listener.

    Used where a Unix socket cannot reach the host (macOS). It sits before the
    permit gate on purpose: the bridge reaches the host, not the internet, so
    pausing outbound traffic must not cut `ssh host`."""
    if not host:
        return '', ''
    bridge_address = str(ipaddress.IPv4Address(host[0]))
    bridge_port = int(host[1])
    if not 1 <= bridge_port <= 65535:
        raise ValueError('Invalid host bridge port')
    incoming = (f'-A INPUT -i eth0 -s {bridge_address}/32 -p tcp --sport {bridge_port} '
                f'-m conntrack --ctstate ESTABLISHED -j ACCEPT\n')
    outgoing = f'-A OUTPUT -o eth0 -d {bridge_address}/32 -p tcp --dport {bridge_port} -j ACCEPT\n'
    return incoming, outgoing


def firewall_rules(address, port, host=None):
    address = str(ipaddress.IPv4Address(address))
    port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError('Invalid upstream port')
    host_in, host_out = host_bridge_rules(host)
    return f'''*filter
:INPUT DROP [0:0]
:FORWARD DROP [0:0]
:OUTPUT DROP [0:0]
:CAGED_DNS - [0:0]
:CAGED_PRIVATE - [0:0]
:CAGED_DIRECT - [0:0]
:CAGED_GATE - [0:0]
-A CAGED_GATE -m mark --mark {PROBE_MARK} -j RETURN
-A CAGED_GATE -m connmark --mark {PROBE_MARK} -m conntrack --ctdir REPLY -j RETURN
-A CAGED_GATE -m set --match-set {LEASE} src -j RETURN
-A CAGED_GATE -j DROP
-A INPUT -i eth0 -s {address}/32 -p tcp --sport {port} -m connmark --mark {CORE_MARK} -m conntrack --ctdir REPLY -j ACCEPT
{host_in}-A INPUT -p tcp --dport {BROWSER_HTTPS_PORT} -j ACCEPT
-A INPUT -i lo -p udp -m multiport --ports 53 -j CAGED_GATE
-A INPUT -i lo -p tcp -m multiport --ports 53 -j CAGED_GATE
-A INPUT -i lo -j ACCEPT
-A INPUT -j CAGED_GATE
-A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
-A INPUT -i {TUN} -j ACCEPT
-A OUTPUT -m mark --mark {PROBE_MARK} -j CONNMARK --save-mark
-A OUTPUT -o eth0 -d {address}/32 -p tcp --dport {port} -m mark --mark {CORE_MARK} -j CONNMARK --save-mark
-A OUTPUT -o eth0 -d {address}/32 -p tcp --dport {port} -m mark --mark {CORE_MARK} -j ACCEPT
{host_out}-A OUTPUT -p tcp --sport {BROWSER_HTTPS_PORT} -m conntrack --ctstate ESTABLISHED -j ACCEPT
-A OUTPUT -o lo -p udp -m multiport --ports 53 -j CAGED_GATE
-A OUTPUT -o lo -p tcp -m multiport --ports 53 -j CAGED_GATE
-A OUTPUT -o lo -j ACCEPT
-A OUTPUT -j CAGED_GATE
-A OUTPUT -p udp -m multiport --dports 53,853 -j CAGED_DNS
-A OUTPUT -p tcp -m multiport --dports 53,853 -j CAGED_DNS
-A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
-A OUTPUT -d 0.0.0.0/8 -j CAGED_PRIVATE
-A OUTPUT -d 10.0.0.0/8 -j CAGED_PRIVATE
-A OUTPUT -d 100.64.0.0/10 -j CAGED_PRIVATE
-A OUTPUT -d 169.254.0.0/16 -j CAGED_PRIVATE
-A OUTPUT -d 172.16.0.0/12 -j CAGED_PRIVATE
-A OUTPUT -d 192.168.0.0/16 -j CAGED_PRIVATE
-A OUTPUT -d 224.0.0.0/4 -j CAGED_PRIVATE
-A OUTPUT -d 240.0.0.0/4 -j CAGED_PRIVATE
-A OUTPUT -o {TUN} -j ACCEPT
-A OUTPUT -j CAGED_DIRECT
-A CAGED_DNS -j DROP
-A CAGED_PRIVATE -j DROP
-A CAGED_DIRECT -j DROP
COMMIT
'''
