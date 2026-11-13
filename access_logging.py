import queue
import sys
import threading
import time


class AccessLog:
    def __init__(self, enabled=True, stream=None):
        self.enabled = enabled
        self.stream = stream if stream is not None else sys.stderr
        self.records = queue.Queue(maxsize=10000)
        self.failure = None
        self.worker = None

        if enabled:
            self.worker = threading.Thread(
                target=self._run,
                name="access-log",
                daemon=False,
            )
            self.worker.start()

    def emit(self, client, method, path, status, node="-"):
        if self.enabled:
            if self.failure is not None:
                raise RuntimeError(
                    "Operation log output failed."
                ) from self.failure

            self.records.put(
                (time.time(), client, method, path, status, node)
            )

    def _run(self):
        last_second = None
        prefix = ""

        while True:
            item = self.records.get()
            items = [item]
            stopping = item is None

            while not stopping and len(items) < 256:
                try:
                    item = self.records.get_nowait()
                except queue.Empty:
                    break

                items.append(item)
                stopping = item is None

            try:
                if self.failure is None:
                    lines = []

                    for record in items:
                        if record is None:
                            continue

                        stamp, client, method, path, status, node = record
                        second = int(stamp)

                        if second != last_second:
                            prefix = time.strftime(
                                "%Y-%m-%dT%H:%M:%S",
                                time.gmtime(second),
                            )
                            last_second = second

                        millis = int((stamp - second) * 1000)
                        lines.append(
                            f"{prefix}.{millis:03d}Z "
                            f"client={client!r} method={method} "
                            f"path={path!r} node={node} status={status}\n"
                        )

                    self.stream.write("".join(lines))
                    self.stream.flush()

            except Exception as error:
                # Continue draining so output failures cannot deadlock
                # producers or shutdown. Report the failure explicitly.
                self.failure = error

            finally:
                for _ in items:
                    self.records.task_done()

            if stopping:
                return

    def close(self):
        if self.worker is not None:
            self.records.put(None)
            self.worker.join()
            self.worker = None

        if self.failure is not None:
            raise RuntimeError(
                "Operation log output failed."
            ) from self.failure