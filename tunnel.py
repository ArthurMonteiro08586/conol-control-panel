"""Durable SSH tunnel (ssh -L equivalent): local 127.0.0.1:9998 -> server 127.0.0.1:9999."""
import paramiko, socketserver, threading, socket, time, select

SERVER = '94.237.95.61'
KEY_PATH = r'C:\Users\User\.ssh\andrey-upcloud'
LOCAL_PORT = 9998
REMOTE_HOST = '127.0.0.1'
REMOTE_PORT = 9999

_transport: paramiko.Transport | None = None
_transport_lock = threading.Lock()


def _get_transport() -> paramiko.Transport:
    """Return a live transport, reconnecting if needed."""
    global _transport
    with _transport_lock:
        if _transport and _transport.is_active():
            return _transport
        # Close stale
        if _transport:
            try: _transport.close()
            except: pass
        # Connect fresh
        key = paramiko.Ed25519Key.from_private_key_file(KEY_PATH)
        t = paramiko.Transport((SERVER, 22))
        t.set_keepalive(30)
        t.connect(username='root', pkey=key)
        _transport = t
        print(f"[tunnel] transport connected to {SERVER}", flush=True)
        return t


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            t = _get_transport()
            src_addr = self.client_address
            chan = t.open_channel('direct-tcpip', (REMOTE_HOST, REMOTE_PORT), src_addr)
            if chan is None:
                return
            sock = self.request
            while True:
                r, _, _ = select.select([sock, chan], [], [], 60)
                if not r:
                    break
                if sock in r:
                    data = sock.recv(8192)
                    if not data: break
                    chan.sendall(data)
                if chan in r:
                    data = chan.recv(8192)
                    if not data: break
                    sock.sendall(data)
        except Exception as e:
            pass
        finally:
            try: chan.close()
            except: pass
            try: self.request.close()
            except: pass


class ThreadedTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    print("[tunnel] starting forward tunnel 127.0.0.1:%d -> %s:%d" % (LOCAL_PORT, SERVER, REMOTE_PORT), flush=True)
    # Pre-connect transport
    _get_transport()
    server = ThreadedTCPServer(('127.0.0.1', LOCAL_PORT), Handler)
    # Monitor thread: reconnect transport if it dies
    def _monitor():
        while True:
            time.sleep(5)
            try:
                if not _transport or not _transport.is_active():
                    print("[tunnel] transport dead, reconnecting...", flush=True)
                    _get_transport()
            except Exception:
                pass
    threading.Thread(target=_monitor, daemon=True).start()
    server.serve_forever()


if __name__ == '__main__':
    main()
