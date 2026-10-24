# Queue observability implementation plan

> For agentic workers: use superpowers:executing-plans inline, with TDD and verification before completion. Parent dispatch explicitly forbids child agents and owns independent review/publication.

**Goal:** Explain idle local workers through one consistent read-only readiness/expiry snapshot and a compact manual-refresh dashboard summary.

**Architecture:** `Inbox.metrics()` samples a validated clock once and returns fixed-size SQL aggregates from one SELECT. `/api/metrics` exposes exactly that object through the existing HTTP boundary. The dashboard refreshes metrics and the bounded job list independently, clearing failed-section data and ignoring stale response generations.

**Tech stack:** Python 3.11+ standard library, unchanged SQLite schema 1, plain HTML/CSS/JavaScript; optional dev-only existing Playwright 1.61.1 and Chromium.

**Spec:** the parent-owned complete brief, `brief-queue-observability.md`, kept outside this repository.

## Global constraints

- Start clean main `dcf8cd27d6d1a12945af95ec909c8acee4562e0f`; own `feat/queue-observability` checkout plus report only; no remote writes or child agents.
- Keep schema 1, stdlib runtime, every worker/backup behavior and historical release evidence; target package 0.4.0.
- No diagnostics reconciliation, background refresh, monitoring service, health/throughput certification, payload enumeration or second CLI initializer.
- Clock accepts finite int/float Unix seconds 0–253402300799 inclusive (through year 9999), rejecting bool and other types. Times absent in the snapshot are JSON null, never invented zero.
- Full suite on 3.11/3.13/3.15 plus fresh installed HTTP/assets, worker, backup and actual Chromium checks. Parent performs independent review/CI/release.

## Review focus

- Exact `available_at == snapshot_at` and `lease_until == snapshot_at` boundaries; fixed expected literals in Task 1.
- A concurrent commit during SELECT cannot mix counts with ages/times; real trace/progress callbacks and second connection in Task 1.
- Clock advancement alone changes derived values without changing persisted rows; complete-row before/after in Task 1.
- Failed metrics must not look current, while failed list must not discard successful metrics; actual Chromium response failures in Task 2.
- Older refresh/detail results must not overwrite newer UI state; browser delayed-response gates and hostile plain-text detail in Task 2.

## Task 1: metrics and actual HTTP

Files: modify `relaybox/store.py`, `relaybox/server.py`; create `tests/test_metrics.py`; extend `tests/test_http.py`.

Interface: `Inbox.metrics() -> dict` with exactly `schema_version`, `snapshot_at`, four persisted `counts`, `queued_ready`, `queued_delayed`, `leases_active`, `leases_expired_reclaimable`, `leases_expired_exhausted`, `oldest_queued_ready_wait_seconds`, `next_retry_at`, `next_lease_expiry`. HTTP GET `/api/metrics` returns it directly, without query/body.

- [x] Write/run failing logical-clock tests for empty nulls, exact readiness/expiry, mixed stored retry policies, all states, secret omission, one clock sample, invalid clocks, unchanged complete rows and concurrent snapshot consistency.
- [x] Implement one SQL aggregate SELECT, fixed-sized result and existing controlled ValidationError. No state mutation, payload read or calls combining stats/list across commits.
- [x] Add/run failing actual HTTP metrics tests: exact object, no private fields, query/body rejection, Host/Origin/Fetch Metadata boundary, same-origin, storage error and unchanged stats/liveness contracts.
- [x] Implement route within existing boundary; run `python3.13 -m unittest discover -v` expecting all tests pass; commit feature/API.

## Task 2: compact operator summary and browser checks

Files: modify three `relaybox/assets/` files; create focused `tests/browser_fixture.py` and `examples/verify_dashboard.mjs`.

Consumes Task 1 metrics object. UI retains four persisted-state cards, labels leased totals as including expired records, and adds an accessible operations definition list with readiness/expiry partitions, wait seconds, local upcoming times and explicit missing-value descriptions. Snapshot and latest-100 list are labeled separate requests. Exhausted expired leases are described as waiting for reconciliation on claim.

- [x] Write/run real Chromium checks before UI implementation (existing module path supplied): mixed logical fixture expected values, empty nulls, manual refresh after clock/state change, invalidated failed metrics with usable list, fresh metrics with failed list, stale response ordering, plain-text hostile payload, no page errors, keyboard and 390px width.
- [x] Implement independent refresh sections using Promise.allSettled with generation guard, clear failed metrics/list data, preserve detail guard/textContent/manual control and usable keyboard focus.
- [x] Run Chromium checks plus full Python suite; inspect a screenshot locally; commit dashboard and focused reusable dev verification. No runtime browser dependency and no new browser CI job unless justified by available infrastructure.

## Task 3: fresh package and handoff

Files: modify version metadata/`__version__`, `examples/verify_install.py` and `examples/verify_backup.py` version expectation; update README, API, architecture, Chinese guide and verification record.

- [x] Extend installed check to require 0.4.0 metadata/module version, assert actual HTTP metrics and compare all packaged asset bytes through isolated outside-checkout installed resource reads. Run against prior install first and observe failure.
- [x] Set 0.4.0, run fresh wheel/install, all three installed scripts; run full Python suites on 3.11/3.13/3.15 and final Chromium check.
- [x] Document exact fields, predicates, null/time/range semantics, read-only/no-reconciliation behavior, local snapshot versus separate list, manual refresh/error policy, linear scan limits, constructor initialization and liveness scope.
- [x] Self-review exact diff/spec, `git diff --check`, record all red/green results/commands/commits/limits in owned report, commit and hand off clean branch for parent independent review. No eight-hour or hosted-CI/publication claim.
