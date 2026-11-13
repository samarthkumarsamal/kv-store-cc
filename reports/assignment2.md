# Assignment 2: Dockerizing the Key-Value Store

**Student:** Samarth Kumar Samal  
**Course:** CSCI 5144: Cloud Computing

## Objective and implementation

Package the persistent Python KV server so that its runtime, dependencies,
startup command, port, and persistent data location are reproducible.

`Dockerfile` uses `python:3.12-alpine`, sets `/app` as its working directory,
and installs the pinned packages in `requirements-fast.txt`. The current fast
engine has external dependencies; the earlier standard-library-only description
does not describe this image. Linux uses uvloop; the application code is Python.

The image explicitly copies `server.py`, `storage.py`, `runtime_logging.py`,
`access_logging.py`, `fast_runtime.py`, `fast_http.py`, and `consistent_hash.py`.
All must be present in the submitted repository. An unprivileged `kvuser` runs
the application. `ENTRYPOINT` starts `python server.py`; the default command
binds to `0.0.0.0:8080` and selects `/data/kv.db`. `EXPOSE 8080` documents the
container port; `-p` publishes it to the host.

A named volume mounted at `/data` retains the SQLite database and WAL files
across container replacement. `.dockerignore` excludes environments, Git
metadata, databases, logs, reports and migration backups from the build context.
The health check accepts 200 or 404 for a probe GET. It checks responsiveness,
not full data integrity. `--init` forwards signals and reaps child processes.

## Build, run and reproduce persistence

Stop other services using host port 8080. Build and start:

```bash
docker build -t kv-store:1.0 .
docker run -d --name kv-store --init -p 127.0.0.1:8080:8080 -v kv-data:/data kv-store:1.0
```

After `docker ps` reports healthy:

```bash
python test_api.py
python kv_bench.py set
docker stop kv-store
docker rm kv-store
docker run -d --name kv-store --init -p 127.0.0.1:8080:8080 -v kv-data:/data kv-store:1.0
```

Wait for healthy status, then run `python kv_bench.py get` without SET.
Expected: 300 correct reads, zero errors and mismatches. Use the same named
volume. View logs with `docker logs kv-store`.

## Recorded evidence

The original standalone-container experiment used Windows, Docker Desktop/WSL2,
three threads, and 300 keys per phase:

| Historical phase | Successful | Throughput (ops/s) | Average (ms) | p99 (ms) |
| --- | ---: | ---: | ---: | ---: |
| SET | 300 | 31.7 | 93.88 | 239.29 |
| GET | 300 | 653.9 | 4.57 | 19.69 |
| GET after standalone-container replacement | 300 | 558.8 | 5.35 | 25.07 |

Every historical phase had zero errors/mismatches. These are earlier-version
observations, not performance measurements of the final optimized image.

For the updated implementation, both images built successfully on the laptop.
The three-node Compose deployment became healthy and passed all seven API
tests. After recreation with its existing named volumes, a GET-only run
verified 3,000 existing values with zero errors/mismatches: 3,328.6 ops/s,
0.89 ms average and 2.29 ms p99. This is current clustered-container persistence
evidence; it is not a new standalone-container benchmark. Assignment 3 records
the optimized cluster's other measurements.

## Challenges and limitations

An early recreation failed because `kv-store:1.0` was not available locally;
building the expected tag resolved it. Host-port conflicts were resolved by
stopping the competing standalone or Compose deployment. Source selection must
include every Docker COPY input, particularly `runtime_logging.py`.

Named volumes provide persistence across containers, not replication or disk
failure protection. SQLite WAL/FULL commits and operation logging remain
enabled. The base tag can change over time; pinning a tested digest and adding
CI image-build tests would strengthen reproducibility. Docker Desktop and
native Linux networking/storage can produce different benchmark results.