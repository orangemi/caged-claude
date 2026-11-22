"""Real traffic: kernel expiry cuts established TCP, new TCP, UDP and DNS."""
import json
import random
import socket
import subprocess
import unittest
import uuid

from caged.host import images
from tests.fixtures import script

IMAGE = images.IMAGES['network']


def docker(*args, check=True, timeout=25):
    value = subprocess.run(['docker', *args], text=True, capture_output=True, timeout=timeout)
    if check and value.returncode:
        raise RuntimeError(value.stderr or value.stdout)
    return value


class KernelLease(unittest.TestCase):
    def test_expiry_blocks_existing_flows_without_any_monitor_and_keeps_ui_and_privileged_diagnostics(self):
        prefix = 'caged-lease-' + uuid.uuid4().hex[:9]
        network, gateway, sink, client = [prefix + suffix for suffix in ('-net', '-gate', '-sink', '-client')]
        with socket.socket() as server:
            server.bind(('127.0.0.1', 0)); port = server.getsockname()[1]
        created = False
        try:
            docker('network', 'create', '--internal', '--subnet', f'198.19.{random.randint(1,254)}.0/24', network)
            created = True
            docker('run','-d','--rm','--name',sink,'--network',network,'--cap-drop','ALL','--entrypoint','python3',IMAGE,'-u','-c',script('lease_server.py'))
            target = docker('inspect',sink,'--format','{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}').stdout.strip()
            docker('run','-d','--rm','--name',gateway,'--network',network,'--cap-add','NET_ADMIN',
                   '-p',f'127.0.0.1:{port}:3000','--entrypoint','sleep',IMAGE,'infinity')
            docker('exec',gateway,'ip','link','set','eth0','name','caged0')
            docker('exec','-d',gateway,'python3','-u','-c',script('lease_server.py'))
            setup = "from caged.runtime.network import lease as g;from caged.runtime.network.firewall import command,firewall_rules;g.ROOT.mkdir(exist_ok=True);g.initialize();command(['iptables-restore'],data=firewall_rules('203.0.113.1',443,ui_port=3000))"
            docker('exec',gateway,'python3','-c',setup)
            docker('run','-d','--rm','--name',client,'--network','container:'+gateway,'--cap-drop','ALL','--entrypoint','sleep',IMAGE,'infinity')
            docker('exec',gateway,'python3','-c',"from caged.runtime.network.lease import renew;renew(5)")
            result = docker('exec',client,'python3','-c',script('lease_traffic.py'),target,timeout=20)
            observed = json.loads(result.stdout)
            self.assertTrue(all(observed['baseline'].values()))
            self.assertFalse(any(observed['expired'].values()),observed)
            diagnostics = "import socket,sys;from caged.runtime.network.lease import PROBE_MARK;s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_MARK,PROBE_MARK);s.settimeout(2);s.connect((sys.argv[1],18080));s.sendall(b'ping');assert s.recv(4)==b'ping'"
            docker('exec',gateway,'python3','-c',diagnostics,target)
            marked_udp = "import socket,sys;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.setsockopt(socket.SOL_SOCKET,socket.SO_MARK,0x434101);s.bind(('',45678));s.settimeout(2);s.sendto(b'ping',(sys.argv[1],18081));assert s.recv(4)==b'ping'"
            docker('exec',gateway,'python3','-c',marked_udp,target)
            reuse = "import socket,sys;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.bind(('',45678));s.settimeout(1);s.sendto(b'ping',(sys.argv[1],18081));s.recv(4)"
            self.assertNotEqual(docker('exec',client,'python3','-c',reuse,target,check=False).returncode,0,
                                'Reusing a diagnostic UDP tuple must not inherit its outbound permit')
            forbidden = "import socket;s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_MARK,0x434101)"
            self.assertNotEqual(docker('exec',client,'python3','-c',forbidden,check=False).returncode,0)
            ui = docker('exec',client,'python3','-c',"import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:3000',timeout=2).read().decode())")
            self.assertEqual(ui.stdout.strip(),'OK')
        finally:
            docker('rm','-f',client,gateway,sink,check=False)
            if created:docker('network','rm',network,check=False)


if __name__ == '__main__':
    unittest.main(verbosity=2)
