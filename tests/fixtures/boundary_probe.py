import socket, sys, json
host=sys.argv[1]
result={}
try:
 with socket.create_connection((host,18080),timeout=0.8) as sock:
  sock.sendall(b'GET / HTTP/1.1\r\nHost: fixture\r\n\r\n')
  result['tcp']=b'200 OK' in sock.recv(1024)
except OSError: result['tcp']=False
for key,port in [('dns_tcp',53),('dot',853)]:
 try:
  with socket.create_connection((host,port),timeout=0.8): result[key]=True
 except OSError: result[key]=False
for key,port in [('udp',18081),('dns_port',53)]:
 try:
  with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
   sock.settimeout(0.8)
   sock.sendto(b'caged-fixture',(host,port))
   result[key]=sock.recv(1024)==b'caged-fixture'
 except OSError: result[key]=False
print(json.dumps(result))
