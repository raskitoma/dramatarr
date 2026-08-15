"""In-process pub/sub that feeds the admin SSE stream.

dramatarr runs a single API process (see the topology section of the architecture doc), so a
set of `asyncio.Queue`s is the whole broker. Nothing here touches the database: publishers
hand over an already-serialized payload and never wait on a subscriber.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Final

log = logging.getLogger(__name__)

#: Per-subscriber buffer. A tab that stops reading loses old events instead of growing the
#: process's memory, and for a live progress feed the newest event is the one worth keeping.
QUEUE_MAXSIZE: Final = 100

Event = tuple[str, dict[str, Any]]

JOB_CREATED: Final = "job.created"
JOB_STARTED: Final = "job.started"
JOB_PROGRESS: Final = "job.progress"
JOB_FINISHED: Final = "job.finished"
QUEUE_STATE: Final = "queue.state"

EVENT_NAMES: Final = (JOB_CREATED, JOB_STARTED, JOB_PROGRESS, JOB_FINISHED, QUEUE_STATE)

_CLOSED = object()


class Subscription:
    """One SSE consumer.

    Registration happens in `__init__` rather than on first iteration so that events
    published between `subscribe()` and the first `__anext__` are not lost.
    """

    def __init__(self, maxsize: int = QUEUE_MAXSIZE) -> None:
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)
        self._closed = False
        self.dropped = 0
        _subscribers.add(self)

    # --- Delivery -------------------------------------------------------------------------

    def _deliver(self, item: Event) -> None:
        if self._closed:
            return
        while True:
            try:
                self._queue.put_nowait(item)
                return
            except asyncio.QueueFull:
                try:
                    self._queue.get_nowait()
                    self.dropped += 1
                except asyncio.QueueEmpty:  # pragma: no cover - only if drained concurrently
                    return

    # --- Iteration ------------------------------------------------------------------------

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> Event:
        if self._closed and self._queue.empty():
            raise StopAsyncIteration
        try:
            item = await self._queue.get()
        except asyncio.CancelledError:
            self.close()
            raise
        if item is _CLOSED:
            raise StopAsyncIteration
        return item

    # --- Lifecycle ------------------------------------------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        _subscribers.discard(self)
        try:
            # Wakes an iterator parked on get(); the emptiness check in __anext__ handles
            # the case where the queue was already full.
            self._queue.put_nowait(_CLOSED)
        except asyncio.QueueFull:
            pass

    async def aclose(self) -> None:
        self.close()

    async def __aenter__(self) -> Subscription:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - interpreter teardown ordering
        try:
            _subscribers.discard(self)
        except Exception:
            pass


_subscribers: set[Subscription] = set()


def subscribe(maxsize: int = QUEUE_MAXSIZE) -> Subscription:
    """Register a consumer. Use it as `async with subscribe() as events:` or iterate it and
    call `close()` when done — the SSE endpoint should do the former."""
    return Subscription(maxsize)


async def publish(event: str, data: dict[str, Any]) -> None:
    """Fan out to every subscriber. Never blocks and never raises into the caller: a job
    must not fail because a browser stopped reading."""
    if not _subscribers:
        return
    item: Event = (event, data)
    for subscription in tuple(_subscribers):
        try:
            subscription._deliver(item)
        except Exception:  # pragma: no cover - a subscriber bound to a dead loop
            log.debug("dropping dead subscriber for %s", event, exc_info=True)
            subscription.close()


def subscriber_count() -> int:
    return len(_subscribers)


def close_all() -> None:
    """Shutdown hook: end every open stream so the lifespan can finish."""
    for subscription in tuple(_subscribers):
        subscription.close()


__all__ = [
    "EVENT_NAMES",
    "JOB_CREATED",
    "JOB_FINISHED",
    "JOB_PROGRESS",
    "JOB_STARTED",
    "QUEUE_MAXSIZE",
    "QUEUE_STATE",
    "Event",
    "Subscription",
    "close_all",
    "publish",
    "subscribe",
    "subscriber_count",
]
