from __future__ import annotations

import asyncio
import contextvars
import threading
import uuid
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any

from redlotus.infra.shared_http import close_all_clients
from redlotus.runtime.context import WorkspaceContext, execution_role, workspace_context


@dataclass(frozen=True)
class SubagentSpec:
    session_id: str
    turn_id: str | None
    workspace: WorkspaceContext
    role: str = "worker"


class SubagentHandle:
    """One Agent, one execution thread, one loop, and an idempotent cancel path."""

    def __init__(
        self,
        spec: SubagentSpec,
        execute: Callable[[], Awaitable[Any]],
    ) -> None:
        self.id = uuid.uuid4().hex
        self.spec = spec
        self._execute = execute
        self._future: Future = Future()
        self._cancelled = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        context = contextvars.copy_context()
        self.thread = threading.Thread(
            target=lambda: context.run(self._run),
            name=f"subagent-{self.id[:8]}",
            daemon=True,
        )

    def start(self) -> None:
        try:
            self.thread.start()
        except RuntimeError as exc:
            self._future.set_exception(exc)
            raise

    def _run(self) -> None:
        async def execute():
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.current_task()
            if self._cancelled.is_set():
                raise asyncio.CancelledError()
            try:
                with (
                    workspace_context(self.spec.workspace),
                    execution_role(self.spec.role),
                ):
                    return await self._execute()
            finally:
                await close_all_clients()

        try:
            result = asyncio.run(execute())
        except asyncio.CancelledError:
            self._future.cancel()
        except BaseException as exc:
            self._future.set_exception(exc)
        else:
            self._future.set_result(result)
        finally:
            self._task = None
            self._loop = None

    def cancel(self) -> None:
        if self._cancelled.is_set():
            return
        self._cancelled.set()
        loop, task = self._loop, self._task
        if loop is not None and task is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # The thread finished between the check and scheduling.

    async def result(self):
        future = asyncio.wrap_future(self._future)
        future.add_done_callback(
            lambda done: None if done.cancelled() else done.exception()
        )
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            self.cancel()
            raise

    async def close(self) -> None:
        if not self._future.done():
            self.cancel()
        # Yield to the UI and other threads while cooperative cleanup finishes.
        while self.thread.is_alive():
            await asyncio.sleep(0.01)
        if not self._future.cancelled():
            self._future.exception()  # Retrieve errors even when its caller was cancelled.


class SubagentFactory:
    """Own the concurrency limit and all live child handles on the caller's loop."""

    def __init__(self, max_concurrent: int = 3) -> None:
        self._slots = asyncio.Semaphore(max(1, max_concurrent))
        self._handles: dict[str, SubagentHandle] = {}
        self._generation = 0
        self._closed = False
        self._releases: set[asyncio.Task] = set()
        self._background_slots = threading.BoundedSemaphore(max(1, max_concurrent))
        self._background_handles: dict[str, SubagentHandle] = {}
        self._background_lock = threading.Lock()

    def start_background(
        self, spec: SubagentSpec, execute: Callable[[], Awaitable[Any]]
    ):
        """Run background work until completion or an explicit factory shutdown."""
        if self._closed:
            raise RuntimeError("Agent factory is closed")

        async def bounded():
            while not self._background_slots.acquire(blocking=False):
                await asyncio.sleep(0.01)
            try:
                return await execute()
            finally:
                self._background_slots.release()

        handle = SubagentHandle(spec, bounded)

        def finished(future):
            with self._background_lock:
                self._background_handles.pop(handle.id, None)
            if not future.cancelled():
                future.exception()

        with self._background_lock:
            self._background_handles[handle.id] = handle
        handle._future.add_done_callback(finished)
        handle.start()
        return handle

    @property
    def handles(self) -> tuple[SubagentHandle, ...]:
        return tuple(self._handles.values())

    async def run(self, spec: SubagentSpec, execute: Callable[[], Awaitable[Any]]):
        generation = self._generation
        await self._slots.acquire()
        if self._closed or generation != self._generation:
            self._slots.release()
            raise asyncio.CancelledError()
        handle = SubagentHandle(spec, execute)
        self._handles[handle.id] = handle
        try:
            handle.start()
            return await handle.result()
        finally:
            release = asyncio.create_task(self._release(handle))
            self._releases.add(release)
            release.add_done_callback(self._releases.discard)
            await asyncio.shield(release)

    async def _release(self, handle: SubagentHandle) -> None:
        try:
            await handle.close()
        finally:
            self._handles.pop(handle.id, None)
            self._slots.release()

    async def cancel_all(self) -> None:
        self._generation += 1
        handles = self.handles
        for handle in handles:
            handle.cancel()
        await asyncio.gather(*(handle.close() for handle in handles))
        await asyncio.gather(*list(self._releases))

    async def cancel_turn(self, turn_id: str) -> None:
        handles = [handle for handle in self.handles if handle.spec.turn_id == turn_id]
        for handle in handles:
            handle.cancel()
        await asyncio.gather(*(handle.close() for handle in handles))

    async def cancel_session(self, session_id: str) -> None:
        handles = [
            handle for handle in self.handles if handle.spec.session_id == session_id
        ]
        for handle in handles:
            handle.cancel()
        await asyncio.gather(*(handle.close() for handle in handles))

    async def close(self) -> None:
        handles = self.stop()
        await asyncio.gather(*(handle.close() for handle in handles))
        await asyncio.gather(*list(self._releases))

    def stop(self) -> tuple[SubagentHandle, ...]:
        """Close admission and cancel every owned task before awaiting any cleanup."""
        self._closed = True
        self._generation += 1
        with self._background_lock:
            handles = (*self.handles, *self._background_handles.values())
        for handle in handles:
            handle.cancel()
        return handles
