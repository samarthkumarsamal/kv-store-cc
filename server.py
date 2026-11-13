import argparse
import json
import logging
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from storage import KeyNotFoundError, KeyValueStore


LOGGER = logging.getLogger("kv-server")
MAX_BODY_BYTES = 1024 * 1024


class KVServer(ThreadingHTTPServer):
    request_queue_size = 256

    # Finish active request threads before closing the database.
    daemon_threads = False
    block_on_close = True

    def __init__(self, address, store):
        self.store = store
        super().__init__(address, KVRequestHandler)


class KVRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True
    wbufsize = 65536

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, message, *args):
        # Responses are logged centrally in _send_body.
        pass

    def _send_json(self, status, payload):
        body = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")

        self._send_body(status, body)

    def _send_body(self, status, body):
        self.send_response(status)
        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8",
        )
        self.send_header("Content-Length", str(len(body)))

        if self.close_connection:
            self.send_header("Connection", "close")

        self.end_headers()
        self.wfile.write(body)

        LOGGER.info(
            "client=%s method=%s path=%r status=%s",
            self.client_address[0],
            self.command,
            self.path,
            status,
        )

    def _key(self):
        path = urlsplit(self.path).path
        key = unquote(path[1:])

        if not path.startswith("/") or not key:
            raise ValueError(
                "Provide a non-empty key in the URL."
            )

        if len(key.encode("utf-8")) > 1024:
            raise ValueError(
                "Key must not exceed 1024 UTF-8 bytes."
            )

        return key

    def _read_value(self):
        # Reject ambiguous framing so unread bytes cannot become
        # another request on the same persistent connection.
        if self.headers.get("Transfer-Encoding") is not None:
            raise ValueError(
                "Transfer-Encoding is not supported."
            )

        lengths = (
            self.headers.get_all("Content-Length") or []
        )

        if len(lengths) != 1:
            raise ValueError(
                "Exactly one Content-Length header is required."
            )

        try:
            length = int(lengths[0])
        except ValueError:
            raise ValueError(
                "Content-Length must be an integer."
            ) from None

        if length < 0 or length > MAX_BODY_BYTES:
            raise ValueError(
                "Request body must be between 0 and "
                "1048576 bytes."
            )

        content_type = self.headers.get(
            "Content-Type", ""
        )

        if (
            content_type.split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            raise ValueError(
                "Content-Type must be application/json."
            )

        raw = self.rfile.read(length)

        if len(raw) != length:
            raise ValueError("Incomplete request body.")

        def reject_constant(value):
            raise ValueError(
                f"Invalid JSON constant: {value}"
            )

        try:
            payload = json.loads(
                raw,
                parse_constant=reject_constant,
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
        ):
            raise ValueError(
                "Request body must contain valid JSON."
            ) from None

        if (
            not isinstance(payload, dict)
            or "value" not in payload
        ):
            raise ValueError(
                'JSON body must contain a "value" field.'
            )

        return payload["value"]

    def _dispatch(self, operation):
        # Close if a read/delete carries a body we do not consume.
        if operation in ("get", "delete"):
            if (
                self.headers.get("Transfer-Encoding") is not None
                or self.headers.get_all("Content-Length")
                not in (None, ["0"])
            ):
                self.close_connection = True

        try:
            key = self._key()

            if operation == "get":
                self._send_body(
                    200,
                    self.server.store.get_response(key),
                )

            elif operation == "put":
                value = self._read_value()
                self.server.store.put(key, value)
                self._send_json(
                    200,
                    {
                        "key": key,
                        "message": "Value stored.",
                    },
                )

            elif operation == "delete":
                self.server.store.delete(key)
                self._send_json(
                    200,
                    {
                        "key": key,
                        "message": "Key deleted.",
                    },
                )

        except KeyNotFoundError:
            self._send_json(
                404,
                {"error": "Key not found."},
            )

        except (ValueError, RecursionError) as error:
            self.close_connection = True
            self._send_json(
                400,
                {"error": str(error)},
            )

        except sqlite3.Error:
            self.close_connection = True
            LOGGER.exception(
                "Database operation failed"
            )
            self._send_json(
                500,
                {"error": "Storage operation failed."},
            )

        except (
            BrokenPipeError,
            ConnectionResetError,
            TimeoutError,
        ):
            self.close_connection = True
            LOGGER.warning(
                "Client disconnected or timed out: "
                "method=%s path=%r",
                self.command,
                self.path,
            )

    def do_GET(self):
        self._dispatch("get")

    def do_POST(self):
        self._dispatch("put")

    def do_PUT(self):
        self._dispatch("put")

    def do_DELETE(self):
        self._dispatch("delete")


def main():
    parser = argparse.ArgumentParser(
        description="Persistent key-value server"
    )

    parser.add_argument(
        "--host",
        default="127.0.0.1",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
    )
    parser.add_argument(
        "--database",
        default="data/kv.db",
    )
    parser.add_argument(
        "--engine",
        choices=("auto", "threaded", "async"),
        default=os.environ.get("KV_ENGINE", "auto"),
    )
    parser.add_argument(
        "--cache-mib",
        type=int,
        default=64,
    )
    parser.add_argument(
        "--cache-coherence",
        choices=("strict", "process"),
        default=os.environ.get(
            "KV_CACHE_COHERENCE", "strict"
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=64,
    )
    parser.add_argument(
        "--storage",
        choices=("sqlite", "memory"),
        default=os.environ.get(
            "KV_STORAGE", "sqlite"
        ),
        help=(
            "sqlite is durable; memory loses all "
            "values on process exit"
        ),
    )
    parser.add_argument(
        "--access-log",
        choices=("on", "off"),
        default=os.environ.get(
            "KV_ACCESS_LOG", "on"
        ),
        help=(
            "Keep on when assignment rules require "
            "logging every operation"
        ),
    )

    args = parser.parse_args()

    if (
        args.storage not in ("sqlite", "memory")
        or args.access_log not in ("on", "off")
    ):
        parser.error(
            "Invalid KV_STORAGE or KV_ACCESS_LOG "
            "environment setting."
        )

    if (
        args.cache_coherence not in ("strict", "process")
        or args.engine not in ("auto", "threaded", "async")
    ):
        parser.error(
            "Invalid cache coherence or engine "
            "environment setting."
        )

    if args.cache_mib < 0 or args.batch_size < 1:
        parser.error(
            "Cache budget must be nonnegative "
            "and batch size positive."
        )

    from fast_runtime import available, run_storage

    if (
        args.engine == "async"
        or (
            args.engine == "auto"
            and available()
        )
    ):
        run_storage(args)
        return

    from runtime_logging import configure_logging
    from storage import MemoryKeyValueStore

    listener, log_queue = configure_logging(
        LOGGER,
        args.access_log,
    )

    store = (
        KeyValueStore(
            args.database,
            cache_bytes=args.cache_mib << 20,
            coherence=args.cache_coherence,
            batch_size=args.batch_size,
        )
        if args.storage == "sqlite"
        else MemoryKeyValueStore()
    )

    if args.storage == "memory":
        LOGGER.warning(
            "IN-MEMORY MODE: values are not durable"
        )

    server = None

    try:
        server = KVServer(
            (args.host, args.port),
            store,
        )

        LOGGER.info(
            "Listening on http://%s:%s; database=%s",
            args.host,
            args.port,
            args.database,
        )

        server.serve_forever()

    except KeyboardInterrupt:
        LOGGER.info("Stopping server")

    finally:
        if server is not None:
            server.server_close()

        store.close()

        # Request threads have finished producing log records.
        log_queue.join()
        listener.stop()


if __name__ == "__main__":
    main()