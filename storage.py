from collections import OrderedDict
from concurrent.futures import Future
import json
from pathlib import Path
import queue
import sqlite3
import threading


class KeyNotFoundError(Exception):
    """The requested key does not exist."""


def validate_key(key):
    if not isinstance(key, str) or not key:
        raise ValueError("Key must be a non-empty string.")
    if len(key.encode("utf-8")) > 1024:
        raise ValueError("Key must not exceed 1024 UTF-8 bytes.")


def encode_value(value):
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    )
    return encoded, b'{"value":' + encoded.encode("utf-8") + b"}"


class KeyValueStore:
    def __init__(
        self,
        database_path="data/kv.db",
        *,
        cache_bytes=64 << 20,
        coherence="strict",
        batch_size=64,
    ):
        if coherence not in ("strict", "process"):
            raise ValueError("Cache coherence must be strict or process.")
        if cache_bytes < 0 or batch_size < 1:
            raise ValueError("Invalid cache budget or commit batch size.")

        path = Path(database_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._cache_lock = threading.Lock()
        self._admission = threading.Lock()

        self._cache = OrderedDict()
        self._cache_size = 0
        self._cache_bytes = cache_bytes
        self._coherence = coherence
        self._batch_size = batch_size
        self._closing = False
        self._failed = None

        self._connection = sqlite3.connect(
            str(path),
            timeout=5,
            check_same_thread=False,
        )
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA busy_timeout=5000")

        with self._connection:
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS kv "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )

        self._version = self._connection.execute(
            "PRAGMA data_version"
        ).fetchone()[0]

        self._jobs = queue.Queue(maxsize=4096)
        self._worker = threading.Thread(
            target=self._write_loop,
            name="sqlite-writer",
            daemon=False,
        )
        self._worker.start()

    _validate_key = staticmethod(validate_key)

    def _check_open(self):
        if self._closing:
            raise RuntimeError("Storage is closing.")
        if self._failed is not None:
            raise sqlite3.DatabaseError(
                "Storage requires restart after a failed rollback."
            ) from self._failed

    def _clear_cache(self):
        with self._cache_lock:
            self._cache.clear()
            self._cache_size = 0

    def _refresh(self):
        # Caller holds the connection lock. Own commits do not change this
        # connection's data_version; external commits do.
        version = self._connection.execute(
            "PRAGMA data_version"
        ).fetchone()[0]

        if version != self._version:
            self._clear_cache()
            self._version = version

        return version

    def _cached(self, key):
        with self._cache_lock:
            body = self._cache.get(key)
            if body is not None:
                self._cache.move_to_end(key)
            return body

    @staticmethod
    def _entry_size(key, body):
        return len(key.encode("utf-8")) + len(body)

    def _remember(self, key, body):
        with self._cache_lock:
            previous = self._cache.pop(key, None)
            if previous is not None:
                self._cache_size -= self._entry_size(key, previous)

            if body is None or not self._cache_bytes:
                return

            size = self._entry_size(key, body)
            if size > self._cache_bytes:
                return

            self._cache[key] = body
            self._cache_size += size

            while (
                self._cache_size > self._cache_bytes
                or len(self._cache) > 65536
            ):
                old_key, old_body = self._cache.popitem(last=False)
                self._cache_size -= self._entry_size(old_key, old_body)

    def get_response(self, key):
        """Return immutable encoded JSON without per-GET JSON conversion."""
        validate_key(key)
        self._check_open()

        if self._coherence == "process":
            body = self._cached(key)
            if body is not None:
                return body

        with self._lock:
            self._check_open()

            if self._coherence == "strict":
                self._refresh()

            body = self._cached(key)
            if body is None:
                row = self._connection.execute(
                    "SELECT value FROM kv WHERE key = ?",
                    (key,),
                ).fetchone()

                if row is None:
                    raise KeyNotFoundError(key)

                try:
                    # Validate only on a cache miss. Invalid database contents
                    # must not produce a successful malformed JSON response.
                    value = json.loads(row[0])
                    _, body = encode_value(value)
                except (ValueError, TypeError, RecursionError) as error:
                    raise sqlite3.DatabaseError(
                        "Stored value is not valid finite JSON."
                    ) from error

                self._remember(key, body)

            return body

    def get(self, key):
        return json.loads(self.get_response(key))["value"]

    def _enqueue(self, operation, key, encoded=None, body=None):
        future = Future()

        with self._admission:
            self._check_open()
            self._jobs.put((operation, key, encoded, body, future))

        return future

    def submit_put(self, key, value):
        validate_key(key)
        encoded, body = encode_value(value)
        self._check_open()

        if self._coherence == "strict":
            with self._lock:
                self._refresh()
                identical = self._cached(key) == body
        else:
            identical = self._cached(key) == body

        if identical:
            # The exact value is already committed. A no-op can linearize
            # before an overlapping queued mutation. Sequential requests see
            # the cache updated before the preceding mutation's ACK.
            return None

        return self._enqueue("put", key, encoded, body)

    def put(self, key, value):
        future = self.submit_put(key, value)
        if future is not None:
            future.result()

    def submit_delete(self, key):
        validate_key(key)
        return self._enqueue("delete", key)

    def delete(self, key):
        self.submit_delete(key).result()

    @staticmethod
    def _finish(future, error=None):
        # An admitted write may commit after its HTTP client disconnects.
        # Cancellation must not terminate the writer thread.
        if not future.set_running_or_notify_cancel():
            return

        if error is None:
            future.set_result(None)
        else:
            future.set_exception(error)

    def _write_loop(self):
        while True:
            first = self._jobs.get()

            if first is None:
                self._jobs.task_done()
                return

            batch = [first]
            stopping = False

            # Group already queued writes without adding an artificial delay.
            while len(batch) < self._batch_size:
                try:
                    job = self._jobs.get_nowait()
                except queue.Empty:
                    break

                if job is None:
                    self._jobs.task_done()
                    stopping = True
                    break

                batch.append(job)

            self._commit(batch)

            for _ in batch:
                self._jobs.task_done()

            if stopping:
                return

    def _commit(self, batch):
        errors = [None] * len(batch)
        changes = {}

        with self._lock:
            try:
                if self._failed is not None:
                    raise sqlite3.DatabaseError("Storage is unavailable.")

                self._connection.execute("BEGIN IMMEDIATE")
                before = (
                    self._refresh()
                    if self._coherence == "strict"
                    else None
                )

                for index, job in enumerate(batch):
                    operation, key, encoded, body, _ = job

                    if operation == "put":
                        self._connection.execute(
                            "INSERT INTO kv(key,value) VALUES(?,?) "
                            "ON CONFLICT(key) DO UPDATE "
                            "SET value=excluded.value "
                            "WHERE kv.value IS NOT excluded.value",
                            (key, encoded),
                        )
                        changes[key] = body
                    else:
                        cursor = self._connection.execute(
                            "DELETE FROM kv WHERE key=?",
                            (key,),
                        )
                        changes[key] = None

                        if not cursor.rowcount:
                            errors[index] = KeyNotFoundError(key)

                self._connection.commit()

                if self._coherence == "strict":
                    after = self._refresh()
                    # An external writer could commit between COMMIT and
                    # data_version. Do not publish stale local cache values.
                    publish = before == after
                else:
                    publish = True

                if publish:
                    for key, body in changes.items():
                        self._remember(key, body)

            except Exception as error:
                try:
                    self._connection.rollback()
                except sqlite3.Error as rollback_error:
                    self._failed = rollback_error

                self._clear_cache()
                errors = [error] * len(batch)

        for job, error in zip(batch, errors):
            self._finish(job[-1], error)

    def close(self):
        with self._admission:
            if self._closing:
                return

            self._closing = True
            self._jobs.put(None)

        self._worker.join()

        with self._lock:
            self._clear_cache()
            self._connection.close()


class MemoryKeyValueStore:
    """Explicitly non-durable mode; never selected implicitly."""

    def __init__(self):
        self._lock = threading.Lock()
        self._values = {}

    def put(self, key, value):
        validate_key(key)
        _, body = encode_value(value)

        with self._lock:
            self._values[key] = body

    def submit_put(self, key, value):
        self.put(key, value)

    def get_response(self, key):
        validate_key(key)

        with self._lock:
            try:
                return self._values[key]
            except KeyError:
                raise KeyNotFoundError(key) from None

    def get(self, key):
        return json.loads(self.get_response(key))["value"]

    def delete(self, key):
        validate_key(key)

        with self._lock:
            try:
                del self._values[key]
            except KeyError:
                raise KeyNotFoundError(key) from None

    def submit_delete(self, key):
        self.delete(key)

    def close(self):
        pass


def smoke_test():
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as directory:
        path = str(Path(directory) / "test.db")
        store = KeyValueStore(path)

        try:
            store.put("course", "Cloud Computing")
            assert store.get("course") == "Cloud Computing"

            store.put("course", {"assignment": 1, "complete": False})
            store.put("temporary", "remove me")
            store.delete("temporary")

            try:
                store.get("temporary")
                raise AssertionError("Deleted key exists")
            except KeyNotFoundError:
                pass
        finally:
            store.close()

        reopened = KeyValueStore(path)

        try:
            assert reopened.get("course") == {
                "assignment": 1,
                "complete": False,
            }
        finally:
            reopened.close()

    print("PASS: create, read, update, delete, and persistence.")


if __name__ == "__main__":
    smoke_test()