# Verification record

Verified locally on macOS arm64 on 23 October 2026. All records and requests used synthetic data. This record distinguishes observed behavior from unverified deployment or reliability claims.

## Automated suite

The final suite contains **52 tests**: 6 authentication checks, 21 storage/state-machine checks, 17 live HTTP boundary checks, and 8 CLI subprocess checks. Tests use actual SQLite files and independent connections; HTTP tests bind an ephemeral loopback port and send real HTTP/TCP requests.

| Interpreter | Exact command | Observed result |
|---|---|---|
| CPython 3.11.15 | `.local/python/cpython-3.11.15-macos-aarch64-none/bin/python3.11 -m unittest discover -v` | 52 passed, 1.859 seconds |
| CPython 3.13.7 | `python3.13 -m unittest discover -v` | 52 passed, 2.145 seconds |
| CPython 3.15.0 | `python3 -m unittest discover -v` | 52 passed, 2.169 seconds |

The repository-local Python 3.11 interpreter was downloaded solely for compatibility verification and is ignored by Git. It is not needed to run the project on another machine with Python 3.11+ already installed. Timing is local test duration, not a service benchmark.

Coverage includes exact-byte duplicate identity, conflict preservation, malformed/nonfinite/duplicate-key JSON, UTF-8 and surrogate rejection, a 65,536-byte accepted boundary, oversized rejection without persistence, stale/future MAC checks, delivery-ID substitution, expired token rejection before/after reclaim, wrong tokens, final lease exhaustion, retry delay/cap, replay, policy persistence across connections, concurrent claims and concurrent duplicate ingestion. Thirty independent claim calls contend for twenty jobs; every claimed ID and token is unique. Sixteen concurrent submissions of the same ID persist once.

HTTP tests also cover duplicate headers, missing/negative/ambiguous lengths, unsupported chunking/media/compression/Expect, timed-out partial bodies, Host/Origin/Fetch Metadata rejection, same-origin reads, query validation, unknown routes, unsupported methods, JSON errors, asset responses, and token omission. CLI tests cover demo, claim/ack/fail/replay, signing, missing secrets, invalid ports/IDs, JSON errors and exit codes.

## Test-first record and repairs

Authentication/storage tests were written before their modules and initially failed because the modules were absent. HTTP tests were likewise written before the server. CLI tests initially failed on the missing module entry point. After implementation, tests exposed and drove corrections to missing-authentication status handling and the demo's logical timing.

A later negative test reproduced oversized job IDs escaping as a SQLite `OverflowError`; validation now returns a JSON CLI error with exit 2. A real Python 3.11 suite run exposed a randomly minted URL-safe token beginning with `-`, which argparse interpreted as another option in the documented CLI form. A deterministic failing token-contract assertion preceded the fix: tokens now encode 256 random bits as 64 lowercase hex characters. The complete 52-test suite then passed on all three interpreters above. These findings are included rather than omitted from the record.

## Package and installed service

Executed:

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install --no-deps --no-cache-dir .
.venv/bin/relaybox --help
.venv/bin/python examples/verify_install.py
```

After the final source fix, the package was reinstalled using `--force-reinstall --no-deps --no-cache-dir .` and the installed smoke check was rerun. The wheel built and installed successfully. The smoke script changes the child process working directory away from the source tree, seeds a fresh database, starts the installed module on an ephemeral port, requests all three packaged dashboard assets, and sends the same signed delivery twice.

Observed output:

```json
{"installed_cli":"ok","demo_states":4,"dashboard_assets":3,"ingest_statuses":[202,200],"source_directory_import":false}
```

The dependency-free runtime and pinned setuptools build are defined by `pyproject.toml`. CI performs the suite and installed-service checks on Python 3.11 and 3.13, with read-only repository permissions and commit-pinned actions. Action references were resolved from the official [checkout](https://github.com/actions/checkout/releases) and [setup-python](https://github.com/actions/setup-python/releases) releases. Hosted CI is not claimed by this local handoff record; its actual status is available in the linked workflow badge after publication.

## Quickstart and browser verification

Executed the documented seed command:

```bash
python3 -m relaybox --db .local/demo.sqlite3 demo
python3 -m relaybox --db .local/demo.sqlite3 serve --demo-secret --port 8797
```

The seed reported exactly one `queued`, `leased`, `succeeded`, and `dead_letter` job. Port 8787 was already occupied by an unrelated local process, so the documented alternate-port option was used; that other process was not touched. The installed-service check independently verified real signed HTTP ingestion and duplicate behavior on a fresh ephemeral port. The fixed-port `examples/send_webhook.py` requires a free default port 8787 and was not run against the unrelated listener.

Playwright 1.61.1 with Chromium was available in the enclosing workspace and was used for a separate browser check; it is not an application dependency. Observed checks:

- Four initial rows and one count per state at a 1440-pixel viewport.
- State filter reduced the table to the expected dead letter.
- Selecting a delivery rendered its synthetic English/Mandarin JSON payload.
- A real signed POST containing `<img src=x onerror="window.pwned=1">` rendered as text; it created no image element and executed no script.
- At 390×844 pixels, the page had no horizontal document overflow and retained its labeled filter. The table's own scroll container remained available.
- Keyboard Tab moved from the State select to Refresh.
- A genuine zero-match state returned an empty table and explicit empty message.
- A deliberately aborted browser API request displayed the error state. This one transport failure was simulated; ingestion, filtering, detail and empty-state data came from the real service.
- No JavaScript page errors were observed.

The initial screenshot was visually inspected and saved at [dashboard.png](dashboard.png). The server was stopped after verification. Browser scripts/screenshots used for scratch checks and local databases remain ignored. This was a focused functional and visual review, not a full accessibility audit or cross-browser certification.

## Review boundaries

An implementation self-review checked transaction/fencing paths, input rejection, token exposure, packaged assets, unsafe DOM insertion, and documentation consistency. A separate parent review is expected before publication. No internet-facing deployment, load/capacity benchmark, disaster recovery drill, independent security audit, external webhook consumer, or real customer workflow was tested. The [architecture](ARCHITECTURE.md), [README limits](../README.md#limits-that-matter), and [security guidance](../SECURITY.md) describe those boundaries.
