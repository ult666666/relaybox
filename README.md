# RelayBox

[![RelayBox checks](https://github.com/ult666666/relaybox/actions/workflows/ci.yml/badge.svg)](https://github.com/ult666666/relaybox/actions/workflows/ci.yml)

RelayBox is a webhook inbox and outbox that runs locally on Python 3.11+ and SQLite, with no
runtime dependencies. It accepts HMAC-signed deliveries, drops duplicates, and hands each one to a
worker through a leased job with retries and a dead-letter queue. It also sends your own events
to subscribers, signed per the
[Standard Webhooks](https://github.com/standard-webhooks/standard-webhooks/blob/main/spec/standard-webhooks.md)
spec, with jittered retries and replay. A small dashboard shows both sides.

![The RelayBox dashboard with the synthetic demo data: inbox totals and the snapshot details, four inbound deliveries, one payload, and four outbound deliveries to a loopback subscriber, one per state, with a Replay button on the dead letter](docs/dashboard.png)

## Run it

From a checkout, with no installation:

```bash
python3 -m relaybox --db .local/demo.sqlite3 demo
python3 -m relaybox --db .local/demo.sqlite3 serve --demo-secret
```

Open http://127.0.0.1:8787. The demo seeds one inbound and one outbound delivery in each state,
with synthetic payloads, and sends no request. `--demo-secret` uses a public key that is only for
synthetic data; for your own experiments, set `RELAYBOX_SECRET` to a random value of at least 16
bytes instead. The [guide](docs/GUIDE.md#run-the-demo) covers installing the CLI and the other
options.

## A tour of the API

### Receive

With the demo server running, send a signed delivery from another terminal:

```bash
python3 examples/send_webhook.py
```

`POST /webhooks` answers `202` for a new delivery, `200` for an identical repeat and `409` when
the same delivery ID arrives with different bytes. The HMAC covers the timestamp, the delivery ID
and the raw body, and a timestamp more than five minutes from the server's clock is refused.
`python3 -m relaybox sign ID FILE --demo-secret` prints the headers for any JSON file.

### Process

A worker claims ready jobs under a lease and acknowledges or fails each one:

```bash
python3 -m relaybox --db .local/demo.sqlite3 work --receipts .local/receipts --max-jobs 10
```

`work` writes each payload to a local JSON receipt file and acknowledges the job. A failure
schedules a retry with exponential backoff, and the last failure moves the job to `dead_letter`,
where `replay JOB_ID` can requeue it. `claim`, `renew`, `ack` and `fail` do the same steps by hand
with the lease token that `claim` returns.

### Deliver

Start the example receiver in one terminal. It checks every signature and answers 503 to the
first attempt:

```bash
python3 examples/receive_webhooks.py --demo-secret --fail-first 1
```

In another terminal, subscribe it, publish an event and run the dispatcher:

```bash
python3 -m relaybox --db .local/out.sqlite3 subscription add http://127.0.0.1:8788/hook --event-type example.created --demo-secret
python3 -m relaybox --db .local/out.sqlite3 publish example.created examples/payload.json
python3 -m relaybox --db .local/out.sqlite3 deliver --max-deliveries 10 --wait 15
```

The first attempt gets 503 with `Retry-After: 1`, and the retry succeeds with the same
`webhook-id`. `relay` turns inbound jobs into outbound events, and `replay-delivery ID` requeues
an outbound dead letter.

### Observe and recover

`GET /api/metrics` (and the dashboard) separates queued work that is
ready from delayed retries, and active leases from expired ones, without changing any job.
`backup` takes a consistent online snapshot, `verify-backup` checks one, and `restore` copies one
to a new path.

[docs/GUIDE.md](docs/GUIDE.md) walks through each workflow with its options and expected output,
including the library API. [docs/API.md](docs/API.md) is the full HTTP, CLI and library contract.

## Design notes

- Each job and outbound delivery has one owner at a time. A claim runs in a `BEGIN IMMEDIATE`
  transaction and issues a random 256-bit lease token, and acknowledging, failing or renewing
  requires the current, unexpired token. A worker whose lease expired cannot finish the job.
- Inbound jobs and outbound deliveries share this lease state machine (`store.LeaseQueue`), so
  claims, completion, dead letters and replay follow the same rules; only the retry schedule
  differs.
- Delivery is at least once. A crashed worker's job is claimed again after its lease expires, and
  an outbound retry keeps its `webhook-id`, so receivers can drop repeats.
- Outbound retries use full jitter: a random wait between 0 and the exponential backoff, raised
  to honor `Retry-After`. 2xx succeeds; 408, 429, 5xx and network errors retry; 410 disables the
  subscription; other 4xx and every 3xx dead-letter at once.
- Each outbound attempt is bounded by a wall-clock deadline and, by default, a 4 KiB response
  read, and redirects are never followed.
- Secret rotation signs with every active key during a grace period, so receivers can switch keys
  without failed deliveries.
- Backups use SQLite's online backup API inside a pinned read transaction, so a snapshot is
  consistent while writers keep committing.

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) explains each mechanism and its trade-offs.

## Tests

```bash
python3 -m unittest discover -v
```

The tests run real SQLite connections, live HTTP sockets and CLI subprocesses.
[`tests/test_store.py`](tests/test_store.py) races workers across connections and checks that an
expired worker is rejected. [`tests/test_http.py`](tests/test_http.py) sends malformed framing,
oversized bodies, stale timestamps and hostile content. [`tests/test_dispatch.py`](tests/test_dispatch.py)
runs the dispatcher against loopback receivers that fail, stall, trickle bytes, refuse
connections, redirect and send malformed status lines, and kills a dispatcher mid-request.
[`tests/test_standard_webhooks.py`](tests/test_standard_webhooks.py) checks signing against the
specification's published test vector.

CI runs the suite on Python 3.11 and 3.13, then installs the package and runs the installed CLI,
worker, backup and outbound checks from outside the checkout. A separate job drives the installed
dashboard in Chromium. [docs/VERIFICATION.md](docs/VERIFICATION.md) lists every check and how to
run it locally.

## Limits

- It serves only 127.0.0.1 over plain HTTP, with one shared HMAC key for inbound requests and no
  user accounts.
- Delivery is at least once, so handlers and receivers must make their own side effects
  idempotent.
- `deliver` is a bounded command rather than a background service, with no per-endpoint rate
  limit or circuit breaker.
- Signing secrets and payloads are stored unencrypted in the database and its backups.
- It is a demonstration with synthetic data: there is no capacity benchmark or independent
  security audit.

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#limits-in-detail) lists every limit in detail.

## More

[Architecture](docs/ARCHITECTURE.md) · [Guide](docs/GUIDE.md) · [API](docs/API.md) ·
[Verification](docs/VERIFICATION.md) · [Measurement](docs/PERFORMANCE.md) ·
[中文指南](docs/GUIDE.zh-CN.md) · [Security](SECURITY.md) · [Contributing](CONTRIBUTING.md)

Built with AI coding assistants. Licensed under [MIT](LICENSE).
