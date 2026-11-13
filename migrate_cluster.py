"""Copy and verify snapshot data while the benchmark router is stopped."""

import argparse
import json
from pathlib import Path
import subprocess

from consistent_hash import ConsistentHashRing
from export_cluster import container_id


IMPORT_CODE = """
import json
import sqlite3
import sys

records = json.load(sys.stdin)
connection = sqlite3.connect("/data/kv.db", timeout=30)

try:
    connection.execute("PRAGMA synchronous=FULL")

    with connection:
        for key, value in records.items():
            connection.execute(
                '''
                INSERT INTO kv (key, value)
                VALUES (?, ?)
                ON CONFLICT(key)
                DO UPDATE SET value = excluded.value
                ''',
                (key, json.dumps(value, ensure_ascii=True, allow_nan=False)),
            )

    for key, expected in records.items():
        row = connection.execute(
            "SELECT value FROM kv WHERE key = ?", (key,)
        ).fetchone()

        if row is None or json.loads(row[0]) != expected:
            raise RuntimeError("Verification failed for key: " + key)

    print("Copied and verified " + str(len(records)) + " keys.")
finally:
    connection.close()
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument(
        "--target-nodes",
        type=int,
        choices=(1, 2, 3),
        required=True,
    )
    args = parser.parse_args()

    # Reject migration while the benchmark router is serving requests.
    running = subprocess.run(
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

    if running.stdout.strip():
        raise RuntimeError("Stop the benchmark router before migrating.")

    with args.snapshot.open(encoding="utf-8") as file:
        snapshot = json.load(file)

    records = snapshot["records"]
    if not isinstance(records, dict):
        raise ValueError("Snapshot records must be a JSON object.")

    nodes = [f"node{i}" for i in range(1, args.target_nodes + 1)]
    ring = ConsistentHashRing(
        nodes,
        virtual_nodes=snapshot["virtual_nodes"],
    )

    # Resolve every destination before performing any writes.
    containers = {node: container_id(node) for node in nodes}
    partitions = {node: {} for node in nodes}

    for key, value in records.items():
        partitions[ring.owner(key)][key] = value

    for node in nodes:
        result = subprocess.run(
            [
                "docker", "exec", "-i",
                containers[node],
                "python", "-c", IMPORT_CODE,
            ],
            input=json.dumps(partitions[node], allow_nan=False),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        print(f"{node}: {result.stdout.strip()}")

    print(f"\nPASS: copied and verified {len(records)} snapshot values.")
    print("Old copies and the snapshot have been retained.")
    print("Configure the router for the target membership before starting it.")


if __name__ == "__main__":
    main()