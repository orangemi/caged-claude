import socketserver, threading, time
class TCP(socketserver.BaseRequestHandler):
 def handle(self):
  self.request.recv(4096)
  self.request.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK')
class UDP(socketserver.BaseRequestHandler):
 def handle(self):
  data, sock = self.request
  sock.sendto(data, self.client_address)
for cls, port, handler in [(socketserver.ThreadingTCPServer,18080,TCP),(socketserver.ThreadingUDPServer,18081,UDP),(socketserver.ThreadingUDPServer,53,UDP),(socketserver.ThreadingTCPServer,53,TCP),(socketserver.ThreadingTCPServer,853,TCP)]:
 server=cls(('0.0.0.0',port),handler)
 threading.Thread(target=server.serve_forever,daemon=True).start()
while True: time.sleep(1)
