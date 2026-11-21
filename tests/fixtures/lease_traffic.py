import socket,sys,json,time
host=sys.argv[1]
def tcp():
 try:
  with socket.create_connection((host,18080),timeout=1) as s:s.sendall(b'ping');return s.recv(4)==b'ping'
 except OSError:return False
def udp(address,port):
 try:
  with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as s:
   s.settimeout(1);s.sendto(b'ping',(address,port));return s.recv(4)==b'ping'
 except OSError:return False
with socket.create_connection((host,18080),timeout=1) as stream:
 stream.settimeout(1);stream.sendall(b'ping');before=stream.recv(4)==b'ping'
 baseline={'tcp':before,'udp':udp(host,18081),'dns':udp('127.0.0.1',53)}
 assert all(baseline.values()),baseline
 time.sleep(6)
 try:stream.sendall(b'ping');existing=stream.recv(4)==b'ping'
 except OSError:existing=False
 print(json.dumps({'baseline':baseline,'expired':{'existing_tcp':existing,'tcp':tcp(),'udp':udp(host,18081),'dns':udp('127.0.0.1',53)}}))
