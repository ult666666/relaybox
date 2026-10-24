# Verification record

Verified locally on macOS arm64 on 23 October 2026. All records and requests used synthetic data. This record distinguishes observed behavior from unverified deployment or reliability claims.

## v0.4.1 readable oldest-ready wait

Started from integrated main `c0a7adeaa323a52a069f3a211da3aca47c041e15`. Installed 0.4.0 captures showed `4.6398069858551025 seconds since ready`, because the dashboard interpolated the exact API float. At runtime only the shipped `relaybox/assets/app.js` and the version string change; API, library, worker, recovery and metrics semantics are unchanged. The RED checkpoint `efe947c` added the browser check in `browser/` before any runtime change.

**RED.** With the released 0.4.0 wheel installed in `.local/queue-final-venv` (CPython 3.13.7), Node 24.18.0, Playwright 1.61.1 and Chromium 149.0.7827.55:

```bash
cd browser
npm ci --ignore-scripts
npm test -- "$PWD/../.local/queue-final-venv/bin/python"
```

Installed HTTP returned exactly `4.625` seconds at the supplied clock 1792785600.25 (2026-10-23 20:00:00.25 UTC). The page showed `4.625 seconds since ready`, the expected `About 4.6 seconds since ready` assertion failed with exit 1, and the fixture still shut down with exit 0. The 0.4.1 assertion in `examples/verify_install.py` also fails against that 0.4.0 environment.

**Change.** The page rounds the wait for display: seconds below 60, minutes below 3600, hours below 86400, days beyond, at most one decimal place, browser number formatting, singular/plural units and an `About` prefix. Null keeps `— No ready queued jobs`, exactly 0 shows `0 seconds since ready`, and values below 0.1 show `Less than 0.1 seconds since ready`. The unit is chosen before rounding, so 59.99 displays `About 60 seconds since ready`. A wait that is not null or a finite nonnegative number throws into the existing metrics error path, which clears the section (`Snapshot unavailable: invalid oldest ready wait. …`) instead of showing an invented time.

**GREEN.** The release-candidate wheel was built from a clean `git archive` of `148882e`, whose packaged files are identical to this record's commit, and installed into a new environment outside the checkout:

```bash
git archive 148882e | tar -x -C "$SRC"
/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13 -m venv "$ENV"
"$ENV/bin/python" -m pip wheel --no-deps --no-cache-dir --wheel-dir "$WHEEL" "$SRC"
"$ENV/bin/python" -m pip install --no-deps --no-index "$WHEEL/relaybox_inbox-0.4.1-py3-none-any.whl"
cd browser && npm ci --ignore-scripts && npx playwright install chromium && npm test -- "$ENV/bin/python"
```

The wheel was 33,975 bytes, SHA-256 `5b956e0053928813ba6f39117b1e7496791ec5460a5e1298b3db091f8ff5aca5`. All 12 packaged runtime files are byte-identical in the wheel, installed site-packages and source. METADATA reports 0.4.1 with no dependencies, and the served `app.js` SHA-256 `2c4df6856a546c8cb57841edc280379e1440f660512a410437aaf05228b2b0f7` equals source. The browser check passed (exit 0, about 3 seconds), as did five further consecutive runs; no fixture or Chromium process remained.

Actual installed evidence: exact API value `4.625` displayed as `About 4.6 seconds since ready`; after a supplied-clock advance and manual Refresh, the actual 104.625-second wait displayed as `About 1.7 minutes since ready`; counts, 2026 dates, empty inbox, state filter, plain-text hostile payload, keyboard focus, no page overflow at 1440/1280/390/320 px (the table keeps its own scroller), and a no-JavaScript static shell with no data. The synthetic screenshots were inspected visually.

Controlled probes run in the same browser and are labeled separately in the receipt. Substituted metrics responses checked 20 duration values (null, 0, 0.000001, 0.099999, 0.1, 0.96, 1, 1.04, 1.25, 4.6398…, 59.99, 60, 3599, 3600, 5400, 86399, 86400, 129600, 172800 and 253402300799 → `About 2,932,897 days since ready`), the longest text at 320 px, year-9999 dates, de-DE output (`About 4,6 seconds`, `About 2.932.897 days`), and negative, string, boolean, omitted and nonfinite (`1e999`) waits clearing only the metrics section. Injected 503s left the other section working. Actual responses were held to show that late metrics, list and detail responses cannot replace newer ones.

The checkpoint's late-response probes waited on `networkidle`, which resolves immediately on an already idle page, so they passed only when the browser happened to handle the held response first. They now wait until the page's own JSON read of the held URL settles, using a test-only counter installed for that phase. Mutations applied only to a separate scratch installation's `app.js` each failed: removing the metrics, list or detail generation guard (`'2' !== '4'`, `9 !== 4`, detail showing `ready-old`) and rendering an invalid wait as zero. A missing Chromium (`PLAYWRIGHT_BROWSERS_PATH` set to an empty directory), a nonexistent interpreter, an interpreter without RelayBox, and an injected failure while an actual response was held each exited 1 with a receipt, and every fixture server that started shut down with exit 0. One authoring mistake, an async helper returning a bare promise, hung a run until the 150-second bound; cleanup still closed the browser and fixture.

| Interpreter | Exact command (`RELAYBOX_BENCH_PYTHON="$ENV/bin/python"`) | Observed result |
|---|---|---|
| CPython 3.11.15 | `.local/python/cpython-3.11.15-macos-aarch64-none/bin/python3.11 -m unittest discover -v` | 147 passed, 0 skipped, 3.808 seconds |
| CPython 3.13.7 | `/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13 -m unittest discover -v` | 147 passed, 0 skipped, 4.482 seconds |
| CPython 3.15.0 | `/opt/homebrew/bin/python3.15 -m unittest discover -v` | 147 passed, 0 skipped, 4.630 seconds |

The 147 tests are the 135 runtime tests plus 12 measurement-tool tests; both installed-interpreter tests executed against the 0.4.1 environment. No Python test was added because the change is browser rendering, covered above. Logs had no ResourceWarning or DeprecationWarning. The source-checkout `examples/verify_dashboard.mjs`, with its two exact-wait expectations updated, also passed. The installed checks passed with the same environment:

```json
{"installed_cli": "ok", "demo_states": 4, "dashboard_assets": 3, "ingest_statuses": [202, 200], "source_directory_import": false, "version": "0.4.1", "metrics_http": true, "asset_bytes_equal": true}
{"installed_worker": "ok", "ingest_statuses": [202, 200], "summary": {"claimed": 1, "succeeded": 1, "failed": 0, "lost_leases": 0}, "receipt_files": 1, "final_state": "succeeded", "outside_checkout": true}
{"installed_backup": "ok", "version": "0.4.1", "snapshot_jobs": 1, "verified_sha256": true, "rejected_wal_unchanged": true, "all_columns_preserved": true, "duplicate_created": false, "summary": {"claimed": 1, "succeeded": 1, "failed": 0, "lost_leases": 0}, "receipt_reused_without_rewrite": true, "original_state": "leased", "restored_state": "succeeded", "outside_checkout": true}
```

CI now has a `browser` job: 15-minute limit, workflow-level read-only `contents` permission, checkout without persisted credentials, Node 24, `npm ci --ignore-scripts`, `npx playwright install --with-deps chromium`, the built wheel installed outside the checkout, a mandatory Chromium run, and upload of only the synthetic receipt and four screenshots. It reuses this workflow's checkout v7.0.1 and setup-python v7.0.0 pins. setup-node v6.5.0 and upload-artifact v4.6.2 reuse SHAs already pinned in the owner's other repositories, and both resolve to those official tags. Replaying the job's commands locally on the clean export (macOS, without `--with-deps`) passed and produced the five uploaded files. **The hosted job has not run yet; its result is unverified.**

`store.py`, measurement source, `docs/PERFORMANCE.md`, raw receipts, tracked screenshots and the 0.4.0 release evidence are unchanged; no measurement was rerun for documentation. These are focused Chromium checks, not an accessibility, security, performance or cross-browser audit. Fresh wall-clock demo screenshots, hosted CI, review and release remain separate.

## v0.4.0 queue readiness and expiry snapshot

Started from clean released main `dcf8cd27d6d1a12945af95ec909c8acee4562e0f`. The 0.4.0 suite contains **135 tests**: 6 authentication, 36 storage/state-machine, 7 queue metrics, 20 HTTP, 16 CLI, 30 worker/receipt and 20 backup/recovery tests. All preceding release evidence is retained below.

| Interpreter | Exact command | Observed result |
|---|---|---|
| CPython 3.11.15 | `.local/python/cpython-3.11.15-macos-aarch64-none/bin/python3.11 -m unittest discover -v` | 135 passed, 4.326 seconds |
| CPython 3.13.7 | `python3.13 -m unittest discover -v` | 135 passed, 5.070 seconds |
| CPython 3.15.0 | `python3 -m unittest discover -v` | 135 passed, 5.250 seconds |

Seven initial metrics tests failed on the missing method, and three new HTTP tests failed on the absent route. Fixed-clock oracles then verified empty explicit nulls, exact ready/expiry boundaries, all four persisted states, mixed stored attempt policies, fractional readiness wait, one clock sample, inclusive timestamp limits and controlled invalid clock values. Complete persisted rows are equal before/after diagnostics, including after clock advancement alone changes derived values. An actual SQLite authorizer rejects private/payload column reads during the metrics call, proving the aggregate does not select those fields.

A real reader is paused during aggregate execution using trace/progress callbacks. A second connection commits 200 queued rows to succeeded while the reader is active; the returned object retains 200 queued-ready jobs and their original 100-second wait together, while the next call reports 200 succeeded and no ready wait. No sleeps, substituted SQL results or stats/list mixing are used. The first fixture attempted to install the progress handler inside a trace callback, which did not affect that already-running statement on the local runtime; installing it at connection creation and arming it from SELECT trace produced the required actual-read synchronization. The implementation uses one aggregate SELECT and one validated supplied-clock sample.

HTTP tests exercise the exact metrics object and private-value omission, Host/Origin/Fetch Metadata, duplicate framing headers, query/body rejection, same-origin success, explicit storage failure and nonfinite-clock rejection. Existing `/api/stats` and process-liveness `/healthz` shapes remain unchanged. All final suite logs are free of ResourceWarning/FAILED/ERROR entries.

The extended installed check first failed against the old 0.3.0 wheel's version assertion. A fresh final 0.4.0 wheel was then built and installed after the keyboard-focus fix:

```bash
python3.13 -m venv .local/queue-final-venv
.local/queue-final-venv/bin/python -m pip wheel --no-deps --no-cache-dir --wheel-dir .local/queue-final-wheel .
.local/queue-final-venv/bin/python -m pip install --no-deps --no-index .local/queue-final-wheel/relaybox_inbox-0.4.0-py3-none-any.whl
.local/queue-final-venv/bin/python examples/verify_install.py
.local/queue-final-venv/bin/python examples/verify_worker.py
.local/queue-final-venv/bin/python examples/verify_backup.py
```

The final wheel was 33,288 bytes, SHA-256 `e6f4e4ea2b2bb53073b956c33b95237fa473c57a5053d1049d873314ee19b93a`. Every command exited 0. Isolated `python -I` child processes outside the checkout assert module/metadata version 0.4.0 in site-packages, read the three installed asset resources, and compare actual HTTP response bytes with those installed bytes. Actual HTTP `/api/metrics` is checked for all fields, persisted counts, readiness/active partitions and nullable times. Existing signed ingestion/duplicate, receipt worker and backup/restore drills pass on the same installed wheel.

```json
{"installed_cli":"ok","demo_states":4,"dashboard_assets":3,"ingest_statuses":[202,200],"source_directory_import":false,"version":"0.4.0","metrics_http":true,"asset_bytes_equal":true}
```

Optional development browser verification used the existing external Playwright **1.61.1** module and its Chromium; no browser/npm runtime dependency was added:

```bash
node examples/verify_dashboard.mjs /path/to/node_modules/playwright python3.13
```

The script starts a real loopback HTTP server with a private SQLite fixture and logical clock, controlled only over test-process stdin. The initial red run failed on the absent operations summary. Green checks cover mixed exact values, explicit empty/null descriptions, manual refresh after clock-only/state changes, persisted leased counts after expiry, independent failed metrics and failed list responses, stale metrics ordering, stale detail ordering, safe hostile text, keyboard interaction and 390px mobile width. Failures and response delays are deliberately simulated at the browser transport boundary; fixture data/metrics/HTML come from the actual local application. A later self-review assertion found that disabling Refresh dropped keyboard focus; keeping it focusable with an accessible busy indicator fixed that failing browser test. Temporarily removing the metrics generation guard caused the held-old-response check to fail with ready count 2 instead of 4; the guard was restored before final verification.

```json
{"chromium":"ok","real_http":true,"logical_clock":true,"mixed_empty_refresh":true,"independent_failures":true,"stale_metrics_and_detail":true,"keyboard_mobile":true,"injected_elements":0,"page_errors":0}
```

Desktop and mobile screenshots were saved under ignored `.local/queue-dashboard-desktop.png` and `.local/queue-dashboard-mobile.png` and opened for visual inspection. The summary stays compact in four desktop/two mobile columns, labels wrap, no whole-document horizontal overflow was observed, and the delivery table retains its scroll area. This is focused Chromium/keyboard coverage, not a full accessibility or cross-browser certification. The browser script is optional dev tooling; hosted browser CI is not claimed. Existing CI runs the extended installed HTTP/asset check through its current Python job.

Metrics are local read-only diagnostics, not a monitoring service, automatic reconciler, throughput/latency estimate, completion assurance or service-health certificate. Counts/ages/times share one SQL snapshot, but the dashboard's latest-100 list is a separate request and can reflect a different commit. One validated clock sample is not a distributed time guarantee. Wait means time since readiness, not creation. Nulls remain explicit. SQL can scan every job despite fixed result size and no selected payload columns. Normal Inbox construction still initializes/configures a database; only the metrics operation is read-only. No new state transition, schema change, background refresh, runtime dependency, remote collector, public backend, eight-hour run, hosted CI result or remote publication is claimed. Parent independent review/publication remains separate.

## v0.3.0 consistent snapshots and new-path recovery

The v0.3.0 suite contains **125 tests**: 6 authentication, 36 storage/state-machine, 17 live HTTP, 16 CLI subprocess, 30 worker/receipt, and 20 backup/recovery tests. The v0.1.0 and v0.2.0 records/tags are retained as historical evidence. The branch started from clean integrated main `3b4df17d8c8f5252206bb839398170c4e5ea49fd`.

| Interpreter | Exact command | Observed result |
|---|---|---|
| CPython 3.11.15 | `.local/python/cpython-3.11.15-macos-aarch64-none/bin/python3.11 -m unittest discover -v` | 125 passed, 3.690 seconds |
| CPython 3.13.7 | `python3.13 -m unittest discover -v` | 125 passed, 4.519 seconds |
| CPython 3.15.0 | `python3 -m unittest discover -v` | 125 passed, 4.676 seconds |

All final runs completed without resource warnings. Tests first failed on the absent backup module (11 failures), then missing CLI commands. A later failing schema test found that case-folding declarations accepted changed CHECK string literals; validation now preserves their case. A failing diagnostic test found that the symlink rejection was being rewrapped by the broad path-value handler; the explicit rejection is now preserved. The first full test run passed but revealed unclosed test-fixture connections on Python 3.13; fixtures now explicitly close them. A CHECK-corruption fixture initially expected an integrity diagnostic, but SQLite's read-only integrity scan on the local build omitted that particular CHECK result; the explicit known-state validation still rejected it. The assertion now accepts either rejection, and a separate damaged b-tree test exercises real corruption rejection.

Independent review of `bc2e7cf` found that rejecting a normal closed WAL inbox during verification created persistent source WAL/SHM files: SQLite `mode=ro` can write sidecars in a writable directory. A new library regression failed on both extra entries, and the updated installed recovery drill failed against the prior installed wheel. The verifier now reads only the fixed 100-byte SQLite header before connecting and requires rollback-format read/write versions, rejecting WAL before SQLite can create files. It does not delete source sidecars or change the shared live-backup reader. The regression repeats rejection and compares directory entries plus all pre-existing file bytes; the installed CLI check performs the same negative-input check before its successful recovery drill. Quiescent trusted-input and no-sidecar limits still apply. The preflight follows the official [SQLite header format](https://www.sqlite.org/fileformat.html#database_header) and [read-only WAL rules](https://www.sqlite.org/wal.html#read_only_databases).

Real WAL tests keep a separate writer open, commit a field into WAL, then leave a later update uncommitted. The published standalone snapshot contains the committed field and excludes the uncommitted value, while source bytes remain unchanged. Another test commits a new row from the actual SQLite backup progress callback during a multi-step copy: the source ends with 81 jobs while the pinned snapshot consistently has the original 80. No synchronization sleeps are used. Temporarily removing the source read transaction made this test fail on inconsistent snapshot metadata. Temporarily removing deadline cancellation from the progress callback made the cancellation test run 19 iterations instead of stopping after its first incomplete step. Both mutations were restored and the complete suites above passed.

Backup tests cover all four states, complete row equality, private token/worker/payload omission from metadata, attempts, per-job policy, replay counts, duplicate/conflict behavior, expired lease reclamation and stale-token rejection after restore. They reject missing, nonregular, empty, foreign, changed-schema, truncated and corrupt sources without source initialization or publication. Existing files/directories, self/hardlink/symlink targets, selected source symlinks, Unicode/spaced paths, trusted ancestor aliases, private POSIX mode, injected publication failure and a real exclusive-create destination collision are exercised. Late review added failing regressions for existing destination SQLite sidecars: the copier now rejects `-wal`, `-shm` and `-journal` entries without deleting them, and verification rejects sidecars before SQLite can attempt recovery.

A real second connection holds a rollback-journal `BEGIN EXCLUSIVE`; a 0.1-second request raises the deadline error and publishes nothing. Actual backup progress is also intercepted solely to advance an injected monotonic clock, proving cancellation before a second copy step and cleanup of private files. Invalid/nonfinite timeout types and the 1 GiB sparse-file bound are checked. The deadline is cooperative; these tests do not claim interruption of a blocked OS I/O syscall or a machine-wide real-time limit.

A fresh final Python 3.13 environment built and installed the final 0.3.0 wheel:

```bash
python3.13 -m venv .local/backup-wal-repair-venv
.local/backup-wal-repair-venv/bin/python -m pip wheel --no-deps --no-cache-dir --wheel-dir .local/backup-wal-repair-wheel .
.local/backup-wal-repair-venv/bin/python -m pip install --no-deps --no-index .local/backup-wal-repair-wheel/relaybox_inbox-0.3.0-py3-none-any.whl
.local/backup-wal-repair-venv/bin/python examples/verify_install.py
.local/backup-wal-repair-venv/bin/python examples/verify_worker.py
.local/backup-wal-repair-venv/bin/python examples/verify_backup.py
```

The final wheel was 30,993 bytes with SHA-256 `43b8fbe143a61d6a5adfee68a631abf43afeb4d316ff42e41042448e4d4f68cc`. All three installed checks passed. Their child commands run in temporary directories outside the checkout; the recovery setup uses isolated `python -I`, checks `site-packages`, and asserts both installed metadata and `relaybox.__version__` equal 0.3.0. An earlier run against the old `.venv` failed on its missing worker module, demonstrating that the installed check does not silently import current source.

The new drill creates a synthetic receipt before acknowledgment, leaves the original worker stopped, backs up and verifies the expired lease, restores into a nonexistent Unicode/spaced path, checks every database column, and tests same-byte deduplication plus changed-byte conflict. Installed `work` reclaims the lease at attempt 2, reuses the retained receipt without rewriting bytes or mtime, and acknowledges only the restored database. A repeat restore is rejected without overwrite. Verification does not create the unrelated `--db` destination. Observed output:

```json
{"installed_backup":"ok","version":"0.3.0","snapshot_jobs":1,"verified_sha256":true,"rejected_wal_unchanged":true,"all_columns_preserved":true,"duplicate_created":false,"summary":{"claimed":1,"succeeded":1,"failed":0,"lost_leases":0},"receipt_reused_without_rewrite":true,"original_state":"leased","restored_state":"succeeded","outside_checkout":true}
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
