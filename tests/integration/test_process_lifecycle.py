"""Exercise detached startup, rollback and stop against real Docker containers."""
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import uuid

from caged.host import docker as engine
from caged.host.instance import Instance
from caged.host.errors import CageError
from caged.host.paths import ROOT, LABEL


class ProcessLifecycle(unittest.TestCase):
    def setUp(self):
        self.instance = 'caged-lifecycle-' + uuid.uuid4().hex[:10]
        self.names = Instance(self.instance)
        self.temp = tempfile.TemporaryDirectory(prefix='caged-workspace-')
        self.workspace = Path(self.temp.name).resolve()
        self.state = ROOT / 'config' / ('.fixture-' + uuid.uuid4().hex)
        self.environments = tempfile.TemporaryDirectory(prefix='caged-state-')
        self.log = tempfile.TemporaryFile(mode='w+')
        with socket.socket() as server:
            server.bind(('127.0.0.1', 0))
            self.port = server.getsockname()[1]

    def launch(self, action, fail=False):
        env={key:value for key,value in os.environ.items() if not key.startswith('CAGED_CLAUDE_')}
        env.update(PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE='1')
        if fail:env['CAGED_TEST_FAIL_CLAUDE']='1'
        result=subprocess.run([sys.executable, '-u', str(ROOT/'tests/fixtures/lifecycle_launcher.py'), action,
            '--settings',str(ROOT/'config.example/app.yaml'),'--port',str(self.port),'--instance',self.instance,
            '--workspace',str(self.workspace),'--claude-config-dir',str(self.state),
            '--state-dir',self.environments.name],
            env=env,stdin=subprocess.DEVNULL,stdout=self.log,stderr=subprocess.STDOUT,timeout=50)
        return result.returncode

    def diagnostic(self):
        self.log.flush();self.log.seek(0)
        return self.log.read()[-3000:]

    def assert_removed(self):
        for name in (self.names.claude,self.names.browser,self.names.network):
            self.assertIsNone(engine.inspect(name),name+' was left behind')

    def tearDown(self):
        for name in (self.names.claude,self.names.browser,self.names.check,self.names.network):
            state=engine.inspect(name)
            if state and state.get('Config',{}).get('Labels',{}).get(LABEL)==str(ROOT):
                engine.stop_managed(name)
        self.log.close()
        self.temp.cleanup()
        self.environments.cleanup()
        if self.state.exists():shutil.rmtree(self.state)

    def test_start_returns_and_all_three_containers_share_the_gateway(self):
        self.assertEqual(self.launch('start'),0,self.diagnostic())
        gateway=engine.inspect(self.names.network)
        self.assertTrue(gateway['State']['Running'])
        for name in (self.names.browser,self.names.claude):
            current=engine.inspect(name)
            self.assertTrue(current['State']['Running'])
            self.assertEqual(current['HostConfig']['NetworkMode'],'container:'+gateway['Id'])
            self.assertTrue(current['HostConfig']['AutoRemove'])
            mounts={item['Destination']:item['Source'] for item in current['Mounts'] if item['Type']=='bind'}
            self.assertEqual(mounts[str(self.workspace)],str(self.workspace))
        env=lambda name:dict(item.split('=',1) for item in engine.inspect(name)['Config']['Env'] if '=' in item)
        browser,claude=env(self.names.browser),env(self.names.claude)
        for key in ('TZ','LANG','LC_ALL','LANGUAGE','PUID','PGID','CAGED_CLAUDE_POLICY'):
            self.assertEqual(browser[key],claude[key],key)

    def test_stop_removes_all_containers_and_keeps_persistent_state(self):
        self.assertEqual(self.launch('start'),0,self.diagnostic())
        marker=self.state/'persistent-fixture.json'
        marker.write_text('{"fixture":true}')
        self.assertEqual(self.launch('stop'),0,self.diagnostic())
        self.assert_removed()
        self.assertTrue(marker.is_file())

    def test_partial_startup_failure_rolls_back_all_three_containers(self):
        self.assertNotEqual(self.launch('start',fail=True),0)
        self.assertIn('injected Claude readiness failure',self.diagnostic())
        self.assert_removed()

    def test_gateway_failure_is_reported_without_replacing_client_networks(self):
        self.assertEqual(self.launch('start'),0,self.diagnostic())
        engine.docker('stop','-t','5',self.names.network)
        self.assertEqual(self.launch('status'),0,self.diagnostic())
        self.assertIn('gateway unavailable; outbound blocked',self.diagnostic())
        for name in (self.names.browser,self.names.claude):
            self.assertTrue(engine.inspect(name)['State']['Running'])
        from caged.host.claude import command
        with self.assertRaisesRegex(CageError,'gateway is unavailable'):
            command(['--instance',self.instance,'--app-settings',str(ROOT/'config.example/app.yaml')],tty=False)


if __name__=='__main__':unittest.main(verbosity=2)
