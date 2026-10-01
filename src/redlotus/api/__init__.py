"""Channel contracts and adapters for the shared command controller."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from rich.console import Console

if TYPE_CHECKING:
    from pydantic_ai import BinaryContent
    from redlotus.TTS import Transcript
    from redlotus.sessions.context import UserMessage


@dataclass
class InputDelivery:
    message: UserMessage
    reply: Callable[[str], Awaitable[None]]
    loop: asyncio.AbstractEventLoop
    prepare: Callable[[], Awaitable[list[BinaryContent | Transcript]]] | None
    turn_id: str


@dataclass(frozen=True)
class OutboundFile:
    path: Path
    data: bytes
    media_type: str

    @classmethod
    def read(cls, path: Path):
        from redlotus.api.media import mime_magic
        from redlotus.runtime.network import ModelInputPolicy

        policy = ModelInputPolicy.for_role()
        policy.check([path.stat().st_size])
        with path.open("rb") as stream:
            data = stream.read(policy.max_file_bytes + 1)
        policy.check([len(data)])
        return cls(path, data, mime_magic(data) or "application/octet-stream")


class ChannelSender(ABC):
    """The runtime binds the recipient; tools can supply only existing file content."""

    @abstractmethod
    async def send_file(self, item: OutboundFile) -> str:
        raise NotImplementedError


class LocalSender(ChannelSender):
    async def send_file(self, item: OutboundFile) -> str:
        from redlotus.ui.presentation import print_markdown

        link = f"[{item.path.name}](<{item.path.as_posix()}>)"
        print_markdown(link)
        return f"已展示本地文件入口：{link}"


class LoopChannelSender(ChannelSender):
    def __init__(self, sender: ChannelSender, loop):
        from redlotus.runtime.resources import bind_to_loop
        self.send = bind_to_loop(sender.send_file, loop)

    async def send_file(self, item: OutboundFile) -> str:
        return await self.send(item)


class BoundChannelSender(ChannelSender):
    """Reject sends from an expired turn, including child tools on another loop."""

    def __init__(self, recipient, is_current):
        self.recipient, self.is_current = recipient, is_current

    async def send_file(self, item: OutboundFile) -> str:
        if not self.is_current():
            raise asyncio.CancelledError()
        sender = self.recipient()
        if sender is None:
            raise ValueError("当前渠道没有可用的文件发送接口")
        return await sender.send_file(item)


class CommandOutput:
    """Render one command's output without redirecting another session's console."""

    supports_model_stream = False

    def __init__(self):
        self.buffer = StringIO()
        self.console = Console(file=self.buffer, width=120, color_system=None)

    def emit(self, renderable):
        self.console.print(renderable)

    def update(self, action, *args):
        if action == "rule":
            self.console.rule(*args)

    @property
    def text(self):
        return self.buffer.getvalue().strip()


class ChannelCommands:
    """Only channel interaction differs; all slash actions use CommandDispatcher."""

    is_chat = True

    def __init__(self, bot, identity, state, reply):
        self.bot, self.identity, self.state, self.reply = bot, identity, state, reply
        self.system = bot._agent_for_session(identity)
        self.is_owner = bot._is_owner(identity)
        self.voice_problem = getattr(reply, "speech_error", None)

    @property
    def is_transitioning(self):
        return self.state.control_busy

    async def run(self, raw):
        from redlotus.runtime.resources import bind_context, workspace_context
        from redlotus.ui.cli_commands import CommandDispatcher
        from redlotus.ui.presentation import OUTPUT_SINK

        if self.state.control_busy and raw.split()[0] not in {"/stop", "/clear", "/help"}:
            await self.reply("会话正在切换，请稍后重试。")
            return
        capture = CommandOutput()
        workspace = getattr(self.system, "workspace", None)
        with bind_context(OUTPUT_SINK, capture), workspace_context(workspace):
            token = self.bot._agent_ctx.set((self.identity, self.state, self.reply,
                                           asyncio.get_running_loop(), self.state.generation))
            try:
                self.bot._bind_voice_output(self.identity, self.state, self.reply, self.state.generation)
                await CommandDispatcher(self, self.state, raw).run()
            except (OSError, ValueError) as exc:
                capture.emit(f"命令未执行：{exc}")
            finally:
                self.bot._agent_ctx.reset(token)
        if capture.text and self.bot._sessions.get(self.identity) is self.state:
            await self.reply(capture.text)

    async def reset_session(self, history):
        await self.bot._reset_session(self.identity)
        self.state = self.bot._session(self.identity)

    async def resume_current_turn(self, state):
        return await state.resume(self.system,
            lambda message, admission, data: self.bot._consume_turn(self.identity, state, message, admission, data),
            lambda: None)

    async def enter_current_workspace(self, *, state, force_picker=False, workspace=None, choice=None):
        from redlotus.runtime.resources import session_data_dir
        from redlotus.sessions.storage import list_workspace_snapshots, SnapshotSelection, SnapshotAction
        from redlotus.ui.presentation import print_message

        state.control_busy = True
        try:
            if workspace is not None:
                await self.system.switch_workspace(workspace, update_default=False)
                state.history.reset()
                state.deliveries.clear()
                return False
            if choice is None:
                state.snapshot_choices = await asyncio.to_thread(list_workspace_snapshots,
                    root=session_data_dir(self.system.workspace))
                print_message("当前会话可加载的记录：\n" + "\n".join(
                    f"{index}. {item.label}" for index, item in enumerate(state.snapshot_choices, 1))
                    + "\n使用 /load <编号或会话ID>，/load 0 新建。")
                return None
            snapshots = getattr(state, "snapshot_choices", [])
            selection = SnapshotSelection.resolve(choice, snapshots)
            if selection.action == SnapshotAction.CANCEL:
                print_message("已取消加载，会话保持不变。")
                return None
            if selection.action == SnapshotAction.NEW:
                await self.reset_session(state.history)
                print_message("已新建会话。")
                return False
            chosen = selection.snapshot
            await self.system.bind_loaded_snapshot(chosen.path, state=state, title=chosen.title)
            state.deliveries.clear()
            print_message(f"已加载会话：{chosen.title}")
            return True
        finally:
            state.control_busy = False
