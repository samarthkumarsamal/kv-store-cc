"""Check fresh, changed, and repeated writes using temporary keys."""

import argparse
import http.client
import json
import queue
import threading
import time
import uuid
from urllib.parse import urlsplit

from kv_bench import report


def run_phase(url, threads, timeout, method, items, label):
    parsed = urlsplit(url)
    tasks = queue.Queue()

    for item in items:
        tasks.put(item)

    latencies = []
    counts = {"error": 0, "mismatch": 0}
    lock = threading.Lock()
    ready = threading.Event()

    def worker():
        connection = http.client.HTTPConnection(
            parsed.hostname,
            parsed.port or 80,
            timeout=timeout,
        )

        try:
            ready.wait()

            while True:
                try:
                    key, expected = tasks.get_nowait()
                except queue.Empty:
                    return

                started = time.perf_counter()
                outcome = "error"
                problem = None

                try:
                    if method == "POST":
                        connection.request(
                            method,
                            "/" + key,
                            body=json.dumps({"value": expected}),
                            headers={
                                "Content-Type": "application/json"
                            },
                        )
                    else:
                        connection.request(method, "/" + key)

                    response = connection.getresponse()
                    raw = response.read()

                    if method == "DELETE" and response.status == 404:
                        outcome = "ok"

                    elif response.status != 200:
                        raise RuntimeError(
                            f"HTTP {response.status}: {raw[:150]!r}"
                        )

                    elif method == "GET":
                        payload = json.loads(raw)

                        if (
                            not isinstance(payload, dict)
                            or payload.get("value") != expected
                        ):
                            outcome = "mismatch"
                            problem = f"Value mismatch for {key}"
                        else:
                            outcome = "ok"

                    else:
                        outcome = "ok"

                except Exception as error:
                    problem = f"{key}: {error}"
                    connection.close()

                elapsed = time.perf_counter() - started

                with lock:
                    if outcome == "ok":
                        latencies.append(elapsed)
                    else:
                        counts[outcome] += 1

                        if counts["error"] + counts["mismatch"] <= 5:
                            print(f"  {label}: {problem}")

        finally:
            connection.close()

    workers = [
        threading.Thread(target=worker)
        for _ in range(threads)
    ]

    for thread in workers:
        thread.start()

    started = time.perf_counter()
    ready.set()

    for thread in workers:
        thread.join()

    return {
        "phase": label,
        "elapsed": time.perf_counter() - started,
        "latencies": latencies,
        **counts,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url", default="http://127.0.0.1:8080"
    )
    parser.add_argument("--threads", type=int, default=3)
    parser.add_argument("--keys", type=int, default=3000)
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args()

    try:
        parsed = urlsplit(args.url)
        port = parsed.port

        if (
            parsed.scheme != "http"
            or not parsed.hostname
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError(
                "Use an HTTP base URL without credentials, "
                "path, query, or fragment."
            )

        if port is not None and not 1 <= port <= 65535:
            raise ValueError("Invalid port.")

    except ValueError as error:
        parser.error(str(error))

    if (
        args.threads < 1
        or args.keys < 1
        or args.timeout <= 0
    ):
        parser.error(
            "Threads, keys, and timeout must be positive."
        )

    prefix = "writecheck_" + uuid.uuid4().hex + "_"

    initial = [
        (f"{prefix}{i}", f"initial_{i}")
        for i in range(args.keys)
    ]
    changed = [
        (key, f"changed_{i}")
        for i, (key, _) in enumerate(initial)
    ]

    print(
        f"URL={args.url}, threads={args.threads}, "
        f"keys={args.keys}"
    )
    print(f"Temporary namespace: {prefix}")

    failed = False

    try:
        for method, items, label in (
            ("POST", initial, "fresh_set"),
            ("GET", initial, "verify_fresh_get"),
            ("POST", changed, "changed_set"),
            ("GET", changed, "verify_changed_get"),
            ("POST", changed, "repeated_set"),
            ("GET", changed, "verify_repeated_get"),
        ):
            result = run_phase(
                args.url,
                args.threads,
                args.timeout,
                method,
                items,
                label,
            )
            report(result)

            if (
                result["error"]
                or result["mismatch"]
                or len(result["latencies"]) != args.keys
            ):
                failed = True
                break

    finally:
        cleanup = run_phase(
            args.url,
            args.threads,
            args.timeout,
            "DELETE",
            initial,
            "cleanup",
        )

        if (
            cleanup["error"]
            or cleanup["mismatch"]
            or len(cleanup["latencies"]) != args.keys
        ):
            failed = True
            print(
                "Cleanup incomplete; retain the namespace "
                "above to identify remaining test keys."
            )
        else:
            print(
                "Cleanup complete: temporary test keys removed."
            )

    if failed:
        raise SystemExit(1)

    print(
        "PASS: fresh, changed, and repeated writes verified."
    )


if __name__ == "__main__":
    main()