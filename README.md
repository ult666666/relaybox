# RelayBox

[![RelayBox checks](https://github.com/ult666666/relaybox/actions/workflows/ci.yml/badge.svg)](https://github.com/ult666666/relaybox/actions/workflows/ci.yml)

**A signed webhook inbox you can inspect, retry, and recover locally.**

RelayBox is a Python 3.11+ engineering demo built with the standard library and SQLite. It receives authenticated JSON deliveries, deduplicates by delivery ID **and exact payload digest**, and exposes a small durable job state machine. An accessible dashboard makes queued work, active leases, successful acknowledgments, and dead letters visible.

No runtime dependencies, external services, or outbound webhook delivery. This is an educational portfolio project, not a production service. Implementation and tests were developed with AI assistance; review scope and limitations are explicit below.

![RelayBox 0.4.1 local dashboard with four synthetic deliveries, the whole-inbox operations summary and the oldest ready wait shown as "About 9 seconds since ready"](docs/dashboard.png)

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

Process ready synthetic deliveries into local JSON receipts:

```bash
python3 -m relaybox --db .local/demo.sqlite3 work --receipts .local/receipts --max-jobs 10
```

This bounded worker claims at most 10 jobs and stops as soon as none is ready. It does not wait for future retries. Each successful receipt is acknowledged; ordinary handler failures schedule the existing retry/dead-letter policy. Output is a token-free summary such as `{"claimed":1,"succeeded":1,"failed":0,"lost_leases":0}`. The default limit is one claim; `--max-jobs` accepts integers 1–1000. `--worker` defaults to `local-worker`; `--lease-seconds` defaults to 30.

Receipts contain version 1, delivery ID, original body SHA-256 digest, and parsed synthetic payload. The filename hashes the delivery ID together with the body digest, so database-local job numbers and path-like IDs cannot collide or escape the directory. An existing byte-identical receipt succeeds without rewriting it; a conflicting, corrupt, noncanonical, symlink, or nonregular receipt fails without overwriting it. Publication uses a fully written temporary file and an exclusive atomic hard link. Use a trusted local directory on a filesystem supporting hard links; the selected directory and its parents must not be concurrently replaced by another actor. Trusted ancestor aliases (such as macOS `/var`) are canonicalized; the selected directory itself must not be a symlink. New directories use mode `0700` and files `0600`, subject to umask; existing permissions are unchanged. This demonstrates local idempotency, with no distributed exactly-once or power-loss guarantee. Full [receipt format and limits](docs/API.md#local-worker-library-and-receipt-format).

For manual ownership and recovery:

```bash
python3 -m relaybox --db .local/demo.sqlite3 list --state dead_letter
python3 -m relaybox --db .local/demo.sqlite3 replay 3
python3 -m relaybox --db .local/demo.sqlite3 claim --worker local-reviewer --lease-seconds 30
```

The claim response includes a private `lease_token`. Complete the claimed ID with that token before expiry:

```bash
# Extend a still-current lease before expiry (same token, no new attempt):
python3 -m relaybox --db .local/demo.sqlite3 renew JOB_ID --token TOKEN --lease-seconds 60
python3 -m relaybox --db .local/demo.sqlite3 ack JOB_ID --token TOKEN
# Or record a failure; this schedules a retry or ends in dead_letter:
python3 -m relaybox --db .local/demo.sqlite3 fail JOB_ID --token TOKEN --error "Synthetic local failure"
```

Commands return JSON; successful commands exit `0`, invalid arguments or failed operations exit `2`. `work` exits `1` if any handler failure was recorded or ownership was lost before completion. An empty claim is successful and returns `{"job": null}`. Replay is deliberately explicit and only accepts dead letters. Each replay resets the attempt budget; a local operator can request further replays. Worker operations stay in the CLI/library, so the dashboard cannot mutate jobs.

## Understand an idle worker

Version 0.4.0 adds a read-only operations summary. Use Refresh in the dashboard, or GET `/api/metrics` from the local server, to distinguish queued work that is ready now from delayed retries, and active leases from expired leases. The four state cards still show persisted totals: an expired exhausted lease stays in **Leased** until a worker's next claim moves it to dead letter. Viewing diagnostics never claims or reconciles a job.

The summary labels its inbox snapshot separately from the latest 100 matching deliveries. Readiness and expiry use one clock sample and one database statement; the list is a separate request and can reflect a different commit. Oldest ready wait means seconds since `available_at`, not time since ingestion or processing latency. Version 0.4.1 displays it rounded, such as `About 4.6 seconds since ready` or `About 1.7 minutes since ready`, while the API keeps exact seconds; an invalid wait value clears the summary with an error instead of showing an invented time. Missing wait/retry/expiry values are explicit descriptions rather than zero dates. Upcoming times use your browser's local timezone. Refresh is manual. A failed section clears its values and displays an error while the other section remains usable; older responses cannot replace a newer refresh.

Library users can call `inbox.metrics()` on their existing `Inbox` instance. Normal `Inbox(path)` construction still initializes a missing database and applies the existing WAL setup; `metrics()` itself performs only a SELECT. Clock values must be finite Unix seconds from 0 through 253402300799 (through year 9999); booleans and invalid ranges raise `ValidationError`. This is a local diagnostic snapshot, not a monitor, throughput estimate, completion promise or service-health certificate. Aggregates can scan all rows, though they do not select payloads or private job fields. See the [exact metrics contract](docs/API.md#queue-metrics).

To inspect that scan cost, use the source-only [local measurement tool](docs/PERFORMANCE.md). It checks a fixed synthetic workload against an independent oracle, then retains raw installed-library call timings and environment metadata. No production capacity or timing threshold is implied.

## Back up and drill recovery

Version 0.3.0 adds consistent local SQLite snapshots. Never copy just a live inbox's main file: committed jobs may still be in its WAL. Choose an existing trusted private directory and fresh destination names:

```bash
python3 -m relaybox --db .local/demo.sqlite3 backup .local/snapshot.sqlite3 --timeout 10
python3 -m relaybox verify-backup .local/snapshot.sqlite3
```

The JSON receipt reports canonical paths, schema version, integrity, job/state counts, physical SHA-256 and file size. It omits payloads and private lease tokens. The **snapshot itself contains all database data, including tokens and payloads**; protect it like the original. The online backup includes committed WAL data and excludes uncommitted changes. It validates the known schema and completed snapshot before exclusively publishing a private `0600` file. Existing paths, symlinks, and stale SQLite sidecars are refused; no overwrite or `--force` exists.

Before switching to a restored path, **stop the original service and every original worker**. Keep the original stopped during the drill. Restore to a new name; do not initialize that destination first:

```bash
python3 -m relaybox --db .local/recovered.sqlite3 restore .local/snapshot.sqlite3
python3 -m relaybox --db .local/recovered.sqlite3 stats
python3 -m relaybox --db .local/recovered.sqlite3 work --receipts .local/receipts --max-jobs 10
```

Compare restore counts with the saved backup receipt before processing work. Attempts, retry policy, replay count and lease fields survive exactly. Expired leases reconcile on the next claim; an unexpired lease remains leased until it expires. The restored copy is an independent local inbox and cannot fence workers using the original. Work performed after the snapshot may repeat after recovery. Retain the same canonical receipt directory separately: backing up the database does not back up receipts. An identical retained receipt lets the demonstration handler repeat safely after a crash before ack; deleting/editing it removes that evidence. External handlers still need their own idempotency. There is no cross-database exactly-once guarantee.

Only operator-owned RelayBox schema-1 files in trusted local directories are supported; this is not a sandbox for hostile database files. No migration or repair is attempted. Ancestor aliases are canonicalized, while explicitly selected symlinks are rejected. Snapshot operations accept a finite 0.1–60 second deadline (default 10), check SQLite VM/backup progress and hash chunks, and reject databases larger than 1 GiB. The deadline cannot interrupt a blocked operating-system I/O call. Verification requires a quiescent standalone DELETE-journal snapshot without sidecars; use `backup` first for live inboxes. Hard-link support is required, Windows ACLs are unverified, hard kills may leave private temporary files, and directory metadata is not fsynced. See the [complete contract](docs/API.md#backup-and-recovery-contract).

After installing, `python examples/verify_backup.py` performs an isolated synthetic backup → verify → restore → expired-lease receipt drill outside the checkout.

## Library API

```python
from relaybox.store import Inbox
from relaybox.worker import ReceiptHandler, run_worker

inbox = Inbox("inbox.sqlite3", max_attempts=3, base_delay=2, max_delay=60)
job, created = inbox.ingest("example-001", b'{"synthetic":true}')
summary = run_worker(inbox, ReceiptHandler("receipts"), max_jobs=10)
print(summary)

# Or pass your own local idempotent handler:
inbox.ingest("example-002", b'{"synthetic":true}')
def handle(job):
    print(job["payload"]["synthetic"])
    # Before a long phase, while ownership is still valid:
    inbox.renew(job["id"], job["lease_token"], lease_seconds=60)

run_worker(inbox, handle, worker="example-worker", lease_seconds=30)
```

`ingest` validates content but does not authenticate a sender; HMAC verification happens at the HTTP boundary. Direct library and CLI access is trusted local operator access. Handlers receive the claimed job, including the private token, and return normally to acknowledge or raise an `Exception` to fail. Long handlers must arrange explicit renewal before expiry or stay below the lease duration: there is no automatic renewal thread or handler timeout. External effects still require the caller's own idempotency. KeyboardInterrupt/SystemExit propagate and leave the lease for expiry recovery; inbox storage/I/O errors also propagate.

## Inspect the engineering

Start with [`store.py`](relaybox/store.py) for atomic `BEGIN IMMEDIATE` claims and token checks, then [`tests/test_store.py`](tests/test_store.py) for real multi-connection contention and expired-worker rejection. [`tests/test_http.py`](tests/test_http.py) exercises live TCP requests, malformed framing, size limits, clock checks, hostile content, and JSON error responses.

```bash
python3 -m unittest discover -v
```

CI runs the complete suite, installs the package, executes the installed CLI outside the checkout, requests assets from the installed server, and verifies signed ingestion → receipt processing → acknowledgment plus snapshot → new-path recovery on Python 3.11 and 3.13. A separate CI job installs the built wheel outside the checkout and checks the served dashboard in Chromium with the development tooling in [`browser/`](browser/README.md). After installation, run `python examples/verify_install.py`, `python examples/verify_worker.py`, and `python examples/verify_backup.py` with that environment's interpreter. Read [verification evidence](docs/VERIFICATION.md), [architecture](docs/ARCHITECTURE.md), [API](docs/API.md), or the [中文指南](docs/GUIDE.zh-CN.md).

## Limits that matter

- **Local only:** IPv4 loopback, plain HTTP, one shared HMAC key; no account authentication, key rotation, multi-tenant permissions, or TLS deployment configuration. Any process/user with local HTTP access can read payloads.
- **At least once:** a lease can expire during work. Tokens fence acknowledgments in this SQLite database; they cannot undo or prevent external side effects. Consumers need their own idempotency strategy.
- **Small scope:** 64 KiB JSON objects, 32 active HTTP handlers, three-second socket read timeout, 10-second SQLite busy timeout. The local worker bounds claims, not handler runtime. No outbound delivery, rate quotas, background scheduler, automatic lease renewal, or distributed worker coordination.
- **Recovery requires activity:** expiry is reconciled on the next claim. The dashboard reports persisted state and may show an expired lease until a worker polls.
- **Storage is bounded only by your disk:** no retention, encryption at rest, automatic backups, migrations beyond schema version 1, payload search, or full audit history. The dashboard shows the latest 100 matches; the API allows up to 500.
- **Demonstration evidence:** synthetic records and deterministic logical state transitions, with measured local checks. No customer usage, production reliability, deployment-capacity benchmark, or independent security audit claims.

See [SECURITY.md](SECURITY.md) and [CONTRIBUTING.md](CONTRIBUTING.md). Licensed under [MIT](LICENSE).
