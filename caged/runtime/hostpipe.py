"""ssh ProxyCommand: relay stdin/stdout to the host bridge's Unix socket.

Lives in the read-only /caged-runtime mount, so the container needs no
socat or nc. Usage: python3 hostpipe.py /run/caged-host/host.sock
"""
import os
import selectors
import socket
import sys


def relay(path, stdin=0, stdout=1):
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.connect(path)
    except OSError as error:
        print(f'caged host bridge: cannot connect to {path}: {error.strerror}. '
              'Is the bridge running? Check ./cage status on the host.', file=sys.stderr)
        return 255
    selector = selectors.DefaultSelector()
    selector.register(stdin, selectors.EVENT_READ, 'up')
    selector.register(connection, selectors.EVENT_READ, 'down')
    while selector.get_map():
        for key, _ in selector.select():
            if key.data == 'up':
                data = os.read(stdin, 65536)
                if data:
                    connection.sendall(data)
                else:  # ssh closed its side; let the server finish
                    selector.unregister(stdin)
                    connection.shutdown(socket.SHUT_WR)
            else:
                data = connection.recv(65536)
                if not data:
                    return 0
                view = memoryview(data)
                while view:
                    view = view[os.write(stdout, view):]
    return 0


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(relay(sys.argv[1]))
