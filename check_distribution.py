"""Verify placement of the 300 benchmark keys across local KV nodes."""

from collections import Counter
import json
from urllib.error import HTTPError
from urllib.request import urlopen

from consistent_hash import ConsistentHashRing


NODES = {
    "node1": "http://127.0.0.1:8081",
    "node2": "http://127.0.0.1:8082",
    "node3": "http://127.0.0.1:8083",
}

NUM_KEYS = 300


def read_key(base_url, key):
    try:
        response = urlopen(f"{base_url}/{key}", timeout=10)
    except HTTPError as error:
        response = error

    with response:
        return response.status, json.loads(response.read())


def main():
    ring = ConsistentHashRing(NODES.keys())
    counts = Counter()
    problems = 0

    for index in range(NUM_KEYS):
        key = f"key_{index}"
        expected_value = f"value_{index}"
        expected_owner = ring.owner(key)

        for node, base_url in NODES.items():
            status, body = read_key(base_url, key)

            if node == expected_owner:
                if status == 200 and body == {"value": expected_value}:
                    counts[node] += 1
                else:
                    problems += 1
                    print(
                        f"FAIL: {key} on owner {node}: "
                        f"status={status}, body={body}"
                    )
            elif status != 404:
                problems += 1
                print(
                    f"FAIL: {key} on non-owner {node}: "
                    f"expected 404, received {status}"
                )

    print("\nVerified benchmark-key distribution:")

    for node in NODES:
        count = counts[node]
        print(
            f"  {node}: {count} keys "
            f"({100 * count / NUM_KEYS:.2f}%)"
        )

    print(f"\nCorrectly placed values: {sum(counts.values())}/{NUM_KEYS}")
    print(f"Problems: {problems}")

    if problems:
        raise SystemExit(1)

    print("PASS: all benchmark keys have correct values and placement.")


if __name__ == "__main__":
    main()