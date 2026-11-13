# Assignment 3: Consistent-Hash Distributed Key-Value Store

**Author:** Samarth Kumar Samal  
**Course:** CSCI 5144: Cloud Computing  
**Evaluated application revision:** `c19354d`  
**Repository:** https://github.com/samarthkumarsamal/kv-store-cc

## 1. Objective and Scope

Extend the persistent single-server KV service to distribute requests across
one to three storage nodes using consistent hashing. Containerize the services,
verify routing and data correctness, and evaluate throughput and latency as the
node count changes.

The implementation supports fixed-membership operation and planned maintenance
migration. Online reconfiguration, replication and automatic storage-node
failover are outside the implemented scope. General migration into previously
used destinations still requires stronger state reconciliation.

## 2. Implementation Process and Architecture

Development proceeded through five stages:

1. Implement the HTTP API, concurrent mutation handling and SQLite persistence.
2. Package the service with Docker, health checks and persistent named volumes.
3. Implement an immutable consistent-hash ring and an HTTP routing service.
4. Add asynchronous HTTP, persistent upstream connections, bounded response
   caching and grouped durable commits.
5. Validate key placement, demonstrate controlled migration, and measure
   one-/two-/three-node configurations.

Clients communicate with the router on port 8080. GET, POST/PUT and DELETE are
forwarded to the selected storage node. Each node owns an independent SQLite
database and Docker volume. The main `kv-cluster` project runs three nodes;
`kv-benchmark` supplies separate one-/two-/three-node configurations and volumes.

The ring uses SHA-256 and 150 virtual positions per stable node name. A key
belongs to the first clockwise position at or after its hash, wrapping at the
end. Sorting node identities makes routing independent of configuration order.
Virtual nodes improve balance without guaranteeing identical shard sizes.

The current async engine uses Uvicorn, httptools and Linux uvloop. Upstream
HTTP/1.1 connections are reused through bounded pools. Each node uses SQLite
WAL/FULL synchronization, a dedicated writer and a bounded encoded-response
cache with strict `data_version` coherence. Changed writes are acknowledged
after commit. Identical cached, committed values can satisfy repeated writes
without another database write. Timestamped operation logging remains enabled.

## 3. Functional and Routing Validation

| Check | Evidence |
| --- | --- |
| API behavior and concurrency | Seven integration tests passed against the updated Docker cluster, including same-key writes and concurrent PUT/DELETE. |
| Hash-ring behavior | Nine tests passed for deterministic routing, node ordering, additions/removals, invalid inputs and Unicode keys. |
| Placement | The 300-key check returned all expected values with no unexpected copies. |
| Persistence | GET-only verification after container recreation returned all 3,000 existing values with zero errors/mismatches. |
| Current Linux node-count study | All 9,000 measured operations succeeded; SQLite contents and ownership were independently checked after every trial. |

Recorded placement of the default 300 benchmark keys:

| Node | Correct Keys | Share |
| --- | ---: | ---: |
| node1 | 78 | 26.00% |
| node2 | 115 | 38.33% |
| node3 | 107 | 35.67% |

For a separate 10,000-key routing demonstration, the three-node shares were
32.02%, 34.57% and 33.41%. Adding node3 changed ownership for 33.41% of keys,
all moving to node3. This demonstrates ownership changes rather than physical
record relocation.

## 4. Measurement Methodology

Two node-count studies are reported separately:

| Setting | Intermediate Implementation | Current Async Implementation |
| --- | --- | --- |
| Environment | Windows client and Docker Desktop/WSL2 | Independent Linux validation workspace |
| Deployment | Containerized router and storage nodes | Native processes using loopback HTTP |
| Resources | Docker reported 8 CPUs and 7.529 GiB memory | Two-CPU quota and 8 GiB memory, shared by client and services |
| Runtime | Intermediate threaded optimization | CPython 3.12.14; Uvicorn 0.52.1, httptools 0.9.0, uvloop 0.23.0 |
| Workload | Three client threads, 300 keys per phase | Same bundled client, three threads, 300 keys per phase |
| Repetitions | One warm-up plus five measured trials per node count | One warm-up plus five measured trials per node count |

Each trial performs SET followed by GET/value verification. Current-runtime
tests use fresh temporary databases for each node-count configuration, one
router worker, 150 virtual nodes per store, SQLite FULL durability, strict
coherence and enabled logging. Independent SQL checks verify values and
placement after each trial. No laptop database or volume is used by the
independent validation study.

Tables report medians of per-trial throughput and latency measurements. A p99
entry is the median of five per-trial p99 values, not a pooled percentile.
Measured SET phases repeat the warm-up dataset; they do not represent
fresh-insert capacity. The bundled `http.client` benchmark differs from the
provided `requests.Session` example, so results are identified by client.

## 5. Performance Results

### 5.1 Intermediate Runtime: Windows/Docker Study

These medians match the committed node-count CSVs and generated summary:

| Nodes | SET ops/s | GET ops/s | SET Avg ms | GET Avg ms | SET p99 ms | GET p99 ms |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 1,084.3 | 1,208.0 | 2.75 | 2.46 | 4.98 | 5.86 |
| 2 | 976.0 | 1,226.7 | 3.06 | 2.43 | 10.18 | 5.76 |
| 3 | 1,118.5 | 1,281.3 | 2.66 | 2.33 | 5.90 | 4.86 |

![Intermediate threaded runtime: throughput](throughput.png)

![Intermediate threaded runtime: average and per-trial p99 latency](latency.png)

These figures describe the intermediate runtime. From one to three nodes,
SET throughput increased approximately 3.2% and GET approximately 6.1%, with
a SET dip and higher SET tail latency at two nodes.

### 5.2 Current Async Runtime: Controlled Linux Study

| Nodes | SET ops/s | GET ops/s | SET Avg ms | GET Avg ms | SET p99 ms | GET p99 ms |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 4,070.6 | 5,086.3 | 0.730 | 0.583 | 1.433 | 1.189 |
| 2 | 4,890.2 | 5,160.0 | 0.607 | 0.575 | 1.338 | 1.089 |
| 3 | 4,761.7 | 5,754.5 | 0.622 | 0.515 | 1.204 | 1.074 |

From one to three nodes, median repeated-workload SET throughput increased
17.0% and GET increased 13.1%. SET peaked at two nodes and then declined
slightly at three, so the trend is not monotonic.

The current-version study completed all 9,000 measured operations with zero
errors and mismatches, plus 1,800 warm-up operations. Per-trial metrics are
included in Appendix A. These Linux results are separate from the Windows
figures above; their differences cannot be attributed solely to the runtime
change because the environments differ.

### 5.3 Additional Current-Runtime Write Study

A separate Linux batch used three nodes, three client threads, 3,000 keys per
phase and five measured trials after a warm-up. SQL state was independently
verified. Fresh tables or changed committed values were prepared before SET,
outside the timed phase, while strict coherence remained enabled.

| SET Workload | Median ops/s | Average ms | Per-Trial p99 ms |
| --- | ---: | ---: | ---: |
| Fresh inserts | 2,127.7 | 1.405 | 4.939 |
| Changed values | 2,158.4 | 1.385 | 3.162 |
| Identical repeated values | 4,412.5 | 0.675 | 1.440 |

This separates real insert/update work from identical-value optimizations.
Higher repeated-write throughput should not be presented as fresh disk-write
capacity. These rows are not part of the 300-key node-count comparison.

### 5.4 Router Worker Selection

In the laptop's separate-client Docker study, 20 GET runs per configuration
used 16 threads and 3,000 keys. Both configurations verified 60,000 reads
without errors or mismatches.

| Router Workers | Median ops/s | Median Average ms | Median Per-Run p99 ms |
| ---: | ---: | ---: | ---: |
| 1 | 4,645.0 | 3.43 | 10.97 |
| 2 | 4,525.8 | 3.52 | 12.41 |

Two workers did not demonstrate a benefit, so one remains the default. The
sessions occurred at different times and were not randomized or interleaved.

## 6. Analysis and Evaluation Limits

Both node-count studies show gains between one and three nodes under their
respective conditions, with non-monotonic SET results. Neither demonstrates
linear scaling. In the Linux study, the client and all services share a
two-CPU quota, and the small warmed dataset fits within one node's cache.
Client work, routing, logging, scheduling and storage compete for those resources.

These are short, sequentially ordered experiments, with five measured trials
per configuration and no confidence intervals. The individual causes of the
observed differences were not isolated. Larger datasets, longer trials,
randomized configuration order and independent server resources would provide
stronger evidence. No measurements from the professor's server or an official
leaderboard benchmark are claimed.

## 7. Membership Changes and Data Migration

Consistent hashing computes new ownership without moving stored values. The
maintenance procedure pauses the router and all other writers, exports a fresh
snapshot from current authoritative owners, starts destination nodes, imports
and verifies the values, and resumes with the target membership. The included
helpers target `kv-benchmark`.

A controlled one-to-three-node migration used a 300-value snapshot. It copied
78/115/107 values to the three owners and passed a subsequent GET-only check
with zero errors and mismatches.

The current importer upserts values while retaining existing destination rows.
It therefore does not reconcile a complete snapshot: stale or previously
deleted values may survive in reused destinations. An isolated audit confirmed
that importing an empty partition retained a pre-existing row. Complete-state
reconciliation with recoverable backups is required before claiming general
migration correctness for reused volumes.

Migration requires downtime and is not atomic across nodes. Old copies are
not replicas. The controlled demonstration establishes a limited maintenance
case, not online reconfiguration or automatic failure recovery.

## 8. Challenges and Resolutions

| Challenge | Resolution or Remaining Work |
| --- | --- |
| Dropped or incomplete HTTP responses | Explicit framing and persistent connection handling resolved the observed failures. |
| Competing mutations and durable acknowledgments | A dedicated writer orders mutations; cache publication follows commit. |
| Request and logging overhead | Async HTTP, bounded persistent pools, encoded-response caching and background logging reduce overhead. |
| Docker image/port conflicts and missing build inputs | Consistent image tags, separate deployment configurations and tracking every COPY input made builds reproducible. |
| Misleading performance comparisons | Workload, client, environment, cache/write history and implementation stage are identified separately. |
| Data relocation after ownership changes | Snapshot migration supports a controlled case; complete destination reconciliation remains unfinished. |

## 9. Conclusion and Future Work

The system implements persistent sharded storage, deterministic routing,
containerized deployment and measured one-/two-/three-node operation. Tests
verified concurrent API behavior, key placement and persistence. Current-runtime
Linux measurements demonstrate modest scaling within the tested resource budget.

Future priorities are complete-state migration, replication and automatic
recovery, authentication/TLS, and broader workload studies. Fixed membership,
the router's single point of failure and missing storage replication limit
availability guarantees.

## Appendix A: Current-Runtime Per-Trial Results

All rows use three client threads, 300 keys per phase and one router worker.
Values are rounded for readability; Section 5.2 medians use the unrounded
measurements. The benchmark's successful-request latency statistics are used.

<details>
<summary>Expand the Linux validation trial metrics</summary>

```csv
nodes,trial,phase,successful,errors,mismatches,throughput_ops_s,average_ms,p99_ms
1,1,set,300,0,0,3891.078341,0.762650,2.529876
1,1,get,300,0,0,6116.749293,0.482426,0.802308
1,2,set,300,0,0,3533.646403,0.837258,1.644024
1,2,get,300,0,0,3818.824526,0.777305,4.050808
1,3,set,300,0,0,4820.378400,0.614742,1.419042
1,3,get,300,0,0,5086.344944,0.583422,1.188920
1,4,set,300,0,0,4298.502638,0.690209,1.157774
1,4,get,300,0,0,5394.222841,0.549439,1.011657
1,5,set,300,0,0,4070.574481,0.730479,1.433283
1,5,get,300,0,0,4827.093511,0.613529,1.796220
2,1,set,300,0,0,4170.372448,0.712866,1.379873
2,1,get,300,0,0,5710.863547,0.517768,1.088672
2,2,set,300,0,0,5256.091771,0.565063,1.471289
2,2,get,300,0,0,4591.530555,0.645842,1.499670
2,3,set,300,0,0,4543.903078,0.652801,1.109192
2,3,get,300,0,0,5159.951265,0.574851,0.982475
2,4,set,300,0,0,4890.233254,0.603985,1.338061
2,4,get,300,0,0,4412.296063,0.673708,1.356569
2,5,set,300,0,0,4896.783956,0.607106,1.171254
2,5,get,300,0,0,5679.456044,0.521674,0.960122
3,1,set,300,0,0,4424.674670,0.670902,1.192343
3,1,get,300,0,0,6011.845861,0.490948,0.849463
3,2,set,300,0,0,5693.538460,0.520884,0.886584
3,2,get,300,0,0,7060.492132,0.418786,0.878754
3,3,set,300,0,0,4761.656929,0.622395,1.405212
3,3,get,300,0,0,4522.603134,0.654106,1.073547
3,4,set,300,0,0,4923.223802,0.602321,1.287027
3,4,get,300,0,0,5754.488655,0.514686,3.759727
3,5,set,300,0,0,4688.842401,0.632958,1.204030
3,5,get,300,0,0,5721.956842,0.518117,1.288539
```

</details>

Run instructions: [Project README](../README.md). Historical CSVs, their
pre-optimization baselines, summaries and figures remain in this report folder.
