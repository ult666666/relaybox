"""Scripted loopback receivers for outbound delivery tests. Standard library only.

Each request takes the next scripted behavior (an int status or a callable that
writes the response itself), falling back to a default status. Every request
is recorded with lowercase header names and the exact body bytes.
"""

import base64
import hashlib
import hmac
import http.server
import socket
import sys
import threading
import time


def independent_verify(secret, headers, body):
    """Check a v1 signature from the spec text alone; shares no code with relaybox."""
    key = base64.b64decode(secret[len("whsec_"):] + "=" * (-len(secret[len("whsec_"):]) % 4))
    content = headers["webhook-id"].encode() + b"." + headers["webhook-timestamp"].encode() + b"." + body
    expected = "v1," + base64.b64encode(hmac.new(key, content, hashlib.sha256).digest()).decode()
    return any(hmac.compare_digest(expected, part) for part in headers["webhook-signature"].split(" "))


def respond(status, headers=(), body=b""):
    def behavior(handler):
        handler.send_response(status)
        for name, value in headers:
            handler.send_header(name, value)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
    return behavior


def raw(data):
    """Write raw bytes (for example an invalid status line), then close."""
    def behavior(handler):
        handler.wfile.write(data)
        handler.wfile.flush()
        hang_up(handler)
    return behavior


def hang_up(handler):
    handler.close_connection = True
    try:
        handler.connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def slow(seconds, status=204):
    def behavior(handler):
        time.sleep(seconds)
        try:
            respond(status)(handler)
        except OSError:
            pass  # The client gave up first, as intended.
    return behavior


def trickle(interval, seconds):
    """Send a valid response one byte per interval, keeping each read alive."""
    def behavior(handler):
        data = b"HTTP/1.1 204 No Content\r\n" + b"X-Slow: " + b"a" * 1000 + b"\r\nContent-Length: 0\r\n\r\n"
        stop = time.monotonic() + seconds
        try:
            for byte in data:
                if time.monotonic() > stop:
                    break
                handler.wfile.write(bytes([byte]))
                handler.wfile.flush()
                time.sleep(interval)
        except OSError:
            pass
        hang_up(handler)
    return behavior


def endless(status, chunk=65_536, limit=64 * 1024 * 1024):
    """Advertise and stream a very large body until the client hangs up."""
    def behavior(handler):
        handler.send_response(status)
        handler.send_header("Content-Length", str(limit))
        handler.end_headers()
        sent = 0
        try:
            while sent < limit:
                handler.wfile.write(b"x" * chunk)
                sent += chunk
        except OSError:
            pass
        handler.sent = sent
    return behavior


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Clients hang up on purpose in these tests; report anything else.
        if not isinstance(sys.exc_info()[1], OSError):
            super().handle_error(request, client_address)


class Receiver:
    def __init__(self, *behaviors, default=204):
        self.behaviors = list(behaviors)
        self.default = default
        self.requests = []
        self.arrived = threading.Condition()
        self.release = threading.Event()  # Lets a held behavior finish at teardown.
        receiver = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                with receiver.arrived:
                    receiver.requests.append({"path": self.path, "body": body, "at": time.monotonic(),
                                              "headers": {name.lower(): value for name, value in self.headers.items()}})
                    behavior = receiver.behaviors.pop(0) if receiver.behaviors else receiver.default
                    receiver.arrived.notify_all()
                (respond(behavior) if isinstance(behavior, int) else behavior)(self)

            def log_message(self, format, *args):
                pass

        self.server = _Server(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)

    def url(self, path="/hook"):
        return f"http://127.0.0.1:{self.server.server_port}{path}"

    def wait_for(self, count, timeout=10):
        with self.arrived:
            if not self.arrived.wait_for(lambda: len(self.requests) >= count, timeout):
                raise AssertionError(f"receiver got {len(self.requests)} of {count} requests")

    def hold(self):
        """A behavior that keeps the request open until teardown."""
        def behavior(handler):
            self.release.wait(30)
            hang_up(handler)
        return behavior

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


def refused_url():
    """A loopback URL with no listener (the port was free a moment ago)."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}/hook"


class PlainTextOnConnect:
    """A TCP listener that writes an HTTP error immediately, before any TLS bytes."""

    def __init__(self):
        self.socket = socket.create_server(("127.0.0.1", 0))
        self.port = self.socket.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self):
        try:
            connection, _ = self.socket.accept()
        except OSError:
            return
        with connection:
            connection.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            time.sleep(0.5)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.socket.close()
        self.thread.join(5)
