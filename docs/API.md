# API and CLI contract

Base URL: `http://127.0.0.1:8787`. Every API response is UTF-8 JSON with `Cache-Control: no-store`. There is no CORS support or remote binding option. `Host` must be `127.0.0.1:PORT` or `localhost:PORT`. Cross-site Origin/Fetch Metadata requests are rejected.

## HTTP routes

| Method | Route | Behavior |
|---|---|---|
| GET | `/` | Read-only dashboard |
| GET | `/assets/app.js`, `/assets/style.css` | Bundled assets |
| GET | `/healthz` | Process-level health: `{"status":"ok","service":"relaybox"}`; not a storage health check |
| POST | `/webhooks` | Verify and durably ingest a delivery |
| GET | `/api/metrics` | Read-only [queue metrics](#queue-metrics), returned directly as one snapshot object |
| GET | `/api/stats` | `{"counts":{"queued":0,"leased":0,"succeeded":0,"dead_letter":0}}` |
| GET | `/api/jobs?state=queued&limit=100` | Latest summaries in descending ID order; state optional; limit 1–500, default 100 |
| GET | `/api/jobs/1` | Detail including parsed `payload`; excludes lease token |

Unknown routes return `404`. Unsupported known methods return `405` (unrecognized HTTP methods use the standard parser's JSON `501`). Duplicate/unknown list parameters return `400`. No worker or replay mutation route is exposed over HTTP.

## Queue metrics

`Inbox.metrics() -> dict` and GET `/api/metrics` return exactly the following fields. No query parameters or GET body are accepted (queries follow the existing unmatched-route 404 contract; a body is 400). Host, Origin, Fetch Metadata and common request/header boundaries are unchanged. HTTP uses `Cache-Control: no-store`; no IDs, digests, tokens, workers, error text or payloads are returned.

| Field | Meaning / unit |
|---|---|
| `schema_version` | Current inbox database schema, 1 |
| `snapshot_at` | One supplied-clock sample, Unix seconds |
| `counts` | Integer `queued`, `leased`, `succeeded`, `dead_letter` persisted totals |
| `queued_ready` | Queued rows with `available_at <= snapshot_at` |
| `queued_delayed` | Queued rows with `available_at > snapshot_at` |
| `leases_active` | Leased rows with `lease_until > snapshot_at` |
| `leases_expired_reclaimable` | Leased rows with `lease_until <= snapshot_at` and `attempts < max_attempts` |
| `leases_expired_exhausted` | Leased rows with `lease_until <= snapshot_at` and `attempts >= max_attempts` |
| `oldest_queued_ready_wait_seconds` | Maximum `snapshot_at - available_at` among ready queued rows; null when none |
| `next_retry_at` | Minimum future queued `available_at`, Unix seconds; null when none |
| `next_lease_expiry` | Minimum active `lease_until`, Unix seconds; null when none |

At exact readiness a queued row is ready; at exact lease expiry it is expired. All waits are nonnegative and measure readiness waiting, not `created_at` age or end-to-end latency. Null time/age values are present explicitly, never omitted or replaced with zero. Stored per-job attempt policies govern the expired partition. Expired exhausted records remain persisted leased until ordinary `claim` reconciles them; diagnostics performs no transition.

The method samples `Inbox.clock` once, accepts finite int/float seconds in the inclusive range 0–253402300799 (through year 9999, suitable for browser date rendering), and rejects booleans, other types, nonfinite, negative or larger values with `ValidationError`. Fractional seconds are preserved. One aggregate SQL statement computes counts, partitions and times from the same actual SQLite snapshot, without enumerating payload/private columns. The returned object has fixed size; the SQL scan may visit every row. No constant-time or large-scale performance claim is made. SQLite storage errors propagate; HTTP retains explicit database-unavailable 503 handling.

Call this on an existing `Inbox`. Construction is unchanged: `Inbox(path)` initializes missing databases and configures WAL; metrics itself only reads data. The snapshot is local and finite, reflecting a database statement and a sampled application clock rather than a distributed time guarantee. It does not poll, reconcile leases, schedule work or certify completion. `/api/stats` retains its original persisted counts response, and `/healthz` remains process liveness rather than queue readiness or completion.

The dashboard takes its four cards and operations summary from metrics, and obtains the latest-100 list separately. It labels those separate scopes/instants, shows local browser times and descriptive unavailable markers, and refreshes only on user action. Each section renders as soon as its own request succeeds; a failed section clears its values and displays failure text without hiding successful data in the other section. Counts/times clear while a new snapshot loads, so old values cannot silently look current. Refresh remains focusable while busy, preserving keyboard focus and allowing an explicit retry; generation checks ignore older refresh and detail responses. There is no public hosted backend or automatic telemetry collector.

Since 0.4.1 the dashboard rounds `oldest_queued_ready_wait_seconds` for display only; the HTTP and library value remains exact seconds. Null keeps `— No ready queued jobs`; exactly 0 shows `0 seconds since ready`; values below 0.1 show `Less than 0.1 seconds since ready`. Other values use seconds below 60, minutes below 3600, hours below 86400 and days beyond, with at most one decimal place, the browser's number formatting, singular/plural units and an `About` prefix, for example `About 4.6 seconds since ready` for 4.625 or `About 2 days since ready` for 172800. The unit is chosen before rounding, so 59.99 shows `About 60 seconds`. A wait that is not null or a finite nonnegative number clears the metrics section through its usual error state instead of showing an invented time.

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
| `backup DESTINATION [--timeout 10]` | Online snapshot of existing `--db`; destination and sidecars must not exist |
| `verify-backup FILE [--timeout 10]` | Read-only verification of a quiescent standalone snapshot; ignores `--db` |
| `restore BACKUP [--timeout 10]` | Verified online copy into a nonexistent `--db`; no initialization/overwrite |
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

## Backup and recovery contract

`relaybox.backup.backup_database(source, destination, *, timeout=10) -> dict` accepts an existing regular local schema-1 inbox or snapshot and a new destination in an existing trusted directory. It opens the source with SQLite URI `mode=ro`, `query_only=ON`, `trusted_schema=OFF`, and a read transaction. It never constructs `Inbox`, creates a missing source, executes source-provided SQL, loads extensions, migrates schemas, or attempts repair. Current schema 1 is preserved: no new application ID or migration metadata is required.

Validation allows only the original `jobs` table, its implicit delivery-ID unique index, and `jobs_ready` index, comparing bounded schema declarations and all 17 column definitions (including type, nullability, default, primary-key and hidden flags). Unknown versions, views, triggers, extra objects, altered constraints or structures are rejected. The module runs SQLite `integrity_check` and counts only the four known states. These checks recognize the application's existing structure; they do not authenticate a file or exhaustively validate every historical payload/policy value. Only trusted operator-owned files are supported. SQLite still parses the input file, so this is not a hostile-file security boundary.

The pinned source transaction supplies consistent metadata and pages through Python's SQLite online backup API (`pages=64`). A same-directory, exclusively created private temporary file receives the database; it is finalized into DELETE journal mode, closed, reopened read-only, validated, and checked against the source snapshot's schema/counts. The completed bytes are SHA-256 hashed and fsynced before an exclusive atomic hard link publishes the final file. The final snapshot needs no source WAL/SHM files. Ordinary errors remove private temporary files and their SQLite sidecars. Source data and existing destination entries are not changed. Existing files, directories, hard links, self-aliases and explicitly selected symlinks fail; existing `DESTINATION-wal`, `-shm`, or `-journal` entries also fail without deletion. Sidecars are checked again before publication.

Success returns this shape (counts shown for an empty inbox):

```json
{"source":"/trusted/inbox.sqlite3","destination":"/trusted/snapshot.sqlite3","schema_version":1,"integrity":"ok","jobs":0,"counts":{"queued":0,"leased":0,"succeeded":0,"dead_letter":0},"sha256":"...64 lowercase hex characters...","size_bytes":16384}
```

Size is measured, not fixed; callers must not rely on the example value. Metadata never includes row payloads, workers, errors, delivery IDs or lease tokens. Snapshot bytes retain every column, including private tokens and payloads. `sha256` describes the physical snapshot, not a logical digest or signature; a restore may have a different physical hash while preserving all rows.

`relaybox.backup.verify_backup(path, *, timeout=10) -> dict` opens an existing regular file read-only, performs the same schema/integrity/count checks, and returns the metadata above with `path` instead of `source`/`destination`. It requires a quiescent standalone DELETE-journal snapshot without any `-wal`, `-shm`, or `-journal` entries. Before opening SQLite, it reads only the 100-byte database header and requires SQLite magic plus rollback-format read/write versions (both 1 at offsets 18/19). WAL-format inputs are rejected before a `mode=ro` connection could create WAL/SHM files; no arbitrary source sidecars are cleaned up. Do not edit it or run writers during verification. Passing `--db` to the CLI does not initialize or inspect that unrelated path. A live WAL inbox must first be snapshotted with `backup`.

All three CLI commands validate finite numeric timeout from 0.1 through 60 seconds (default 10); library booleans and other types are rejected. A shared monotonic deadline covers source validation, online copy, snapshot validation, hash chunks and pre-publication checks. SQLite busy waits use the remaining budget (millisecond rounding), VM queries have a progress handler every 1000 instructions, and backup callbacks cancel between 64-page steps, including busy retries. Hashing checks after each 1 MiB chunk. Physical source size and `page_count × page_size` must each be at most 1 GiB (1,073,741,824 bytes), before full integrity scanning/copy. These are cooperative bounds: a single SQLite step, filesystem syscall or fsync may return after the deadline. There is no background timeout thread or strict real-time cancellation guarantee.

Invalid inputs and recognized-but-unsupported files raise the existing `store.ValidationError`; `backup.BackupTimeout` is its deadline subtype. Normal SQLite and filesystem errors propagate. The CLI renders these as JSON errors on stderr and exits 2; success prints the receipt and exits 0. No failure is converted into success. There is no overwrite option. CLI `restore BACKUP` calls the same safe copier with `BACKUP` as source and global `--db` as the new destination; neither destination nor parent is initialized.

The selected file and destination directory's ancestors are canonicalized; selected symlinks are refused. Local files/directories must be trusted against concurrent replacement or modification. The checks do not constitute a filesystem sandbox, and cannot secure attacker-writable paths. New snapshot files request `0600` subject to umask and platform rules; no existing permissions are changed. Atomic hard-link support in the destination filesystem is required; unsupported filesystems fail explicitly. Windows ACL behavior, network filesystems and adversarial races are unverified. File contents are fsynced; final directory metadata is not, so power-loss persistence is not promised. A hard kill may leave private `.relaybox-backup-` files; inspect them only with the owning process stopped.

Recovery preserves all attempts, retry policy, timestamps, tokens, worker fields and replay counts. Stop the original service/workers before switching to the restored copy: each database has independent fencing. Expired leases are reclaimed or dead-lettered by ordinary `claim`; unexpired leases retain their remaining expiry. Deduplication reflects only rows present in the snapshot. Receipt files must be retained/backed up separately, and work after the snapshot may repeat. Retaining identical canonical receipts supports the sample handler's local idempotency; it does not create cross-database exactly-once execution or restore external side effects.
