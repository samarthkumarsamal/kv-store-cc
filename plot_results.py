"""Validate benchmark CSV files and generate comparison artifacts."""

import csv
from pathlib import Path
import statistics

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


REPORTS = Path("reports")
METRICS = ("throughput_ops_s", "average_ms", "p99_ms")


def load_results():
    summaries = []
    workload = None

    for nodes in (1, 2, 3):
        path = REPORTS / f"benchmark_{nodes}_nodes.csv"

        with path.open(newline="", encoding="utf-8-sig") as file:
            rows = list(csv.DictReader(file))

        expected = {
            (trial, phase)
            for trial in range(1, 6)
            for phase in ("set", "get")
        }
        seen = set()

        for row in rows:
            pair = (int(row["trial"]), row["phase"])

            if pair not in expected or pair in seen:
                raise ValueError(f"{path}: unexpected or duplicate trial.")

            seen.add(pair)

            if int(row["nodes"]) != nodes:
                raise ValueError(f"{path}: incorrect node count.")

            current_workload = (int(row["threads"]), int(row["keys"]))

            if workload is None:
                workload = current_workload
            elif current_workload != workload:
                raise ValueError("Workload differs between trials.")

            if (
                int(row["errors"]) != 0
                or int(row["mismatches"]) != 0
                or int(row["successful"]) != int(row["keys"])
            ):
                raise ValueError(f"{path}: unsuccessful benchmark trial.")

            for metric in METRICS:
                value = float(row[metric])
                if not 0 < value < float("inf"):
                    raise ValueError(f"{path}: invalid {metric}.")

        if seen != expected:
            raise ValueError(f"{path}: expected five complete trials.")

        for phase in ("set", "get"):
            selected = [row for row in rows if row["phase"] == phase]
            summary = {"nodes": nodes, "phase": phase}

            for metric in METRICS:
                summary[metric] = statistics.median(
                    float(row[metric]) for row in selected
                )

            summaries.append(summary)

    return summaries, workload


def main():
    summaries, workload = load_results()

    summary_path = REPORTS / "benchmark_summary.csv"

    with summary_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(summaries[0]))
        writer.writeheader()
        writer.writerows(summaries)

    table = [
        "# Benchmark Comparison",
        "",
        f"Workload: {workload[0]} threads and {workload[1]} keys per phase.",
        "One warm-up and five measured trials per configuration.",
        "",
        "| Nodes | Phase | Throughput (ops/s) | Average (ms) | p99 (ms) |",
        "| ---: | --- | ---: | ---: | ---: |",
    ]

    for row in summaries:
        table.append(
            f"| {row['nodes']} | {row['phase'].upper()} "
            f"| {row['throughput_ops_s']:.1f} "
            f"| {row['average_ms']:.2f} "
            f"| {row['p99_ms']:.2f} |"
        )

    table.extend([
        "",
        "Each metric is the median of five per-trial measurements.",
        "The p99 column is the median of per-trial p99 latencies,",
        "not a percentile calculated from pooled requests.",
        "",
        "![Throughput comparison](throughput.png)",
        "",
        "![Latency comparison](latency.png)",
        "",
    ])

    (REPORTS / "benchmark_comparison.md").write_text(
        "\n".join(table),
        encoding="utf-8",
    )

    for filename, panels in (
        (
            "throughput",
            [("throughput_ops_s", "Successful operations / second")],
        ),
        (
            "latency",
            [
                ("average_ms", "Average latency (ms)"),
                ("p99_ms", "Per-trial p99 latency (ms)"),
            ],
        ),
    ):
        fig, axes = plt.subplots(
            1, len(panels),
            figsize=(6 * len(panels), 4),
            squeeze=False,
        )

        for axis, (metric, label) in zip(axes[0], panels):
            for phase in ("set", "get"):
                selected = [
                    row for row in summaries if row["phase"] == phase
                ]
                axis.plot(
                    [row["nodes"] for row in selected],
                    [row[metric] for row in selected],
                    marker="o",
                    label=phase.upper(),
                )

            axis.set_xlabel("Number of KV nodes")
            axis.set_ylabel(label)
            axis.set_xticks([1, 2, 3])
            axis.set_ylim(bottom=0)
            axis.grid(alpha=0.3)
            axis.legend()

        fig.suptitle(
            "Median of five trials "
            f"({workload[0]} threads, {workload[1]} keys per phase)"
        )
        fig.tight_layout()
        fig.savefig(REPORTS / f"{filename}.png", dpi=200)
        fig.savefig(REPORTS / f"{filename}.svg")
        plt.close(fig)

    print("\n".join(table[:13]))
    print("\nSaved summary CSV, Markdown table, and PNG/SVG graphs in reports/.")


if __name__ == "__main__":
    main()