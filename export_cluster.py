"""Export authoritative data from a paused benchmark cluster.

Stop the router and all other writers before running this script.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

from consistent_hash import ConsistentHashRing


def container_id(service):
    result = subprocess.run(
        [
            "docker", "ps",
            "--filter", "label=com.docker.compose.project=kv-benchmark",
            "--filter", f"label=com.docker.compose.service={service}",
            "--format", "{{.ID}}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    matches = result.stdout.split()

    if len(matches) != 1:
        raise RuntimeError(f"Expected one running container for {service}.")

    return matches[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=int, choices=(1, 2, 3), required=True)
    args = parser.parse_args()

    nodes = [f"node{i}" for i in range(1, args.nodes + 1)]
    ring = ConsistentHashRing(nodes)
    records = {}

    # SQLite is opened read-only. One SELECT obtains a consistent
    # per-node snapshot; stopping all writers makes the cluster stable.
    export_code = (
        "import sqlite3,json;"
        "c=sqlite3.connect('file:/data/kv.db?mode=ro',uri=True);"
        "rows=c.execute('SELECT key,value FROM kv').fetchall();"
        "print(json.dumps(rows,ensure_ascii=True));"
        "c.close()"
    )

    for node in nodes:
        result = subprocess.run(
            ["docker", "exec", container_id(node), "python", "-c", export_code],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

        rows = json.loads(result.stdout)
        accepted = 0

        for key, encoded_value in rows:
            # Previous benchmark configurations may have left stale
            # copies. Only the current ring's owner is authoritative.
            if ring.owner(key) == node:
                records[key] = json.loads(encoded_value)
                accepted += 1

        print(f"{node}: exported {accepted} authoritative keys")

    snapshot = {
        "source_nodes": nodes,
        "virtual_nodes": 150,
        "records": records,
    }

    directory = Path("backups")
    directory.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = directory / f"cluster_{timestamp}.json"

    with path.open("x", encoding="utf-8") as file:
        json.dump(snapshot, file, ensure_ascii=True, indent=2)

    print(f"\nExported {len(records)} keys.")
    print(f"Snapshot: {path}")


if __name__ == "__main__":
    main()