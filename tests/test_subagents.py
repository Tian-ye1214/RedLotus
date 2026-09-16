import asyncio
import threading

import pytest

from redlotus.core.agents import WorkspaceContext
from redlotus.core.agents import SubagentFactory
from redlotus.core.agents import SubagentSpec


async def test_thread_limit_queue_and_disposal(tmp_path):
    factory = SubagentFactory(3)
    spec = SubagentSpec("session", "turn", WorkspaceContext.from_path(tmp_path))
    barrier = threading.Barrier(3)
    seen = []

    async def work():
        seen.append((threading.get_ident(), id(asyncio.get_running_loop())))
        await asyncio.to_thread(barrier.wait, 2)
        return threading.current_thread().name

    values = await asyncio.wait_for(
        asyncio.gather(*(factory.run(spec, work) for _ in range(3))), 5
    )
    assert len({row[0] for row in seen}) == 3
    assert len({row[1] for row in seen}) == 3
    assert len(set(values)) == 3
    assert not factory.handles
    await factory.close()


async def test_cancel_releases_active_and_discards_queued(tmp_path):
    factory = SubagentFactory(1)
    spec = SubagentSpec("session", "turn", WorkspaceContext.from_path(tmp_path))
    started = threading.Event()
    released = threading.Event()
    count = []

    async def work():
        count.append(1)
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            released.set()

    jobs = [asyncio.create_task(factory.run(spec, work)) for _ in range(2)]
    for _ in range(100):
        if started.is_set():
            break
        await asyncio.sleep(0.01)
    handles = factory.handles
    await factory.cancel_all()
    result = await asyncio.gather(*jobs, return_exceptions=True)
    assert all(isinstance(r, asyncio.CancelledError) for r in result)
    assert len(count) == 1
    assert released.is_set()
    assert all(h.thread is None or not h.thread.is_alive() for h in handles)
    await factory.close()


async def test_repeated_cancel_keeps_slot_until_cleanup_finishes(tmp_path):
    factory = SubagentFactory(1)
    spec = SubagentSpec("s", "t", WorkspaceContext.from_path(tmp_path))
    started, cleaning, finished, second_started = (threading.Event() for _ in range(4))

    async def first():
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            cleaning.set()
            await asyncio.sleep(0.2)
            finished.set()

    async def second():
        assert finished.is_set()
        second_started.set()
        return "ok"

    task = asyncio.create_task(factory.run(spec, first))
    async with asyncio.timeout(5):
        while not started.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        while not cleaning.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        next_task = asyncio.create_task(factory.run(spec, second))
        await asyncio.sleep(0.03)
        assert not second_started.is_set()
        assert await next_task == "ok"
        await asyncio.gather(task, return_exceptions=True)
    await factory.close()
    assert not factory.handles


async def test_child_exception_closes_its_http_pool(tmp_path):
    import httpx
    from redlotus.core.config import get_client
    from redlotus.core.config import close_all_clients

    factory = SubagentFactory()
    spec = SubagentSpec("s", "t", WorkspaceContext.from_path(tmp_path))
    parent = get_client("test", httpx.AsyncClient)
    clients = []

    async def child():
        client = get_client("test", httpx.AsyncClient)
        assert client is get_client("test", httpx.AsyncClient)
        assert client is not parent
        clients.append(client)
        raise ValueError("child failed")

    with pytest.raises(ValueError, match="child failed"):
        await factory.run(spec, child)
    assert clients[0].is_closed and not parent.is_closed
    await factory.close()
    await close_all_clients()


async def test_worker_memory_calls_progress_when_worker_slots_are_full(tmp_path):
    from redlotus.memory.service import MemoryService

    workspace = WorkspaceContext.from_path(tmp_path)
    memory = MemoryService(workspace=workspace)
    workers = SubagentFactory(3)
    owner = asyncio.get_running_loop()
    ready = threading.Barrier(3)
    task = SubagentSpec("s", "t", workspace)
    perception = SubagentSpec("memory:s", None, workspace, role="perception")

    async def produce():
        return "persisted"

    async def worker():
        await asyncio.to_thread(ready.wait, 3)
        future = asyncio.run_coroutine_threadsafe(
            memory._perception_factory.run(perception, produce), owner
        )
        return await asyncio.wrap_future(future)

    try:
        outputs = await asyncio.wait_for(
            asyncio.gather(*(workers.run(task, worker) for _ in range(3))), 5
        )
        assert outputs == ["persisted"] * 3
        assert not workers.handles and not memory._perception_factory.handles
    finally:
        await workers.close()
        await memory.close()
