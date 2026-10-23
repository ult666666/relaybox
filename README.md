# RelayBox

[![RelayBox checks](https://github.com/ult666666/relaybox/actions/workflows/ci.yml/badge.svg)](https://github.com/ult666666/relaybox/actions/workflows/ci.yml)

**A signed webhook inbox you can inspect, retry, and recover locally.**

RelayBox is a Python 3.11+ engineering demo built with the standard library and SQLite. It receives authenticated JSON deliveries, deduplicates by delivery ID **and exact payload digest**, and exposes a small durable job state machine. An accessible dashboard makes queued work, active leases, successful acknowledgments, and dead letters visible.

No runtime dependencies, external services, or outbound webhook delivery. This is an educational portfolio project, not a production service. Implementation and tests were developed with AI assistance; review scope and limitations are explicit below.

![RelayBox local dashboard with synthetic deliveries](docs/dashboard.png)

## Run in a minute

From a checkout, no installation is necessary:

```bash
python3 -m relaybox --db .local/demo.sqlite3 demo
python3 -m relaybox --db .local/demo.sqlite3 serve --demo-secret
```

Open **http://127.0.0.1:8787**. If that port is occupied, add `--port 8797` to the serve command and open the matching port. The demo seeds one delivery in each state, using visibly synthetic English/Mandarin payloads. It refuses to overwrite a nonempty database. Choose a new database path to start another demo. Stop the server with Ctrl+C.

The `--demo-secret` key is public: `relaybox-synthetic-demo-secret-only`. It is appropriate only for synthetic local data. For your own local experiment, set `RELAYBOX_SECRET` to a random value of at least 16 bytes and omit that flag. Keep database files in a private directory.

To install the CLI in an isolated environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/relaybox --help
.venv/bin/relaybox --db .local/installed.sqlite3 demo
```

PowerShell users can use `.venv\Scripts\python.exe` and `.venv\Scripts\relaybox.exe`. Building uses the exact setuptools version in `pyproject.toml`; the running application uses only Python's standard library. No PyPI release is implied.

## Send a signed delivery

With the demo server running on its default port 8787, run this in another terminal from the checkout. The client sends only to that fixed loopback address:

```bash
python3 examples/send_webhook.py
```

Expected result: HTTP `202` with `created: true`, then HTTP `200` for the identical repeat. Reusing that ID with different bytes returns `409`. Click **Refresh** on the dashboard to see the new delivery.

For any UTF-8 JSON object file, obtain signing headers with:

```bash
python3 -m relaybox sign example-001 examples/payload.json --demo-secret
```

Send those headers and the **unchanged file bytes** to `POST /webhooks` within five minutes. The signature covers `timestamp + "." + delivery_id + "." + raw_body`. Signing the ID prevents a valid body/signature from being reused under another delivery ID. See the full [HTTP contract](docs/API.md).

## Work and recover

```bash
python3 -m relaybox --db .local/demo.sqlite3 list --state dead_letter
python3 -m relaybox --db .local/demo.sqlite3 replay 3
python3 -m relaybox --db .local/demo.sqlite3 claim --worker local-reviewer --lease-seconds 30
```

The claim response includes a private `lease_token`. Complete the claimed ID with that token before expiry:

```bash
python3 -m relaybox --db .local/demo.sqlite3 ack JOB_ID --token TOKEN
# Or record a failure; this schedules a retry or ends in dead_letter:
python3 -m relaybox --db .local/demo.sqlite3 fail JOB_ID --token TOKEN --error "Synthetic local failure"
```

Commands return JSON; successful commands exit `0`, invalid arguments or failed operations exit `2`. An empty claim is successful and returns `{"job": null}`. Replay is deliberately explicit and only accepts dead letters. Each replay resets the attempt budget; a local operator can request further replays. Worker operations stay in the CLI/library, so the dashboard cannot mutate jobs.

## Library API

```python
from relaybox.store import Inbox

inbox = Inbox("inbox.sqlite3", max_attempts=3, base_delay=2, max_delay=60)
job, created = inbox.ingest("example-001", b'{"synthetic":true}')
claim = inbox.claim("example-worker", lease_seconds=30)
if claim:
    # Process locally. This demo intentionally has no outbound delivery worker.
    inbox.ack(claim["id"], claim["lease_token"])
```

`ingest` validates content but does not authenticate a sender; HMAC verification happens at the HTTP boundary. Direct library and CLI access is trusted local operator access.

## Inspect the engineering

Start with [`store.py`](relaybox/store.py) for atomic `BEGIN IMMEDIATE` claims and token checks, then [`tests/test_store.py`](tests/test_store.py) for real multi-connection contention and expired-worker rejection. [`tests/test_http.py`](tests/test_http.py) exercises live TCP requests, malformed framing, size limits, clock checks, hostile content, and JSON error responses.

```bash
python3 -m unittest discover -v
```

CI runs the complete suite, installs the package, executes the installed CLI outside the checkout, and requests assets from the installed server on Python 3.11 and 3.13. Read [verification evidence](docs/VERIFICATION.md), [architecture](docs/ARCHITECTURE.md), [API](docs/API.md), or the [中文指南](docs/GUIDE.zh-CN.md).

## Limits that matter

- **Local only:** IPv4 loopback, plain HTTP, one shared HMAC key; no account authentication, key rotation, multi-tenant permissions, or TLS deployment configuration. Any process/user with local HTTP access can read payloads.
- **At least once:** a lease can expire during work. Tokens fence acknowledgments in this SQLite database; they cannot undo or prevent external side effects. Consumers need their own idempotency strategy.
- **Small scope:** 64 KiB JSON objects, 32 active HTTP handlers, three-second socket read timeout, 10-second SQLite busy timeout. No outbound delivery, rate quotas, background scheduler, lease renewal, or distributed workers.
- **Recovery requires activity:** expiry is reconciled on the next claim. The dashboard reports persisted state and may show an expired lease until a worker polls.
- **Storage is bounded only by your disk:** no retention, encryption at rest, automatic backups, migrations beyond schema version 1, payload search, or full audit history. The dashboard shows the latest 100 matches; the API allows up to 500.
- **Demonstration evidence:** synthetic records and deterministic logical state transitions, with measured local checks. No customer usage, production reliability, benchmark, or independent security audit claims.

See [SECURITY.md](SECURITY.md) and [CONTRIBUTING.md](CONTRIBUTING.md). Licensed under [MIT](LICENSE).
