"""``EmbeddingSyncLoop``'s polling/start/stop/error-isolation contract.

Structurally the same tests as ``tc_worker.khoj_sync_loop.KhojSyncLoop`` would
need (there is no ``test_khoj_sync_loop.py`` to mirror directly), driven with
a bare async callable stand-in for ``DeliverEmbeddingSync`` rather than the
real outbox/source/embed/writer stack - this loop only cares that ``deliver``
is an awaitable returning an int, and that exceptions from it never escape.
"""

from __future__ import annotations

import asyncio

import pytest

from tc_worker.embedding_sync_loop import EmbeddingSyncLoop


class RecordingDeliver:
    def __init__(self) -> None:
        self.calls = 0
        self.event = asyncio.Event()

    async def __call__(self) -> int:
        self.calls += 1
        self.event.set()
        return 0


class RaisingDeliver:
    def __init__(self) -> None:
        self.calls = 0
        self.event = asyncio.Event()

    async def __call__(self) -> int:
        self.calls += 1
        self.event.set()
        raise RuntimeError("boom: token=abc123")


async def test_start_is_idempotent_and_polls_at_least_once() -> None:
    deliver = RecordingDeliver()
    loop = EmbeddingSyncLoop(deliver, interval=0.01)

    loop.start()
    task = loop._task
    loop.start()  # second call must not create a second task

    assert loop._task is task

    await asyncio.wait_for(deliver.event.wait(), timeout=1.0)
    loop.stop()


async def test_stop_without_start_does_not_error() -> None:
    loop = EmbeddingSyncLoop(RecordingDeliver(), interval=0.01)

    loop.stop()  # must not raise

    assert loop._task is None


async def test_stop_is_idempotent() -> None:
    deliver = RecordingDeliver()
    loop = EmbeddingSyncLoop(deliver, interval=0.01)
    loop.start()
    await asyncio.wait_for(deliver.event.wait(), timeout=1.0)

    loop.stop()
    loop.stop()  # calling twice must not raise

    assert loop._task is None


async def test_a_raising_deliver_does_not_kill_the_loop() -> None:
    deliver = RaisingDeliver()
    loop = EmbeddingSyncLoop(deliver, interval=0.01)

    loop.start()
    await asyncio.wait_for(deliver.event.wait(), timeout=1.0)
    deliver.event.clear()
    # A second poll cycle happening at all proves the first cycle's
    # exception was caught rather than propagating out of ``_run`` and
    # ending the task.
    await asyncio.wait_for(deliver.event.wait(), timeout=1.0)

    assert deliver.calls >= 2
    loop.stop()


async def test_stop_cancels_the_underlying_task_cleanly() -> None:
    deliver = RecordingDeliver()
    loop = EmbeddingSyncLoop(deliver, interval=10.0)
    loop.start()
    await asyncio.wait_for(deliver.event.wait(), timeout=1.0)
    task = loop._task
    assert task is not None

    loop.stop()

    with pytest.raises(asyncio.CancelledError):
        await task
