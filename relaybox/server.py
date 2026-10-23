"""Loopback-only HTTP boundary; worker mutations remain local CLI/API calls."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import re
import socket
import sqlite3
import threading
from urllib.parse import parse_qs, urlsplit

from .auth import AuthError, verify
from .store import Conflict, Inbox, MAX_BODY, ValidationError, validate_delivery_id


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, address, handler, *, inbox, secret, read_timeout):
        self.inbox = inbox
        self.secret = secret
        self.read_timeout = read_timeout
        self.slots = threading.BoundedSemaphore(32)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            body = b'{"error":"server busy"}'
            try:
                request.settimeout(self.read_timeout)
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "RelayBox/0.1"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(self.server.read_timeout)

    def log_message(self, format, *args):
        # Do not echo arbitrary request paths, secrets, payloads or terminal escapes.
        pass

    def _respond(self, status, body, content_type="application/json; charset=utf-8"):
        self.close_connection = True
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=True, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def send_error(self, code, message=None, explain=None):
        self._respond(code, {"error": message or self.responses.get(code, ("request rejected",))[0]})

    def handle_expect_100(self):
        self.send_error(417, "Expect requests are unsupported")
        return False

    def _boundary(self):
        for name in ("Host", "Origin", "Content-Length", "Content-Type", "Content-Encoding", "Transfer-Encoding", "X-RelayBox-ID", "X-RelayBox-Timestamp", "X-RelayBox-Signature"):
            if len(self.headers.get_all(name, [])) > 1:
                self.send_error(400, f"duplicate {name} header")
                return False
        allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if self.headers.get("Host", "") not in allowed:
            self.send_error(421, "Host must address this loopback server")
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {"http://" + host for host in allowed}:
            self.send_error(403, "cross-origin requests are unsupported")
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.send_error(403, "cross-site requests are unsupported")
            return False
        if self.headers.get("Transfer-Encoding") is not None:
            self.send_error(400, "Transfer-Encoding is unsupported; use Content-Length")
            return False
        return True

    def _safe_job(self, job):
        return {key: value for key, value in job.items() if key != "lease_token"}

    def do_GET(self):
        if not self._boundary():
            return
        if self.headers.get("Content-Length", "0") != "0":
            self.send_error(400, "GET requests cannot contain a body")
            return
        try:
            path = urlsplit(self.path)
            if path.scheme or path.netloc or path.fragment:
                self.send_error(400, "use an origin-form request target")
                return
            if path.path == "/api/jobs":
                query = parse_qs(path.query, keep_blank_values=True, max_num_fields=8)
                if any(key not in {"state", "limit"} or len(values) != 1 for key, values in query.items()):
                    raise ValidationError("use state and limit query parameters once each")
                limit_text = query.get("limit", ["100"])[0]
                if not re.fullmatch(r"[0-9]{1,3}", limit_text):
                    raise ValidationError("invalid limit")
                jobs = self.server.inbox.list_jobs(state=query.get("state", [None])[0], limit=int(limit_text))
                self._respond(200, {"jobs": jobs, "limit": int(limit_text)})
            elif re.fullmatch(r"/api/jobs/[1-9][0-9]{0,17}", path.path) and not path.query:
                job = self.server.inbox.get(int(path.path.rsplit("/", 1)[1]))
                self._respond(200, {"job": self._safe_job(job)})
            elif path.path == "/api/metrics" and not path.query:
                self._respond(200, self.server.inbox.metrics())
            elif path.path == "/api/stats" and not path.query:
                self._respond(200, {"counts": self.server.inbox.stats()})
            elif path.path == "/healthz" and not path.query:
                self._respond(200, {"status": "ok", "service": "relaybox"})
            elif path.path in {"/", "/assets/app.js", "/assets/style.css"} and not path.query:
                name, content_type = {"/": ("index.html", "text/html"), "/assets/app.js": ("app.js", "text/javascript"), "/assets/style.css": ("style.css", "text/css")}[path.path]
                asset = files("relaybox").joinpath("assets", name).read_bytes()
                self._respond(200, asset, content_type + "; charset=utf-8")
            else:
                self.send_error(404, "route not found")
        except KeyError:
            self.send_error(404, "job not found")
        except (ValueError, ValidationError):
            self.send_error(400, "invalid query parameters")
        except sqlite3.Error:
            self.send_error(503, "database unavailable")

    def do_POST(self):
        if not self._boundary():
            return
        if self.path != "/webhooks":
            self.send_error(404, "route not found")
            return
        length = self.headers.get("Content-Length")
        if length is None:
            self.send_error(411, "Content-Length is required")
            return
        if not re.fullmatch(r"(?:0|[1-9][0-9]{0,11})", length):
            self.send_error(400, "invalid Content-Length")
            return
        if int(length) > MAX_BODY:
            self.send_error(413, "payload exceeds 65536 bytes")
            return
        media = self.headers.get("Content-Type", "").lower().replace(" ", "")
        if media not in {"application/json", "application/json;charset=utf-8"} or self.headers.get("Content-Encoding") is not None:
            self.send_error(415, "use uncompressed application/json with UTF-8")
            return
        try:
            body = self.rfile.read(int(length))
        except (socket.timeout, TimeoutError):
            self.send_error(408, "request body read timed out")
            return
        if len(body) != int(length):
            self.send_error(400, "incomplete request body")
            return
        delivery = self.headers.get("X-RelayBox-ID", "")
        if any(not self.headers.get(name) for name in ("X-RelayBox-ID", "X-RelayBox-Timestamp", "X-RelayBox-Signature")):
            self.send_error(401, "signature, timestamp and delivery ID are required")
            return
        try:
            # Validate the identifier before constructing the ASCII signing input.
            validate_delivery_id(delivery)
            verify(self.server.secret, self.headers.get("X-RelayBox-Timestamp", ""),
                   self.headers.get("X-RelayBox-Signature", ""), delivery, body,
                   now=self.server.inbox.clock())
            job, created = self.server.inbox.ingest(delivery, body)
            self._respond(202 if created else 200, {"created": created, "job": self._safe_job(job)})
        except AuthError:
            self.send_error(401, "invalid signature or timestamp")
        except ValidationError as exc:
            self.send_error(400, str(exc))
        except Conflict as exc:
            self.send_error(409, str(exc))
        except sqlite3.Error:
            self.send_error(503, "database unavailable")

    def _unsupported(self):
        self.send_error(405, "method not allowed")

    do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = _unsupported


def make_server(inbox: Inbox, secret: bytes, *, host="127.0.0.1", port=8787, read_timeout=3):
    """Create an unstarted bounded server. Binding beyond IPv4 loopback is refused."""
    if host != "127.0.0.1":
        raise ValueError("RelayBox only binds to 127.0.0.1")
    if not isinstance(secret, bytes) or len(secret) < 16:
        raise ValueError("the webhook secret must contain at least 16 bytes")
    if not 0 < read_timeout <= 30:
        raise ValueError("read_timeout must be in (0, 30]")
    return _Server((host, port), Handler, inbox=inbox, secret=secret, read_timeout=read_timeout)
