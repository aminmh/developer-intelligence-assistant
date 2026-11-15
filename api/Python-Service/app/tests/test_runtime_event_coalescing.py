"""Ingest publishes often; open Runtime views must not refetch (and rebuild the summary) on every publish."""
import asyncio
import threading

from telemetry.events import publish_runtime_update, runtime_events
from telemetry.registry import SnapshotRegistry

IMAGE = "sha256:" + "c" * 64
HOST = "f" * 64


def _container(state="running", commit="a" * 40):
    return {"container_id": "d" * 64, "project_id": "pizza", "environment": "development", "service": "php",
            "commit_sha": commit, "image_id": IMAGE, "state": state}


def test_a_burst_of_updates_becomes_one_prompt_event_and_one_trailing_event():
    async def scenario():
        stream = runtime_events(min_interval=0.3)
        try:
            await stream.__anext__()  # connected
            publish_runtime_update()
            first = await asyncio.wait_for(stream.__anext__(), timeout=0.1)
            # 20 publishes, some from another thread, inside the interval.
            for _ in range(10):
                publish_runtime_update()
            worker = threading.Thread(target=lambda: [publish_runtime_update() for _ in range(10)])
            worker.start(); worker.join()
            loop = asyncio.get_running_loop()
            started = loop.time()
            second = await asyncio.wait_for(stream.__anext__(), timeout=1.0)
            waited = loop.time() - started
            # Nothing else is pending: the next item is a keep-alive, not another update.
            pending = asyncio.ensure_future(stream.__anext__())
            await asyncio.sleep(0.4)
            assert not pending.done()
            pending.cancel()
            try:
                await pending
            except (asyncio.CancelledError, StopAsyncIteration):
                pass
            return first, second, waited
        finally:
            await stream.aclose()

    first, second, waited = asyncio.run(scenario())
    assert "event: runtime.updated" in first and "event: runtime.updated" in second
    assert 0.15 < waited < 0.6


def test_repeated_identical_deployment_reports_are_not_changes(tmp_path):
    registry = SnapshotRegistry(tmp_path)

    _pairs, changed = registry.report_deployment_changes(HOST, [_container()], inventory=True)
    assert changed is True
    assert registry.report_deployment_changes(HOST, [_container()], inventory=True)[1] is False
    assert registry.report_deployment_changes(HOST, [_container(commit="b" * 40)], inventory=True)[1] is True
    assert registry.report_deployment_changes(HOST, [_container(commit="b" * 40)], inventory=True)[1] is False
    # The container disappearing from the inventory is a change.
    assert registry.report_deployment_changes(HOST, [], inventory=True)[1] is True
