"""Shared input completion, terminal controls, conversation picker and usage widgets."""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from prompt_toolkit.completion import Completer, Completion
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.suggester import Suggester
from textual.widgets import Button, Footer, Input, Label, OptionList, ProgressBar, Select, Static, Switch
from textual.widgets.option_list import Option

from redlotus.runtime.config import (
    get_agent_roles,
    role_supported_thinking_efforts,
    supported_thinking_efforts,
)
from redlotus.runtime import logging as logger
from redlotus.runtime.resources import current_workspace
from redlotus.sessions.control import iter_reference_spans, quote_reference_path
from redlotus.ui.cli_commands import (WorkspaceSnapshot, SnapshotAction, SnapshotSelection,
                                      format_snapshot_choices, format_voice_model_status, TuiRunMode)
from redlotus.ui.presentation import (
    visible_conversation_entries,
    render_panel,
)


COMMAND_HELP = {
    "/help": "显示本帮助",
    "/exit": "退出程序（也接受 quit、exit、退出）",
    "/quit": "退出程序",
    "/clear": "清空上下文并开启新对话（也接受“新任务”，旧快照保留）",
    "/status": "查看 Agent 生命周期与调用状态",
    "/config": "查看配置摘要",
    "/context": "查看上下文 token 用量分解与压缩阈值",
    "/usage": "查看用量与计费统计，可指定日志路径",
    "/panel": "查看工作区运行和全部会话的历史总览",
    "/LTM": "show / clear / retry：查看、清空或重试全局长期记忆",
    "/STM": "show / clear / retry：查看、清空或重试当前项目情景记忆",
    "/pwd": "查看当前项目目录",
    "/cd": "/cd <path>：切换项目并加载该项目对话",
    "/skills": "查看已加载 Skills",
    "/agent": "/agent <role> <预设或模型名>：下一次请求切换模型，保留会话",
    "/effort": "/effort <role> off 或支持的级别：查看或设置思考",
    "/api": "查看配置对话；/api embedding 配置 embedding/rerank 接口",
    "/compress": "压缩 Manager / Coordinator 上下文",
    "/cancel": "/cancel <invocation_id> 或 /cancel agent <agent_id>：取消调用",
    "/stop": "停止当前任务，保留会话",
    "/load": "选择并加载当前项目对话快照",
    "/trace": "/trace <turn_id>：查看追踪记录",
    "/tasks": "查看任务状态与依赖",
    "/voice": "语音 on / off / test / status / prepare / update / rollback / clean",
    "/pets": "桌宠开关；on [charcoal|ivory] / off / status",
}
COMMANDS = tuple(COMMAND_HELP)

CompletionKind = Literal[
    "command", "agent_role", "effort_value", "literal_choice", "file_path"
]

_SUBCOMMAND_CHOICES: dict[str, tuple[str, ...]] = {
    "/ltm": ("show", "clear", "retry"),
    "/stm": ("show", "clear", "retry"),
    "/cancel": ("agent",),
    "/api": ("embedding",),
    "/voice": ("on", "off", "test", "status", "prepare", "update", "rollback", "clean"),
    "/pets on": ("charcoal", "ivory"),
    "/pets": ("on", "off", "status"),
}


@dataclass(frozen=True)
class InputCompletion:
    """Describes what to complete for a given input prefix."""

    kind: CompletionKind
    prefix: str
    at_mode: bool = False
    choices: tuple[str, ...] = ()
    role: str = ""


class RecordButton(Static):
    def on_mouse_down(self, event: events.MouseDown):
        if event.button == 1 and not self.disabled:
            event.stop()
            self.capture_mouse()
            self.app.query_one(VoiceControls).start_recording()

    async def on_mouse_up(self, event: events.MouseUp):
        if event.button == 1:
            event.stop()
            self.release_mouse()
            await self.app.query_one(VoiceControls).release_recording()


class VoiceControls(Vertical):
    DEFAULT_CSS = """
    VoiceControls { height: auto; }
    #voice-buttons { height: 3; align-vertical: middle; }
    #voice-record { width: 14; height: 3; content-align: center middle; border: round $accent; }
    #voice-input-device { width: 1fr; min-width: 10; max-width: 44; }
    #voice-input-refresh { width: 6; min-width: 6; padding: 0; }
    #voice-label { width: 10; padding: 1 0 0 1; }
    #voice-enabled { width: 8; }
    #voice-model-status, #voice-preview { height: auto; min-height: 1; color: $text-muted; }
    """

    def __init__(self):
        super().__init__()
        self.record_task = self.capture = self.player = None
        self._identity = None
        self._serial = 0
        self.device = None
        self._devices = []
        self._devices_task = None
        self._device_invalid = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="voice-buttons"):
            yield RecordButton("按住录音", id="voice-record")
            yield Select([("跟随系统默认（正在读取设备）", -1)], value=-1, allow_blank=False, id="voice-input-device")
            yield Button("刷新", id="voice-input-refresh")
            yield Label("语音回复", id="voice-label")
            yield Switch(False, id="voice-enabled", animate=False)
        yield Static("", id="voice-model-status")
        yield Static("", id="voice-preview")

    def on_mount(self):
        self.sync()
        self.refresh_devices()
        self.set_interval(0.5, self.sync)

    def refresh_devices(self, *, refresh=False):
        if self._devices_task is not None and not self._devices_task.done():
            return
        identity = self._current_identity()
        async def load():
            from redlotus.TTS import SpeechBusy
            from redlotus.TTS.audio import AudioDevices
            try:
                self._devices = await AudioDevices.inputs(refresh=refresh)
                default = next((item.name for item in self._devices if item.is_default), "暂无设备")
                options = [(f"跟随系统默认（{default}）", -1)]
                options.extend((f"{item.name} · {item.hostapi} [{item.index}]", item.index) for item in self._devices)
                selected = -1
                if self.device is not None:
                    matches = [item for item in self._devices if (item.name, item.hostapi) == (self.device.name, self.device.hostapi)]
                    self._device_invalid = len(matches) != 1
                    if self._device_invalid:
                        options.append(("所选麦克风不可用，请重新选择", -2))
                        selected = -2
                        if identity == self._current_identity():
                            self.notice("所选麦克风不可用，请重新选择。")
                    else:
                        self.device = matches[0]
                        selected = self.device.index
                selector = self.query_one(Select)
                selector.set_options(options)
                selector.value = selected
            except Exception as exc:
                await logger.speech_log(identity[1], "刷新麦克风失败", None if isinstance(exc, SpeechBusy) else exc)
                if identity == self._current_identity():
                    self.notice(str(exc) if isinstance(exc, SpeechBusy) else "无法读取麦克风列表，请检查设备后刷新。")
            finally:
                self.call_after_refresh(self.sync)
        self._devices_task = asyncio.create_task(load())
        self.sync()

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "voice-input-refresh":
            event.stop()
            self.refresh_devices(refresh=True)

    def on_select_changed(self, event: Select.Changed):
        if event.value != event.select.value or event.value in (Select.NULL, -2):
            return
        self.device = next((item for item in self._devices if item.index == event.value), None)
        if self._device_invalid:
            self.notice("")
        self._device_invalid = False
        self._status_error = None
        self.sync()

    def edited(self):
        self._serial += 1

    def submitted(self):
        self.edited()
        if self.record_task is not None and not self.record_task.done():
            self.record_task.cancel()
            self.query_one(RecordButton).release_mouse()

    def _current_identity(self):
        return self.app.system._session, self.app.system.workspace, self.app.system._session.generation

    def notice(self, message):
        if self.is_mounted:
            self.query_one("#voice-preview", Static).update(Text(str(message)))

    def voice_error(self, exc):
        self.app.system._session.track_preparation(logger.speech_log(self.app.system.workspace, "语音播放失败", exc))
        self.notice("语音播放失败，请检查输出设备或模型；文字回复仍可使用。")

    def sync(self):
        current = self._current_identity()
        if current != self._identity:
            if self.record_task is not None:
                self.record_task.cancel()
            self.query_one(RecordButton).release_mouse()
            self._identity = current
            self.notice("")
        state = current[0]
        from redlotus.TTS import ModelKind, ModelStage
        from redlotus.TTS.service import SpeechService
        service = SpeechService._shared
        status = {}
        try:
            if service is not None:
                status = service.status()
            if service is not None and self.player is None:
                from redlotus.TTS.audio import AudioPlayer
                self.player = AudioPlayer(pcm_seconds=service.config.pcm_seconds)
            self._status_error = None
        except Exception as exc:
            signature = (current, type(exc), str(exc))
            if signature != self._status_error:
                self._status_error = signature
                state.track_preparation(logger.speech_log(current[1], "读取语音模型状态失败", exc))
            status = {}
        self.query_one("#voice-model-status", Static).update(
            format_voice_model_status(status) if status else
            "语音模型状态暂不可用，请查看日志" if service else "语音模型正在初始化；文字输入可正常使用")
        asr_ready = status.get(ModelKind.ASR) is not None and status[ModelKind.ASR].stage == ModelStage.READY
        tts_ready = status.get(ModelKind.TTS) is not None and status[ModelKind.TTS].stage == ModelStage.READY
        if self.player is not None:
            state.voice_output, state.voice_stop, state.voice_error = self.player.play, self.player.stop, self.voice_error
        busy = self.record_task is not None and not self.record_task.done()
        refreshing = self._devices_task is not None and not self._devices_task.done()
        self.query_one(Select).disabled = busy or refreshing
        self.query_one("#voice-input-refresh", Button).disabled = busy or refreshing
        self.query_one(RecordButton).disabled = not asr_ready or refreshing or self._device_invalid or self.app.query_one("#input", Input).disabled or self.app._ask_future is not None
        switch = self.query_one(Switch)
        switch.disabled, switch.value = not (tts_ready or state.voice_enabled), state.voice_enabled

    def on_switch_changed(self, event: Switch.Changed):
        state = self.app.system._session
        if event.value != event.switch.value or event.value == state.voice_enabled:
            return
        if event.value:
            from redlotus.TTS import ModelKind, ModelStage
            from redlotus.TTS.service import SpeechService
            service = SpeechService._shared
            if service is None or service.status()[ModelKind.TTS].stage != ModelStage.READY:
                event.switch.value = False
                return
        state.voice_enabled = event.value
        if not event.value:
            state.stop_voice()

    def start_recording(self):
        if self.record_task is not None and not self.record_task.done():
            return
        self.sync()
        if self.query_one(RecordButton).disabled:
            return
        from redlotus.TTS.service import SpeechService
        service = SpeechService._shared
        identity, serial = self._identity, self._serial
        state = identity[0]
        self._started = False
        self.notice("正在打开麦克风；松开可取消")

        async def run():
            from redlotus.TTS import NoSpeechDetected, SpeechUnavailable
            from redlotus.TTS.asr import AudioCapture, StreamingRecognizer
            try:
                await state.drain_voice()
                self.capture = AudioCapture(pcm_seconds=service.config.pcm_seconds, device=self.device)
                def preview(result):
                    if identity == self._current_identity():
                        self.notice(result.text or "录音中…")
                def started():
                    self._started = True
                    if identity == self._current_identity():
                        self.notice("录音中，请说话…")
                final = await StreamingRecognizer(service).record(self.capture, preview, on_started=started)
                text = final.text.strip()
                if identity != self._current_identity():
                    return
                if serial != self._serial:
                    self.notice("保留已编辑草稿；录音转写：" + text)
                    return
                composer = self.app.query_one("#input", Input)
                composer.value = (composer.value + " " + text).strip()
                self.notice("转写已加入草稿，可编辑后手动提交")
            except NoSpeechDetected as exc:
                if identity == self._current_identity():
                    self.notice("未识别到语音，请重试。")
            except Exception as exc:
                await logger.speech_log(identity[1], "录音或转写失败", exc)
                if identity == self._current_identity():
                    if isinstance(exc, SpeechUnavailable) and str(exc).startswith(("暂时无法使用麦克风，", "所选麦克风不可用")):
                        message = str(exc)
                    else:
                        message = "录音失败，请检查麦克风与语音模型；文字输入仍可使用。"
                    self.notice(message)
            finally:
                if self._started and self.capture is not None:
                    await logger.speech_log(identity[1], f"采集统计：{getattr(self.capture, 'recording_stats', {})}")
                self.capture = None
                self.call_after_refresh(self.sync)
        self.record_task = state.track_preparation(run())
        self.sync()

    async def release_recording(self):
        if self.record_task is None or self.record_task.done():
            return
        if self._started and self.capture is not None:
            self.notice("正在完成转写…")
            await self.capture.stop()
        else:
            self.record_task.cancel()
            self.notice("已取消录音准备")

    async def on_unmount(self):
        if self._devices_task is not None:
            self._devices_task.cancel()
            await asyncio.gather(self._devices_task, return_exceptions=True)
        if self.record_task is not None:
            self.record_task.cancel()
            await asyncio.gather(self.record_task, return_exceptions=True)
        if self.player is not None:
            await self.player.close()


def completion_for_input(text: str) -> InputCompletion | None:
    """Return completion context for *text*, or None if no completion applies."""
    if text.startswith("/") and " " not in text:
        return InputCompletion(kind="command", prefix=text)

    if text.startswith("/agent "):
        prefix = text[len("/agent ") :]
        if " " not in prefix:
            return InputCompletion(kind="agent_role", prefix=prefix)
        return None

    if text.startswith("/effort "):
        rest = text[len("/effort ") :]
        if " " not in rest:
            return InputCompletion(kind="agent_role", prefix=rest)
        role, prefix = rest.split(" ", 1)
        if " " not in prefix.strip():
            return InputCompletion(
                kind="effort_value", prefix=prefix, role=role.lower()
            )
        return None

    if text.startswith("/cd "):
        prefix = text.split(" ", 1)[1] if " " in text else ""
        return InputCompletion(kind="file_path", prefix=prefix)

    for cmd, choices in _SUBCOMMAND_CHOICES.items():
        if text.lower().startswith(cmd + " "):
            prefix = text[len(cmd) + 1 :]
            if " " not in prefix:
                return InputCompletion(
                    kind="literal_choice", prefix=prefix, choices=choices
                )
            return None

    references = list(iter_reference_spans(text, root=current_workspace()))
    if references:
        reference = references[-1]
        if reference.end == len(text) and not (reference.opener and reference.closed):
            return InputCompletion(
                kind="file_path", prefix=text[reference.start + 1 :], at_mode=True
            )

    return None


class AgentCompleter(Completer):
    """根据光标前上下文补全命令、角色名或文件路径。"""

    def get_completions(self, document, complete_event):
        yield from input_completions(document.text_before_cursor)


def input_completions(text):
    if context := completion_for_input(text):
        if context.kind == "file_path":
            yield from _iter_file_completions(context.prefix, at_mode=context.at_mode)
            return
        choices = {
            "command": COMMANDS,
            "agent_role": get_agent_roles(),
            "literal_choice": context.choices,
        }
        if context.kind == "effort_value":
            values = (
                ("off", *role_supported_thinking_efforts(context.role))
                if context.role in get_agent_roles()
                else ("off", *supported_thinking_efforts(None))
            )
        else:
            values = choices[context.kind]
        for value in values:
            if value.lower().startswith(context.prefix.lower()):
                yield Completion(
                    value, start_position=-len(context.prefix), display_meta=context.kind
                )


def _resolve_parent(fragment: str) -> tuple[Path, str]:
    path = Path(fragment.replace("\\", "/")).expanduser()
    path = path if path.is_absolute() else current_workspace() / path
    return (
        (path, "")
        if fragment.endswith(("/", "\\")) or not fragment
        else (path.parent, path.name)
    )


def _iter_file_completions(fragment: str, *, at_mode: bool):
    opener = fragment[:1] if fragment[:1] in ('"', "'", "{") else ""
    parent, prefix = _resolve_parent(fragment[1:] if opener else fragment)
    if not parent.exists():
        return

    try:
        children = sorted(
            parent.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
        )
    except (PermissionError, OSError):
        return

    match = prefix.casefold() if os.name == "nt" else prefix
    matching = (child for child in children if (child.name.casefold() if os.name == "nt" else child.name).startswith(match))
    for child in matching:
        try:
            candidate = child.relative_to(current_workspace()).as_posix()
        except ValueError:
            candidate = child.as_posix()
        if child.is_dir():
            candidate += "/"
        candidate = quote_reference_path(
            candidate, opener=opener, directory=child.is_dir()
        )
        display = ("@" if at_mode else "") + candidate
        yield Completion(
            candidate,
            start_position=-len(fragment),
            display=display,
            display_meta="dir" if child.is_dir() else "file",
        )








class AgentInputSuggester(Suggester):
    async def get_suggestion(self, value: str) -> str | None:
        for completion in input_completions(value):
            candidate = value[: len(value) + completion.start_position] + completion.text
            if candidate != value:
                return candidate
        return None


class AgentInput(Input):
    @dataclass
    class Submitted(Input.Submitted, namespace="input"):
        urgent: bool = False

    BINDINGS = [
        *Input.BINDINGS,
        Binding("tab", "cursor_right", "Complete", show=False),
    ]

    async def on_key(self, event: events.Key) -> None:
        """Capture submission in the same queue that applies typed characters."""
        if event.key in {"enter", "ctrl+enter", "ctrl+j", "ctrl+\r"}:
            event.stop()
            event.prevent_default()
            await self.action_submit(urgent=event.key != "enter")

    async def action_submit(self, *, urgent=False) -> None:
        """Consume this draft before another key can submit or replace it."""
        if self.disabled:
            return
        if urgent:
            self.post_message(self.Submitted(self, self.value, urgent=True))
        else:
            await super().action_submit()
        self.value = ""

    def finish_question(self, keep_draft: bool) -> None:
        if not keep_draft:
            self.value = ""
        self.password = False
        self.remove_class("ask")
        self.placeholder = "📝 请输入您的任务:"
        self.suggester = AgentInputSuggester(case_sensitive=True, use_cache=False)


class SessionFooter(Footer):
    session_disabled = True

    def compose(self) -> ComposeResult:
        button = Button("会话 / 加载", id="session-load", disabled=self.session_disabled)
        button.can_focus = False
        inserted = False
        for widget in super().compose():
            yield widget
            if getattr(widget, "action", None) == "review":
                yield button
                inserted = True
        if not inserted:
            yield button


class SnapshotPickScreen(ModalScreen[SnapshotSelection]):
    BINDINGS = [
        Binding("escape", "cancel", "取消", show=False),
        Binding("ctrl+c", "cancel", "取消", show=False),
    ]

    DEFAULT_CSS = """
    SnapshotPickScreen {
        align: center middle;
    }
    #snapshot-dialog {
        width: 90%;
        max-width: 120;
        height: auto;
        max-height: 80%;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }
    #snapshot-title {
        text-style: bold;
        margin-bottom: 1;
    }
    .snapshot-hint {
        color: $text-muted;
        margin-bottom: 1;
    }
    #snapshot-list {
        height: auto;
        max-height: 24;
        min-height: 5;
    }
    """

    def __init__(
        self,
        snapshots: list[WorkspaceSnapshot],
        *,
        project_name: str = "",
        current_session_id: str | None = None,
    ) -> None:
        super().__init__()
        self._snapshots = snapshots
        self._project_name = project_name
        self._current_session_id = current_session_id

    def compose(self) -> ComposeResult:
        with Vertical(id="snapshot-dialog"):
            context = "新建会话或恢复原会话"
            if self._project_name:
                session = self._current_session_id or "新会话"
                context = f"项目：{self._project_name} · 当前会话：{session}\n{context}"
            yield Static(context, id="snapshot-title")
            yield Static("↑↓ 选择 · Enter 确认 · Esc 取消", classes="snapshot-hint")
            yield OptionList(
                Option("新建会话", id="new"),
                *[
                    Option(
                        snapshot.label,
                        id=str(index),
                        disabled=not snapshot.is_loadable,
                    )
                    for index, snapshot in enumerate(self._snapshots)
                ],
                Option("取消", id="cancel"),
                id="snapshot-list",
            )

    def on_mount(self) -> None:
        self.query_one("#snapshot-list", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        option_id = event.option.id
        if option_id == "new":
            self.dismiss(SnapshotSelection(SnapshotAction.NEW))
        elif option_id is None or option_id == "cancel":
            self.dismiss(SnapshotSelection(SnapshotAction.CANCEL))
        else:
            self.dismiss(
                SnapshotSelection(SnapshotAction.RESTORE, self._snapshots[int(option_id)])
            )

    def action_cancel(self) -> None:
        self.dismiss(SnapshotSelection(SnapshotAction.CANCEL))


class UsagePanel(VerticalScroll):
    """Persistent usage widgets; refresh updates values without rebuilding layout."""

    def compose(self) -> ComposeResult:
        yield Static("", id="panel-content")
        yield Label(
            "新增内容 Token 占比（当前项目全部会话）",
            classes="panel-chart-title",
        )
        for key, label in (("input", "用户输入（估算）"), ("output", "模型输出（非推理）"), ("reasoning", "推理输出")):
            with Horizontal(classes="panel-bar-row"):
                yield Label(label, classes="panel-bar-label")
                yield ProgressBar(
                    id="panel-comp-" + key, show_eta=False, show_percentage=False
                )
                yield Static("", id="panel-value-" + key, classes="panel-token-value")
        yield Static("", id="panel-content-note")
        yield Label("API 实际用量（包含历史重发）", classes="panel-chart-title")
        yield Static("", id="panel-api-usage")
        yield Label("当前会话 Agent", classes="panel-chart-title")
        yield Static("", id="panel-agent-counts")
        yield Label("计划任务进度", classes="panel-chart-title")
        yield ProgressBar(id="panel-task-progress", show_eta=False)
        yield Static("", id="panel-task-counts")

    def update_snapshot(self, snapshot: Any) -> None:
        """Render independent content, API, Agent and plan counters without rebuilding widgets."""
        self.query_one("#panel-content", Static).update(render_panel(snapshot))
        self._update_content_chart(snapshot)
        self._update_api_usage(snapshot.history)
        self._update_agent_counts(snapshot.runtime)


    def _update_content_chart(self, snapshot) -> None:
        """Show once-counted content and mark measurements with incomplete coverage."""
        content = snapshot.content
        complete = content.complete and not snapshot.history.skipped_count
        total = content.input_tokens + content.output_tokens
        for key, value in (
            ("input", content.input_tokens),
            ("output", content.output_tokens - content.reasoning_tokens),
            ("reasoning", content.reasoning_tokens),
        ):
            bar = self.query_one("#panel-comp-" + key, ProgressBar)
            bar.display = complete and total > 0
            bar.update(total=total or 1, progress=value)
            text = f"{value:,} tokens"
            if key == "input":
                text += "（估算）"
            if key != "input" and content.missing_reasoning_responses:
                text = (f"未知（总输出 {content.output_tokens:,} tokens）" if key == "output"
                        else f"已报告 {content.reasoning_tokens:,} tokens；其余未知")
            elif complete:
                text += f"  {value / total * 100 if total else 0:.1f}%"
            self.query_one("#panel-value-" + key, Static).update(text)
        notes = ["用户输入及引用文本只计一次；不含系统提示词、旧回复和工具结果。"]
        if not complete:
            notes.append("统计不完整，暂不展示完整占比。")
        for amount, label in (
            (content.incomplete_sessions, "{amount} 个旧会话输入统计不完整；输入仅为已统计部分。"),
            (content.unmetered_attachments, "未计量附件 {amount} 个。"),
            (content.missing_reasoning_responses, "{amount} 次响应推理明细未知。"),
            (content.missing_usage_responses, "{amount} 次响应未报告用量。"),
        ):
            if amount:
                notes.append(label.format(amount=amount))
        self.query_one("#panel-content-note", Static).update("\n".join(notes))


    def _update_api_usage(self, history) -> None:
        """Keep provider request accounting separate from unique input estimates."""
        self.query_one("#panel-api-usage", Static).update(
            f"输入 {history.input_tokens:,} tokens · 输出 {history.output_tokens:,} tokens（含推理）\n"
            f"输入缓存：命中 {history.cache_hit_tokens:,} · 未命中 {history.cache_miss_tokens:,} · "
            f"未报告 {max(0, history.input_tokens - history.cache_hit_tokens - history.cache_miss_tokens):,} tokens"
        )


    def _update_agent_counts(self, runtime) -> None:
        """Display live Agents separately from the optional planning checklist."""
        self.query_one("#panel-agent-counts", Static).update(
            "暂不可用" if runtime.active_invocations_error else
            Text.assemble((f"Running {runtime.running_agents}", "cyan"), "   ",
                          (f"Queued {runtime.queued_agents}", "yellow"))
        )
        tasks = runtime.tasks
        task_total = tasks.total or 0
        self.query_one("#panel-task-progress", ProgressBar).display = task_total > 0
        self.query_one("#panel-task-progress", ProgressBar).update(
            total=task_total or 1, progress=tasks.completed or 0
        )
        self.query_one("#panel-task-counts", Static).update(Text.assemble(
            (f"✓ Completed {tasks.completed}/{task_total}", "green"), "   ",
            (f"⟳ Running {tasks.running}", "cyan"), "   ",
            (f"✗ Failed {tasks.failed}", "red"), "   ",
            (f"… Pending {tasks.pending}", "yellow"),
        ) if task_total else "暂无计划任务")


READY_LABEL = "就绪"
PREPARING_LABEL = "正在准备会话…"
WORKING_LABEL = "工作中"


class RunStatus(Static):
    """Render interaction state when existing UI events refresh the widget."""

    def _is_working(self) -> bool:
        return bool(
            (self.app._ask_future is not None and not self.app._ask_future.done())
            or self.app._active_line_handlers > 0
            or self.app.system.has_current_turn
            or self.app.system._session.queue.pending
        )


    def _mode_chip(self) -> Text:
        label, color = {
            TuiRunMode.REVIEW: (" ⏵ 审查模式 ", "cyan"),
            TuiRunMode.PASS: (" ⏵⏵ 放行模式 ", "green"),
            TuiRunMode.GOAL: (" ◎ 目标模式 ", "yellow"),
        }[self.app._run_mode]
        return Text(label, style=f"bold black on {color}")


    def render(self) -> Any:
        if self.app._panel_mode:
            return Text(str(self.app.query_one("#panel-view").border_title), style="bold")
        if self.app._review_mode:
            return Text(
                "审查改动中    ·    y 保留    ·    n 撤销    ·    ↑↓ 切换    ·    Esc 退出",
                style="bold",
            )
        text = Text.assemble(self._mode_chip(), "  ")
        if self.app._startup_locked and not (
            self.app._ask_future is not None and not self.app._ask_future.done()
        ):
            text.append(PREPARING_LABEL, style="dim")
            return text
        session = self.app.system._session
        if self.app._turn_control_pending or session.control_busy:
            text.append("恢复中" if self.app._turn_control_pending and self.app._turn_control_pending[1] == "resume" else "暂停中", style="dim")
            return text
        if session.paused:
            text.append("已暂停", style="dim")
            return text
        if not self._is_working():
            text.append(READY_LABEL, style="dim")
            if self.app._run_mode == TuiRunMode.REVIEW and self.app._pending_count > 0:
                text.append("       ")
                text.append(
                    f" ⚑ 待审查 {self.app._pending_count} 处 · 按 Ctrl+R 审查 ",
                    style="bold black on yellow",
                )
            return text
        if self.app.system.has_current_goal_turn:
            iteration = self.app.system.current_goal_iteration
            label = f"目标循环第 {iteration} 轮" if iteration else "目标循环"
            text.append(label)
        else:
            text.append(WORKING_LABEL)
        return text
