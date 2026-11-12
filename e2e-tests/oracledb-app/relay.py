"""Private TCP relay with observable, deliberately interruptible Oracle traffic."""
import socket
import threading
import time


class Relay:
    def __init__(self, host, port):
        self.target = (host, port)
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(4)
        self.listener.settimeout(.1)
        self.port = self.listener.getsockname()[1]
        self.stopped = threading.Event()
        self.hold_responses = threading.Event()
        self.lock = threading.Lock()
        self.sockets = []
        self.threads = []
        self.bytes = {"client": 0, "server": 0, "held_server": 0}
        self.acceptor = threading.Thread(target=self.accept, daemon=True)
        self.acceptor.start()

    def accept(self):
        while not self.stopped.is_set():
            try:
                client, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                server = socket.create_connection(self.target, timeout=5)
                client.settimeout(.1)
                server.settimeout(.1)
                with self.lock:
                    self.sockets.extend([client, server])
                for source, destination, name in ((client, server, "client"), (server, client, "server")):
                    thread = threading.Thread(target=self.pump, args=(source, destination, name), daemon=True)
                    self.threads.append(thread)
                    thread.start()
            except OSError:
                client.close()

    def pump(self, source, destination, direction):
        try:
            while not self.stopped.is_set():
                try:
                    data = source.recv(65536)
                except socket.timeout:
                    continue
                if not data:
                    break
                with self.lock:
                    self.bytes[direction] += len(data)
                while direction == "server" and self.hold_responses.is_set() and not self.stopped.is_set():
                    with self.lock:
                        self.bytes["held_server"] += len(data)
                    # One held packet is enough; the counter describes withheld bytes, not retries.
                    while self.hold_responses.is_set() and not self.stopped.wait(.01):
                        pass
                if not self.stopped.is_set():
                    destination.sendall(data)
        except OSError:
            pass

    def cut(self):
        self.stopped.set()
        try:
            self.listener.close()
        except OSError:
            pass
        with self.lock:
            sockets = list(self.sockets)
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def close(self):
        self.cut()
        self.acceptor.join(timeout=2)
        for thread in self.threads:
            thread.join(timeout=2)
        if self.acceptor.is_alive() or any(thread.is_alive() for thread in self.threads):
            raise RuntimeError("Relay thread did not stop")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class ConnectionProxy:
    def __init__(self, inner, *, execute=None, executemany=None, commit=None, close=None):
        object.__setattr__(self, "inner", inner)
        object.__setattr__(self, "execute_hook", execute)
        object.__setattr__(self, "many_hook", executemany)
        object.__setattr__(self, "commit_hook", commit)
        object.__setattr__(self, "close_hook", close)

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def __setattr__(self, name, value):
        setattr(self.inner, name, value)

    def cursor(self):
        return CursorProxy(self.inner.cursor(), self.execute_hook, self.many_hook)

    def commit(self):
        if self.commit_hook:
            return self.commit_hook(self.inner)
        return self.inner.commit()

    def close(self):
        try:
            return self.inner.close()
        finally:
            if self.close_hook:
                self.close_hook()


class CursorProxy:
    def __init__(self, inner, execute, many):
        self.inner, self.execute_hook, self.many_hook = inner, execute, many

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.inner.close()

    def execute(self, sql, *args, **kwargs):
        if self.execute_hook:
            return self.execute_hook(self.inner, sql, args, kwargs)
        return self.inner.execute(sql, *args, **kwargs)

    def executemany(self, sql, rows, **kwargs):
        if self.many_hook:
            self.many_hook()
        return self.inner.executemany(sql, rows, **kwargs)
