"""Integration tests against a running key-value server."""

import json
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import Request, urlopen


BASE_URL = "http://127.0.0.1:8080"


def request(method, key, payload=None, raw_body=None):
    """Return (HTTP status, decoded JSON), including error responses."""
    headers = {}
    body = raw_body

    if payload is not None:
        body = json.dumps(payload).encode("utf-8")

    if body is not None:
        headers["Content-Type"] = "application/json"

    req = Request(
        f"{BASE_URL}/{key}",
        data=body,
        headers=headers,
        method=method,
    )

    try:
        response = urlopen(req, timeout=15)
    except HTTPError as error:
        response = error

    with response:
        return response.status, json.loads(response.read())


class KVAPITests(unittest.TestCase):
    def setUp(self):
        # Unique keys prevent interference with existing application data.
        self.prefix = f"test_{uuid.uuid4().hex}"
        self.keys = set()

    def key(self, suffix):
        key = f"{self.prefix}_{suffix}"
        self.keys.add(key)
        return key

    def tearDown(self):
        for key in self.keys:
            request("DELETE", key)

    def test_create_read_update_delete(self):
        key = self.key("crud")

        status, _ = request("POST", key, {"value": "hello"})
        self.assertEqual(status, 200)

        status, body = request("GET", key)
        self.assertEqual(status, 200)
        self.assertEqual(body, {"value": "hello"})

        status, _ = request("PUT", key, {"value": "updated"})
        self.assertEqual(status, 200)

        status, body = request("GET", key)
        self.assertEqual(status, 200)
        self.assertEqual(body, {"value": "updated"})

        status, _ = request("DELETE", key)
        self.assertEqual(status, 200)

        status, body = request("GET", key)
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_missing_key(self):
        key = self.key("missing")

        for method in ("GET", "DELETE"):
            status, body = request(method, key)
            self.assertEqual(status, 404)
            self.assertIn("error", body)

    def test_json_value_types(self):
        key = self.key("types")
        values = [
            "Cloud Computing",
            42,
            3.5,
            True,
            None,
            ["one", 2],
            {"course": "cloud", "assignment": 1},
            "Hello 🌍",
        ]

        for value in values:
            with self.subTest(value=value):
                status, _ = request("PUT", key, {"value": value})
                self.assertEqual(status, 200)

                status, body = request("GET", key)
                self.assertEqual(status, 200)
                self.assertEqual(body["value"], value)

    def test_invalid_requests(self):
        key = self.key("invalid")

        invalid_bodies = [
            b'{"value":',
            b'{"wrong_field": "hello"}',
            b'["not", "an", "object"]',
            b'{"value": NaN}',
        ]

        for body in invalid_bodies:
            with self.subTest(body=body):
                status, result = request("POST", key, raw_body=body)
                self.assertEqual(status, 400)
                self.assertIn("error", result)

        # Invalid writes must not create a value.
        status, _ = request("GET", key)
        self.assertEqual(status, 404)

    def test_concurrent_independent_keys(self):
        keys = [self.key(f"parallel_{i}") for i in range(30)]

        def write_and_read(item):
            index, key = item
            value = {"index": index, "message": f"value_{index}"}

            status, _ = request("POST", key, {"value": value})
            self.assertEqual(status, 200)

            status, body = request("GET", key)
            self.assertEqual(status, 200)
            self.assertEqual(body["value"], value)

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(write_and_read, enumerate(keys)))

    def test_concurrent_writes_to_same_key(self):
        key = self.key("shared")
        values = [{"writer": i, "text": "x" * 1000} for i in range(30)]

        def write(value):
            status, _ = request("PUT", key, {"value": value})
            self.assertEqual(status, 200)

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(write, values))

        status, body = request("GET", key)
        self.assertEqual(status, 200)

        # Scheduling determines the final writer, but the stored value
        # must be one complete submitted value.
        self.assertIn(body["value"], values)

    def test_concurrent_put_and_delete(self):
        key = self.key("mixed")

        def operate(index):
            if index % 2 == 0:
                status, _ = request("PUT", key, {"value": index})
                self.assertEqual(status, 200)
            else:
                status, _ = request("DELETE", key)
                self.assertIn(status, (200, 404))

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(operate, range(40)))

        # After competing operations finish, a fresh write must work.
        status, _ = request("PUT", key, {"value": "final"})
        self.assertEqual(status, 200)

        status, body = request("GET", key)
        self.assertEqual(status, 200)
        self.assertEqual(body["value"], "final")


if __name__ == "__main__":
    unittest.main(verbosity=2)