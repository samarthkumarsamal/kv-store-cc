import asyncio
import socket
import time
from urllib.parse import quote

import httptools


MAX_RESPONSE = 8 << 20


class PoolBusyError(Exception):
    pass


class Reply:
    def __init__(self):
        self.parser = httptools.HttpResponseParser(self)
        self.waiting = False
        self.done = False

    def begin(self):
        self.waiting = True
        self.done = False
        self.body = []
        self.body_size = 0
        self.header_size = 0
        self.header_count = 0
        self.length = None
        self.chunked = False
        self.started = False
        self.status = 0
        self.reusable = False

    def on_message_begin(self):
        if not self.waiting or self.started:
            raise ValueError("Unexpected extra upstream response.")

        self.started = True

    def on_header(self, name, value):
        self.header_size += len(name) + len(value) + 4
        self.header_count += 1

        if self.header_size > 16384 or self.header_count > 100:
            raise ValueError("Upstream headers are too large.")

        name = name.lower()

        if name == b"content-length":
            if self.length is not None:
                raise ValueError("Duplicate upstream Content-Length.")

            self.length = int(value)

            if not 0 <= self.length <= MAX_RESPONSE:
                raise ValueError("Upstream body exceeds the limit.")

        elif name == b"transfer-encoding":
            self.chunked = value.lower() == b"chunked"

        elif (
            name == b"content-encoding"
            and value.lower() != b"identity"
        ):
            raise ValueError(
                "Compressed upstream responses are not supported."
            )

    def on_headers_complete(self):
        self.status = self.parser.get_status_code()

        if not 200 <= self.status <= 599:
            raise ValueError("Unexpected upstream status.")

        if (
            self.length is None
            and not self.chunked
            and self.status not in (204, 304)
        ):
            raise ValueError(
                "Upstream response requires explicit framing."
            )

    def on_body(self, data):
        self.body_size += len(data)

        if self.body_size > MAX_RESPONSE:
            raise ValueError("Upstream body exceeds the limit.")

        self.body.append(data)

    def on_message_complete(self):
        # The parser resets connection flags after this callback.
        # Capture keep-alive status before that reset happens.
        self.reusable = self.parser.should_keep_alive()
        self.done = True
        self.waiting = False


class Connection:
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.reply = Reply()

    def close(self):
        self.writer.close()

    async def exchange(self, method, path, body, host_header):
        reply = self.reply
        reply.begin()

        head = (
            method.encode("ascii")
            + b" /"
            + path
            + b" HTTP/1.1\r\nHost: "
            + host_header
            + b"\r\n"
        )

        if body is not None:
            head += (
                b"Content-Type: application/json\r\n"
                b"Content-Length: "
                + str(len(body)).encode("ascii")
                + b"\r\n"
            )

        self.writer.write(head + b"\r\n" + (body or b""))
        await self.writer.drain()

        while not reply.done:
            data = await self.reader.read(65536)

            if not data:
                raise ConnectionError(
                    "Upstream closed before completing the response."
                )

            reply.parser.feed_data(data)

        payload = (
            reply.body[0]
            if len(reply.body) == 1
            else b"".join(reply.body)
        )

        return reply.status, payload, reply.reusable


class NodePool:
    def __init__(self, host, port, limit=64):
        self.host = host.strip("[]")
        self.port = port
        self.host_header = (
            host.encode("idna")
            + b":"
            + str(port).encode("ascii")
        )
        self.slots = asyncio.BoundedSemaphore(limit)
        self.idle = []

    async def acquire(self):
        try:
            await asyncio.wait_for(
                self.slots.acquire(),
                timeout=3,
            )
        except asyncio.TimeoutError:
            raise PoolBusyError(
                "Selected node connection pool is busy."
            ) from None

        try:
            while self.idle:
                connection, released = self.idle.pop()

                if (
                    time.monotonic() - released < 10
                    and not connection.writer.is_closing()
                    and not connection.reader.at_eof()
                ):
                    return connection

                connection.close()

            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=3,
            )

            writer.get_extra_info("socket").setsockopt(
                socket.IPPROTO_TCP,
                socket.TCP_NODELAY,
                1,
            )

            return Connection(reader, writer)

        except BaseException:
            self.slots.release()
            raise

    def release(self, connection, reusable):
        if (
            reusable
            and not connection.writer.is_closing()
            and not connection.reader.at_eof()
        ):
            self.idle.append(
                (connection, time.monotonic())
            )
        else:
            connection.close()

        self.slots.release()

    async def request(self, method, key, body):
        connection = None
        reusable = False

        try:
            async with asyncio.timeout(10):
                connection = await self.acquire()

                result = await connection.exchange(
                    method,
                    quote(key, safe="").encode("ascii"),
                    body,
                    self.host_header,
                )

                status, payload, reusable = result
                return status, payload

        finally:
            if connection is not None:
                self.release(connection, reusable)

    async def close(self):
        connections = [
            connection
            for connection, _ in self.idle
        ]
        self.idle.clear()

        for connection in connections:
            connection.close()

        await asyncio.gather(
            *(
                connection.writer.wait_closed()
                for connection in connections
            ),
            return_exceptions=True,
        )