"""Unit tests for consistent-hash routing."""

import unittest

from consistent_hash import ConsistentHashRing


class ConsistentHashTests(unittest.TestCase):
    def setUp(self):
        self.keys = [f"key_{index}" for index in range(5000)]

    def test_single_node_owns_every_key(self):
        ring = ConsistentHashRing(["node1"])

        for key in self.keys:
            self.assertEqual(ring.owner(key), "node1")

    def test_node_order_does_not_change_routing(self):
        first = ConsistentHashRing(["node1", "node2", "node3"])
        second = ConsistentHashRing(["node3", "node1", "node2"])

        for key in self.keys:
            self.assertEqual(first.owner(key), second.owner(key))

    def test_rebuilding_ring_preserves_routing(self):
        first = ConsistentHashRing(["node1", "node2"])
        second = ConsistentHashRing(["node1", "node2"])

        for key in self.keys:
            self.assertEqual(first.owner(key), second.owner(key))

    def test_adding_node_only_moves_keys_to_new_node(self):
        before = ConsistentHashRing(["node1", "node2"])
        after = ConsistentHashRing(["node1", "node2", "node3"])
        moved = 0

        for key in self.keys:
            old_owner = before.owner(key)
            new_owner = after.owner(key)

            if old_owner != new_owner:
                moved += 1
                self.assertEqual(new_owner, "node3")

        self.assertGreater(moved, 0)
        self.assertLess(moved, len(self.keys))

    def test_removing_node_preserves_other_owners(self):
        before = ConsistentHashRing(["node1", "node2", "node3"])
        after = ConsistentHashRing(["node1", "node3"])
        reassigned = 0

        for key in self.keys:
            old_owner = before.owner(key)
            new_owner = after.owner(key)

            if old_owner == "node2":
                reassigned += 1
                self.assertIn(new_owner, ("node1", "node3"))
            else:
                self.assertEqual(new_owner, old_owner)

        self.assertGreater(reassigned, 0)

    def test_every_node_receives_keys(self):
        nodes = ["node1", "node2", "node3"]
        ring = ConsistentHashRing(nodes)
        owners = {ring.owner(key) for key in self.keys}

        self.assertEqual(owners, set(nodes))

    def test_invalid_configuration(self):
        invalid_node_lists = [
            [],
            ["node1", "node1"],
            [""],
            ["   "],
            [123],
            "node1",
        ]

        for nodes in invalid_node_lists:
            with self.subTest(nodes=nodes):
                with self.assertRaises(ValueError):
                    ConsistentHashRing(nodes)

        for count in (0, -1, True, 1.5):
            with self.subTest(virtual_nodes=count):
                with self.assertRaises(ValueError):
                    ConsistentHashRing(["node1"], virtual_nodes=count)

    def test_invalid_keys(self):
        ring = ConsistentHashRing(["node1"])

        for key in ("", None, 123):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    ring.owner(key)

    def test_unicode_keys(self):
        ring = ConsistentHashRing(["node1", "node2"])

        for key in ("你好", "café", "🌍"):
            with self.subTest(key=key):
                self.assertIn(ring.owner(key), ring.nodes)


if __name__ == "__main__":
    unittest.main(verbosity=2)