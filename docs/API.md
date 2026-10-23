# API and CLI contract

Base URL: `http://127.0.0.1:8787`. Every API response is UTF-8 JSON with `Cache-Control: no-store`. There is no CORS support or remote binding option. `Host` must be `127.0.0.1:PORT` or `localhost:PORT`. Cross-site Origin/Fetch Metadata requests are rejected.

## HTTP routes

| Method | Route | Behavior |
|---|---|---|
| GET | `/` | Read-only dashboard |
| GET | `/assets/app.js`, `/assets/style.css` | Bundled assets |
| GET | `/healthz` | Process-level health: `{"status":"ok","service":"relaybox"}`; not a storage health check |
| POST | `/webhooks` | Verify and durably ingest a delivery |
| GET | `/api/stats` | `{"counts":{"queued":0,"leased":0,"succeeded":0,"dead_letter":0}}` |
| GET | `/api/jobs?state=queued&limit=100` | Latest summaries in descending ID order; state optional; limit 1–500, default 100 |
| GET | `/api/jobs/1` | Detail including parsed `payload`; excludes lease token |

Unknown routes return `404`. Unsupported known methods return `405` (unrecognized HTTP methods use the standard parser's JSON `501`). Duplicate/unknown list parameters return `400`. No worker or replay mutation route is exposed over HTTP.

## Signed ingestion

Required headers:

| Header | Contract |
|---|---|
| `Content-Type` | `application/json` or `application/json; charset=utf-8` |
| `Content-Length` | Canonical nonnegative decimal; at most 65,536 body bytes |
| `X-RelayBox-ID` | 1–128 ASCII letters, digits, `.`, `_`, `:`, `-` |
| `X-RelayBox-Timestamp` | Canonical Unix seconds, up to 12 digits; absolute skew ≤ 300 seconds |
| `X-RelayBox-Signature` | `sha256=` followed by 64 lowercase hexadecimal characters |

Signature calculation:

```python
import hashlib
import hmac

message = str(timestamp).encode("ascii") + b"." + delivery_id.encode("ascii") + b"." + raw_body
signature = "sha256=" + hmac.new(secret, message, hashlib.sha256).hexdigest()
```

The body is a strict UTF-8 JSON object. Arrays/scalars at the top level, duplicate keys at any depth, invalid Unicode and nonfinite numbers (including exponent overflow) are rejected. Nested arrays and objects are allowed within parser limits. The digest uses the original bytes, not canonical JSON; whitespace and key order affect identity.

| Status | Meaning |
|---|---|
| `202` | New durable delivery; `created: true` |
| `200` | Existing ID with the same raw SHA-256 digest; `created: false` |
| `400` | Invalid identifier, JSON, headers, framing, or query |
| `401` | Required authentication headers missing, stale/future timestamp, or MAC mismatch |
| `403` / `421` | Cross-site request / unacceptable Host |
| `408` | Socket body read timeout |
| `409` | Existing ID with a different digest; original row is preserved |
| `411` / `413` / `415` / `417` | Missing length / oversized body / unsupported media or compression / unsupported Expect |
| `503` | Handler capacity exhausted or SQLite unavailable |

Accepted response shape: `{"created":true,"job":{...}}`. A job includes `id`, `delivery_id`, `digest`, `payload`, `state`, `attempts`, retry policy, timestamps, nullable lease expiry/worker/error, and `replay_count`. Lists omit payloads. All HTTP responses omit `lease_token`. Error shape: `{"error":"description"}`. Do not depend on exact English error wording.

## Local commands

Global `--db PATH` comes **before** the subcommand. Default database: `inbox.sqlite3`.

| Command | Purpose |
|---|---|
| `init`, `stats` | Initialize/inspect the database |
| `demo` | Seed four synthetic deliveries into an empty database |
| `serve [--port 8787] [--demo-secret]` | Run the loopback server; otherwise reads `RELAYBOX_SECRET` |
| `sign ID FILE [--timestamp SECONDS] [--demo-secret]` | Emit signed request headers, without reading/writing the database |
| `list [--state STATE] [--limit 100]` | Latest job summaries |
| `show JOB_ID` | Payload and state, without token |
| `claim --worker NAME [--lease-seconds 30]` | Atomically claim ready work; lease from 0.001 to 3600 seconds |
| `renew JOB_ID --token TOKEN [--lease-seconds 30]` | Extend a current unexpired lease; return job without token |
| `work --receipts DIR [--worker local-worker] [--max-jobs 1] [--lease-seconds 30]` | Process bounded ready work into local synthetic receipts; no imports or shell handlers |
| `ack JOB_ID --token TOKEN` | Complete current unexpired ownership |
| `fail JOB_ID --token TOKEN --error TEXT` | Schedule bounded backoff or dead letter; error length 1–500 |
| `replay JOB_ID` | Reset a dead letter to queued and increment replay count |

All data commands print JSON. Normal completion exits `0`; invalid input, a failed manual transition, or an inbox storage/I/O error exits `2`. `work` exits `1` when its summary contains recorded handler failures or lost leases. An invalid receipt directory fails setup with exit `2`; receipt content/publication failures during handling use the retry/dead-letter policy and exit `1`. A ready-empty claim returns `{"job":null}`. Newly minted tokens contain 64 lowercase hex characters (256 random bits), so they can be pasted safely as CLI option values. Tokens are private local worker capabilities: keep claim output and shell history private when using non-synthetic data. `work` emits counts only, never job payloads or tokens.

## Lease renewal library contract

`Inbox.renew(job_id: int, token: str, *, lease_seconds=30) -> dict` requires a positive signed 64-bit job ID and a finite numeric duration from 0.001 to 3600 seconds (booleans rejected). It samples the clock after acquiring `BEGIN IMMEDIATE` and requires the current leased state, matching token, and `lease_until > now`. Exact expiry is expired. Missing IDs raise `KeyError`; expired, stale, wrong-token, or already completed jobs raise `InvalidTransition`; invalid configuration raises `ValidationError`.

The expiry becomes `max(existing lease_until, now + lease_seconds)` and `updated_at` becomes `now`. Renewal cannot shorten a lease, change its worker/token, or increment attempts. The library returns the full claimed-job shape including the existing token; the CLI omits the token. Renewal and completion serialize through SQLite, so renewal cannot resurrect a completed job. There is no automatic renewal thread. A long-running handler must explicitly renew before expiry or finish inside its lease; all external side effects still need caller idempotency.

## Local worker library and receipt format

`relaybox.worker.run_worker(inbox, handler, *, worker='local-worker', max_jobs=1, lease_seconds=30) -> dict` accepts a callable receiving a claimed job. Worker-name and lease validation match `Inbox.claim`; `max_jobs` must be an integer 1–1000 (booleans rejected). All configuration is checked before any claim. A normal handler return attempts acknowledgment; a handler `Exception` records a nonempty UTF-8 diagnostic of at most 500 characters and attempts failure. A handler's return value is ignored.

If rendering an exception's text raises another ordinary `Exception`, the diagnostic falls back to the handler exception's type and an unavailable-text message, with the same encoding and length bound. This fallback does not catch KeyboardInterrupt/SystemExit raised during formatting, and it does not catch storage errors from failure completion.

The result has integer `claimed`, `succeeded`, `failed`, and `lost_leases` counts. `failed` counts recorded retry/dead-letter transitions. If acknowledgment or failure rejects ownership with `InvalidTransition`, only `lost_leases` increments; therefore `claimed = succeeded + failed + lost_leases` on normal return. Inbox errors other than ownership rejection propagate instead of becoming a successful-looking summary. KeyboardInterrupt/SystemExit propagate without failure completion, leaving the lease for recovery. No handler timeout, wait for future retries, background renewal, or unbounded polling occurs. With zero-delay retry policies the same delivery can count as several claims, up to the bound.

`ReceiptHandler(path)` is a callable local demonstration handler. It publishes a file named:

```text
sha256(UTF8(delivery_id) + b'\0' + ASCII(raw_body_sha256_hex)).hexdigest() + '.json'
```

Version 1 contains exactly `version: 1`, `delivery_id`, `digest` (the original raw-body SHA-256 hex), and `payload` (the parsed synthetic JSON object). It deliberately omits database-local job IDs, attempts, timestamps, workers, and lease tokens. The bytes are Python `json.dumps` with sorted keys recursively, `ensure_ascii=True`, `allow_nan=False`, separators `(',', ':')`, and one trailing newline, encoded as UTF-8. Example:

```json
{"delivery_id":"synthetic-001","digest":"...64 lowercase hex characters...","payload":{"synthetic":true},"version":1}
```

Only byte-identical existing contents succeed without rewriting. Conflicting, corrupt, noncanonical or unexpected contents fail explicitly; symlink, directory, and other nonregular receipt entries are rejected. A same-directory temporary file is fully written, flushed and fsynced before an atomic hard link publishes it exclusively; final names are never overwritten, and normal error cleanup removes the temporary file. A hard kill may leave a `.relaybox-` temporary file, never a partially published final receipt. No receipt cleanup or retention is automatic.

Use a trusted local directory and trusted parent directories that no other actor concurrently renames or replaces. The selected receipt directory itself must not be a symlink; trusted ancestor aliases are resolved once so ordinary OS paths work. The canonical directory is rechecked before each effect. These checks do not make an attacker-writable directory safe. Newly created directories request mode `0700`, receipts `0600`, subject to umask/platform permissions; existing permissions are unchanged. Filesystems without atomic hard-link support fail explicitly. POSIX permissions and behavior are locally verified; Windows ACL semantics are not certified. Receipt publication and inbox completion are separate operations; directory metadata is not fsynced, so no power-loss durability or distributed exactly-once guarantee is claimed. Idempotency relies on retaining the identical receipt, a stable receipt format, and collision resistance of SHA-256.
