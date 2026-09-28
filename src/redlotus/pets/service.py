"""Async ownership of one optional desktop child; importing this module never loads Qt."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from redlotus.runtime.resources import finish_io


@dataclass(frozen=True)
class PetStatus:
    state: str
    character: str
    error: str = ""
    pid: int | None = None

    def __str__(self):
        label = {"off": "已关闭", "starting": "启动中", "running": "运行中",
                 "stopping": "关闭中", "failed": "失败"}[self.state]
        return f"桌宠：{label} · {self.character}" + (f"\n{self.error}" if self.error else "")


class PetService(ABC):
    @abstractmethod
    async def start(self, character: str | None = None) -> PetStatus: ...

    @abstractmethod
    async def stop(self) -> PetStatus: ...

    @abstractmethod
    async def toggle(self) -> PetStatus: ...

    @abstractmethod
    async def status(self) -> PetStatus: ...

    @abstractmethod
    async def close(self) -> PetStatus:
        """Permanently end this controller's ownership when RedLotus exits."""

    async def select(self, character: str) -> PetStatus:
        return await self.start(character)

    async def command(self, parts: list[str]) -> str:
        if not parts:
            return str(await self.toggle())
        action, *arguments = parts
        if action == "on" and len(arguments) <= 1:
            try:
                return str(await self.start(arguments[0] if arguments else None))
            except ValueError as exc:
                return str(exc)
        if not arguments and action in {"off", "status"}:
            return str(await (self.stop() if action == "off" else self.status()))
        return "用法：/pets [on [charcoal|ivory] | off | status]"


class ProcessPetService(PetService):
    START_TIMEOUT = 15
    STOP_TIMEOUT = 2

    def __init__(self):
        self._selected = "charcoal"
        self._closed = False
        self._target = None
        self._state, self._error, self._stderr = "off", "", ""
        self._process = self._operation = self._reader = self._watcher = None
        self._revision = 0
        self._admission, self._serial = asyncio.Lock(), asyncio.Lock()
        self._tasks = set()

    async def status(self):
        return PetStatus(self._state, self._target or self._selected, self._error,
                         self._process.pid if self._process else None)

    async def start(self, character=None):
        return await self._request("on", character)

    async def stop(self):
        return await self._request("off", None)

    async def toggle(self):
        return await self._request("toggle", None)

    async def close(self):
        async with self._admission:
            self._closed = True
        return await self.stop()

    def _track(self, coroutine, name):
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task):
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def _request(self, action, character):
        from .model import CHARACTERS

        if character is not None and character not in CHARACTERS:
            raise ValueError("未知桌宠角色；可选角色：charcoal、ivory")
        async with self._admission:
            enabled = action == "on" or (action == "toggle" and self._state not in {"starting", "running"})
            if enabled and self._closed:
                return await self.status()
            target = (character or self._selected) if enabled else None
            pending = self._operation is not None and not self._operation.done()
            reusable = (pending and not self._operation.cancelling()) or (not pending and self._state in {"off", "running"})
            same = target == self._target and reusable
            if same:
                task = self._operation
            else:
                if pending and not self._operation.cancelling():
                    self._operation.cancel()
                self._revision += 1
                self._target, self._error = target, ""
                self._state = "starting" if target else "stopping"
                task = self._track(self._change(target, self._revision), "pets-transition")
                self._operation = task
        if task is not None:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    await finish_io(self._cancel_request(task))
                    raise
                # Another command superseded this operation; it owns the final state.
        return await self.status()

    async def _cancel_request(self, task):
        if not task.done() and not task.cancelling():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if self._operation is task:
            await self.stop()

    async def _change(self, character, revision):
        async with self._serial:
            try:
                await finish_io(self._stop_owned())
                if revision != self._revision:
                    return
                if character is None:
                    self._state = "off"
                    return
                # Draining cancellation retains the handle even if process creation finishes late.
                await finish_io(self._launch(character))
                line = await asyncio.wait_for(self._process.stdout.readline(), self.START_TIMEOUT)
                if not line:
                    raise RuntimeError(self._stderr.strip() or "桌宠在就绪前退出")
                message = json.loads(line)
                if message.get("event") != "ready" or message.get("character") != character:
                    raise RuntimeError(message.get("error", "桌宠返回了无效就绪回执"))
                if revision == self._revision:
                    self._selected, self._state = character, "running"
                    self._watcher = self._track(self._watch(self._process), "pets-exit")
            except asyncio.CancelledError:
                await finish_io(self._stop_owned())
                if revision == self._revision:
                    self._state, self._target = "off", None
                raise
            except Exception as exc:
                await finish_io(self._stop_owned())
                if revision == self._revision:
                    self._state = "failed"
                    self._error = "桌宠启动超时（15 秒）" if isinstance(exc, TimeoutError) else str(exc)

    def _command(self, character):
        entry = ["--pets-child"] if getattr(sys, "frozen", False) else ["-m", "redlotus.pets.desktop"]
        return [sys.executable, *entry, character]

    async def _launch(self, character):
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self._stderr = ""
        source = None if getattr(sys, "frozen", False) else str(Path(__file__).parents[2])
        env = dict(os.environ, PYTHONPATH=source) if source else None
        self._process = await asyncio.create_subprocess_exec(
            *self._command(character), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=flags, cwd=source, env=env)
        self._reader = self._track(self._read_stderr(self._process), "pets-stderr")

    async def _read_stderr(self, process):
        while chunk := await process.stderr.read(4096):
            message = chunk.decode("utf-8", errors="replace")
            self._stderr = (self._stderr + message)[-4096:]
            logging.getLogger(__name__).debug("Desktop pet: %s", message.rstrip())

    async def _watch(self, process):
        code = await process.wait()
        if self._reader:
            await asyncio.gather(self._reader, return_exceptions=True)
        if self._process is process and self._state == "running":
            self._process = None
            self._state = "off" if code == 0 else "failed"
            self._error = "" if code == 0 else f"桌宠异常退出（{code}）\n{self._stderr.strip()}"
            self._target = None if code == 0 else self._target

    async def _stop_owned(self):
        process = self._process
        if process is None:
            return
        self._process = None
        if process.stdin:
            process.stdin.close()
        waiter = asyncio.create_task(process.wait(), name="pets-reap")
        try:
            await asyncio.wait_for(asyncio.shield(waiter), self.STOP_TIMEOUT)
        except TimeoutError:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(asyncio.shield(waiter), self.STOP_TIMEOUT)
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await waiter
        await asyncio.gather(*(t for t in (self._reader, self._watcher) if t), return_exceptions=True)
        self._reader = self._watcher = None
