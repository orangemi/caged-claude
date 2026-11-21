import socketserver,threading,time
class Echo(socketserver.BaseRequestHandler):
 def handle(self):
  while True:
   data=self.request.recv(1024)
   if not data:return
   self.request.sendall(data)
class Datagram(socketserver.BaseRequestHandler):
 def handle(self):
  data,sock=self.request;sock.sendto(data,self.client_address)
class UI(socketserver.BaseRequestHandler):
 def handle(self):
  self.request.recv(1024);self.request.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK')
for kind,port,handler in [(socketserver.ThreadingTCPServer,18080,Echo),(socketserver.ThreadingUDPServer,18081,Datagram),(socketserver.ThreadingUDPServer,53,Datagram),(socketserver.ThreadingTCPServer,3001,UI)]:
 server=kind(('0.0.0.0',port),handler)
 threading.Thread(target=server.serve_forever,daemon=True).start()
while True:time.sleep(1)
