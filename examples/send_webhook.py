"""Send one synthetic event twice to a fixed loopback demo endpoint."""

import hashlib
import hmac
import http.client
import json
from pathlib import Path
import time

body = Path(__file__).with_name("payload.json").read_bytes()
timestamp = str(int(time.time()))
delivery = "example-001"
message = f"{timestamp}.{delivery}.".encode("ascii") + body
headers = {
    "Content-Type": "application/json",
    "X-RelayBox-ID": delivery,
    "X-RelayBox-Timestamp": timestamp,
    "X-RelayBox-Signature": "sha256=" + hmac.new(b"relaybox-synthetic-demo-secret-only", message, hashlib.sha256).hexdigest(),
}
for _ in range(2):
    connection = http.client.HTTPConnection("127.0.0.1", 8787, timeout=5)
    try:
        connection.request("POST", "/webhooks", body=body, headers=headers)
        response = connection.getresponse()
        data = json.loads(response.read())
        print(json.dumps({"status": response.status, "created": data.get("created"),
                          "job_id": data.get("job", {}).get("id"), "error": data.get("error")}))
    finally:
        connection.close()
