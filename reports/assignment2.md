# Assignment 2: Dockerizing the Key-Value Store

**Author:** Samarth Kumar Samal  
**Course:** CSCI 5144: Cloud Computing

## 1. Objective

Package the persistent KV server with its runtime, source files, dependencies,
port configuration and startup command so it can run consistently in a Linux
container. Preserve database contents across container replacement.

## 2. Implementation and Requirement Coverage

| Requirement | Implementation |
| --- | --- |
| Appropriate lightweight base image | `FROM python:3.12-alpine` provides Python on Alpine Linux. |
| Application files included | COPY instructions add the storage, server, runtime, hashing and logging modules under `/app`. |
| Dependencies installed | `requirements-fast.txt` is copied and installed with pip during the build. It pins Uvicorn, httptools, uvloop and supporting packages. |
| Service port exposed | `EXPOSE 8080` declares the container port; `-p 127.0.0.1:8080:8080` publishes it locally. |
| Automatic startup | `ENTRYPOINT ["python", "server.py"]`; CMD supplies `--host 0.0.0.0`, `--port 8080` and `--database /data/kv.db`. |

The storage image includes `server.py`, `storage.py`, `fast_runtime.py`,
`fast_http.py`, `consistent_hash.py`, `access_logging.py` and `runtime_logging.py`.
The latter is required by the Docker COPY instruction even when the async engine
is selected. The current image installs external dependencies; it is not limited
to the standard library.

An unprivileged `kvuser` runs the application. Environment settings disable
bytecode generation and enable unbuffered output. `.dockerignore` excludes local
environments, Git metadata, databases, logs, reports and migration backups.

A named volume mounted at `/data` retains the SQLite database and WAL files.
SQLite WAL and FULL synchronization remain enabled. A health check accepts 200
or 404 from a probe GET, confirming HTTP responsiveness. `--init` provides signal
forwarding and child-process reaping.

## 3. Build and Run Procedure

Run from the repository root with Docker Desktop/Linux containers available.
Use an available container name and stop other services publishing port 8080.

```bash
docker build -t kv-store:1.0 .
docker run -d --name kv-store --init -p 127.0.0.1:8080:8080 -v kv-data:/data kv-store:1.0
docker ps
```

Wait for healthy status, then validate the API and populate benchmark keys:

```bash
python test_api.py
python kv_bench.py set
docker logs kv-store
```

## 4. Persistence Verification Procedure

Replace the container while reusing the same named volume:

```bash
docker stop kv-store
docker rm kv-store
docker run -d --name kv-store --init -p 127.0.0.1:8080:8080 -v kv-data:/data kv-store:1.0
docker ps
```

After the replacement is healthy, verify existing values without another SET:

```bash
python kv_bench.py get
```

The default workload expects `GET: ok=300 mismatch=0 error=0`.

## 5. Observed Validation Results

| Check | Recorded Outcome |
| --- | --- |
| Updated storage and router image builds from the final repository | Both builds succeeded. |
| Updated three-node Compose deployment | All three storage nodes and the router became healthy. |
| Updated cluster API integration | All seven tests passed. |
| Updated cluster after volume-preserving container recreation | GET-only verification returned all 3,000 existing values, with zero errors or mismatches. |
| Earlier standalone-container replacement | All 300 benchmark values remained correct, with zero errors or mismatches. |

The 3,000-key result is current clustered-container evidence; the 300-key
standalone replacement result belongs to the earlier implementation. The
standalone procedure above is supplied for reproduction, without presenting a
new standalone-image performance result. Detailed throughput studies are in
[Report 3](assignment3.md).

## 6. Challenges, Assumptions and Improvements

An early launch failed because the expected image tag was unavailable locally;
building `kv-store:1.0` resolved it. Port conflicts were resolved by stopping the
competing deployment. Tracking every Docker COPY input, including
`runtime_logging.py`, makes a fresh checkout buildable.

Named volumes preserve data across containers but provide neither replication
nor disk-failure protection. The health probe is a responsiveness check, not a
full integrity test. Pinning the base image by digest and adding automated image
build/integration checks would strengthen reproducibility.
