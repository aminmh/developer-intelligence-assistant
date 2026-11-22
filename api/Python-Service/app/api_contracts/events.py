"""Live delivery of contract change events to consumer projects' listeners (IDE extensions).

Spec: @.archcore/frontend-assist/contract-change-notifications.spec.md (delivery).

Modelled on @api/Python-Service/app/telemetry/events.py: an in-process publish/subscribe that is
safe to publish into from any thread. Unlike runtime invalidations, each change event carries data
and is sent to the streams of the consumer projects it concerns only; nothing is coalesced.
A listener that misses events (disconnected, or the process restarted) catches up with the
polling route, using the last ``id`` it saw.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, AsyncIterator, Iterable

KEEPALIVE_SECONDS = 25.0
QUEUE_LIMIT = 100
HEADLINE_ITEMS = 5

_lock = threading.Lock()
# Each open stream: its loop, its queue, and the consumer projects it listens for.
_subscribers: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue, frozenset[str]]] = set()


def headline(event: dict[str, Any], consumer_project_id: str) -> dict[str, Any]:
    """The compact form pushed to listeners: no full item list, the most severe items first."""
    items = [item for item in event.get("items") or [] if item["severity"] != "safe"][:HEADLINE_ITEMS]
    return {**{key: value for key, value in event.items() if key != "items"},
            "consumer_project_id": consumer_project_id, "headline": items}


def _offer(queue: asyncio.Queue, message: str) -> None:
    try:
        queue.put_nowait(message)
    except asyncio.QueueFull:  # a stalled listener: drop; it backfills through polling
        pass


def publish_change(event: dict[str, Any], consumer_project_ids: Iterable[str]) -> int:
    """Send ``event`` to every stream listening for one of ``consumer_project_ids``. Returns streams reached."""
    projects = set(consumer_project_ids)
    if not projects:
        return 0
    with _lock:
        subscribers = tuple(_subscribers)
    reached = 0
    for loop, queue, wanted in subscribers:
        for project_id in sorted(projects & wanted):
            message = f"id: {event['seq']}\nevent: contract.changed\ndata: " \
                      f"{json.dumps(headline(event, project_id), separators=(',', ':'))}\n\n"
            try:
                loop.call_soon_threadsafe(_offer, queue, message)
                reached += 1
            except RuntimeError:  # the stream's loop has closed
                pass
    return reached


async def change_events(project_ids: Iterable[str], keepalive: float = KEEPALIVE_SECONDS) -> AsyncIterator[str]:
    """Server-sent events for the given consumer projects until the client disconnects."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_LIMIT)
    subscriber = (loop, queue, frozenset(project_ids))
    with _lock:
        _subscribers.add(subscriber)
    try:
        yield f"event: connected\ndata: {json.dumps({'projects': sorted(subscriber[2])})}\n\n"
        while True:
            try:
                yield await asyncio.wait_for(queue.get(), timeout=keepalive)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
    finally:
        with _lock:
            _subscribers.discard(subscriber)


def subscriber_count() -> int:
    with _lock:
        return len(_subscribers)
