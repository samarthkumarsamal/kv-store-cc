import argparse
import http.client
import json
import logging
import os
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlsplit

from consistent_hash import ConsistentHashRing


LOGGER = logging.getLogger("kv-router")
MAX_BODY_BYTES = 1024 * 1024


class PoolBusyError(Exception):
    pass


class ConnectionPool:
    """Bounded per-node HTTP/1.1 pool with exclusive connection leases."""

    def __init__(self, host, port, size=64):
        self.host = host
        self.port = port
        self._slots = threading.BoundedSemaphore(size)
        self._idle = queue.LifoQueue()

    def acquire(self):
        if not self._slots.acquire(timeout=3):
            raise PoolBusyError()

        try:
            while True:
                try:
                    connection, released = self._idle.get_nowait()
                except queue.Empty:
                    return http.client.HTTPConnection(
                        self.host,
                        self.port,
                        timeout=10,
                    )

                # Backend idle timeout is 15 seconds.
                # Discard idle connections before that deadline.
                if time.monotonic() - released < 10:
                    return connection

                connection.close()

        except BaseException:
            self._slots.release()
            raise

    def release(self, connection, reusable):
        try:
            if reusable:
                self._idle.put(
                    (connection, time.monotonic())
                )
            else:
                connection.close()
        finally:
            self._slots.release()

    def close(self):
        while True:
            try:
                connection, _ = self._idle.get_nowait()
            except queue.Empty:
                return

            connection.close()


class RouterServer(ThreadingHTTPServer):
    request_queue_size = 256
    daemon_threads = False
    block_on_close = True

    def __init__(self, address, nodes, virtual_nodes):
        self.nodes = nodes
        self.pools = {
            node: ConnectionPool(host, port)
            for node, (host, port) in nodes.items()
        }
        self.ring = ConsistentHashRing(
            nodes.keys(),
            virtual_nodes=virtual_nodes,
        )

        super().__init__(address, RouterHandler)

    def server_close(self):
        # Finish handlers before closing upstream connections.
        super().server_close()

        for pool in self.pools.values():
            pool.close()


class RouterHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True
    wbufsize = 65536

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, message, *args):
        # Log responses centrally in _respond.
        pass

    def _respond(self, status, body, node=None):
        self.send_response(status)
        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8",
        )
        self.send_header(
            "Content-Length",
            str(len(body)),
        )

        if node is not None:
            self.send_header("X-KV-Node", node)

        if self.close_connection:
            self.send_header("Connection", "close")

        self.end_headers()
        self.wfile.write(body)

        LOGGER.info(
            "client=%s method=%s path=%r node=%s status=%s",
            self.client_address[0],
            self.command,
            self.path,
            node or "-",
            status,
        )

    def _error(self, status, message):
        body = json.dumps(
            {"error": message}
        ).encode("utf-8")

        self._respond(status, body)

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

    def _body(self):
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

        body = self.rfile.read(length)

        if len(body) != length:
            raise ValueError("Incomplete request body.")

        return body

    def _forward(self):
        connection = None
        pool = None
        reusable = False
        response_started = False

        try:
            key = self._key()
            node = self.server.ring.owner(key)
            pool = self.server.pools[node]

            body = None
            headers = {}

            if self.command in ("POST", "PUT"):
                body = self._body()
                headers["Content-Type"] = "application/json"

            elif (
                self.headers.get("Transfer-Encoding") is not None
                or self.headers.get_all("Content-Length")
                not in (None, ["0"])
            ):
                # Do not reuse a client stream with unread bytes.
                self.close_connection = True

            # Encode Unicode and reserved characters for upstream HTTP.
            upstream_path = "/" + quote(key, safe="")

            connection = pool.acquire()
            connection.request(
                self.command,
                upstream_path,
                body=body,
                headers=headers,
            )

            response = connection.getresponse()
            response_body = response.read()
            status = response.status
            reusable = not response.will_close

            response_started = True
            self._respond(
                status,
                response_body,
                node=node,
            )

        except ValueError as error:
            self.close_connection = True
            self._error(400, str(error))

        except PoolBusyError:
            self.close_connection = True
            self._error(
                503,
                "Selected node connection pool is busy.",
            )

        except (
            BrokenPipeError,
            ConnectionResetError,
        ):
            self.close_connection = True
            LOGGER.warning(
                "Connection interrupted: path=%r",
                self.path,
            )

            if not response_started:
                self._error(
                    502,
                    "Connection to the selected node "
                    "was interrupted.",
                )

        except (OSError, http.client.HTTPException):
            self.close_connection = True
            LOGGER.exception(
                "Request forwarding failed"
            )
            self._error(
                502,
                "Unable to complete request to "
                "the selected node.",
            )

        finally:
            if connection is not None:
                pool.release(connection, reusable)

    def do_GET(self):
        self._forward()

    def do_POST(self):
        self._forward()

    def do_PUT(self):
        self._forward()

    def do_DELETE(self):
        self._forward()


def parse_node(value):
    """Parse NAME=HOST:PORT, such as node1=127.0.0.1:8081."""
    try:
        name, address = value.split("=", 1)
        host, port_text = address.rsplit(":", 1)
        port = int(port_text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "Use NAME=HOST:PORT, for example "
            "node1=127.0.0.1:8081"
        ) from None

    if (
        not name
        or any(
            character not in
            "abcdefghijklmnopqrstuvwxyz"
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "0123456789_-"
            for character in name
        )
    ):
        raise argparse.ArgumentTypeError(
            "Node names must use letters, digits, "
            "underscores, or hyphens."
        )

    if not host or not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(
            "Invalid node host or port."
        )

    return name, (host, port)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__
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
        "--virtual-nodes",
        type=int,
        default=150,
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=os.environ.get(
            "KV_ROUTER_WORKERS", "1"
        ),
        help=(
            "Async router processes; default 1. "
            "Storage nodes stay single-process."
        ),
    )
    parser.add_argument(
        "--engine",
        choices=("auto", "threaded", "async"),
        default=os.environ.get(
            "KV_ENGINE", "auto"
        ),
    )
    parser.add_argument(
        "--access-log",
        choices=("on", "off"),
        default=os.environ.get(
            "KV_ACCESS_LOG", "on"
        ),
    )
    parser.add_argument(
        "--node",
        action="append",
        required=True,
        type=parse_node,
        help=(
            "Repeat for each node: "
            "--node node1=127.0.0.1:8081"
        ),
    )

    args = parser.parse_args()

    if args.virtual_nodes < 1:
        parser.error(
            "--virtual-nodes must be positive."
        )

    if args.workers < 1:
        parser.error(
            "--workers must be positive."
        )

    nodes = dict(args.node)

    if len(nodes) != len(args.node):
        parser.error(
            "Node names must be unique."
        )

    if (
        args.access_log not in ("on", "off")
        or args.engine not in (
            "auto", "threaded", "async"
        )
    ):
        parser.error(
            "Invalid KV_ACCESS_LOG or KV_ENGINE "
            "environment setting."
        )

    from fast_runtime import available, run_router

    if (
        args.engine == "async"
        or (
            args.engine == "auto"
            and available()
        )
    ):
        run_router(args, nodes)
        return

    if args.workers != 1:
        parser.error(
            "--workers greater than 1 requires the "
            "async engine and requirements-fast.txt."
        )

    from runtime_logging import configure_logging

    listener, log_queue = configure_logging(
        LOGGER,
        args.access_log,
    )

    server = RouterServer(
        (args.host, args.port),
        nodes,
        args.virtual_nodes,
    )

    LOGGER.info(
        "Router listening on http://%s:%s; nodes=%s",
        args.host,
        args.port,
        ", ".join(sorted(nodes)),
    )

    try:
        server.serve_forever()

    except KeyboardInterrupt:
        LOGGER.info("Stopping router")

    finally:
        server.server_close()
        log_queue.join()
        listener.stop()


if __name__ == "__main__":
    main()