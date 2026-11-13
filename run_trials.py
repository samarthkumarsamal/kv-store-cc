"""Run repeated benchmarks against the active Docker benchmark cluster."""

import argparse
import csv
import json
from pathlib import Path
import statistics
import subprocess

from kv_bench import Benchmark, percentile


def verify_cluster(expected_nodes):
    result = subprocess.run(
        [
            "docker", "ps",
            "--filter", "label=com.docker.compose.project=kv-benchmark",
            "--filter", "label=com.docker.compose.service=router",
            "--format", "{{.ID}}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    containers = result.stdout.split()

    if len(containers) != 1:
        raise RuntimeError("Expected exactly one running benchmark router.")

    result = subprocess.run(
        ["docker", "inspect", containers[0]],
        check=True,
        capture_output=True,
        text=True,
    )
    command = json.loads(result.stdout)[0]["Config"]["Cmd"]
    configured = [
        command[index + 1]
        for index, argument in enumerate(command)
        if argument == "--node"
    ]
    expected = [
        f"node{index}=node{index}:8080"
        for index in range(1, expected_nodes + 1)
    ]

    if sorted(configured) != sorted(expected):
        raise RuntimeError(
            f"Router membership is {configured}; expected {expected}."
        )


def summarize(result, nodes, trial, threads, keys):
    latencies = sorted(result["latencies"])
    successful = len(latencies)

    if (
        result["error"]
        or result["mismatch"]
        or successful != keys
    ):
        raise RuntimeError(
            f"Trial failed: phase={result['phase']}, "
            f"ok={successful}, errors={result['error']}, "
            f"mismatches={result['mismatch']}"
        )

    return {
        "nodes": nodes,
        "trial": trial,
        "threads": threads,
        "keys": keys,
        "phase": result["phase"],
        "successful": successful,
        "errors": result["error"],
        "mismatches": result["mismatch"],
        "duration_s": result["elapsed"],
        "throughput_ops_s": successful / result["elapsed"],
        "average_ms": 1000 * statistics.mean(latencies),
        "p50_ms": 1000 * percentile(latencies, 0.50),
        "p95_ms": 1000 * percentile(latencies, 0.95),
        "p99_ms": 1000 * percentile(latencies, 0.99),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=int, choices=(1, 2, 3), required=True)
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--threads", type=int, default=3)
    parser.add_argument("--keys", type=int, default=300)
    args = parser.parse_args()

    if min(args.trials, args.threads, args.keys) < 1:
        parser.error("Trials, threads, and keys must be positive.")

    verify_cluster(args.nodes)

    output = Path("reports") / f"benchmark_{args.nodes}_nodes.csv"
    output.parent.mkdir(parents=True, exist_ok=True)

    benchmark = Benchmark(
        "http://127.0.0.1:8080",
        args.threads,
        args.keys,
        10,
    )

    # Exclusive creation prevents accidental overwriting of measurements.
    with output.open("x", newline="", encoding="utf-8") as file:
        writer = None
        rows = []

        for trial in range(args.trials + 1):
            label = "Warm-up" if trial == 0 else f"Trial {trial}"
            print(f"\n{label}: {args.nodes} node(s)")

            for phase in ("set", "get"):
                result = benchmark.run_phase(phase)
                row = summarize(
                    result,
                    args.nodes,
                    trial,
                    args.threads,
                    args.keys,
                )

                print(
                    f"  {phase.upper()}: "
                    f"{row['throughput_ops_s']:.1f} ops/s, "
                    f"average={row['average_ms']:.2f} ms, "
                    f"p99={row['p99_ms']:.2f} ms"
                )

                if trial == 0:
                    continue

                if writer is None:
                    writer = csv.DictWriter(file, fieldnames=list(row))
                    writer.writeheader()

                writer.writerow(row)
                file.flush()
                rows.append(row)

    print("\nMeasured-trial medians:")
    for phase in ("set", "get"):
        selected = [row for row in rows if row["phase"] == phase]
        throughput = statistics.median(
            row["throughput_ops_s"] for row in selected
        )
        average = statistics.median(
            row["average_ms"] for row in selected
        )
        p99 = statistics.median(row["p99_ms"] for row in selected)

        print(
            f"  {phase.upper()}: {throughput:.1f} ops/s, "
            f"average={average:.2f} ms, p99={p99:.2f} ms"
        )

    print(f"\nSaved: {output}")


if __name__ == "__main__":
    main()