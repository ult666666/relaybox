import http.client
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from relaybox import __version__
from relaybox.auth import sign
from relaybox.server import make_server
from relaybox.store import Inbox


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.secret = b"synthetic-http-test-secret-only"
        cls.inbox = Inbox(Path(cls.tmp.name) / "http.sqlite3")
        cls.server = make_server(cls.inbox, cls.secret, port=0, read_timeout=0.2)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        self.addCleanup(conn.close)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        data = response.read()
        return response.status, dict(response.getheaders()), data

    def headers(self, body, delivery="http-1", timestamp=None):
        timestamp = int(time.time()) if timestamp is None else timestamp
        return {"Content-Type": "application/json", "X-RelayBox-ID": delivery,
                "X-RelayBox-Timestamp": str(timestamp),
                "X-RelayBox-Signature": sign(self.secret, timestamp, delivery, body)}

    def raw(self, request):
        with socket.create_connection(("127.0.0.1", self.port), timeout=3) as sock:
            sock.sendall(request)
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        head, body = b"".join(chunks).split(b"\r\n\r\n", 1)
        return int(head.split(b" ")[1]), json.loads(body)

    def test_signed_ingest_duplicate_and_conflict(self):
        body = b'{"event":"demo.created","label":"hello"}'
        headers = self.headers(body)
        first = self.request("POST", "/webhooks", body, headers)
        self.assertEqual(first[0], 202)
        self.assertTrue(json.loads(first[2])["created"])
        second = self.request("POST", "/webhooks", body, headers)
        self.assertEqual(second[0], 200)
        self.assertFalse(json.loads(second[2])["created"])
        other = b'{"event":"other"}'
        self.assertEqual(self.request("POST", "/webhooks", other, self.headers(other))[0], 409)

    def test_server_header_reports_the_package_version(self):
        status, headers, _ = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Server"].strip(), f"RelayBox/{__version__}")

    def test_auth_rejections_do_not_persist(self):
        before = sum(self.inbox.stats().values())
        body = b'{"x":1}'
        for headers in [{"Content-Type": "application/json"}, self.headers(body, "bad-stale", timestamp=1), {**self.headers(body, "bad-mac"), "X-RelayBox-Signature": "sha256=" + "0" * 64}]:
            with self.subTest(headers=headers):
                self.assertEqual(self.request("POST", "/webhooks", body, headers)[0], 401)
        self.assertEqual(sum(self.inbox.stats().values()), before)

    def test_signature_cannot_be_reused_with_another_delivery_id(self):
        body = b'{"x":1}'
        headers = self.headers(body, "original")
        headers["X-RelayBox-ID"] = "substituted"
        self.assertEqual(self.request("POST", "/webhooks", body, headers)[0], 401)

    def test_invalid_json_and_utf8_do_not_persist(self):
        before = sum(self.inbox.stats().values())
        for index, body in enumerate([b"{", b"[]", b'{"x":NaN}', b'{"x":1e999}', b'{"x":1,"x":2}', b'{"x":"\xff"}', b'{"x":"\\udfff"}']):
            with self.subTest(body=body):
                self.assertEqual(self.request("POST", "/webhooks", body, self.headers(body, f"bad-json-{index}"))[0], 400)
        self.assertEqual(sum(self.inbox.stats().values()), before)

    def test_oversized_body_rejected_before_persistence(self):
        before = sum(self.inbox.stats().values())
        body = b'{"x":"' + b"a" * 65536 + b'"}'
        self.assertEqual(self.request("POST", "/webhooks", body, self.headers(body, "huge"))[0], 413)
        self.assertEqual(sum(self.inbox.stats().values()), before)

    def test_missing_invalid_and_duplicate_length_are_json_errors(self):
        for headers, expected in [(b"", 411), (b"Content-Length: -1\r\n", 400), (b"Content-Length: 0\r\nContent-Length: 0\r\n", 400), (b"Content-Length: 999999\r\n", 413), (b"Transfer-Encoding: chunked\r\n", 400)]:
            with self.subTest(headers=headers):
                request = f"POST /webhooks HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n".encode() + headers + b"Content-Type: application/json\r\n\r\n"
                status, data = self.raw(request)
                self.assertEqual(status, expected)
                self.assertIn("error", data)

    def test_incomplete_body_times_out_with_bounded_json_response(self):
        request = f"POST /webhooks HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nContent-Type: application/json\r\nContent-Length: 20\r\n\r\n{{".encode()
        self.assertEqual(self.raw(request)[0], 408)

    def test_duplicate_auth_header_is_rejected(self):
        request = f"POST /webhooks HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nContent-Type: application/json\r\nContent-Length: 0\r\nX-RelayBox-ID: first\r\nX-RelayBox-ID: second\r\n\r\n".encode()
        self.assertEqual(self.raw(request)[0], 400)

    def test_unsupported_media_and_content_encoding(self):
        body = b"{}"
        for headers in [{**self.headers(body, "text"), "Content-Type": "text/plain"}, {**self.headers(body, "gzip"), "Content-Encoding": "gzip"}]:
            with self.subTest(headers=headers):
                self.assertEqual(self.request("POST", "/webhooks", body, headers)[0], 415)

    def test_host_origin_and_fetch_metadata_reject_cross_site_requests(self):
        for headers in [{"Host": "attacker.example"}, {"Origin": "https://attacker.example"}, {"Sec-Fetch-Site": "cross-site"}]:
            with self.subTest(headers=headers):
                status, _, data = self.request("GET", "/api/jobs", headers=headers)
                self.assertIn(status, [403, 421])
                self.assertIn("error", json.loads(data))

    def test_get_endpoints_and_safe_payload_detail(self):
        job, _ = self.inbox.ingest("hostile-text", b'{"text":"<img src=x onerror=alert(1)>"}')
        claim = self.inbox.claim("http-test-worker")
        status, headers, data = self.request("GET", "/api/jobs?limit=5")
        self.assertEqual(status, 200)
        self.assertNotIn("lease_token", json.loads(data)["jobs"][0])
        detail = json.loads(self.request("GET", f'/api/jobs/{job["id"]}')[2])["job"]
        self.assertEqual(detail["payload"]["text"], "<img src=x onerror=alert(1)>")
        self.assertNotIn("lease_token", detail)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.request("GET", "/api/stats")[0], 200)
        self.assertEqual(json.loads(self.request("GET", "/healthz")[2])["status"], "ok")

    def test_invalid_filters_paths_and_methods_are_json_errors(self):
        for method, path, expected in [("GET", "/api/jobs?state=bad", 400), ("GET", "/api/jobs?limit=-1", 400), ("GET", "/api/jobs?limit=1&limit=2", 400), ("GET", "/api/jobs?extra=1", 400), ("GET", "/api/jobs/999999", 404), ("GET", "/missing", 404), ("GET", "/../store.py", 404), ("PUT", "/webhooks", 405), ("POST", "/api/replay", 404)]:
            with self.subTest(method=method, path=path):
                status, headers, body = self.request(method, path)
                self.assertEqual(status, expected)
                self.assertIn("application/json", headers["Content-Type"])
                self.assertIn("error", json.loads(body))

    def test_dashboard_assets_have_browser_security_headers(self):
        for path, content_type in [("/", "text/html"), ("/assets/app.js", "javascript"), ("/assets/style.css", "text/css")]:
            with self.subTest(path=path):
                status, headers, body = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertIn(content_type, headers["Content-Type"])
                self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
                self.assertTrue(body)

    def test_server_refuses_external_binding_and_short_secret(self):
        for kwargs in [{"host": "0.0.0.0"}, {"secret": b"short"}]:
            options = {"inbox": self.inbox, "secret": self.secret, "port": 0, **kwargs}
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                make_server(**options)

    def test_expect_continue_is_rejected_without_body_read(self):
        request = f"POST /webhooks HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nExpect: 100-continue\r\nContent-Length: 20\r\n\r\n".encode()
        self.assertEqual(self.raw(request)[0], 417)

    def test_exact_size_limit_is_accepted(self):
        body = b'{"x":"' + b"a" * 65528 + b'"}'
        self.assertEqual(len(body), 65536)
        self.assertEqual(self.request("POST", "/webhooks", body, self.headers(body, "exact-size"))[0], 202)

    def test_same_origin_read_is_allowed(self):
        self.assertEqual(self.request("GET", "/api/stats", headers={"Origin": f"http://127.0.0.1:{self.port}", "Sec-Fetch-Site": "same-origin"})[0], 200)


class MetricsHTTPTests(unittest.TestCase):
    request = HTTPTests.request
    raw = HTTPTests.raw

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.inbox = Inbox(Path(cls.tmp.name) / "metrics-http.sqlite3", clock=lambda: 1000)
        cls.inbox.ingest("private-delivery-id", b'{"private":"hidden-http-payload"}')
        cls.lease = cls.inbox.claim("private-http-worker", lease_seconds=10)
        cls.inbox.ingest("second-private-id", b"{}")
        cls.server = make_server(cls.inbox, b"synthetic-metrics-test-secret", port=0)
        cls.port = cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.tmp.cleanup()

    def test_metrics_returns_exact_snapshot_without_private_job_fields(self):
        status, headers, body = self.request("GET", "/api/metrics")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {
            "schema_version": 1, "snapshot_at": 1000,
            "counts": {"queued": 1, "leased": 1, "succeeded": 0, "dead_letter": 0},
            "queued_ready": 1, "queued_delayed": 0, "leases_active": 1,
            "leases_expired_reclaimable": 0, "leases_expired_exhausted": 0,
            "oldest_queued_ready_wait_seconds": 0.0, "next_retry_at": None, "next_lease_expiry": 1010.0,
        })
        for private in ["private-delivery-id", "hidden-http-payload", "private-http-worker", self.lease["lease_token"], "lease_token", "digest", "last_error"]:
            self.assertNotIn(private, body.decode())
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(json.loads(self.request("GET", "/api/stats")[2]), {"counts": {"queued": 1, "leased": 1, "succeeded": 0, "dead_letter": 0}})
        self.assertEqual(json.loads(self.request("GET", "/healthz")[2]), {"status": "ok", "service": "relaybox"})

    def test_metrics_obeys_origin_host_query_and_body_boundary(self):
        for headers, expected in [({"Host": "attacker.example"}, 421), ({"Origin": "https://attacker.example"}, 403), ({"Sec-Fetch-Site": "cross-site"}, 403)]:
            with self.subTest(headers=headers):
                self.assertEqual(self.request("GET", "/api/metrics", headers=headers)[0], expected)
        self.assertEqual(self.request("GET", "/api/metrics?extra=1")[0], 404)
        self.assertEqual(self.request("GET", "/api/metrics", body=b"x", headers={"Content-Length": "1"})[0], 400)
        same_origin = {"Origin": f"http://127.0.0.1:{self.port}", "Sec-Fetch-Site": "same-origin"}
        self.assertEqual(self.request("GET", "/api/metrics", headers=same_origin)[0], 200)
        request = f"GET /api/metrics HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nContent-Length: 0\r\nContent-Length: 0\r\n\r\n".encode()
        self.assertEqual(self.raw(request)[0], 400)

    def test_metrics_storage_error_is_explicit_and_does_not_leak_diagnostics(self):
        import sqlite3
        from unittest.mock import patch
        with patch.object(self.inbox, "metrics", side_effect=sqlite3.OperationalError("private storage detail")):
            status, _, body = self.request("GET", "/api/metrics")
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body), {"error": "database unavailable"})
        with patch.object(self.inbox, "clock", return_value=float("nan")):
            status, _, body = self.request("GET", "/api/metrics")
        self.assertEqual(status, 400)
        self.assertNotIn("NaN", body.decode())
