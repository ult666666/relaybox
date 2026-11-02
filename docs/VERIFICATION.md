# Verification

GitHub Actions runs the checks below on every push and pull request. The
[workflow page](https://github.com/ult666666/relaybox/actions/workflows/ci.yml)
shows the latest results. All data is synthetic, and every request goes to a
local loopback server.

## What CI runs

[`ci.yml`](../.github/workflows/ci.yml) has three jobs.

**`test (3.11)` and `test (3.13)`**, one per Python version:

1. The full `unittest` suite: authentication, storage and the job state machine,
   queue metrics, live HTTP requests, CLI subprocesses, the worker and receipts,
   backup and recovery, the measurement tool, Standard Webhooks signing and
   outbound delivery against live loopback receivers.
2. `pip install .`, then four scripts that run the installed package from a
   directory outside the checkout:
   - `examples/verify_install.py`: the installed CLI and demo, the packaged
     dashboard assets served over HTTP, `/api/metrics`, and signed ingestion
     (202, then 200 for the same delivery).
   - `examples/verify_worker.py`: signed ingestion, a receipt written by `work`,
     and the acknowledged final state.
   - `examples/verify_backup.py`: backup, verification and restore to a new path,
     then recovery of an expired lease that reuses its retained receipt.
   - `examples/verify_outbound.py`: the installed CLI subscribes two endpoints of
     `examples/receive_webhooks.py`, publishes one message and delivers it. One
     endpoint answers 503 once and then 204; the other answers 422, is
     dead-lettered and replayed. The receiver checks every signature with its own
     code, and all four requests carry the same `webhook-id`.
3. A small run of `examples/benchmark_metrics.py`, which checks the tool's results
   against an independent oracle. It has no timing threshold.
4. `relaybox --help`.

**Installed dashboard in Chromium** builds the wheel, installs it in a fresh
environment outside the checkout, installs the locked Playwright 1.61.1 and
Chromium, and runs the [browser check](../browser/README.md) against the installed
server. The job fails if Chromium is missing. It uploads the synthetic screenshots
and a JSON receipt as an artifact.

## Run the same checks locally

```bash
python3 -m unittest discover -v
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python examples/verify_install.py
.venv/bin/python examples/verify_worker.py
.venv/bin/python examples/verify_backup.py
.venv/bin/python examples/verify_outbound.py
```

Two measurement-tool tests in `tests/test_benchmark.py` run an installed
interpreter, so they are skipped unless `RELAYBOX_BENCH_PYTHON` points at one
(for example `.venv/bin/python`). The browser check needs Node 24; see
[`browser/README.md`](../browser/README.md).

## How the tests work

- Tests use real SQLite files and separate connections. Lock-contention tests
  hold a real write lock in a second connection and advance an injected clock,
  with no sleeps.
- Thirty concurrent claims on twenty jobs each get a unique job and token, and
  sixteen concurrent submissions of one delivery persist once.
- HTTP tests send real requests to a loopback port: malformed framing, size
  limits, stale or future timestamps, a signature reused under another delivery
  ID, cross-site Origin and Fetch Metadata headers, and hostile content.
- Backup tests write to a live WAL database during the copy, and check that the
  snapshot is consistent and leaves out uncommitted changes.
- The browser check holds back real responses to show that a late reply cannot
  overwrite a newer refresh.
- Outbound tests run the dispatcher against loopback receivers that answer every
  status class, stall past the deadline, send a valid response one byte at a time,
  advertise a 64 MiB body, close the connection, send malformed status lines and
  redirect to a second receiver (which must stay unused). A connection to a closed
  port covers refusals.
- Signing reproduces the specification's published test vector, and every test
  receiver verifies signatures with a separate implementation written from the
  specification text.
- A dispatcher subprocess is killed with SIGKILL while its receiver holds the
  request open. A second dispatcher then reclaims the expired lease and sends the
  same body with the same `webhook-id`. A second test expires a lease during a
  slow attempt and checks that the first dispatcher cannot record its late result.
- Backoff tests check 5,000 random policies against the full-jitter window, compare
  a real retry schedule with draws from an identically seeded generator, and run a
  Kolmogorov-Smirnov test of 20,000 delays against the uniform distribution at a
  fixed seed (the 1% critical value for that sample size is 0.0115).
- Schema tests build a version-1 file with the exact 0.4.1 statements, back it up,
  upgrade it in place, and back up the result.

## Not covered

- Internet-facing deployment, TLS, load or capacity testing, and an independent
  security audit. Outbound HTTPS is tested only up to a failed handshake against a
  plain-text listener; no test sends to a real remote host.
- Windows ACLs, network filesystems, adversarial concurrent file replacement and
  power loss.
- Browsers other than Chromium, and a full accessibility audit.

The [README limits](../README.md#limits), [architecture](ARCHITECTURE.md)
and [security guidance](../SECURITY.md) describe these boundaries.

## Bugs found by tests and review

New behavior in each version started as a failing test. Tests and code review
also found these bugs, which were reproduced with a failing test and then fixed:

- **0.1.0:** a claim, acknowledgment or failure that waited for SQLite's write
  lock used a clock reading from before the wait, so it could hand out or accept
  an expired lease. Time is now read inside the transaction. Separately, a random
  lease token that started with `-` was parsed as a CLI option; tokens are now 64
  hex characters.
- **0.2.0:** a handler exception whose message itself raised an error skipped the
  failure transition. The worker now falls back to the exception's type name.
- **0.3.0:** verifying a WAL-mode file could create `-wal` and `-shm` files next
  to it. Verification now reads the SQLite header first and rejects WAL files
  before opening them. Schema validation also accepted CHECK constraints whose
  text differed only in letter case.
- **0.4.0:** disabling the Refresh button while loading dropped keyboard focus. It
  now stays focusable and shows a busy state.
- **0.4.1:** the dashboard printed the raw wait, such as
  `4.6398069858551025 seconds since ready`. It now rounds for display, and the API
  keeps exact seconds. The browser check's late-response probes could also pass by
  chance; they now wait for the page's own request to finish.
- **0.5.0:** a local smoke run showed that `deliver --wait` exited 1 even when a
  retried delivery finally succeeded, because the exit code counted attempts. The
  summary now reports `awaiting_retry` per delivery, and the exit code uses it.
  The installed outbound check found two bugs in the example receiver while it was
  being written: it treated the same `webhook-id` at two endpoints as a duplicate
  (one message fans out with one ID), and a renamed variable shadowed its HMAC key,
  so every request failed (the dispatcher correctly retried those as dropped
  connections). Review found that `http://127.0.0.1:0/` fell back to port 80, that
  Python releases disagree on whether `::ffff:127.0.0.1` is loopback (3.12.3 says
  no; 3.11.17 and 3.13.17 say yes), and that
  the socket timeout and the deadline watchdog could both fire at the timeout, so
  the error text varied between runs. Each is now decided explicitly and tested;
  the trickle test's elapsed-time bound shows that the watchdog ends that attempt.
  A review after the merge found three more. A receiver's `Retry-After` above
  1,000,000,000 seconds, or a far-future date, raised out of `deliver`: the
  delivery stayed leased and the deliveries behind it were not attempted. Such
  values now count as one day. Replay kept a delivery's old HTTP status, although
  the API contract says replay clears it. The test that accepts the smallest
  backup deadline (0.1 s) ran a real backup against that deadline and failed once
  in 24 loaded runs on Python 3.11; it now freezes the deadline clock.

Release notes for each version are on the
[releases page](https://github.com/ult666666/relaybox/releases).
