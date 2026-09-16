"""Queued Agent work must not allocate threads or bypass its session's quota."""

import asyncio
from pathlib import Path
import threading
import unittest
import pytest

from redlotus.core.agents import WorkspaceContext
from redlotus.core.agents import SubagentFactory
from redlotus.core.agents import SubagentSpec


class SessionThreadLimitTests(unittest.IsolatedAsyncioTestCase):
    def spec(self, session, role="worker"):
        return SubagentSpec(session, "turn", WorkspaceContext.from_path(Path.cwd()), role)

    async def test_waiting_background_jobs_do_not_allocate_threads(self):
        factory = SubagentFactory(2)
        started = []

        async def work():
            started.append(threading.get_ident())
            await asyncio.sleep(60)

        handles = [factory.start_background(self.spec("one", "perception"), work) for _ in range(7)]
        try:
            async with asyncio.timeout(3):
                while len(started) < 2:
                    await asyncio.sleep(0.01)
            self.assertEqual(sum(bool(h.thread and h.thread.is_alive()) for h in handles), 2)
            self.assertEqual(sum(h.thread is not None for h in handles), 2)
        finally:
            await asyncio.wait_for(factory.close(), 3)
        self.assertFalse(any(h.thread and h.thread.is_alive() for h in handles))

    async def test_worker_and_perception_share_the_same_session_slots(self):
        factory = SubagentFactory(1)
        started = []

        async def work(name):
            started.append(name)
            await asyncio.sleep(60)

        worker = asyncio.create_task(factory.run(self.spec("one"), lambda: work("worker")))
        try:
            async with asyncio.timeout(3):
                while not started:
                    await asyncio.sleep(0.01)
            memory = factory.start_background(self.spec("one", "perception"), lambda: work("memory"))
            await asyncio.sleep(0.03)
            self.assertEqual(started, ["worker"])
            self.assertIsNone(memory.thread)
            memory.cancel()
        finally:
            await asyncio.wait_for(factory.close(), 3)
            await asyncio.gather(worker, return_exceptions=True)

    async def test_different_sessions_have_independent_limits(self):
        factory = SubagentFactory(1)
        started = []

        async def work(name):
            started.append(name)
            await asyncio.sleep(60)

        jobs = [asyncio.create_task(factory.run(self.spec(name), lambda name=name: work(name))) for name in ("a", "b")]
        try:
            async with asyncio.timeout(1):
                while len(started) < 2:
                    await asyncio.sleep(0.01)
            self.assertEqual(set(started), {"a", "b"})
        finally:
            await asyncio.wait_for(factory.close(), 3)
            await asyncio.gather(*jobs, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()


@pytest.mark.asyncio
async def test_configured_sixteen_slots_keep_memory_bridge_live(tmp_path, monkeypatch):
    from redlotus.core.config import get_agent_run_policy
    from redlotus.core.agents import AgentRegistry
    from redlotus.memory.records import PerceptionResult
    from memory_helpers import new_memory

    limit = get_agent_run_policy().max_concurrent_threads_per_session
    assert limit == 16
    factory = SubagentFactory()
    workspace = WorkspaceContext.from_path(tmp_path)
    memory = new_memory(workspace=workspace, factory=factory)
    memory.bind_runner(AgentRegistry(), input_source=lambda: memory.current.user_inputs)
    await memory.begin_turn("session", "turn", "This is auxiliary concurrency evidence")
    assert memory.perception.factory is None
    entered = set()
    release = threading.Event()
    owner = asyncio.get_running_loop()

    async def produce(service, job):
        job.result = PerceptionResult(records=[], reason="Auxiliary empty production", request_authorized=True)

    monkeypatch.setattr("redlotus.memory.service.produce_job", produce)

    async def work(index):
        entered.add(threading.get_ident())
        while not release.is_set():
            await asyncio.sleep(.01)
        future = asyncio.run_coroutine_threadsafe(memory.remember(f"request {index}"), owner)
        return await asyncio.wrap_future(future)

    handles = [factory.start_background(SubagentSpec("session", "turn", workspace),
               lambda index=index: work(index)) for index in range(32)]
    try:
        async with asyncio.timeout(5):
            while len(entered) < limit:
                await asyncio.sleep(.01)
        assert sum(handle.thread is not None for handle in handles) == 16
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*(handle.result() for handle in handles)), 15)
        assert len(results) == 32 and all('"status": "superseded"' in value for value in results)
    finally:
        release.set()
        await memory.close()
        await factory.close()
    assert all(handle.thread is None or not handle.thread.is_alive() for handle in handles)
