"""Benchmark a running KV server.

Usage:
    python kv_bench.py
    python kv_bench.py set
    python kv_bench.py get

Run "set", restart the server, then run "get" to check durability.
"""

import argparse
import http.client
import json
import math
import queue
import threading
import time
from urllib.parse import urlsplit


class Benchmark:
    def __init__(self, base_url, threads, keys, timeout):
        parsed = urlsplit(base_url)

        if parsed.scheme != "http" or not parsed.hostname:
            raise ValueError("Use an HTTP URL, such as http://127.0.0.1:8080")
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("Base URL must not contain a path, query, or fragment.")

        self.host = parsed.hostname
        self.port = parsed.port or 80
        self.threads = threads
        self.keys = keys
        self.timeout = timeout
        self.problem_lock = threading.Lock()
        self.problems_logged = 0

    def log_problem(self, message):
        with self.problem_lock:
            self.problems_logged += 1
            if self.problems_logged <= 5:
                print(f"  {message}")
            elif self.problems_logged == 6:
                print("  Further problems suppressed; see final counts.")

    def run_phase(self, phase):
        tasks = queue.Queue()
        for index in range(self.keys):
            tasks.put((f"key_{index}", f"value_{index}"))

        latencies = []
        counts = {"error": 0, "mismatch": 0}
        stats_lock = threading.Lock()
        start_event = threading.Event()

        def worker():
            connection = http.client.HTTPConnection(
                self.host, self.port, timeout=self.timeout
            )

            try:
                start_event.wait()

                while True:
                    try:
                        key, expected = tasks.get_nowait()
                    except queue.Empty:
                        return

                    started = time.perf_counter()
                    outcome = "error"

                    try:
                        if phase == "set":
                            body = json.dumps({"value": expected})
                            connection.request(
                                "POST",
                                f"/{key}",
                                body=body,
                                headers={"Content-Type": "application/json"},
                            )
                        else:
                            connection.request("GET", f"/{key}")

                        response = connection.getresponse()
                        raw = response.read()

                        if not 200 <= response.status < 300:
                            raise RuntimeError(
                                f"HTTP {response.status}: "
                                f"{raw[:150].decode('utf-8', errors='replace')}"
                            )

                        if phase == "get":
                            payload = json.loads(raw)
                            if not isinstance(payload, dict):
                                raise ValueError("Expected a JSON object.")

                            if payload.get("value") != expected:
                                outcome = "mismatch"
                                self.log_problem(f"Value mismatch for {key}")
                            else:
                                outcome = "ok"
                        else:
                            outcome = "ok"

                    except Exception as error:
                        self.log_problem(f"{phase} {key}: {error}")
                        # Reconnect for the next operation after a failure.
                        connection.close()

                    elapsed = time.perf_counter() - started

                    with stats_lock:
                        if outcome == "ok":
                            latencies.append(elapsed)
                        else:
                            counts[outcome] += 1
            finally:
                connection.close()

        workers = [
            threading.Thread(target=worker)
            for _ in range(self.threads)
        ]

        for thread in workers:
            thread.start()

        started = time.perf_counter()
        start_event.set()

        for thread in workers:
            thread.join()

        elapsed = time.perf_counter() - started

        return {
            "phase": phase,
            "elapsed": elapsed,
            "latencies": latencies,
            **counts,
        }


def percentile(sorted_values, percentage):
    """Nearest-rank percentile."""
    index = max(0, math.ceil(percentage * len(sorted_values)) - 1)
    return sorted_values[index]


def report(result):
    latencies = sorted(result["latencies"])
    successful = len(latencies)
    elapsed = result["elapsed"]

    print(
        f"\n{result['phase'].upper()}: "
        f"ok={successful} "
        f"mismatch={result['mismatch']} "
        f"error={result['error']}"
    )
    print(f"  Duration:   {elapsed:.3f} s")
    print(f"  Throughput: {successful / elapsed:.1f} successful ops/s")

    if latencies:
        print(f"  Average:    {1000 * sum(latencies) / successful:.2f} ms")
        print(f"  p50:        {1000 * percentile(latencies, 0.50):.2f} ms")
        print(f"  p95:        {1000 * percentile(latencies, 0.95):.2f} ms")
        print(f"  p99:        {1000 * percentile(latencies, 0.99):.2f} ms")

    return successful


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phases", nargs="*", help="set, get, or both")
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--threads", type=int, default=3)
    parser.add_argument("--keys", type=int, default=300)
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args()

    phases = args.phases or ["set", "get"]

    if any(phase not in ("set", "get") for phase in phases):
        parser.error("Phases must be 'set' or 'get'.")
    if args.threads < 1 or args.keys < 1 or args.timeout <= 0:
        parser.error("Threads, keys, and timeout must be positive.")

    try:
        benchmark = Benchmark(
            args.url, args.threads, args.keys, args.timeout
        )
    except ValueError as error:
        parser.error(str(error))

    results = []
    for phase in phases:
        print(
            f"Running {phase}: {args.keys} keys, "
            f"{args.threads} threads, URL={args.url}"
        )
        results.append(benchmark.run_phase(phase))

    total_ok = sum(report(result) for result in results)
    total_time = sum(result["elapsed"] for result in results)

    print(
        f"\nTOTAL: {total_ok} successful operations in "
        f"{total_time:.3f} s "
        f"({total_ok / total_time:.1f} ops/s)"
    )
    print("Latency statistics include successful operations only.")

    if any(result["error"] or result["mismatch"] for result in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()