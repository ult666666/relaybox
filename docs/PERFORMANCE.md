# Measure the diagnostic tradeoff locally

`Inbox.metrics()` uses one aggregate SELECT for a consistent snapshot. Its work
can grow with the number of persisted rows, including completed jobs. The latest
100-job list is a separate query with a different purpose. The source-only
`examples/benchmark_metrics.py` tool makes those two costs inspectable using the
actual installed library. It changes no application behavior and is not included
in the wheel.

Install the trusted package into a virtual environment, then invoke the script
with that environment's interpreter and Python isolated mode. On Unix, from a
checkout:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python -I examples/benchmark_metrics.py --sizes 100 1000 10000 --samples 30 --warmups 5
```

The command emits a JSON receipt to standard output. To save it, redirect stdout
to a **new** file of your choice; shell redirection can overwrite an existing
file. The script itself accepts no database or output path. On Windows, use
`.venv\Scripts\python.exe` for the interpreter. This script's execution on
Windows has not been verified.

Python isolated mode prevents the working directory and Python path overrides
from selecting the source package. The tool additionally checks the imported
store against installed distribution metadata and the interpreter prefix. Run
trusted installed code: these checks are not a sandbox or authentication of a
package. A checkout import fails with exit 2 instead of being presented as an
installed measurement. The receipt records the actual version and store-file
SHA-256, along with the measurement-script hash. Those hashes identify bytes;
they are not authorship or supply-chain attestations.

## Fixed workload and independent checks

Each size is a multiple of ten. A repeated, explicitly synthetic ten-row pattern
has four queued, three leased, two succeeded and one dead-letter job. At the
fixed logical time 1000, two queued rows are ready and two are delayed; one lease
is active, one expired and reclaimable, and one expired at its attempt limit.
The oldest ready wait is 100 seconds, next retry 1040 and next active expiry 1010.
These are fixture values, not elapsed measurements.

The public `Inbox` constructor creates the real schema. Controlled bulk SQL then
populates only a new private temporary database using the checked-in profile.
Seeding is excluded from timed calls. This is **not an ingestion benchmark**.
Before and after timing, the tool independently checks all metric counts and
times, plus the latest-list count, IDs, order, states and readiness/lease fields.
A logical dump hash covers the schema and every persisted column before and
after the measurement. No rows, payloads, worker tokens or database files appear
in the receipt; temporary state is removed on ordinary completion or failure.

For each size, the script warms and measures `metrics()` first, then
`list_jobs(limit=100)`. Every timed call includes the real method's connection,
PRAGMA setup, SQL query and Python result materialization. Both methods use
separate connections. Seeding, correctness checks, logical hashing and page-size
inspection are excluded. No HTTP server, concurrent writer, signing, worker,
disk-capacity test or durable-write latency is measured.

The receipt includes all raw integer `perf_counter_ns` durations, nearest-rank
p50/p95 and maximum, Python/SQLite/OS/machine/CPU-count metadata, clock resolution,
database page bytes and input/profile hashes. For 20 sorted samples, p95 is the
19th sample. Quantiles use no interpolation. Nanosecond units do not imply
nanosecond accuracy. Five samples in CI are a correctness smoke; they are not a
stable performance estimate.

## Bounds and interpretation

Choose one to four unique sizes from 10 through 200,000, all multiples of ten;
5–200 samples and 1–20 warmups. A cooperative ten-minute deadline is checked
between operations and before publishing success. It cannot interrupt an
in-progress SQLite/OS call, seeding or dump hashing. Interrupted process kills
may leave its private temporary directory. No artificial sleeps are used.

The process uses a shared developer/CI machine without controlled background
activity. Warm-cache, fixed-order measurements can vary with the filesystem,
SQLite build, Python version, page cache, hardware and other work. They do not
establish throughput, a production capacity limit, provider latency, an SLO or
the behavior of a write-heavy concurrent service. Rerun on your own environment
and workload before drawing an operational conclusion. An unexpectedly flat or
noisy result is a measurement to inspect, not a performance promise.

The behavior tests check hand-derived quantiles and the fixed profile; they do
not assert timing thresholds. To include the real outside-checkout installed
smoke in the unit suite, set `RELAYBOX_BENCH_PYTHON` to an installed interpreter's
absolute path. CI also invokes the actual script with its installed package on
Python 3.11 and 3.13.

## One dated local observation

A run on **2026-10-23 at 22:38 UTC**, using the released 0.4.0 wheel on macOS
arm64, Python 3.13.7 and SQLite 3.50.4, produced the following. It used 5 warmups
and 30 samples per operation. Other development work was running on the same
machine; background activity, hardware frequency and cache residency were not
controlled. These are observed call durations, not comparative benchmark scores.

| Persisted rows | Metrics p50 (ms) | Metrics p95 (ms) | Latest 100 p50 (ms) |
| ---: | ---: | ---: | ---: |
| 100 | 0.447 | 0.580 | 0.589 |
| 1,000 | 0.698 | 0.876 | 0.636 |
| 10,000 | 3.509 | 4.056 | 0.581 |
| 100,000 | 31.475 | 34.692 | 0.983 |

The [complete raw receipt](measurements/macos-arm64-20261023.json) retains all
samples and exact hashes. All four independent profile checks and logical
nonmutation checks passed. The larger aggregate call durations are consistent
with the disclosed row scan; this single run does not isolate a causal factor or
justify a universal scaling model. The list retrieves at most 100 summaries and
is not a replacement for the whole-inbox metrics contract. Rerun before choosing
an operational refresh interval or changing the database design.
