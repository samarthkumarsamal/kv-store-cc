"""Bounded, lossless operation logging outside HTTP request threads."""

import logging
import logging.handlers
import queue


class BlockingQueueHandler(logging.handlers.QueueHandler):
    def enqueue(self, record):
        # Apply backpressure instead of silently discarding operation logs.
        self.queue.put(record)


def configure_logging(logger, access_log):
    records = queue.Queue(maxsize=10000)
    sink = logging.StreamHandler()
    sink.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s"
    ))
    logger.handlers = [BlockingQueueHandler(records)]
    logger.propagate = False
    logger.setLevel(logging.INFO if access_log == "on" else logging.WARNING)
    listener = logging.handlers.QueueListener(records, sink)
    listener.start()
    return listener, records
