"""Real CLI and a persistent Linux environment across container recreation."""
from pathlib import Path
import shutil
import tempfile
import time
import unittest
import uuid

from caged.host import docker as engine, images, stateful
from caged.host.session import Session
from caged.host.instance import Instance
from caged.host.paths import ROOT
from caged.host.config import resolve


class ClaudeContainer(unittest.TestCase):
    @classmethod
    def setUpClass(cls): images.build('claude')

    @unittest.skipIf(stateful.host_os()=='Darwin','host folders on macOS keep no Linux ownership; see the image test')
    def test_environment_state_survives_kill_and_recreation(self):
        self.exercise('dir')

    def test_image_backend_survives_kill_detach_and_recreation(self):
        # Attaches a loop device through the privileged storage helper; needs
        # a Docker that allows --privileged (Linux, WSL, OrbStack, Desktop).
        self.exercise('image')

    def exercise(self,backend):
        instance='caged-persistence-'+uuid.uuid4().hex[:10]
        credentials=ROOT/'config'/('.fixture-'+uuid.uuid4().hex)
        environments=Path(tempfile.mkdtemp(prefix='caged-state-'))
        with tempfile.TemporaryDirectory(prefix='caged-shared-') as folder:
            workspace=Path(folder).resolve()
            args=resolve(['--settings',str(ROOT/'config.example/app.yaml'),'--instance',instance,
                          '--workspace',str(workspace),'--claude-config-dir',str(credentials),
                          '--state-dir',str(environments),'--state-backend',backend],environ={})
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
            def execute(*command,check=True,root=False):
                user='0:0' if root else f'{args.puid}:{args.pgid}'
                return engine.docker('exec','--user',user,names.claude,*command,check=check)
            try:
                session.prepare_state()
                self.assertTrue((session.state.root/'.caged-state').is_file())
                if backend=='image':
                    self.assertTrue(session.state.disk.is_file())
                    self.assertFalse((session.state.root/'system').exists(),'the layout lives inside disk.img')
                start()
                self.assertIn('Claude Code',execute('claude','--version').stdout)
                self.assertEqual(execute('id','-un').stdout.strip(),'claude-user')
                self.assertEqual((int(execute('id','-u').stdout),int(execute('id','-g').stdout)),(args.puid,args.pgid))
                self.assertEqual(execute('sh','-c','echo $HOME').stdout.strip(),'/home/claude')
                # This isolated fixture has no account; no model requests are made.
                auth=execute('claude','auth','status','--json',check=False)
                self.assertIn(auth.returncode,(0,1))
                self.assertTrue((credentials/'.claude.json').is_file(),'Global app state must live inside CLAUDE_CONFIG_DIR')
                # Changes in every layer: credentials, HOME, /etc, /usr/local and an
                # executable installed through sudo, plus a workspace write.
                writer=("import os,pathlib; p=pathlib.Path(os.environ['CLAUDE_CONFIG_DIR'])/'.credentials.json'; "
                        "p.write_text('{}'); p.chmod(0o600); pathlib.Path('shared-fixture.txt').write_text('from container'); "
                        "pathlib.Path.home().joinpath('notes.txt').write_text('home')")
                execute('python3','-c',writer)
                execute('sudo','-n','sh','-c',"printf 'etc\\n' > /etc/caged-fixture && printf '#!/bin/sh\\necho tool\\n' > /usr/local/bin/caged-tool && chmod 755 /usr/local/bin/caged-tool")
                self.assertEqual((workspace/'shared-fixture.txt').read_text(),'from container')
                # Writes land in the state immediately, before any stop.
                if backend=='dir':
                    self.assertEqual((session.state.home/'notes.txt').read_text(),'home')
                    self.assertTrue((session.state.system/'etc'/'caged-fixture').is_file())
                else:
                    seen=engine.call(['docker','run','--rm','--network','none','--entrypoint','/bin/sh',
                                      '--mount',f'type=volume,src={session.state.volume},dst=/state',
                                      images.IMAGES['claude'],'-c','cat /state/home/notes.txt /state/system/etc/caged-fixture']).stdout
                    self.assertEqual(seen,'homeetc\n')
                engine.docker('kill',names.claude)
                for _ in range(100):
                    if engine.inspect(names.claude) is None:break
                    time.sleep(0.1)
                if backend=='image':
                    # As after ./cage down: the file is detached and the volume gone.
                    session.state.release()
                    self.assertNotEqual(engine.docker('volume','inspect',session.state.volume,check=False).returncode,0)
                    identity=session.state.marker()['id']
                session.prepare_state()  # reuses the marked state, no reseed
                if backend=='image':
                    self.assertEqual(session.state.marker()['id'],identity)
                start()
                self.assertEqual(execute('cat','/home/claude/notes.txt').stdout,'home')
                self.assertEqual(execute('cat','/etc/caged-fixture').stdout,'etc\n')
                self.assertEqual(execute('caged-tool').stdout.strip(),'tool')
                reader="import os,pathlib; p=pathlib.Path(os.environ['CLAUDE_CONFIG_DIR']); assert (p/'.credentials.json').read_text()=='{}'; assert (p/'.claude.json').is_file()"
                execute('python3','-c',reader)
                self.assertTrue((credentials/'.credentials.json').stat().st_mode & 0o777==0o600)
                self.assertEqual(execute('touch','/caged-runtime/caged/x',check=False,root=True).returncode!=0,True,
                                 'the probe runtime must stay read-only, even for root')
            finally:
                engine.stop_managed(names.claude)
                session.state.release()  # image: detach before the file is removed
                shutil.rmtree(credentials,ignore_errors=True)
                # Seeded files are root-owned; remove them from a container.
                engine.call(['docker','run','--rm','--network','none','--user','0:0','--entrypoint','/bin/sh',
                             '--mount',f'type=bind,src={environments},dst=/state',images.IMAGES['claude'],
                             '-c','rm -rf /state/* /state/.[!.]*'],check=False,timeout=300)
                shutil.rmtree(environments,ignore_errors=True)


if __name__=='__main__':unittest.main(verbosity=2)
