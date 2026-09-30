from __future__ import annotations

import asyncio
import contextvars
import importlib
import mimetypes
import os
import signal
import sys
import threading
from abc import ABC, abstractmethod
from copy import deepcopy
from functools import partial
from urllib.parse import urlsplit

from redlotus.runtime import config as app_config
from redlotus.runtime import logging as logger
from redlotus.runtime.config import (
    ConfigError,
    _model_selection_field,
    config_file,
    config_source_summary,
    config_value,
    get_model_and_params,
    settings,
    update_config,
)
from redlotus.runtime.network import close_all_clients

from redlotus.runtime.resources import WorkspaceContext, current_workspace, finish_io
from redlotus.sessions.control import SessionController, UserMessage
from redlotus.TTS import NoSpeechDetected, SpeechError
from redlotus.tools import registry as tool_telemetry


class BotBase(ABC):
    """所有平台机器人的公共基类。

    子类提供平台名称及适配方法；公共模板负责接收、准备、执行和回复。
    """

    RESET_COMMANDS = frozenset({"新任务", "/新任务", "/reset"})
    END_TASK_COMMANDS = frozenset(
        {"结束任务", "/结束任务", "结束当前任务", "/结束当前任务"}
    )
    _MIME_MAP: dict[str, str] = {}

    def __init__(self):
        self._sessions: dict[str, SessionController] = {}
        self._released = False
        self._agent_ctx = contextvars.ContextVar(
            f"{type(self).__name__}_context", default=None
        )

    platform_tag: str
    session_prefix: str

    @abstractmethod
    def adapt_message(self, *args):
        """Return (identity, UserMessage, reply, prepare), or None to ignore an event."""
        raise NotImplementedError

    async def _handle_message(self, *args):
        if adapted := self.adapt_message(*args):
            identity, message, reply, prepare = adapted
            await self.dispatch_user_message(identity, message, reply, prepare=prepare)

    def _session(self, session_id):
        if session_id not in self._sessions:
            state = SessionController()
            self._sessions[session_id] = state
        return self._sessions[session_id]

    def _agent_for_session(self, session_id):
        from redlotus.core.system import AgentSystem
        from redlotus.ui import presentation

        state = self._session(session_id)
        if state.agent is None:
            state.agent = AgentSystem(
                presentation=presentation,
                owner_memory_allowed=self._is_owner(session_id),
                input_controller=state,
            )
            logger.activate_log_dir(logger.prepare_log_dir(state.agent.workspace))
            logger.prune_old_logs()
            state.agent.set_ask_user_handler(self._ask_user)
            state.agent.toolkit.set_task_directory(f"{self.platform_tag}_{session_id}")
        return state.agent

    def _is_owner(self, identity):
        prefix = "private_" if self.platform_tag == "QQ" else self.session_prefix
        owners = settings().get("bot", {}).get("owner_channels", {}).get(self.platform_tag.lower(), [])
        owners = owners if isinstance(owners, list) else [owners]
        return identity.startswith(prefix) and identity[len(prefix):] in {str(owner) for owner in owners}

    async def _close_session(self, state):
        state.reset(discard=True)
        await state.drain_voice()
        await state.queue.cancel(discard=True)
        await state.queue.join()
        if state.agent:
            await state.agent.shutdown()

    async def _reset_session(self, session_id, *, preserve_queue=False):
        old = self._sessions.pop(session_id, None)
        state = SessionController()
        state.queue.ready.clear()
        self._sessions[session_id] = state
        if old:
            old.reset(discard=True)
            pending = [entry[2] for entry in old.queue.pending]
            old.queue.discard()
            if preserve_queue:
                for request in pending:
                    message, reply, _, prepare = old.deliveries[request["id"]]
                    self._submit_turn(session_id, state, message, reply, prepare=prepare, request=request)
        try:
            if old:
                await self._close_session(old)
        finally:
            state.queue.ready.set()

    def _submit_turn(self, identity, state, message, send_reply, *, prepare=None, request=None):
        admission = state.admit(WorkspaceContext.from_path(current_workspace()), input_id=(request or {}).get('id'))
        request = request if request is not None else dict(text=message.text, id=admission.id, goal_mode=False)
        state.deliveries[admission.id] = (message, send_reply, asyncio.get_running_loop(), prepare)
        async def capture():
            await state.prepare_message(self._agent_for_session(identity), message, prepare=prepare)
            request['text'] = message.text
            request['reference_ids'] = [ref.id for ref in message.references]
            if message.speech_body is not None:
                request['speech_body'] = message.speech_body
            if state.paused:
                await state.save_pause(state.agent)
            return message
        prepared = state.track_preparation(capture())
        logger.debug("[%s] input admitted id=%s sequence=%s", self.platform_tag, admission.id, admission.sequence)
        return state.queue.submit(lambda: self._consume_turn(identity, state, message, admission, request, prepared), data=request)

    async def _consume_turn(self, identity, state, message, admission, request, prepared=None):
        try:
            generation = state.generation
            _, send_reply, loop, prepare = state.deliveries[admission.id]
            self._agent_ctx.set((identity, state, send_reply, loop, generation))
            self._bind_voice_output(identity, state, send_reply, generation)
            tool_telemetry.set_user_notify_callback(self._notify)
            try:
                if prepared is not None and not prepared.cancelled():
                    message = await prepared
                with logger.session_log_context(identity):
                    result = await state.start(self._agent_for_session(identity), message, state.history, admission,
                                               prepare=prepare if prepared is not None and prepared.cancelled() else None)
            except NoSpeechDetected:
                result = "未识别到语音，请重试。"
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                logger.error("[%s] Agent 请求失败: %s", self.platform_tag, error)
                result = f"本轮执行失败（输入 {admission.id}），未完成的操作不能视为成功：{error}"
            paused = state.paused and state.paused['request']['id'] == admission.id
            if self._sessions.get(identity) is state and (generation == state.generation or paused):
                try:
                    await send_reply("任务已暂停，发送 /resume 恢复。" if paused else result)
                except Exception as exc:
                    logger.error("[%s] 回复发送未确认，未自动重发: %s", self.platform_tag, exc)
                    raise
        finally:
            if not state.paused:
                state.deliveries.pop(admission.id, None)

    def guess_download_mime(self, *, filename="", media_type_key=""):
        return mimetypes.guess_type(filename)[0] or self._MIME_MAP.get(
            media_type_key.lower(), "application/octet-stream"
        )

    def _bind_voice_output(self, identity, state, reply, generation):
        from contextlib import aclosing
        sender = getattr(reply, "speech_sender", None)
        if sender is None:
            state.voice_output = None
            return
        def current():
            return self._sessions.get(identity) is state and state.generation == generation and state.voice_enabled
        async def output(pcm):
            if problem := getattr(reply, "speech_error", None):
                raise SpeechError(str(problem))
            from redlotus.TTS.audio import AudioIO
            from redlotus.TTS.service import SpeechService
            service = await finish_io(asyncio.to_thread(SpeechService.shared))
            async with aclosing(AudioIO.parse_output(pcm, target=reply.speech_format,
                max_seconds=service.config.clip_seconds)) as clips:
                async for segment in clips:
                    if not current():
                        return
                    await sender(segment)
        async def error(exc):
            logger.warning("[%s] 语音失败，未自动重发: %s", self.platform_tag, exc)
            if current():
                await reply(f"语音处理或发送未确认，文字回复保留：{exc}")
        state.voice_output, state.voice_error = output, error

    def _notify(self, text):
        identity, state, send_reply, loop, generation = self._agent_ctx.get()

        async def send():
            if self._sessions.get(identity) is state and generation == state.generation:
                try:
                    await send_reply(text)
                except Exception as exc:
                    logger.error("[%s] 通知发送失败: %s", self.platform_tag, exc)

        loop.call_soon_threadsafe(lambda: asyncio.create_task(send()))

    async def _ask_user(self, question, timeout=None):
        identity, state, send_reply, loop, generation = self._agent_ctx.get()
        async with state.question_lock:
            if self._sessions.get(identity) is not state or generation != state.generation:
                return None
            state.question = asyncio.get_running_loop().create_future()
            try:
                await send_reply(question)
                return await (state.question if timeout is None else asyncio.wait_for(state.question, timeout))
            except TimeoutError:
                return None
            finally:
                state.question = None

    async def dispatch_user_message(self, session_id, message, send_reply, *, prepare=None):
        user_text = message.text.strip()
        if not session_id or self._released:
            return
        state = self._session(session_id)
        if user_text.startswith("/voice"):
            if user_text not in {"/voice on", "/voice off"}:
                await send_reply("用法：/voice on 或 /voice off")
            else:
                state.voice_enabled = user_text.endswith(" on")
                if not state.voice_enabled:
                    state.stop_voice()
                await send_reply("已开启语音回复，文字仍保留。" if state.voice_enabled else "已关闭语音回复。")
            return
        if user_text == "/stop":
            if state.agent:
                await state.agent.stop_current_turn()
            else:
                state.reset()
                await state.queue.cancel()
            await send_reply("已停止当前任务，保留会话记录。")
            return
        if (
            user_text in self.RESET_COMMANDS
            or user_text == "/clear"
            or user_text in self.END_TASK_COMMANDS
        ):
            await self._reset_session(
                session_id, preserve_queue=user_text in self.END_TASK_COMMANDS
            )
            await send_reply("已结束当前任务并清空上下文。")
            return
        if user_text == "/resume":
            resumed = await state.resume(self._agent_for_session(session_id),
                lambda message, admission, data: self._consume_turn(session_id, state, message, admission, data), lambda: None)
            await send_reply("正在恢复任务。" if resumed else "没有暂停的任务。")
            return
        if state.question and not state.question.done():
            question, generation = state.question, state.generation
            try:
                await state.prepare_message(self._agent_for_session(session_id), message, prepare=prepare)
            except asyncio.CancelledError:
                return
            except (OSError, ValueError, SpeechError) as exc:
                if generation == state.generation:
                    await send_reply(str(exc))
            else:
                if generation == state.generation and not question.done():
                    question.set_result(message)
            return
        if not user_text and not message.attachments and not message.references and prepare is None:
            return
        if missing := app_config.missing_main_api_keys():
            await send_reply("缺少模型接口配置：" + ", ".join(missing))
            return
        self._submit_turn(
            session_id,
            state,
            message, send_reply, prepare=prepare,
        )
        await send_reply("✓ 收到，正在处理…")

    async def release_all_resources_async(self):
        if self._released:
            return
        self._released = True
        sessions, self._sessions = list(self._sessions.values()), {}
        await asyncio.gather(*(self._close_session(state) for state in sessions))
        from redlotus.TTS.service import SpeechService
        await SpeechService.close_shared()
        await close_all_clients()

    def clean_text(self, raw):
        return raw or ""


async def ask_configuration(question: str, *, secret=False):
    """Read a startup answer with the same hidden-key contract as the TUI dialog."""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.application import get_app_session
    from prompt_toolkit.input.typeahead import store_typeahead
    from prompt_toolkit.key_binding import KeyBindings

    bindings = KeyBindings()

    @bindings.add("escape", eager=True)
    def cancel(event):
        event.app.exit(result=None)

    # The TERM=dumb shortcut bypasses password masking; keep the session renderer.
    session = PromptSession(
        key_bindings=bindings, output=get_app_session().output
    )
    answer = await session.prompt_async(question, is_password=secret)
    # A pending Esc must survive the previous prompt's cancelled input-flush task.
    store_typeahead(session.input, session.input.flush_keys())
    return answer

def configuration_prompt_available() -> bool:
    """Return whether the default first-use dialog can safely open a terminal prompt."""
    return bool(
        getattr(sys.stdin, "isatty", lambda: False)()
        and getattr(sys.stdout, "isatty", lambda: False)()
    )

class ConfigurationSetup:
    """Collect explicit configuration edits and commit them as a single atomic change."""

    def __init__(self, ask=None, emit=print):
        self.values = settings()
        self.changes = {}
        self.ask, self.emit = ask or ask_configuration, emit

    @staticmethod
    def assign(values, path, value):
        node = values
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = deepcopy(value)

    def connection_paths(self):
        return [("BASE_URL",), ("API_KEY",)]

    async def fill(self, path):
        """Edit only model parameters and connection fields; never collect runtime policies."""
        key = ".".join(path)
        model = _model_selection_field(path)
        connection = path in rag_configuration_paths() or path in self.connection_paths()
        if not model and not connection:
            raise ConfigError(f"配置向导仅支持模型与连接字段；请在 {config_file()} 编辑 {key}")
        current = config_value(self.values, path)
        secret = connection and ("key" in path[-1].lower() or "token" in path[-1].lower())
        can_keep = isinstance(current, str) and bool(current.strip())
        shown = ("已填写" if current else "空") if secret else str(current if current is not None else "空")
        hint = "回车保留" if can_keep else "必须填写"
        if not can_keep:
            hint += "；作用：" + ("选择调用的模型" if model else "连接服务的地址或认证凭据") + "；类型：字符串；可选值：无枚举限制"
        if model and path[0] == "models":
            hint += "；输入 =角色名 复用模型名，保留本角色的连接与策略"
        while True:
            answer = await self.ask(f"{key}（当前 {shown}；{hint}；Esc 取消）：", secret=secret)
            if answer is None or answer == "\x1b":
                return False
            text = answer.strip()
            if not text and can_keep:
                return True
            if not text:
                self.emit(f"{key} 不能为空。")
                continue
            try:
                value = get_model_and_params(text[1:], cfg=self.values)[0] if text.startswith("=") and model else text
                if connection and ("url" in path[-1].lower() or path[-1] == "SILICONFLOW_BASE"):
                    parsed = urlsplit(value)
                    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                        self.emit(f"{key} 需要完整的 http/https 服务地址。")
                        continue
                candidate = deepcopy(self.values)
                self.assign(candidate, path, value)
                if path[0] == "models":
                    get_model_and_params(path[1], cfg=candidate)
            except (ValueError, KeyError, TypeError):
                self.emit(f"{key} 无效：请检查模型参数或引用的角色名称。")
                continue
            self.values = candidate
            self.changes[path] = value
            return True

    async def commit(self):
        """Confirm once, then apply only this dialog's edits to the latest locked layer."""
        if not self.changes:
            return True
        answer = await self.ask(f"确认将 {len(self.changes)} 项修改写入 {config_file()}？y 确认 / 其他取消：")
        if answer is None or answer.strip().lower() not in {"y", "yes", "是"}:
            return False
        def apply(values):
            for path, value in self.changes.items():
                self.assign(values, path, value)
        update_config(apply)
        return True


def rag_configuration_paths():
    """Connection and model fields exposed by the existing RAG configuration dialog."""
    return [("SILICONFLOW_BASE",), ("SILICONFLOW_KEY",), ("RAG_models", "embedding"), ("RAG_models", "reranker")]

async def prepare_startup_configuration(*, ask=None, emit=print) -> bool:
    """Collect model names and credentials without inventing runtime configuration."""
    setup = ConfigurationSetup(ask, emit)
    interactive = ask is not None or configuration_prompt_available()
    emit(f"配置修改目标: {config_file()}\n读取来源: {config_source_summary()}")
    try:
        models = config_value(setup.values, ("models",), purpose="已有 Agent 角色与模型选择", kind=dict)
        if not isinstance(models, dict) or not models:
            raise ConfigError(f"请在 {config_file()} 中提供 models 配置对象")
        for path in [("models", role) for role in models] + setup.connection_paths():
            try:
                if path[0] == "models":
                    get_model_and_params(path[1], cfg=setup.values)
                else:
                    config_value(setup.values, path, purpose="连接模型服务的地址或认证凭据", kind=str)
            except ConfigError as exc:
                if not exc.missing or not interactive:
                    raise
                if not await setup.fill(exc.path):
                    return False
        return await setup.commit()
    except (KeyboardInterrupt, EOFError, asyncio.CancelledError):
        emit("配置已取消，未保存本次填写内容。")
        return False


async def configure_api(*, embedding=False, ask=None, emit=print) -> bool:
    """Edit service fields atomically for both plain CLI and TUI callers."""
    setup = ConfigurationSetup(ask, emit)
    paths = rag_configuration_paths() if embedding else setup.connection_paths()
    try:
        for path in paths:
            if not await setup.fill(path):
                return False
        return await setup.commit()
    except (KeyboardInterrupt, EOFError, asyncio.CancelledError):
        return False

class ExitDeadline(threading.Timer):
    """Bound process exit even when a native call ignores task cancellation."""

    def __init__(self, seconds: float):
        super().__init__(seconds, os._exit, args=(0,))
        self.daemon = True

    close = threading.Timer.cancel

def install_stop_handlers(stop_event: asyncio.Event) -> None:
    """Map process signals to the interactive runner's stop event."""
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, ValueError):
            signal.signal(sig, lambda *_: stop_event.set())

async def start_speech():
    def initialize():
        speech = importlib.import_module("redlotus.TTS.service")
        instance = speech.SpeechService.shared() if speech.ModelFactory.available() else None
        return instance, WorkspaceContext.from_path(current_workspace())

    try:
        service, workspace = await finish_io(asyncio.to_thread(initialize))
        if service is not None:
            service.bootstrap(report_failure=partial(logger.speech_log, workspace, "语音模型准备失败"))
        return service
    except Exception as exc:
        await logger.speech_log(None, "语音启动失败，文字功能仍可使用", exc)


async def run_cli(system=None):
    """Run the interactive RedLotus CLI/TUI."""
    from redlotus.core.system import AgentSystem
    from redlotus.ui import presentation
    from redlotus.ui.console import AgentCliController

    if system is None:
        system = AgentSystem(presentation=presentation)
    stop_event = asyncio.Event()
    install_stop_handlers(stop_event)
    controller = AgentCliController(system)
    speech_task = asyncio.create_task(start_speech(), name="speech-startup")
    try:
        await controller.run_interactive(stop_event=stop_event)
    finally:
        try:
            await finish_io(controller.pets.close())
        finally:
            speech_task.cancel()
            await asyncio.gather(speech_task, return_exceptions=True)
            seconds = config_value(settings(), ('lifecycle', 'shutdown_grace_seconds'), kind=(int, float))
            deadline = ExitDeadline(seconds) if seconds is not None else None
            if deadline is not None:
                deadline.start()
            try:
                await system.shutdown()
                from redlotus.TTS.service import SpeechService
                await SpeechService.close_shared()
                await close_all_clients()
            finally:
                if deadline is not None:
                    deadline.close()
    return system

def main(channel=None) -> None:
    """Configure the selected transport before constructing its runtime."""
    try:
        if not asyncio.run(prepare_startup_configuration()):
            return
        if channel:
            channel().run()
            return
        asyncio.run(run_cli())
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
