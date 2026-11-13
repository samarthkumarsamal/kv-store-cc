# Persistent Python Key-Value Store

**Student:** Samarth Kumar Samal  
**Course:** CSCI 5144: Cloud Computing

A persistent HTTP key-value store developed across three assignments:
standalone storage, Docker packaging, and consistent-hash routing across
one to three storage nodes. All application source is Python.

## Requirements

- Docker Desktop with Linux containers and Docker Compose v2+.
- Python 3.11+ for the fast engine; local Windows testing used Python 3.11.6.
  Containers use `python:3.12-alpine`.
- `requirements-fast.txt` pins the fast engine's packages. Docker installs
  them automatically. Windows skips uvloop and uses asyncio.
- The standard-library fallback supports Python 3.10+ with `--engine threaded`.
- Matplotlib is needed only for generating graphs.

## Files to keep

| Purpose | Files |
| --- | --- |
| Required application | `server.py`, `storage.py`, `router.py`, `consistent_hash.py`, `fast_runtime.py`, `fast_http.py`, `access_logging.py`, **`runtime_logging.py`**, `requirements-fast.txt` |
| Docker deployment | `Dockerfile`, `Dockerfile.router`, `compose.yaml`, `.dockerignore`, `.gitignore` |
| Correctness checks | `test_api.py`, `test_consistent_hash.py`, `check_distribution.py` |
| Node-count benchmarks | `kv_bench.py`, `run_trials.py`, `plot_results.py`, `compose.bench.yaml`, `compose.two.yaml`, `compose.three.yaml` |
| Planned migration | `export_cluster.py`, `migrate_cluster.py` |
| Documentation/evidence | This README, the three assignment reports, benchmark CSVs, `benchmark_comparison.md`, and PNG graphs in `reports/` |
| Optional experiments | `bench_write_workloads.py`, `compose.workers.yaml`, SVG graph copies |

Both Dockerfiles copy `runtime_logging.py`; it must be committed even when
using the async engine. Keep virtual environments, caches, local databases/WAL
files, `.env` files, logs, migration snapshots and `optimization.patch` outside
the submission. Retain backups privately. Exclude `.git/` from a source ZIP;
it remains normal repository metadata. No Go files are required.

## Quick start: three storage nodes

Run from the repository root. Stop other applications publishing ports
8080-8083. If a benchmark cluster is active, stop it first:

```bash
docker compose -f compose.bench.yaml --profile two --profile three stop
docker compose build node1 router
docker compose -f compose.yaml up -d --wait --wait-timeout 120
docker compose -f compose.yaml ps
```

Use `compose.yaml` alone for the selected configuration: one router worker,
three SQLite nodes and a named data volume for each node.

| Component | Host URL |
| --- | --- |
| Router: normal client requests | http://127.0.0.1:8080 |
| node1: placement checks | http://127.0.0.1:8081 |
| node2: placement checks | http://127.0.0.1:8082 |
| node3: placement checks | http://127.0.0.1:8083 |

```bash
python test_consistent_hash.py
python test_api.py
python kv_bench.py
python check_distribution.py
```

The placement checker verifies the default 300 benchmark keys. Run SET first;
the checker expects fixed three-node ownership without stale copies from a
different membership in those volumes.

```bash
docker compose -f compose.yaml logs --tail 50
docker compose -f compose.yaml stop
```

Named volumes preserve data across container recreation. Keep the same
project/volume names. `docker compose down -v` removes the data volumes.

## Standalone Python and Docker modes

For a local server, activate a virtual environment and run:

```bash
python -m pip install -r requirements-fast.txt
python storage.py
python server.py --engine async --database data/kv.db
```

The default address is `http://127.0.0.1:8080`. Run `python test_api.py` in
another terminal. Use `--engine threaded` for the standard-library fallback.
For persistence, run `python kv_bench.py set`, restart with the same database
path, then run `python kv_bench.py get` without SET.

For the single-container assignment, stop other services on port 8080:

```bash
docker build -t kv-store:1.0 .
docker run -d --name kv-store --init -p 127.0.0.1:8080:8080 -v kv-data:/data kv-store:1.0
```

The database is `/data/kv.db` in the named volume. See
[Assignment 2](reports/assignment2.md) for the replacement/persistence test.

## API and implementation

| Method | Path | Body | Success |
| --- | --- | --- | --- |
| POST or PUT | `/{key}` | `{"value": ...}` | 200: stored |
| GET | `/{key}` | None | 200 with `{"value": ...}` |
| DELETE | `/{key}` | None | 200: deleted |

DELETE implements DEL. Values may be standard finite JSON values, including
null. Write bodies require JSON content type and valid Content-Length, and
are limited to 1 MiB. Keys must be non-empty and at most 1,024 UTF-8 bytes;
percent-encode reserved characters. Invalid requests return 400, missing keys
404, and storage errors 500. Router failures return 502; pool exhaustion 503.
The async engine returns 405 for unsupported methods. `/` returns 400.

- Uvicorn/httptools and Linux uvloop handle asynchronous HTTP. Client and
  upstream connections persist; upstream pools are bounded.
- SHA-256 and 150 virtual nodes per storage node determine consistent-hash
  ownership. Membership changes require planned data movement.
- SQLite WAL and `synchronous=FULL` remain enabled. A dedicated writer groups
  up to 64 already-queued mutations without adding a batching timer and
  acknowledges successful mutations after commit.
- A bounded LRU cache stores encoded GET responses. Its default payload/key
  budget is 64 MiB with at most 65,536 entries; object overhead is additional.
  Strict coherence detects external SQLite commits through `data_version`.
- An identical cached, committed value may satisfy a repeated PUT without
  another disk write. Measure this separately from inserts/changed values.
- Timestamped operation logging stays enabled. Bounded background queues
  flush on normal shutdown; abrupt termination can lose buffered log records.
  Values are excluded from operation logs. Explicit memory mode is volatile.

## Measurements and reproduction

```bash
python kv_bench.py --threads 3 --keys 3000
python bench_write_workloads.py --threads 3 --keys 3000
```

The normal benchmark retains `key_0` onward. The supplementary workload check
uses UUID-prefixed keys, verifies fresh/changed/repeated values and removes
its own keys. The repository client uses `http.client`; the instructor's
example uses `requests.Session`, so their timing and percentile calculations
can differ. These local measurements are separate from official grading.

The latest three-thread GET-only persistence check read all 3,000 values after
container recreation: 3,328.6 ops/s, 0.89 ms average, zero errors/mismatches.
In separate-client Docker tests with 16 threads, median GET throughput was
4,645.0 ops/s with one router worker and 4,525.8 with two. One worker remains
selected. Detailed workloads and limitations are in [Assignment 3](reports/assignment3.md).

`compose.workers.yaml` retains the optional two-worker experiment. Include it
only for that experiment; use `compose.yaml` alone for normal operation.

The existing node-count CSVs and graphs in `reports/` are **historical results
from an earlier implementation**, with three threads, 300 keys, one warm-up
and five measured trials. The final async version has not yet had the same
complete one-/two-/three-node experiment on this laptop. Preserve those files
as historical evidence rather than relabeling them as current measurements.

To collect a fresh node-count batch, stop the main cluster and run each
configuration separately:

```bash
docker compose -f compose.yaml stop
docker compose -f compose.bench.yaml up -d --wait
python run_trials.py --nodes 1
docker compose -f compose.bench.yaml stop

docker compose -f compose.bench.yaml -f compose.two.yaml --profile two up -d --wait
python run_trials.py --nodes 2
docker compose -f compose.bench.yaml -f compose.two.yaml --profile two stop

docker compose -f compose.bench.yaml -f compose.three.yaml --profile three up -d --wait
python run_trials.py --nodes 3
docker compose -f compose.bench.yaml -f compose.three.yaml --profile three stop
```

The runner checks membership and refuses to overwrite existing CSVs. Archive
the historical CSVs before collecting a new batch at those paths, and update
report labels accordingly. Benchmark membership switches write all benchmark
keys before reading; they are not live-data migration. Generate graphs with
`python -m pip install matplotlib`, followed by `python plot_results.py`.

## Migration and limits

The included export/import helpers target `kv-benchmark`. Pause its router and
all other writers, export a fresh snapshot of authoritative owners, start the
destination nodes, import, configure the target membership, and verify with
GET before new writes. The importer upserts and retains old rows; reused
destinations can retain stale/deleted values. The demonstrated controlled
migration does not establish complete state reconciliation or online scaling.

There is no replication, automatic storage-node failover, online migration,
authentication or TLS. Migration requires downtime and is not atomic across
nodes. Old copies are not replicas; the router is a single point of failure.
Local results do not guarantee a leaderboard rank or server speedup.

## Reports

- [Assignment 1: single-server design](reports/assignment1.md)
- [Assignment 2: Docker packaging](reports/assignment2.md)
- [Assignment 3: hashing, scaling and optimization](reports/assignment3.md)
- [Historical benchmark comparison](reports/benchmark_comparison.md)