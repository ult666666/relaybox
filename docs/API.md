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
| `ack JOB_ID --token TOKEN` | Complete current unexpired ownership |
| `fail JOB_ID --token TOKEN --error TEXT` | Schedule bounded backoff or dead letter; error length 1–500 |
| `replay JOB_ID` | Reset a dead letter to queued and increment replay count |

All data commands print JSON. Normal completion exits `0`; invalid input or a failed transition exits `2`. A ready-empty claim returns `{"job":null}`. Newly minted tokens contain 64 lowercase hex characters (256 random bits), so they can be pasted safely as CLI option values. Tokens are private local worker capabilities: keep command output and shell history private when using non-synthetic data. There is no automatic worker loop; integrations can call the [library API](../README.md#library-api).
