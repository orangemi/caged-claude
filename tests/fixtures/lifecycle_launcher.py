import os,sys
from caged.host import cli, images, proxy, docker as engine
from caged.host.session import Session
from caged.host.errors import CageError
proxy.load_node=lambda *args: {'name':'fixture','type':'anytls','server':'203.0.113.1','port':443,'password':'non-secret-fixture','sni':'invalid.example','udp':True}
proxy.pin_node=lambda value:value
images.build=lambda *args,**kwargs:None
Session.check_egress=lambda self:{'country':'SG','ip':'203.0.113.2'}
Session.check_browser=lambda self,**kwargs:{'country':'SG','ip':'203.0.113.2'}
Session.wait_gate_open=lambda self:{}
Session.enroll_agent=lambda *args:None
# Stand-in applications run the network image, so no environment is seeded or mounted.
Session.prepare_state=lambda self:(self.args.claude_config_dir.mkdir(parents=True,exist_ok=True),self.state.prepare_browser())
Session.claude_state_arguments=lambda self:[]
def check_claude(self,**kwargs):
 if os.environ.get('CAGED_TEST_FAIL_CLAUDE'):raise CageError('injected Claude readiness failure')
 return {}
Session.check_claude=check_claude
original_call=engine.call
def call(command,**kwargs):
 if command[0] in ('sudo','orb','/sbin/route','/usr/sbin/scutil'):
  raise RuntimeError('Host networking must not be inspected or modified')
 return original_call(command,**kwargs)
engine.call=call
for method in ('browser_command','claude_command'):
 original=getattr(Session,method)
 def fixture(self,original=original):
  command=original(self)
  return command[:-1]+['--entrypoint','sleep',images.IMAGES['network'],'infinity']
 setattr(Session,method,fixture)
cli.cli(sys.argv[1:])
