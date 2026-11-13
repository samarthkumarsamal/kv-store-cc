# Assignment 1: Persistent Single-Server Key-Value Store

**Author:** Samarth Kumar Samal  
**Course:** CSCI 5144: Cloud Computing

## 1. Design Decisions and Justification

The application provides a standalone HTTP API for GET, PUT and DEL, implemented
as GET, PUT and DELETE. POST also stores values for compatibility with the
reference benchmark. Writes accept `{"value": ...}` and successful reads return
the same structure. The default endpoint is `http://127.0.0.1:8080`.

Python was selected for readable application code and its JSON, threading and
SQLite libraries. The optimized engine uses Python 3.11+, Uvicorn/httptools and
Linux uvloop; Windows uses asyncio. Dependencies are pinned in
`requirements-fast.txt`. A standard-library threaded fallback remains available.

SQLite WAL and `synchronous=FULL` provide local persistence. A dedicated writer
orders mutations and groups up to 64 already-queued operations per transaction
without adding a batching delay. Changed writes succeed after commit. Concurrent
same-key requests have an execution order; the final writer is not predetermined.

A bounded cache stores immutable encoded read responses. Strict coherence checks
SQLite `data_version` for external commits. Identical cached, committed values
can satisfy repeated writes without another disk write.

## 2. Error Handling, Logging and Validation

Invalid input returns 400, missing keys 404, and storage failures 500. Write
bodies require valid JSON containing `value`, a JSON content type, and valid
framing. Bodies are limited to 1 MiB; keys to 1,024 UTF-8 bytes. Parameterized
SQL handles keys and values. Timestamped logs record methods, paths and status
codes, excluding stored values. Bounded background queues flush on normal shutdown.

The storage smoke test passed create, read, update, delete and reopen-persistence
checks. Seven API tests cover CRUD, missing keys, JSON types, invalid requests,
independent concurrency, same-key writes and concurrent PUT/DELETE. These passed
against standalone async and threaded engines in Linux validation and against
the containerized cluster on the laptop. Persistence and performance evidence
for the containerized system appear in Reports 2 and 3.

## 3. Challenges and Resolutions

Early connection closures caused incomplete responses. Explicit response framing
and persistent HTTP connections resolved the observed failures. Queued mutations
and post-commit cache publication address competing writes and prevent successful
responses from exposing uncommitted updates.

## 4. Assumptions and Future Improvements

The service assumes trusted clients and local storage, with one process owning
each node's database. Authentication, TLS and replication remain future work.
Abrupt termination can lose queued log records; local durability does not protect
against disk loss. Further work includes log retention and longer workload studies.

Execution and API examples: [Project README](../README.md).
