"""In-process invalidation events for active Runtime views."""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator


_subscribers: set[asyncio.Queue[str]] = set()


def subscribe() -> asyncio.Queue[str]:
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=8)
    _subscribers.add(queue)
    return queue


def unsubscribe(queue: asyncio.Queue[str]) -> None:
    _subscribers.discard(queue)


def publish_runtime_update() -> None:
    """Notify listeners that aggregates changed; payload contains no project data."""
    message = json.dumps({"type": "runtime.updated"}, separators=(",", ":"))
    for queue in tuple(_subscribers):
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            pass


async def runtime_events() -> AsyncIterator[str]:
    queue = subscribe()
    try:
        yield "event: connected\ndata: {}\n\n"
        while True:
            try:
                message = await asyncio.wait_for(queue.get(), timeout=15)
                yield f"event: runtime.updated\ndata: {message}\n\n"
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
    finally:
        unsubscribe(queue)
