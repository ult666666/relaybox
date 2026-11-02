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
| GET | `/api/deliveries?state=dead_letter&limit=100` | Latest outbound delivery summaries, whole-table `counts` and `replay_enabled`; state optional; limit 1–500, default 100 |
| POST | `/api/deliveries/1/replay` | Requeue one outbound dead letter; only when `serve --allow-replay` is set ([contract](#outbound-delivery)) |

Unknown routes return `404`. Unsupported known methods return `405` (unrecognized HTTP methods use the standard parser's JSON `501`). Duplicate/unknown list parameters return `400`. The outbound replay route is the only mutation exposed over HTTP; inbound worker operations and inbound replay stay in the CLI and library.

## Queue metrics

`Inbox.metrics() -> dict` and GET `/api/metrics` return exactly the following fields. No query parameters or GET body are accepted (queries follow the existing unmatched-route 404 contract; a body is 400). Host, Origin, Fetch Metadata and common request/header boundaries are unchanged. HTTP uses `Cache-Control: no-store`; no IDs, digests, tokens, workers, error text or payloads are returned.

| Field | Meaning / unit |
|---|---|
| `schema_version` | Version of this response format, 1. It stays 1 although the database schema became 2 in 0.5.0 |
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

The dashboard takes its four state totals and snapshot details from metrics, and obtains the latest-100 list separately. It labels those separate scopes/instants, shows local browser times and descriptive unavailable markers, and refreshes only on user action. Each section renders as soon as its own request succeeds; a failed section clears its values and displays failure text without hiding successful data in the other section. Counts/times clear while a new snapshot loads, so old values cannot silently look current. Refresh remains focusable while busy, preserving keyboard focus and allowing an explicit retry; generation checks ignore older refresh and detail responses. There is no public hosted backend or automatic telemetry collector.

Since 0.4.1 the dashboard rounds `oldest_queued_ready_wait_seconds` for display only; the HTTP and library value remains exact seconds. Null shows `No ready jobs`; exactly 0 shows `0 seconds since ready`; values below 0.1 show `Less than 0.1 seconds since ready`. Other values use seconds below 60, minutes below 3600, hours below 86400 and days beyond, with at most one decimal place, the browser's number formatting, singular/plural units and an `About` prefix, for example `About 4.6 seconds since ready` for 4.625 or `About 2 days since ready` for 172800. The unit is chosen before rounding, so 59.99 shows `About 60 seconds`. A wait that is not null or a finite nonnegative number clears the metrics section through its usual error state instead of showing an invented time.

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
| `subscription add URL --event-type TYPE [--event-type ...] [--secret whsec_... \| --demo-secret] [--allow-remote]` | Create an active subscription; prints its secret once |
| `subscription list` | Subscriptions with secret IDs, fingerprints and expiry, never secret values |
| `subscription rotate-secret ID [--grace-seconds 86400] [--secret whsec_...]` | Add a new current secret; older ones sign until the grace period ends (0–2,592,000 s); prints the new secret once |
| `subscription enable ID`, `subscription disable ID` | Change whether new messages fan out to it and whether its deliveries are sent |
| `publish TYPE FILE [--webhook-id ID]` | Store a JSON object message; queue one delivery per matching active subscription |
| `relay [--worker relay-worker] [--max-jobs 1] [--lease-seconds 30]` | Fan ready inbound jobs out by their payload's `type` field |
| `deliver [--worker local-dispatcher] [--max-deliveries 1] [--lease-seconds 60] [--timeout 15] [--wait 0] [--allow-remote]` | Attempt bounded ready deliveries |
| `deliveries [--state STATE] [--limit 100]` | Latest delivery summaries and counts |
| `replay-delivery DELIVERY_ID` | Requeue an outbound dead letter with the same `webhook-id` |

All data commands print JSON. Normal completion exits `0`; invalid input, a failed manual transition, or an inbox storage/I/O error exits `2`. `work` and `relay` exit `1` when their summary contains recorded handler failures or lost leases. `deliver` exits `1` when a delivery ended in dead letter, a lease was lost, or a delivery it attempted is still waiting for a retry when it stops. An invalid receipt directory fails setup with exit `2`; receipt content/publication failures during handling use the retry/dead-letter policy and exit `1`. A ready-empty claim returns `{"job":null}`. Newly minted tokens contain 64 lowercase hex characters (256 random bits), so they can be pasted safely as CLI option values. Tokens are private local worker capabilities: keep claim output and shell history private when using non-synthetic data. `work` emits counts only, never job payloads or tokens.

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

`relaybox.backup.backup_database(source, destination, *, timeout=10) -> dict` accepts an existing regular local schema-1 or schema-2 inbox or snapshot and a new destination in an existing trusted directory. It opens the source with SQLite URI `mode=ro`, `query_only=ON`, `trusted_schema=OFF`, and a read transaction. It never constructs `Inbox`, creates a missing source, executes source-provided SQL, loads extensions, migrates schemas, or attempts repair. The source's schema version (1 or 2) is preserved in the copy; a restored version-1 file is upgraded to version 2 only when RelayBox next opens it with `Inbox` or `Outbox`.

Validation allows only the tables and indexes of the file's version: for version 1 the original `jobs` table, its implicit delivery-ID unique index and `jobs_ready`; for version 2 also `subscriptions`, `subscription_secrets`, `messages`, `deliveries`, their implicit unique indexes and `deliveries_ready`. It compares bounded schema declarations and every column definition (including type, nullability, default, primary-key and hidden flags) with a reference built by running RelayBox's own schema statements in a private in-memory database; the 17 version-1 `jobs` columns are also pinned separately. Unknown versions, views, triggers, extra objects, altered constraints or structures are rejected. The module runs SQLite `integrity_check` (and `foreign_key_check` for version 2) and counts only the four known states. These checks recognize the application's existing structure; they do not authenticate a file or exhaustively validate every historical payload/policy value. Only trusted operator-owned files are supported. SQLite still parses the input file, so this is not a hostile-file security boundary.

The pinned source transaction supplies consistent metadata and pages through Python's SQLite online backup API (`pages=64`). A same-directory, exclusively created private temporary file receives the database; it is finalized into DELETE journal mode, closed, reopened read-only, validated, and checked against the source snapshot's schema/counts. The completed bytes are SHA-256 hashed and fsynced before an exclusive atomic hard link publishes the final file. The final snapshot needs no source WAL/SHM files. Ordinary errors remove private temporary files and their SQLite sidecars. Source data and existing destination entries are not changed. Existing files, directories, hard links, self-aliases and explicitly selected symlinks fail; existing `DESTINATION-wal`, `-shm`, or `-journal` entries also fail without deletion. Sidecars are checked again before publication.

Success returns this shape (shown for an empty version-1 inbox):

```json
{"source":"/trusted/inbox.sqlite3","destination":"/trusted/snapshot.sqlite3","schema_version":1,"integrity":"ok","jobs":0,"counts":{"queued":0,"leased":0,"succeeded":0,"dead_letter":0},"sha256":"...64 lowercase hex characters...","size_bytes":16384}
```

Size is measured, not fixed; callers must not rely on the example value. Version-2 receipts add `"outbound": {"subscriptions": N, "messages": N, "deliveries": N, "counts": {...}}` with the four delivery states; version-1 receipts keep the shape above. Metadata never includes row payloads, workers, errors, delivery IDs or lease tokens. Snapshot bytes retain every column, including private tokens and payloads. `sha256` describes the physical snapshot, not a logical digest or signature; a restore may have a different physical hash while preserving all rows.

`relaybox.backup.verify_backup(path, *, timeout=10) -> dict` opens an existing regular file read-only, performs the same schema/integrity/count checks, and returns the metadata above with `path` instead of `source`/`destination`. It requires a quiescent standalone DELETE-journal snapshot without any `-wal`, `-shm`, or `-journal` entries. Before opening SQLite, it reads only the 100-byte database header and requires SQLite magic plus rollback-format read/write versions (both 1 at offsets 18/19). WAL-format inputs are rejected before a `mode=ro` connection could create WAL/SHM files; no arbitrary source sidecars are cleaned up. Do not edit it or run writers during verification. Passing `--db` to the CLI does not initialize or inspect that unrelated path. A live WAL inbox must first be snapshotted with `backup`.

All three CLI commands validate finite numeric timeout from 0.1 through 60 seconds (default 10); library booleans and other types are rejected. A shared monotonic deadline covers source validation, online copy, snapshot validation, hash chunks and pre-publication checks. SQLite busy waits use the remaining budget (millisecond rounding), VM queries have a progress handler every 1000 instructions, and backup callbacks cancel between 64-page steps, including busy retries. Hashing checks after each 1 MiB chunk. Physical source size and `page_count × page_size` must each be at most 1 GiB (1,073,741,824 bytes), before full integrity scanning/copy. These are cooperative bounds: a single SQLite step, filesystem syscall or fsync may return after the deadline. There is no background timeout thread or strict real-time cancellation guarantee.

Invalid inputs and recognized-but-unsupported files raise the existing `store.ValidationError`; `backup.BackupTimeout` is its deadline subtype. Normal SQLite and filesystem errors propagate. The CLI renders these as JSON errors on stderr and exits 2; success prints the receipt and exits 0. No failure is converted into success. There is no overwrite option. CLI `restore BACKUP` calls the same safe copier with `BACKUP` as source and global `--db` as the new destination; neither destination nor parent is initialized.

The selected file and destination directory's ancestors are canonicalized; selected symlinks are refused. Local files/directories must be trusted against concurrent replacement or modification. The checks do not constitute a filesystem sandbox, and cannot secure attacker-writable paths. New snapshot files request `0600` subject to umask and platform rules; no existing permissions are changed. Atomic hard-link support in the destination filesystem is required; unsupported filesystems fail explicitly. Windows ACL behavior, network filesystems and adversarial races are unverified. File contents are fsynced; final directory metadata is not, so power-loss persistence is not promised. A hard kill may leave private `.relaybox-backup-` files; inspect them only with the owning process stopped.

Recovery preserves all attempts, retry policy, timestamps, tokens, worker fields and replay counts. Stop the original service/workers before switching to the restored copy: each database has independent fencing. Expired leases are reclaimed or dead-lettered by ordinary `claim`; unexpired leases retain their remaining expiry. Deduplication reflects only rows present in the snapshot. Receipt files must be retained/backed up separately, and work after the snapshot may repeat. Retaining identical canonical receipts supports the sample handler's local idempotency; it does not create cross-database exactly-once execution or restore external side effects.

## Outbound delivery

All outbound state lives in the same SQLite file as the inbox (schema version 2). `relaybox.outbound.Outbox(path, *, clock=time.time, max_attempts=6, base_delay=2, max_delay=300, rng=None)` validates its policy like `Inbox` (attempts 1–100, delays 0–86,400 seconds) and copies it into each delivery at fan-out. `rng` defaults to `random.SystemRandom()`.

| Method | Contract |
|---|---|
| `subscribe(url, event_types, *, secret=None, allow_remote=False) -> (dict, str)` | Validates the URL (see below) and 1–32 event types of `[A-Za-z0-9_]` segments separated by `.`, or `*`. Returns the subscription and its secret; a generated secret has 32 random bytes. At most 100 active subscriptions. |
| `subscriptions() -> list` | Each subscription with `secrets` metadata: `id`, a 16-hex-digit SHA-256 `fingerprint`, `created_at`, `expires_at`, `active`. Never key bytes. |
| `rotate_secret(id, *, secret=None, grace_seconds=86400) -> (dict, str)` | Adds a current key; sets older keys to expire at `now + grace_seconds` unless they expire sooner; deletes expired keys; at most 5 active keys. |
| `set_subscription_state(id, *, active, reason=None) -> dict` | Enables or disables a subscription. |
| `publish(event_type, body, *, webhook_id=None) -> (dict, bool)` | Body is a JSON object of at most 64 KiB, validated like inbound payloads. `webhook_id` is 1–128 characters of `[A-Za-z0-9_-]` (no `.`), default `msg_` plus 32 random hex digits. Returns the message summary with its delivery count, and whether it was created. Same ID with different type or bytes raises `Conflict`. |
| `relay(job) -> (dict, bool)` | Publishes a stored inbound job's exact bytes under event type `payload["type"]` and the derived ID `msg_` + `sha256(UTF8(delivery_id) + b"\0" + ASCII(digest)).hexdigest()[:32]`. `RelayHandler(outbox)` wraps it for `run_worker`. |
| `claim(worker, *, lease_seconds=60)`, `renew(id, token, *, lease_seconds=60)` | Same contract as `Inbox.claim`/`renew`. The private result includes `lease_token`, `body`, `url`, `webhook_id`, `event_type`, `subscription_state` and `secrets` (active raw keys, newest first). |
| `complete(id, token, outcome) -> str` | Records an `Outcome(kind, status=None, error=None, retry_after=None)` under the current lease and returns the new state. `kind` is `success`, `retry`, `permanent` or `gone`; failures need a 1–500 character error. |
| `replay(id) -> dict` | Dead letter to queued: attempts 0, `replay_count` + 1, error and status cleared, same `webhook-id`. Raises `InvalidTransition` for other states or a disabled subscription. |
| `get(id)`, `list_deliveries(*, state=None, limit=100)` | Summaries: delivery fields plus `url`, `subscription_state`, `webhook_id`, `event_type` and `digest`; never body, token or keys. `list_deliveries` returns `{"deliveries": [...], "counts": {...}}` from one read transaction. |
| `next_due() -> float \| None` | Earliest queued `available_at` or leased `lease_until`. |

`relaybox.dispatch.run_dispatcher(outbox, *, worker="local-dispatcher", max_deliveries=1, lease_seconds=60, timeout=15, allow_remote=False, max_response_bytes=4096, wait=0, sleep=time.sleep) -> dict` checks `max_deliveries` 1–1000, `timeout` 0.01–60 seconds, `wait` 0–3600 seconds, `max_response_bytes` 0–65,536 and `lease_seconds > timeout` before claiming anything. It returns per-attempt counts `claimed`, `succeeded`, `retried`, `dead_lettered`, `lost_leases`, plus `awaiting_retry`: the number of deliveries whose latest attempt in this run scheduled a retry. With `wait` it sleeps until the next due delivery or lease expiry while that falls inside the remaining wait.

Each attempt is one `POST` to the URL's path and query with these headers, and the message body byte for byte:

| Header | Value |
|---|---|
| `Content-Type` | `application/json` |
| `webhook-id` | The message's ID, identical on every attempt and replay |
| `webhook-timestamp` | Attempt time in integer Unix seconds |
| `webhook-signature` | `v1,` + base64(HMAC-SHA256(key, `id.timestamp.body`)), one per active key, space-separated, newest first |
| `User-Agent` | `RelayBox/VERSION` |
| `Connection` | `close` |

Outcomes follow the [classification table](ARCHITECTURE.md#one-attempt). A retry is scheduled at `now + max(uniform(0, min(max_delay, base_delay × 2^(attempts−1))), min(retry_after, max_delay))`, without the second term when there is no usable `Retry-After`. `Retry-After` accepts 1–10 digits of delay-seconds or an HTTP-date with a time zone; a past date counts as 0, a request longer than 86,400 seconds (the largest `max_delay`) counts as 86,400, and anything else is ignored. Errors stored in `last_error` are bounded text: transport failures are fixed phrases such as `connection refused` or `timed out after 15 s`, and HTTP failures add at most 200 printable characters of the response body.

URL rules: absolute `http://` or `https://`, at most 2048 printable ASCII characters, no spaces, credentials or fragment, and a port from 1 to 65535. Without `allow_remote`, the host must be a literal loopback IP address (`127.0.0.0/8`, `::1` or `::ffff:127.0.0.0/104`). With it, any host is allowed but a non-loopback host must use `https://`, verified with `ssl.create_default_context()`.

`POST /api/deliveries/ID/replay` returns `403` unless `serve --allow-replay` was given. It requires `Origin` equal to `http://127.0.0.1:PORT` or `http://localhost:PORT`, `Sec-Fetch-Site: same-origin` if that header is present (`403` otherwise), and `Content-Type: application/json` with no body (`415` otherwise). Success returns `200` with `{"delivery": {...}}`; a delivery that is not a dead letter or belongs to a disabled subscription returns `409`, an unknown ID `404`.
