"""Async ownership of one optional desktop child; importing this module never loads Qt."""
from __future__ import annotations

import asyncio
import json
import logging
import math
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
    scale: float = 1

    def __str__(self):
        label = {"off": "已关闭", "starting": "启动中", "running": "运行中",
                 "stopping": "关闭中", "failed": "失败"}[self.state]
        return f"桌宠：{label} · {self.character} · {self.scale:.0%}" + (f"\n{self.error}" if self.error else "")


class PetService(ABC):
    @abstractmethod
    async def publish_reply(self, *, reply_id: str, phase: str, text: str) -> None:
        """Publish a process-wide increasing decimal reply ID; done replaces its body."""

    @abstractmethod
    async def clear_reply(self) -> None: ...

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
        """Control quietly; return plain text only for a query or an error."""
        try:
            if not parts:
                result = await self.toggle()
            elif parts == ["status"]:
                return str(await self.status())
            elif parts == ["off"]:
                result = await self.stop()
            elif parts[0] == "on" and len(parts) <= 2:
                result = await self.start(parts[1] if len(parts) == 2 else None)
            else:
                return "用法：/pets [on [charcoal|ivory] | off | status]"
        except ValueError as exc:
            return str(exc)
        return result.error or ("桌宠操作失败" if result.state == "failed" else "")


class ProcessPetService(PetService):
    START_TIMEOUT = 15
    STOP_TIMEOUT = 2
    WRITE_TIMEOUT = 1
    FRAME_INTERVAL = .05
    MAX_REPLY = 32768
    MAX_LINE = 262144

    def __init__(self):
        self._selected = "charcoal"
        self._closed = False
        self._target = None
        self._state, self._error, self._stderr = "off", "", ""
        self._process = self._operation = self._reader = self._watcher = None
        self._revision = 0
        self._admission, self._serial = asyncio.Lock(), asyncio.Lock()
        self._tasks = set()
        self._scale = 1.0
        self._stdout_reader = self._sender = self._ready = None
        self._outgoing = asyncio.Event()
        self._reply_seq = 0
        self._last_reply_number = 0
        self._reset_reply()

    def _reset_reply(self):
        self._reply_id = None
        self._reply_text, self._reply_phase = "", "clear"
        self._truncated = False
        self._pending_reply = None
        self._outgoing.clear()

    def _queue_reply(self):
        self._reply_seq += 1
        self._pending_reply = dict(event="reply", reply_id=self._reply_id or "",
                                   phase=self._reply_phase, text=self._reply_text,
                                   truncated=self._truncated, seq=self._reply_seq)
        self._outgoing.set()

    async def clear_reply(self):
        self._reset_reply()
        if self._state == "running":
            self._queue_reply()

    async def publish_reply(self, *, reply_id, phase, text):
        if phase not in {"start", "delta", "done", "cancelled", "failed"}:
            raise ValueError("Unknown pet reply phase")
        if (not isinstance(reply_id, str) or not 1 <= len(reply_id) <= 20
                or not reply_id.isascii() or not reply_id.isdecimal() or not isinstance(text, str)):
            raise ValueError("A pet reply requires a decimal ID and text")
        if phase == "start":
            if int(reply_id) <= self._last_reply_number:
                return
            self._last_reply_number = int(reply_id)
        if self._state != "running" or self._closed:
            return
        if phase == "start":
            self._reset_reply()
            self._reply_id, self._reply_phase = reply_id, "streaming"
        elif reply_id != self._reply_id or self._reply_phase != "streaming":
            return
        if phase == "delta":
            self._truncated |= len(self._reply_text) + len(text) > self.MAX_REPLY
            self._reply_text = (self._reply_text + text[-self.MAX_REPLY:])[-self.MAX_REPLY:]
        elif phase == "done":
            self._reply_text, self._truncated = text[-self.MAX_REPLY:], len(text) > self.MAX_REPLY
        if phase in {"done", "cancelled", "failed"}:
            self._reply_phase = phase
        self._queue_reply()

    async def status(self):
        return PetStatus(self._state, self._target or self._selected, self._error,
                         self._process.pid if self._process else None, self._scale)

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
                await asyncio.wait_for(asyncio.shield(self._ready), self.START_TIMEOUT)
                if revision == self._revision:
                    self._selected, self._state = character, "running"
                    self._watcher = self._track(self._watch(self._process), "pets-exit")
                    self._sender = self._track(self._send_replies(self._process), "pets-replies")
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
        return [sys.executable, *entry, character, *(["--scale", str(self._scale)] if self._scale != 1 else [])]

    async def _launch(self, character):
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self._stderr = ""
        source = None if getattr(sys, "frozen", False) else str(Path(__file__).parents[2])
        env = dict(os.environ, PYTHONPATH=source) if source else None
        self._process = await asyncio.create_subprocess_exec(
            *self._command(character), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=flags, cwd=source, env=env)
        self._ready = asyncio.get_running_loop().create_future()
        self._ready.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        self._reader = self._track(self._read_stderr(self._process), "pets-stderr")
        self._stdout_reader = self._track(self._read_stdout(self._process, character, self._ready), "pets-receipts")

    async def _read_stdout(self, process, character, ready):
        try:
            while line := await process.stdout.readline():
                message = json.loads(line)
                if process is not self._process:
                    return
                event = message.get("event")
                if event == "ready" and message.get("character") == character and not ready.done():
                    ready.set_result(None)
                elif event == "scale" and ready.done():
                    scale = message.get("scale")
                    if type(scale) not in {int, float} or not math.isfinite(scale) or not .5 <= scale <= 3:
                        raise ValueError("Invalid pet scale receipt")
                    self._scale = float(scale)
                else:
                    raise RuntimeError(message.get("error", "桌宠返回了无效回执"))
            if not ready.done():
                raise RuntimeError(self._stderr.strip() or "桌宠在就绪前退出")
            await asyncio.wait_for(process.wait(), self.STOP_TIMEOUT)
        except Exception as exc:
            if not ready.done():
                ready.set_exception(exc)
            else:
                await self._channel_failed(process, exc)

    async def _send_replies(self, process):
        sent = 0
        loop = asyncio.get_running_loop()
        try:
            while process is self._process:
                await self._outgoing.wait()
                await asyncio.sleep(max(0, sent + self.FRAME_INTERVAL - loop.time()))
                message, self._pending_reply = self._pending_reply, None
                self._outgoing.clear()
                if message is None:
                    continue
                data = await finish_io(asyncio.to_thread(json.dumps, message, ensure_ascii=False))
                if process is not self._process or self._state != "running":
                    return
                if message["reply_id"] != (self._reply_id or ""):
                    continue
                packet = data.encode("utf-8") + b"\n"
                if len(packet) > self.MAX_LINE:
                    raise ValueError("Pet reply exceeds the pipe frame limit")
                process.stdin.write(packet)
                await asyncio.wait_for(process.stdin.drain(), self.WRITE_TIMEOUT)
                sent = loop.time()
        except Exception as exc:
            await self._channel_failed(process, exc)

    async def _channel_failed(self, process, error):
        async with self._serial:
            if process is self._process:
                await self._stop_owned()
                self._state = "failed"
                detail = "发送超时" if isinstance(error, TimeoutError) else str(error)
                self._error = f"桌宠通信失败：{detail}"

    async def _read_stderr(self, process):
        while chunk := await process.stderr.read(4096):
            message = chunk.decode("utf-8", errors="replace")
            self._stderr = (self._stderr + message)[-4096:]
            logging.getLogger(__name__).debug("Desktop pet: %s", message.rstrip())

    async def _watch(self, process):
        code = await process.wait()
        if self._reader:
            await asyncio.gather(self._reader, return_exceptions=True)
        async with self._serial:
            if self._process is process and self._state == "running":
                await self._stop_owned()
                self._state = "off" if code == 0 else "failed"
                self._error = "" if code == 0 else f"桌宠异常退出（{code}）\n{self._stderr.strip()}"
                self._target = None if code == 0 else self._target

    async def _stop_owned(self):
        process = self._process
        self._reset_reply()
        if process is None:
            return
        self._process = None
        current = asyncio.current_task()
        tasks = [t for t in (self._sender, self._stdout_reader, self._reader, self._watcher) if t and t is not current]
        for task in (self._sender, self._stdout_reader, self._watcher):
            if task and task is not current:
                task.cancel()
        if self._ready is not None and not self._ready.done():
            self._ready.cancel()
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
        await asyncio.gather(*tasks, return_exceptions=True)
        self._reader = self._stdout_reader = self._sender = self._watcher = self._ready = None
