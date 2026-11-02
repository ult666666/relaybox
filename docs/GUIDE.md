# RelayBox guide

A walkthrough of every RelayBox workflow, with the details the README leaves out. Run each command
from a checkout; `python3 -m relaybox` works without installing. The exact contracts are in
[API.md](API.md), and the reasons behind them are in [ARCHITECTURE.md](ARCHITECTURE.md).
[GUIDE.zh-CN.md](GUIDE.zh-CN.md) is a shorter guide in Chinese.

- [Run the demo](#run-the-demo)
- [Send a signed delivery](#send-a-signed-delivery)
- [Work and recover](#work-and-recover)
- [Deliver to subscribers](#deliver-to-subscribers)
- [Understand an idle worker](#understand-an-idle-worker)
- [Back up and drill recovery](#back-up-and-drill-recovery)
- [Library API](#library-api)

## Run the demo

```bash
python3 -m relaybox --db .local/demo.sqlite3 demo
python3 -m relaybox --db .local/demo.sqlite3 serve --demo-secret
```

Open http://127.0.0.1:8787. If that port is taken, add `--port 8797` to the serve command and
open the matching port. The demo seeds one inbound delivery and one outbound delivery in each
state, using visibly synthetic English and Mandarin payloads, without sending any request. It
refuses to overwrite a nonempty database, so choose a new database path to start another demo.
Stop the server with Ctrl+C.

The `--demo-secret` key is public: `relaybox-synthetic-demo-secret-only`. Use it only for
synthetic local data. For your own local experiment, set `RELAYBOX_SECRET` to a random value of
at least 16 bytes and omit the flag. Keep database files in a private directory.

To install the CLI in an isolated environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/relaybox --help
.venv/bin/relaybox --db .local/installed.sqlite3 demo
```

PowerShell users can use `.venv\Scripts\python.exe` and `.venv\Scripts\relaybox.exe`. Building
uses the setuptools version pinned in `pyproject.toml`; the running application uses only
Python's standard library. No PyPI release is implied.

## Send a signed delivery

With the demo server running on its default port 8787, run this in another terminal from the
checkout. The client sends only to that fixed loopback address:

```bash
python3 examples/send_webhook.py
```

Expected result: HTTP `202` with `created: true`, then HTTP `200` for the identical repeat.
Reusing that ID with different bytes returns `409`. Click Refresh on the dashboard to see the
new delivery.

For any UTF-8 JSON object file, get signing headers with:

```bash
python3 -m relaybox sign example-001 examples/payload.json --demo-secret
```

Send those headers and the unchanged file bytes to `POST /webhooks` within five minutes. The
signature covers `timestamp + "." + delivery_id + "." + raw_body`. Signing the ID stops a valid
body and signature from being reused under another delivery ID. See the full
[HTTP contract](API.md).

## Work and recover

Process ready synthetic deliveries into local JSON receipt files:

```bash
python3 -m relaybox --db .local/demo.sqlite3 work --receipts .local/receipts --max-jobs 10
```

The worker claims at most 10 jobs and stops as soon as none is ready; it does not wait for future
retries. Each written receipt is acknowledged, and a handler failure follows the retry and
dead-letter policy. It prints a summary such as
`{"claimed":1,"succeeded":1,"failed":0,"lost_leases":0}`. Defaults: `--max-jobs 1` (up to 1000),
`--worker local-worker` and `--lease-seconds 30`.

Each receipt holds a format version, the delivery ID, the SHA-256 digest of the original body and
the parsed payload. Its filename hashes the delivery ID with the body digest, so path-like IDs
cannot escape the directory. Running a job again finds the identical receipt and succeeds without
rewriting it; a conflicting or corrupt receipt fails instead of being overwritten. This shows
local idempotency only, with no distributed exactly-once or power-loss guarantee. See the
[receipt format and file-safety rules](API.md#local-worker-library-and-receipt-format).

To claim and finish jobs by hand:

```bash
python3 -m relaybox --db .local/demo.sqlite3 list --state dead_letter
python3 -m relaybox --db .local/demo.sqlite3 replay 3
python3 -m relaybox --db .local/demo.sqlite3 claim --worker local-reviewer --lease-seconds 30
```

The claim response includes a private `lease_token`. Complete the claimed ID with that token
before the lease expires:

```bash
# Extend a still-current lease before expiry (same token, no new attempt):
python3 -m relaybox --db .local/demo.sqlite3 renew JOB_ID --token TOKEN --lease-seconds 60
python3 -m relaybox --db .local/demo.sqlite3 ack JOB_ID --token TOKEN
# Or record a failure; this schedules a retry or ends in dead_letter:
python3 -m relaybox --db .local/demo.sqlite3 fail JOB_ID --token TOKEN --error "Synthetic local failure"
```

- Commands print JSON and exit `0` on success, or `2` for invalid arguments and failed
  operations. `work` exits `1` if a handler failed or a lease was lost.
- An empty claim succeeds and returns `{"job": null}`.
- `replay` only accepts dead letters, and each replay resets the attempt budget.
- Worker operations stay in the CLI and library, so the dashboard cannot change inbound jobs.

## Deliver to subscribers

Outbound delivery was added in 0.5.0. A subscription holds an endpoint URL, the event types it
wants and one or more signing secrets. Publishing a message queues one delivery per matching
subscription. `deliver` sends each one as a JSON POST with the three
[Standard Webhooks](https://github.com/standard-webhooks/standard-webhooks/blob/main/spec/standard-webhooks.md)
headers: `webhook-id`, `webhook-timestamp` and `webhook-signature` (`v1,` plus the base64
HMAC-SHA256 of `id.timestamp.body`).

Start the example receiver in one terminal. It checks every signature with its own code, and
here it answers 503 to the first delivery:

```bash
python3 examples/receive_webhooks.py --demo-secret --fail-first 1
```

In another terminal, subscribe it, publish a message and deliver:

```bash
python3 -m relaybox --db .local/out.sqlite3 subscription add http://127.0.0.1:8788/hook --event-type example.created --demo-secret
python3 -m relaybox --db .local/out.sqlite3 publish example.created examples/payload.json
python3 -m relaybox --db .local/out.sqlite3 deliver --max-deliveries 10 --wait 15
```

The first attempt gets 503 with `Retry-After: 1`. `--wait` keeps the dispatcher running until the
retry is due, and the second attempt succeeds with the same `webhook-id` and a new timestamp.
`deliveries --state dead_letter` lists failures, and `replay-delivery ID` requeues one. Without
`--demo-secret`, `subscription add` generates a secret and prints it once; give it to the
receiver with `--secret`.

- Retries wait a random time between 0 and `min(max_delay, base_delay × 2^(attempts−1))` ("full
  jitter"), so endpoints that fail together are not retried together. A `Retry-After` header
  (seconds or an HTTP date) raises the wait to at least that value, capped at `max_delay`.
  Defaults: 6 attempts, a base of 2 s and a cap of 300 s.
- 2xx succeeds. 408, 429, 5xx, timeouts and connection errors retry. 410 Gone dead-letters the
  delivery and disables the subscription. Every 3xx and every other 4xx dead-letters at once;
  redirects are never followed.
- Each attempt has a wall-clock deadline (`--timeout`, default 15 s) and reads at most 4 KiB of
  the response body. The lease must be longer than the deadline, so the dispatcher never needs to
  renew it.
- A dispatcher that dies mid-request keeps its lease until it expires. Another dispatcher then
  sends the same message again with the same `webhook-id`, so receivers should drop repeats of an
  ID they have already processed.
- Only literal loopback addresses (`127.0.0.0/8` and `::1`) are accepted; `localhost` is not
  resolved. A non-loopback endpoint needs `--allow-remote` on both `subscription add` and
  `deliver`, and HTTPS with certificate checks. Read [SECURITY.md](../SECURITY.md) first.
- `subscription rotate-secret ID --grace-seconds 3600` adds a new secret. Until the grace period
  ends, every delivery carries one signature per active secret, so the receiver can switch keys
  without failed deliveries.
- `relay --max-jobs 10` fans ready inbound jobs out to subscribers, using the payload's top-level
  `type` field as the event type. The outbound `webhook-id` is derived from the inbound delivery,
  so relaying the same job twice queues nothing new.
- The dashboard's outbound section lists the latest deliveries and their last result. Start
  `serve` with `--allow-replay` to replay dead letters from the page; otherwise the page is
  read-only and you use the CLI.

## Understand an idle worker

The read-only operations summary was added in 0.4.0. Use Refresh in the dashboard, or GET
`/api/metrics` from the local server, to tell queued work that is ready now from delayed
retries, and active leases from expired ones. The four totals still show persisted state: an
expired lease with no attempts left stays in Leased until a worker's next claim moves it to dead
letter. Viewing diagnostics never claims or changes a job.

The oldest ready wait is the time since a job became ready (`available_at`), not the time since
ingestion. The dashboard rounds it, for example `About 4.6 seconds since ready`, and the API keeps
exact seconds. Refresh is manual. If one section fails to load, it shows an error and the other
keeps working.

Library users can call `inbox.metrics()` on an existing `Inbox`. It returns a read-only, local
diagnostic snapshot: its query can scan every row, but never selects payloads or private job
fields. See the [metrics contract](API.md#queue-metrics).

To see that scan cost, use the source-only [local measurement tool](PERFORMANCE.md). It checks a
fixed synthetic workload against an independent oracle, then keeps raw installed-library call
timings and environment metadata. No production capacity or timing threshold is implied.

## Back up and drill recovery

Consistent local SQLite snapshots were added in 0.3.0. Never copy just a live inbox's main file:
committed jobs may still be in its WAL. Choose an existing trusted private directory and fresh
destination names:

```bash
python3 -m relaybox --db .local/demo.sqlite3 backup .local/snapshot.sqlite3 --timeout 10
python3 -m relaybox verify-backup .local/snapshot.sqlite3
```

`backup` prints a JSON summary with the paths, schema version, integrity result, job counts,
SHA-256 and file size; it never includes payloads or lease tokens. The snapshot itself contains
all data, including tokens and payloads, so protect it like the original. A backup includes
committed WAL data, leaves out uncommitted changes, and refuses to overwrite any existing path.

Before switching to a restored path, stop the original service and every original worker, and
keep the original stopped during the drill. Restore to a new name; do not initialize that
destination first:

```bash
python3 -m relaybox --db .local/recovered.sqlite3 restore .local/snapshot.sqlite3
python3 -m relaybox --db .local/recovered.sqlite3 stats
python3 -m relaybox --db .local/recovered.sqlite3 work --receipts .local/receipts --max-jobs 10
```

Compare the restored counts with the saved backup summary before processing work. Attempts, retry
policy, replay counts and leases survive unchanged, and the next claim reclaims or dead-letters
any expired lease. Keep the receipt directory as well, because the database backup does not
include it, and expect work done after the snapshot to repeat. There is no cross-database
exactly-once guarantee.

Backups support trusted RelayBox databases that you own, up to 1 GiB, with a deadline of 0.1 to
60 seconds (default 10). `verify-backup` needs a standalone snapshot that nothing is writing to,
so run `backup` first for a live inbox. See the
[complete contract](API.md#backup-and-recovery-contract) for the file-safety rules and platform
limits.

After installing, `python examples/verify_backup.py` runs an isolated synthetic drill outside the
checkout: backup, verify, restore, and an expired lease that reuses its receipt.

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

# Outbound: subscribe, publish and attempt delivery (loopback endpoints only).
from relaybox.dispatch import run_dispatcher
from relaybox.outbound import Outbox

outbox = Outbox("inbox.sqlite3", max_attempts=10, base_delay=5, max_delay=3600)
subscription, secret = outbox.subscribe("http://127.0.0.1:8788/hook", ["example.created"])
outbox.publish("example.created", b'{"type":"example.created","synthetic":true}')
print(run_dispatcher(outbox, max_deliveries=10, timeout=15, lease_seconds=60))
```

`ingest` validates content but does not authenticate a sender. HMAC verification happens at the
HTTP boundary, and library and CLI access is trusted local access. A handler returns normally to
acknowledge the job or raises an `Exception` to fail it. There is no automatic lease renewal or
handler timeout, so a long handler must call `renew` before its lease expires. External side
effects still need their own idempotency. See the
[worker library contract](API.md#local-worker-library-and-receipt-format).
