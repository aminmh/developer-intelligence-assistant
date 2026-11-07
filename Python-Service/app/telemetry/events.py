"""In-process invalidation events for active Runtime views.

Each event makes every open Runtime view refetch, and a refetch rebuilds the revision summary in
ClickHouse. Ingest publishes often (one profile upload per sampled request, host-agent reports), so
each stream sends at most one event per MIN_INTERVAL_SECONDS: changes inside the interval are
coalesced into one event sent when it ends, and the last change is never dropped.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import AsyncIterator

MIN_INTERVAL_SECONDS = 5.0
KEEPALIVE_SECONDS = 15.0
_MESSAGE = json.dumps({"type": "runtime.updated"}, separators=(",", ":"))

_lock = threading.Lock()
# Each open stream's wake-up event and the loop it belongs to; publish may run in a worker thread.
_subscribers: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()


def publish_runtime_update() -> None:
    """Notify listeners that aggregates changed; payload contains no project data. Safe from any thread."""
    with _lock:
        subscribers = tuple(_subscribers)
    for loop, event in subscribers:
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:  # the stream's loop has closed
            pass


async def runtime_events(min_interval: float = MIN_INTERVAL_SECONDS) -> AsyncIterator[str]:
    loop = asyncio.get_running_loop()
    changed = asyncio.Event()
    subscriber = (loop, changed)
    with _lock:
        _subscribers.add(subscriber)
    last_sent = float("-inf")
    try:
        yield "event: connected\ndata: {}\n\n"
        while True:
            try:
                await asyncio.wait_for(changed.wait(), timeout=KEEPALIVE_SECONDS)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            wait = last_sent + min_interval - time.monotonic()
            if wait > 0:
                # Further changes during this wait are folded into the event sent after it.
                await asyncio.sleep(wait)
            changed.clear()
            last_sent = time.monotonic()
            yield f"event: runtime.updated\ndata: {_MESSAGE}\n\n"
    finally:
        with _lock:
            _subscribers.discard(subscriber)
