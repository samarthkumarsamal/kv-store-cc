# Assignment 1: Persistent Single-Server Key-Value Store

**Student:** Samarth Kumar Samal  
**Course:** CSCI 5144: Cloud Computing

## Design and justification

The server implements GET, PUT, and DEL as HTTP GET, PUT, and DELETE. POST is
also accepted for the assignment's write/read benchmark. Writes contain
`{"value": ...}`; successful reads return that structure. The default address
is `http://127.0.0.1:8080`. Python provides readable application code, JSON,
SQLite, and a standard-library fallback.

The optimized engine requires Python 3.11+ with the pinned packages in
`requirements-fast.txt`. Uvicorn/httptools handle HTTP and uvloop handles Linux
I/O; Windows uses asyncio. Persistent HTTP/1.1 connections avoid repeated
connection setup. `--engine threaded` retains the original threaded engine.

SQLite uses WAL and `synchronous=FULL`. A dedicated writer serializes queued
mutations, grouping up to 64 already-queued operations into a transaction
without an added delay. Success follows commit. Concurrent operations on one
key have an execution order; no fixed winner is promised. Parameterized SQL
separates data from statements.

A bounded LRU cache holds immutable encoded read responses, avoiding repeated
JSON conversion. Strict coherence detects external SQLite commits through
`data_version`. An identical cached, committed value can satisfy a repeated
PUT without another disk write; this optimization is not fresh-write throughput.

## Errors, logging and validation

Invalid input returns 400, missing keys return 404, and storage failures return
500. Write bodies require JSON with a `value` field, valid framing, and at most
1 MiB. Keys must be non-empty and at most 1,024 UTF-8 bytes. Non-finite JSON
values are rejected. Timestamped operation logs include methods, paths and
status codes, excluding stored values. Bounded background queues reduce logging
work in request handlers and flush during normal shutdown.

The storage smoke test passed CRUD and reopen-persistence checks using a
temporary database. The seven API tests cover CRUD, missing keys, JSON types,
invalid requests, independent concurrency, same-key writes, and concurrent
writes/deletes. They passed against the updated standalone async and threaded
engines in a separate Linux validation workspace, and against the updated
Docker cluster on the laptop. Container benchmarks and the 3,000-key GET-only
persistence check are reported in Assignments 2 and 3.

## Challenges, assumptions and improvements

Early connection closures caused incomplete HTTP responses. Correct framing
and persistent connections resolved the observed errors. Cache publication
must follow commit; failed writes must not publish uncommitted values. Each
storage process owns its database, and strict coherence is the default.

The application assumes trusted clients and local persistent storage. There
is no authentication, TLS, replication, or protection against disk loss.
Abrupt termination can lose queued log records. Future work includes access
control, replication, log retention, and larger repeated workload studies.

Run instructions are in [README](../README.md).