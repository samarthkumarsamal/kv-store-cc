"""Deterministic consistent hashing with virtual nodes."""

import hashlib
from bisect import bisect_left
from collections import Counter


class ConsistentHashRing:
    """An immutable hash ring.

    To change membership, construct a new ring with the desired nodes.
    All routers must use identical node names and virtual-node counts.
    """

    def __init__(self, nodes, virtual_nodes=150):
        if (
            not isinstance(virtual_nodes, int)
            or isinstance(virtual_nodes, bool)
            or virtual_nodes < 1
        ):
            raise ValueError("virtual_nodes must be a positive integer.")

        if isinstance(nodes, (str, bytes)):
            raise ValueError("nodes must be a collection of node names.")

        node_list = list(nodes)

        if not node_list:
            raise ValueError("At least one node is required.")

        if any(
            not isinstance(node, str) or not node.strip()
            for node in node_list
        ):
            raise ValueError("Each node must be a non-empty string.")

        if len(set(node_list)) != len(node_list):
            raise ValueError("Node names must be unique.")

        self.nodes = tuple(sorted(node_list))
        self.virtual_nodes = virtual_nodes

        points = []

        for node in self.nodes:
            for replica in range(virtual_nodes):
                # Length-prefix the node name to avoid ambiguous labels.
                label = f"{len(node)}:{node}:{replica}"
                points.append((self._hash(label), node))

        points.sort()

        self._positions = tuple(position for position, _ in points)
        self._owners = tuple(node for _, node in points)

    @staticmethod
    def _hash(value):
        digest = hashlib.sha256(value.encode("utf-8")).digest()
        return int.from_bytes(digest, byteorder="big")

    def owner(self, key):
        """Return the node responsible for a non-empty string key."""
        if not isinstance(key, str) or not key:
            raise ValueError("Key must be a non-empty string.")

        position = self._hash(key)
        index = bisect_left(self._positions, position)

        # Wrap around to the beginning of the ring.
        if index == len(self._positions):
            index = 0

        return self._owners[index]


def demonstrate():
    keys = [f"key_{index}" for index in range(10000)]
    previous_ring = None

    for count in (1, 2, 3):
        nodes = [f"node{index}" for index in range(1, count + 1)]
        ring = ConsistentHashRing(nodes)

        distribution = Counter(ring.owner(key) for key in keys)

        print(f"\n{count} node(s), {len(keys)} keys:")

        for node in ring.nodes:
            owned = distribution[node]
            print(
                f"  {node}: {owned} keys "
                f"({100 * owned / len(keys):.2f}%)"
            )

        if previous_ring is not None:
            moved = [
                key
                for key in keys
                if previous_ring.owner(key) != ring.owner(key)
            ]

            # When adding one node, changed owners should be that node.
            added_node = f"node{count}"
            assert all(ring.owner(key) == added_node for key in moved)

            print(
                f"  Keys changing owner: {len(moved)} "
                f"({100 * len(moved) / len(keys):.2f}%)"
            )

        # Input order must not affect routing.
        reordered = ConsistentHashRing(list(reversed(nodes)))
        assert all(
            ring.owner(key) == reordered.owner(key)
            for key in keys
        )

        previous_ring = ring

    print("\nPASS: deterministic routing and node-addition checks.")


if __name__ == "__main__":
    demonstrate()