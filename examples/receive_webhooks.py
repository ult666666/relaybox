"""A loopback Standard Webhooks receiver for trying RelayBox outbound delivery.

Standard library only, with its own signature check written from the
specification, so it does not import RelayBox. It rejects stale or unsigned
requests with 401, treats a repeated webhook-id as a duplicate (answering 200
without processing it again; each path counts as its own endpoint), and can
inject failures:

  --fail-first N    answer 503 with Retry-After: 1 to the first N new deliveries
  --reject-path P   answer 422 to every request for path P

Prints one JSON line per request. Synthetic local use only.
"""

import argparse
import base64
import hashlib
import hmac
import http.server
import json
import threading
import time

DEMO_SECRET = "whsec_" + base64.b64encode(b"relaybox-synthetic-outbound-demo-key").decode()


def verified(key, headers, body, tolerance):
    webhook_id, timestamp = headers.get("webhook-id", ""), headers.get("webhook-timestamp", "")
    if not (webhook_id and timestamp.isdigit() and headers.get("webhook-signature")):
        return False
    if abs(time.time() - int(timestamp)) > tolerance:
        return False
    content = f"{webhook_id}.{timestamp}.".encode() + body
    expected = "v1," + base64.b64encode(hmac.new(key, content, hashlib.sha256).digest()).decode()
    return any(hmac.compare_digest(expected, part) for part in headers["webhook-signature"].split(" "))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8788)
    secret = parser.add_mutually_exclusive_group(required=True)
    secret.add_argument("--secret", help="the subscription's whsec_ secret")
    secret.add_argument("--demo-secret", action="store_true", help="use RelayBox's public synthetic outbound demo key")
    parser.add_argument("--fail-first", type=int, default=0)
    parser.add_argument("--reject-path")
    parser.add_argument("--tolerance", type=int, default=300, help="accepted clock skew in seconds")
    args = parser.parse_args()
    text = DEMO_SECRET if args.demo_secret else args.secret
    key = base64.b64decode(text.removeprefix("whsec_") + "=" * (-len(text.removeprefix("whsec_")) % 4))
    seen, lock, failed = set(), threading.Lock(), [0]

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(min(int(self.headers.get("Content-Length", "0")), 1 << 20))
            headers = {name.lower(): value for name, value in self.headers.items()}
            ok = verified(key, headers, body, args.tolerance)
            webhook_id = headers.get("webhook-id")
            # One message fans out to several endpoints with the same webhook-id,
            # so each endpoint (here, each path) deduplicates on its own.
            endpoint = (self.path, webhook_id)
            with lock:
                duplicate = ok and endpoint in seen
                if not ok:
                    status = 401
                elif self.path == args.reject_path:
                    status = 422
                elif duplicate:
                    status = 200  # Already processed: acknowledge without repeating the effect.
                elif failed[0] < args.fail_first:
                    failed[0] += 1
                    status = 503
                else:
                    seen.add(endpoint)
                    status = 204
            self.send_response(status)
            if status == 503:
                self.send_header("Retry-After", "1")
            self.send_header("Content-Length", "0")
            self.end_headers()
            print(json.dumps({"path": self.path, "webhook_id": webhook_id, "verified": ok,
                              "duplicate": duplicate, "status": status}), flush=True)

        def log_message(self, format, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(json.dumps({"listening": f"http://127.0.0.1:{server.server_port}", "synthetic": True}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
