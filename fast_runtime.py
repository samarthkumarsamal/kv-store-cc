import asyncio
import importlib.util
import json
import logging
import sqlite3
import sys
import os
from argparse import Namespace

from access_logging import AccessLog
from consistent_hash import ConsistentHashRing
from storage import KeyNotFoundError, KeyValueStore, MemoryKeyValueStore


LOGGER = logging.getLogger("kv-fast")
MAX_BODY = 1 << 20
JSON_HEADERS = [
    (b"content-type", b"application/json; charset=utf-8")
]
MISSING = b'{"error":"Key not found."}'


def available():
    return (
        sys.version_info >= (3, 11)
        and all(
            importlib.util.find_spec(name) is not None
            for name in ("uvicorn", "httptools")
        )
    )


def json_body(payload):
    return json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def reject_constant(value):
    raise ValueError(
        "Non-finite JSON constants are not supported."
    )


def key_of(scope):
    path = scope["path"]
    key = path[1:]

    if not path.startswith("/") or not key:
        raise ValueError(
            "Provide a non-empty key in the URL."
        )

    if len(key.encode("utf-8")) > 1024:
        raise ValueError(
            "Key must not exceed 1024 UTF-8 bytes."
        )

    return key


async def read_body(scope, receive):
    headers = scope["headers"]

    lengths = [
        value
        for name, value in headers
        if name == b"content-length"
    ]
    types = [
        value
        for name, value in headers
        if name == b"content-type"
    ]

    if (
        any(
            name == b"transfer-encoding"
            for name, _ in headers
        )
        or len(lengths) != 1
    ):
        raise ValueError(
            "Exactly one Content-Length and no "
            "Transfer-Encoding are required."
        )

    try:
        length = int(lengths[0])
    except ValueError:
        raise ValueError(
            "Content-Length must be an integer."
        ) from None

    if not 0 <= length <= MAX_BODY:
        raise ValueError(
            "Request body must be between 0 and "
            "1048576 bytes."
        )

    if (
        len(types) != 1
        or types[0].split(b";", 1)[0].strip().lower()
        != b"application/json"
    ):
        raise ValueError(
            "Content-Type must be application/json."
        )

    chunks = []
    total = 0

    while True:
        event = await receive()

        if event["type"] == "http.disconnect":
            raise ConnectionError(
                "Client disconnected before sending the body."
            )

        chunk = event.get("body", b"")
        total += len(chunk)

        if total > MAX_BODY or total > length:
            raise ValueError(
                "Request body exceeds Content-Length "
                "or the size limit."
            )

        chunks.append(chunk)

        if not event.get("more_body", False):
            break

    if total != length:
        raise ValueError("Incomplete request body.")

    return (
        chunks[0]
        if len(chunks) == 1
        else b"".join(chunks)
    )


class Application:
    def __init__(self, args, nodes=None):
        self.args = args
        self.nodes = nodes
        self.ring = (
            ConsistentHashRing(nodes, args.virtual_nodes)
            if nodes
            else None
        )
        self.store = None
        self.pools = {}
        self.access = None

    async def lifespan(self, receive, send):
        while True:
            event = await receive()

            if event["type"] == "lifespan.startup":
                try:
                    self.access = AccessLog(
                        self.args.access_log == "on"
                    )

                    if self.nodes:
                        from fast_http import NodePool

                        self.pools = {
                            node: NodePool(host, port)
                            for node, (host, port)
                            in self.nodes.items()
                        }

                    elif self.args.storage == "sqlite":
                        self.store = KeyValueStore(
                            self.args.database,
                            cache_bytes=self.args.cache_mib << 20,
                            coherence=self.args.cache_coherence,
                            batch_size=self.args.batch_size,
                        )

                    else:
                        self.store = MemoryKeyValueStore()
                        LOGGER.warning(
                            "MEMORY MODE: values are lost "
                            "on process exit"
                        )

                except Exception as error:
                    if self.access is not None:
                        self.access.close()

                    await send({
                        "type": "lifespan.startup.failed",
                        "message": str(error),
                    })
                    return

                await send({
                    "type": "lifespan.startup.complete"
                })

            elif event["type"] == "lifespan.shutdown":
                try:
                    for pool in self.pools.values():
                        await pool.close()

                    if self.store is not None:
                        await asyncio.to_thread(
                            self.store.close
                        )

                    self.access.close()

                except Exception as error:
                    await send({
                        "type": "lifespan.shutdown.failed",
                        "message": str(error),
                    })
                    return

                await send({
                    "type": "lifespan.shutdown.complete"
                })
                return

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            await self.lifespan(receive, send)
            return

        if scope["type"] != "http":
            return

        method = scope["method"]
        status = 500
        node = "-"
        close = scope["http_version"] == "1.0"
        extra = []

        try:
            if method not in (
                "GET", "POST", "PUT", "DELETE"
            ):
                status = 405
                body = b'{"error":"Method not supported."}'
                extra.append((
                    b"allow",
                    b"GET, POST, PUT, DELETE",
                ))
                close = True

            else:
                key = key_of(scope)

                raw = (
                    await read_body(scope, receive)
                    if method in ("POST", "PUT")
                    else None
                )

                if raw is None and any(
                    name == b"transfer-encoding"
                    or (
                        name == b"content-length"
                        and value != b"0"
                    )
                    for name, value in scope["headers"]
                ):
                    close = True

                if self.ring is not None:
                    from fast_http import PoolBusyError

                    node = self.ring.owner(key)

                    try:
                        status, body = (
                            await self.pools[node].request(
                                method, key, raw
                            )
                        )

                    except PoolBusyError as error:
                        status = 503
                        body = json_body({
                            "error": str(error)
                        })
                        close = True

                    except Exception:
                        LOGGER.exception(
                            "Upstream request failed: node=%s",
                            node,
                        )
                        status = 502
                        body = (
                            b'{"error":'
                            b'"Selected node unavailable."}'
                        )
                        close = True

                    extra.append((
                        b"x-kv-node",
                        node.encode("ascii"),
                    ))

                elif method == "GET":
                    body = self.store.get_response(key)
                    status = 200

                elif method in ("PUT", "POST"):
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

                    future = self.store.submit_put(
                        key,
                        payload["value"],
                    )

                    if future is not None:
                        await asyncio.wrap_future(future)

                    status = 200
                    body = json_body({
                        "key": key,
                        "message": "Value stored.",
                    })

                else:
                    future = self.store.submit_delete(key)

                    if future is not None:
                        await asyncio.wrap_future(future)

                    status = 200
                    body = json_body({
                        "key": key,
                        "message": "Key deleted.",
                    })

        except KeyNotFoundError:
            status = 404
            body = MISSING

        except (ValueError, RecursionError) as error:
            status = 400
            body = json_body({"error": str(error)})
            close = True

        except (sqlite3.Error, RuntimeError):
            LOGGER.exception("Storage operation failed")
            status = 500
            body = b'{"error":"Storage operation failed."}'
            close = True

        except ConnectionError:
            close = True
            status = 400
            body = b'{"error":"Client disconnected."}'

        headers = (
            JSON_HEADERS
            + [(
                b"content-length",
                str(len(body)).encode("ascii"),
            )]
            + extra
        )

        if close:
            headers.append((b"connection", b"close"))

        try:
            await send({
                "type": "http.response.start",
                "status": status,
                "headers": headers,
            })
            await send({
                "type": "http.response.body",
                "body": body,
            })

        finally:
            peer = scope.get("client")

            self.access.emit(
                peer[0] if peer else "-",
                method,
                scope.get("raw_path", b"").decode(
                    "ascii",
                    "backslashreplace",
                ),
                status,
                node,
            )


def router_application():
    """Create independent pools and logging in each router worker."""
    configuration = json.loads(
        os.environ["KV_ROUTER_WORKER_CONFIG"]
    )

    logging.basicConfig(level=logging.WARNING)
    LOGGER.warning(
        "Initializing router worker pid=%s",
        os.getpid(),
    )

    return Application(
        Namespace(**configuration["args"]),
        configuration["nodes"],
    )


def _run(args, nodes=None):
    if not available():
        raise SystemExit(
            "Fast engine needs Python 3.11+ and: "
            "python -m pip install -r requirements-fast.txt "
            "(or use --engine threaded)"
        )

    import uvicorn

    loop = (
        "uvloop"
        if importlib.util.find_spec("uvloop") is not None
        else "asyncio"
    )

    logging.basicConfig(level=logging.WARNING)
    workers = getattr(args, "workers", 1)

    if workers < 1 or (workers > 1 and not nodes):
        raise SystemExit(
            "Multiple workers are supported only "
            "by the async router."
        )

    if workers > 1:
        os.environ["KV_ROUTER_WORKER_CONFIG"] = json.dumps({
            "args": vars(args),
            "nodes": nodes,
        })
        application = "fast_runtime:router_application"
    else:
        application = Application(args, nodes)

    uvicorn.run(
        application,
        host=args.host,
        port=args.port,
        workers=workers,
        factory=workers > 1,
        loop=loop,
        http="httptools",
        lifespan="on",
        access_log=False,
        log_config=None,
        log_level="warning",
        proxy_headers=False,
        server_header=False,
        timeout_keep_alive=15,
        timeout_graceful_shutdown=30,
        backlog=1024,
    )


def run_storage(args):
    _run(args)


def run_router(args, nodes):
    _run(args, nodes)