"""Which nodes the cage can carry, and how a pinned node keeps its TLS name."""
import tempfile
import unittest
from pathlib import Path

from caged.host import proxy
from caged.host.errors import CageError
from caged.shared import protocols

REALITY = {'name': 'reality', 'type': 'vless', 'server': 'node.example', 'port': 443, 'uuid': 'u', 'network': 'tcp',
           'tls': True, 'flow': 'xtls-rprx-vision', 'servername': 'cover.example',
           'reality-opts': {'public-key': 'k', 'short-id': 's'}, 'client-fingerprint': 'chrome', 'udp': True}


class Supported(unittest.TestCase):
    def test_tls_over_tcp_nodes_are_carried(self):
        for node in ({'type': 'anytls'}, {'type': 'trojan'}, REALITY, {'type': 'vmess', 'tls': True, 'network': 'ws'},
                     {'type': 'vless', 'tls': True, 'network': 'grpc'}):
            with self.subTest(node=node.get('type')):
                self.assertIsNone(protocols.check(node))

    def test_other_nodes_say_why(self):
        cases = [({'type': 'hysteria2'}, 'needs UDP to the node', 'carry only TCP'),
                 ({'type': 'tuic'}, 'needs UDP to the node', 'carry only TCP'),
                 ({'type': 'ss'}, 'protocol not supported yet', 'anytls, trojan, vless, vmess over TLS'),
                 ({'type': 'vless'}, 'no TLS', 'tls: true'),
                 ({'type': 'vmess', 'tls': False}, 'no TLS', 'tls: true'),
                 ({**REALITY, 'network': 'h2'}, 'h2 transport', 'tcp, ws, grpc'),
                 ({'type': 'anytls', 'dialer-proxy': 'front'}, 'chained node', 'expanded')]
        for node, short, message in cases:
            with self.subTest(node=node):
                found = protocols.check(node)
                self.assertEqual(found[0], short)
                self.assertIn(message, found[1])
                self.assertEqual(protocols.problem(node), found[1])


class KeepName(unittest.TestCase):
    def test_each_type_keeps_the_host_in_its_own_tls_field(self):
        self.assertEqual(protocols.keep_name({'type': 'anytls'}, 'node.example')['sni'], 'node.example')
        vless = protocols.keep_name({'type': 'vless', 'tls': True}, 'node.example')
        self.assertEqual(vless['servername'], 'node.example')
        self.assertNotIn('sni', vless)
        self.assertEqual(protocols.tls_name(vless), 'node.example')

    def test_explicit_names_and_headers_are_kept(self):
        self.assertEqual(protocols.keep_name(REALITY, 'node.example')['servername'], 'cover.example')
        ws = {'type': 'vmess', 'tls': True, 'network': 'ws', 'ws-opts': {'path': '/p'}}
        kept = protocols.keep_name(ws, 'node.example')
        self.assertEqual(kept['ws-opts'], {'path': '/p', 'headers': {'Host': 'node.example'}})
        self.assertNotIn('headers', ws['ws-opts'])
        own = {**ws, 'ws-opts': {'headers': {'host': 'cdn.example'}}}
        self.assertEqual(protocols.keep_name(own, 'node.example')['ws-opts']['headers'], {'host': 'cdn.example'})


class Loading(unittest.TestCase):
    def test_node_file_lists_every_node_and_refuses_an_unsupported_one(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'nodes.yaml'
            path.write_text('{"proxies": [{"name": "reality", "type": "vless", "tls": true, "server": "node.example", '
                            '"port": 443}, {"name": "fast", "type": "hysteria2", "server": "node.example", "port": 443}]}')
            self.assertEqual([(node['name'], node['unsupported']) for node in proxy.load_node(path)],
                             [('reality', None), ('fast', 'needs UDP to the node')])
            self.assertEqual(proxy.load_node(path, 'reality')['type'], 'vless')
            with self.assertRaisesRegex(CageError, 'hysteria2 nodes need UDP'):
                proxy.load_node(path, 'fast')


if __name__ == '__main__':
    unittest.main()
