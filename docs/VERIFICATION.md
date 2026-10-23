# Verification record

Verified locally on macOS arm64 on 23 October 2026. All records and requests used synthetic data. This record distinguishes observed behavior from unverified deployment or reliability claims.

## v0.3.0 consistent snapshots and new-path recovery

The v0.3.0 suite contains **124 tests**: 6 authentication, 36 storage/state-machine, 17 live HTTP, 16 CLI subprocess, 30 worker/receipt, and 19 backup/recovery tests. The v0.1.0 and v0.2.0 records/tags are retained as historical evidence. The branch started from clean integrated main `3b4df17d8c8f5252206bb839398170c4e5ea49fd`.

| Interpreter | Exact command | Observed result |
|---|---|---|
| CPython 3.11.15 | `.local/python/cpython-3.11.15-macos-aarch64-none/bin/python3.11 -m unittest discover -v` | 124 passed, 3.898 seconds |
| CPython 3.13.7 | `python3.13 -m unittest discover -v` | 124 passed, 4.668 seconds |
| CPython 3.15.0 | `python3 -m unittest discover -v` | 124 passed, 4.860 seconds |

All final runs completed without resource warnings. Tests first failed on the absent backup module (11 failures), then missing CLI commands. A later failing schema test found that case-folding declarations accepted changed CHECK string literals; validation now preserves their case. A failing diagnostic test found that the symlink rejection was being rewrapped by the broad path-value handler; the explicit rejection is now preserved. The first full test run passed but revealed unclosed test-fixture connections on Python 3.13; fixtures now explicitly close them. A CHECK-corruption fixture initially expected an integrity diagnostic, but SQLite's read-only integrity scan on the local build omitted that particular CHECK result; the explicit known-state validation still rejected it. The assertion now accepts either rejection, and a separate damaged b-tree test exercises real corruption rejection.

Real WAL tests keep a separate writer open, commit a field into WAL, then leave a later update uncommitted. The published standalone snapshot contains the committed field and excludes the uncommitted value, while source bytes remain unchanged. Another test commits a new row from the actual SQLite backup progress callback during a multi-step copy: the source ends with 81 jobs while the pinned snapshot consistently has the original 80. No synchronization sleeps are used. Temporarily removing the source read transaction made this test fail on inconsistent snapshot metadata. Temporarily removing deadline cancellation from the progress callback made the cancellation test run 19 iterations instead of stopping after its first incomplete step. Both mutations were restored and the complete suites above passed.

Backup tests cover all four states, complete row equality, private token/worker/payload omission from metadata, attempts, per-job policy, replay counts, duplicate/conflict behavior, expired lease reclamation and stale-token rejection after restore. They reject missing, nonregular, empty, foreign, changed-schema, truncated and corrupt sources without source initialization or publication. Existing files/directories, self/hardlink/symlink targets, selected source symlinks, Unicode/spaced paths, trusted ancestor aliases, private POSIX mode, injected publication failure and a real exclusive-create destination collision are exercised. Late review added failing regressions for existing destination SQLite sidecars: the copier now rejects `-wal`, `-shm` and `-journal` entries without deleting them, and verification rejects sidecars before SQLite can attempt recovery.

A real second connection holds a rollback-journal `BEGIN EXCLUSIVE`; a 0.1-second request raises the deadline error and publishes nothing. Actual backup progress is also intercepted solely to advance an injected monotonic clock, proving cancellation before a second copy step and cleanup of private files. Invalid/nonfinite timeout types and the 1 GiB sparse-file bound are checked. The deadline is cooperative; these tests do not claim interruption of a blocked OS I/O syscall or a machine-wide real-time limit.

A fresh final Python 3.13 environment built and installed the final 0.3.0 wheel:

```bash
python3.13 -m venv .local/backup-final-venv
.local/backup-final-venv/bin/python -m pip wheel --no-deps --no-cache-dir --wheel-dir .local/backup-final-wheel .
.local/backup-final-venv/bin/python -m pip install --no-deps --no-index .local/backup-final-wheel/relaybox_inbox-0.3.0-py3-none-any.whl
.local/backup-final-venv/bin/python examples/verify_install.py
.local/backup-final-venv/bin/python examples/verify_worker.py
.local/backup-final-venv/bin/python examples/verify_backup.py
```

The final wheel was 30,768 bytes with SHA-256 `33cf66ce5cede188f953eaaaaaae558663f09371d32883618af7d8de25711c66`. All three installed checks passed. Their child commands run in temporary directories outside the checkout; the recovery setup uses isolated `python -I`, checks `site-packages`, and asserts both installed metadata and `relaybox.__version__` equal 0.3.0. An earlier run against the old `.venv` failed on its missing worker module, demonstrating that the installed check does not silently import current source.

The new drill creates a synthetic receipt before acknowledgment, leaves the original worker stopped, backs up and verifies the expired lease, restores into a nonexistent Unicode/spaced path, checks every database column, and tests same-byte deduplication plus changed-byte conflict. Installed `work` reclaims the lease at attempt 2, reuses the retained receipt without rewriting bytes or mtime, and acknowledges only the restored database. A repeat restore is rejected without overwrite. Verification does not create the unrelated `--db` destination. Observed output:

```json
{"installed_backup":"ok","version":"0.3.0","snapshot_jobs":1,"verified_sha256":true,"all_columns_preserved":true,"duplicate_created":false,"summary":{"claimed":1,"succeeded":1,"failed":0,"lost_leases":0},"receipt_reused_without_rewrite":true,"original_state":"leased","restored_state":"succeeded","outside_checkout":true}
```

The official [Python 3.11 backup API](https://docs.python.org/3.11/library/sqlite3.html#sqlite3.Connection.backup), [SQLite online backup API](https://www.sqlite.org/backup.html), and [SQLite journal/sidecar recovery](https://www.sqlite.org/tempfiles.html) contracts were read before implementation or the relevant sidecar repair. CI is configured to execute the new installed recovery check on Python 3.11/3.13; hosted CI/publication belongs to the parent review and is not claimed here.

Supported trust is operator-owned local schema-1 databases and trusted directories; the allowlist is not authentication or a hostile-SQLite-file sandbox. Verification accepts quiescent DELETE-journal snapshots only. No schema migration, arbitrary source SQL, extension loading or repair was added. The new 1 GiB cap applies to snapshot operations, not all inbox writes. New files request 0600, hard links are required, selected symlinks are refused and ancestor aliases are resolved. Windows ACLs, network filesystems, adversarial concurrent replacement, hard-kill cleanup and power-loss persistence remain unverified; directory metadata is not fsynced. A restored copy cannot fence original workers; stop them before switching. Receipt files are separate retained artifacts, and recovery may repeat effects after the snapshot. No cross-database exactly-once guarantee, new browser behavior, production recovery certification, or external integration was tested.

## v0.2.0 bounded worker and renewable lease

The v0.2.0 final suite contained **102 tests**: 6 authentication, 36 storage/state-machine, 17 live HTTP boundary, 13 CLI subprocess, and 30 worker/receipt tests. The original v0.1.0 evidence is retained below as historical evidence.

| Interpreter | Exact command | Observed result |
|---|---|---|
| CPython 3.11.15 | `.local/python/cpython-3.11.15-macos-aarch64-none/bin/python3.11 -m unittest discover -v` | 102 passed, 2.780 seconds |
| CPython 3.13.7 | `python3.13 -m unittest discover -v` | 102 passed, 3.555 seconds |
| CPython 3.15.0 | `python3 -m unittest discover -v` | 102 passed, 3.709 seconds |

Tests were written before the new interfaces; the first run failed on absent `Inbox.renew`, worker module, and CLI commands. Renewal tests cover exact expiry, wrong and stale tokens, never-shortening extension, unchanged ownership/attempts, validation, missing jobs, deterministic write-lock clock sampling, and concurrent renewal/completion. The existing trace-callback/event synchronization is reused without timing sleeps. A new out-of-range integer case exposed `math.isfinite` raising `OverflowError` before the bounds check; checking range first now returns the documented `ValidationError`.

Worker tests exercise successful ack, claim bounds, validation before claiming, empty/ready-empty stop, retry/dead-letter policy, explicit in-handler renewal, lost leases on ack and fail, real SQLite completion errors, bounded diagnostics, and KeyboardInterrupt/SystemExit recovery. A handler-dictionary mutation test was checked by temporarily using the modified ID/token for completion: both success/failure cases failed with `KeyError`, and both pass when the original claim capability is retained.

Independent review then reproduced a normal handler exception whose `__str__` raised another exception, preventing the promised failure transition. Six focused regressions cover retry plus remaining ready work, dead-letter policy, long Unicode type-name diagnostic bounds, lost-lease count exclusivity, storage-error propagation, and interrupts during formatting. Against the original formatter, five cases failed on the secondary formatting exception while the interrupt case already passed. A guard limited to ordinary diagnostic-rendering exceptions now supplies a type-based fallback before the common UTF-8/500-character bound. All six pass; KeyboardInterrupt/SystemExit and storage failures still propagate. A test fixture initially used a surrogate in a class name, which Python rejects before worker execution; it was corrected to a valid long Unicode name before rerunning the red/green cycle.

Receipt tests cover repeat after lease expiry/reclaim without rewriting, shared identity across databases, distinct IDs with identical bodies, exact-byte digest differences, hostile legal IDs, conflict/corruption/noncanonical contents, symlink and nonregular targets, private POSIX permissions, concurrent identical publication, and cleanup on publication failure. A focused failing compatibility test drove trusted ancestor canonicalization: normal macOS `/var` aliases work while the selected directory itself and final receipt entries still reject symlinks. An injected publication collision with no retained file now fails explicitly instead of reporting success without an effect. CLI subprocess tests exercise exits 0, 1 and 2; the lost-lease case injects only logical clock samples while retaining real CLI parsing, SQLite transitions, and receipt files.

The 0.2.0 wheel was built and installed into a fresh Python 3.13 virtual environment, then reinstalled after final source changes:

```bash
python3.13 -m venv .local/worker-venv
.local/worker-venv/bin/python -m pip wheel --no-deps --no-cache-dir --wheel-dir .local/worker-wheel .
.local/worker-venv/bin/python -m pip install --force-reinstall --no-deps --no-index .local/worker-wheel/relaybox_inbox-0.2.0-py3-none-any.whl
.local/worker-venv/bin/python examples/verify_install.py
.local/worker-venv/bin/python examples/verify_worker.py
```

Both checks run their child processes outside the checkout. The new worker check invokes the installed `relaybox` entry point, starts its server on an ephemeral loopback port, signs synthetic file bytes through the CLI, ingests them twice (202/200), processes a single receipt, verifies persisted `succeeded` state and token omission, and checks empty work and invalid configuration. The existing installed-service check confirms all three packaged dashboard assets and the original demo behavior. Installed package metadata and `relaybox.__version__` both report 0.2.0 from the virtual environment's site-packages.

After the diagnostic repair, a fresh wheel was built into `.local/worker-review-wheel/` and installed with `--no-deps --no-index` into a new `.local/worker-review-venv/`. Both installed checks passed again with that interpreter. An isolated `python -I` library probe also loaded the installed 0.2.0 package, raised the unprintable handler exception against a real temporary inbox, and observed `{claimed: 2, succeeded: 1, failed: 1, lost_leases: 0}`, a queued retry at 1002, and successful processing of the next ready delivery. This confirms the reviewed repair is present in the installed wheel.

Observed installed-worker output:

```json
{"installed_worker":"ok","ingest_statuses":[202,200],"summary":{"claimed":1,"succeeded":1,"failed":0,"lost_leases":0},"receipt_files":1,"final_state":"succeeded","outside_checkout":true}
```

CI now includes this bounded installed-worker journey on Python 3.11 and 3.13. This local record does not claim hosted CI or publication completed. There is no automatic lease-renewal thread or handler timeout; external effects still require caller idempotency. Receipt checks assume trusted local directories and hard-link support, and do not certify Windows ACLs, adversarial concurrent directory replacement, power-loss durability, or distributed exactly-once processing. The final link's directory metadata is not fsynced; a hard kill may leave temporary files. No additional browser interaction, load test or external integration was performed in this wave.

## v0.1.0 historical verification

The following sections preserve the original release evidence.

## Automated suite

The v0.1.0 final suite contained **59 tests**: 6 authentication checks, 28 storage/state-machine checks, 17 live HTTP boundary checks, and 8 CLI subprocess checks. Tests use actual SQLite files and independent connections; HTTP tests bind an ephemeral loopback port and send real HTTP/TCP requests.

| Interpreter | Exact command | Observed result |
|---|---|---|
| CPython 3.11.15 | `.local/python/cpython-3.11.15-macos-aarch64-none/bin/python3.11 -m unittest discover -v` | 59 passed, 1.705 seconds |
| CPython 3.13.7 | `python3.13 -m unittest discover -v` | 59 passed, 2.048 seconds |
| CPython 3.15.0 | `python3 -m unittest discover -v` | 59 passed, 2.084 seconds |

The repository-local Python 3.11 interpreter was downloaded solely for compatibility verification and is ignored by Git. It is not needed to run the project on another machine with Python 3.11+ already installed. Timing is local test duration, not a service benchmark.

Coverage includes exact-byte duplicate identity, conflict preservation, malformed/nonfinite/duplicate-key JSON, UTF-8 and surrogate rejection, a 65,536-byte accepted boundary, oversized rejection without persistence, stale/future MAC checks, delivery-ID substitution, expired token rejection before/after reclaim, wrong tokens, final lease exhaustion, retry delay/cap, replay, policy persistence across connections, concurrent claims and concurrent duplicate ingestion. Thirty independent claim calls contend for twenty jobs; every claimed ID and token is unique. Sixteen concurrent submissions of the same ID persist once.

HTTP tests also cover duplicate headers, missing/negative/ambiguous lengths, unsupported chunking/media/compression/Expect, timed-out partial bodies, Host/Origin/Fetch Metadata rejection, same-origin reads, query validation, unknown routes, unsupported methods, JSON errors, asset responses, and token omission. CLI tests cover demo, claim/ack/fail/replay, signing, missing secrets, invalid ports/IDs, JSON errors and exit codes.

## Test-first record and repairs

Authentication/storage tests were written before their modules and initially failed because the modules were absent. HTTP tests were likewise written before the server. CLI tests initially failed on the missing module entry point. After implementation, tests exposed and drove corrections to missing-authentication status handling and the demo's logical timing.

A later negative test reproduced oversized job IDs escaping as a SQLite `OverflowError`; validation now returns a JSON CLI error with exit 2. A real Python 3.11 suite run exposed a randomly minted URL-safe token beginning with `-`, which argparse interpreted as another option in the documented CLI form. A deterministic failing token-contract assertion preceded the fix: tokens now encode 256 random bits as 64 lowercase hex characters. The complete 52-test suite then passed on all three interpreters above. These findings are included rather than omitted from the record.

An independent review then identified stale time sampling before SQLite write-lock acquisition. A claim waiting behind another writer could receive an already expired lease; acknowledgment/failure could accept an expired lease using a pre-wait clock sample. Seven deterministic regression tests were written and all seven failed before the fix. A second real connection holds `BEGIN IMMEDIATE`; SQLite's trace callback signals that the worker is attempting its transaction; an injected clock advances before the blocker releases. No lock sleeps, mocked connection results, or substituted database state are used.

Time sampling was moved inside the acquired transaction for `claim`, `_complete` (ack/fail), `ingest`, and `replay`. The seven tests now pass, covering:

- `test_claim_lease_starts_after_waiting_for_write_lock`
- `test_ack_waiting_for_write_lock_cannot_complete_expired_lease`
- `test_fail_waiting_for_write_lock_cannot_modify_expired_lease`
- `test_failure_backoff_starts_after_waiting_for_write_lock`
- `test_retry_becomes_claimable_while_waiting_for_write_lock`
- `test_ingest_timestamps_follow_acquired_write_lock`
- `test_replay_timestamps_follow_acquired_write_lock`

The full suite was rerun on all three interpreters with the 59/59 results in the table. The package was then rebuilt/reinstalled and `examples/verify_install.py` passed again with the output below. HMAC keys and fencing-token behavior were unchanged by this repair. Lease times still use wall-clock values, so clock jumps remain a documented limitation.

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

An implementation self-review checked transaction/fencing paths, input rejection, token exposure, packaged assets, unsafe DOM insertion, and documentation consistency. An independent review identified the lock-contention timing bug documented above; the fix was verified by deterministic regression tests and the complete suite. Parent publication checks are separate. No internet-facing deployment, load/capacity benchmark, disaster recovery drill, independent security audit, external webhook consumer, or real customer workflow was tested. The [architecture](ARCHITECTURE.md), [README limits](../README.md#limits-that-matter), and [security guidance](../SECURITY.md) describe those boundaries.
