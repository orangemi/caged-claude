"""Real CLI installation, shared workspace writes and state across recreation."""
from pathlib import Path
import shutil
import tempfile
import time
import unittest
import uuid

from caged.host import docker as engine, images
from caged.host.session import Session
from caged.host.instance import Instance
from caged.host.paths import ROOT
from caged.host.config import resolve


class ClaudeContainer(unittest.TestCase):
    @classmethod
    def setUpClass(cls): images.build('claude')

    def test_cli_state_survives_recreation_and_workspace_writes_reach_host(self):
        instance='caged-persistence-'+uuid.uuid4().hex[:10]
        state=ROOT/'config'/('.fixture-'+uuid.uuid4().hex)
        state.mkdir(parents=True,mode=0o700)
        with tempfile.TemporaryDirectory(prefix='caged-shared-') as folder:
            workspace=Path(folder).resolve()
            args=resolve(['--settings',str(ROOT/'config.example/app.yaml'),
                          '--instance',instance,'--workspace',str(workspace),'--claude-config-dir',str(state)],environ={})
            names = Instance(instance)
            session=Session(args)
            session.network_container_id,session.network_session_id='offline-fixture','offline-session'
            def start():
                cmd=session.claude_command()
                cmd.insert(2,'-d')
                cmd[cmd.index('--network')+1]='none'
                engine.call(cmd)
                for _ in range(100):
                    ready=engine.docker('exec',names.claude,'test','-f','/run/caged-claude-ready',check=False)
                    if ready.returncode==0:return
                    time.sleep(0.1)
                self.fail('Claude initialization timed out')
            def execute(*command,check=True):
                return engine.docker('exec','--user',f'{args.puid}:{args.pgid}',names.claude,*command,check=check)
            try:
                start()
                version=execute('claude','--version')
                self.assertIn('Claude Code',version.stdout)
                self.assertEqual(execute('id','-un').stdout.strip(),'claude-user')
                self.assertEqual((int(execute('id','-u').stdout),int(execute('id','-g').stdout)),(args.puid,args.pgid))
                # This isolated fixture has no account; no model requests are made.
                auth=execute('claude','auth','status','--json',check=False)
                self.assertIn(auth.returncode,(0,1))
                self.assertTrue((state/'.claude.json').is_file(),'Global app state must live inside CLAUDE_CONFIG_DIR')
                writer="import os,pathlib; p=pathlib.Path(os.environ['CLAUDE_CONFIG_DIR'])/'.credentials.json'; p.write_text('{}'); p.chmod(0o600); pathlib.Path('shared-fixture.txt').write_text('from container')"
                execute('python3','-c',writer)
                self.assertEqual((workspace/'shared-fixture.txt').read_text(),'from container')
                engine.stop_managed(names.claude)
                self.assertTrue((state/'.credentials.json').is_file())
                start()
                reader="import os,pathlib; p=pathlib.Path(os.environ['CLAUDE_CONFIG_DIR']); assert (p/'.credentials.json').read_text()=='{}'; assert (p/'.claude.json').is_file(); assert pathlib.Path('shared-fixture.txt').read_text()=='from container'"
                execute('python3','-c',reader)
                self.assertTrue((state/'.credentials.json').stat().st_mode & 0o777==0o600)
            finally:
                engine.stop_managed(names.claude)
                shutil.rmtree(state)


if __name__=='__main__':unittest.main(verbosity=2)
