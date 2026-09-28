"""Agent request orchestration, completion checkpoints and auxiliary model calls."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from itertools import count
from typing import Any

from pydantic import BaseModel, Field, field_validator
from pydantic_ai import (
    Agent,
    FunctionToolset,
    ModelRequestNode,
    PromptedOutput,
    RunContext,
)
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior
from pydantic_ai.messages import (
    FunctionToolResultEvent,
    InstructionPart,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolReturnPart,
    UserPromptPart,
)

from redlotus.prompts.prompt import load_prompt, with_runtime_context
from redlotus.runtime import logging as logger
from redlotus.runtime.config import get_agent_usage_limits
from redlotus.runtime.network import (
    InputLimitError,
    ModelInputPolicy,
    ModelTarget,
    context_length_exceeded,
    create_model,
    is_transport_interruption,
)
from redlotus.sessions.context import agent_context, current_agent_id, current_usage_recorder, pending_tool_calls


class RequestPolicy(AbstractCapability):
    def __init__(self, role, target, model, *, usage_category, follow_config=False, task_state=None, persist_context=None):
        self.role, self.target, self.model = role, target, model
        self.usage_category = usage_category
        self.follow_config = follow_config
        self.task_state = task_state
        self.persist_context = persist_context
        self._pending_requests = {}

    async def before_model_request(self, ctx, request_context):
        target = ModelTarget.for_role(self.role) if self.follow_config else self.target
        if target != self.target:
            model = create_model(target)
        else:
            model = self.model
        parameters = request_context.model_request_parameters
        tool_definitions = [*parameters.function_tools, *parameters.output_tools]
        candidate = request_context.messages
        options = target.options
        if (self.usage_category != "auxiliary" or self.persist_context is not None) and "auto_compress_ratio" in options["context"]:
            from redlotus.core.history import compact_request_messages

            candidate = await compact_request_messages(
                request_context.messages,
                role=self.role,
                target=target,
                task_state=self.task_state() if self.task_state else "",
                tools=tool_definitions,
            )
        request = candidate[-1]
        request.metadata = {
            **(request.metadata or {}),
            "instruction_prefix_length": len(InstructionPart.join([
                part for part in parameters.instruction_parts or [] if not part.dynamic
            ]) or ""),
        }
        if candidate is not request_context.messages and self.persist_context:
            await self.persist_context(candidate)
        request_context.messages = candidate
        self.target, self.model = target, model
        ModelInputPolicy.from_limits(options["limits"]).check_messages(
            request_context.messages
        )
        request_context.model = model
        request_context.model_settings = model.settings or {}
        return request_context

    async def after_model_request(self, ctx, *, request_context, response):
        self._pending_requests.pop(ctx.run_id, None)
        response.metadata = {
            **(response.metadata or {}),
            "usage_category": self.usage_category,
            "model_target": {
                "name": self.target.name,
                "protocol": self.target.protocol,
            },
        }
        if record := current_usage_recorder():
            await record([response], role=self.role, invocation=ctx.run_id,
                         agent_id=current_agent_id(), cancelling=bool(asyncio.current_task().cancelling()))
        return response

    async def wrap_model_request(self, ctx, *, request_context, handler):
        """Retry an oversized durable request once, after saving its summaries."""
        for attempt in range(2):
            self._pending_requests[ctx.run_id] = ctx, request_context.messages[-1], len(ctx.messages)
            try:
                return await handler(request_context)
            except ModelHTTPError as error:
                if (
                    attempt or self.persist_context is None
                    or not context_length_exceeded(error)
                    or "auto_compress_ratio" not in self.target.options["context"]
                ):
                    raise
                logger.info("容量超限，正在压缩并保存后重试 capacity_retry=%s", json.dumps(dict(
                    role=self.role, model=self.target.name, invocation=ctx.run_id, run_step=ctx.run_step,
                    status_code=400, attempt=1, message=error.body["message"], usage=None), ensure_ascii=False))
                self._pending_requests.pop(ctx.run_id, None)
                from redlotus.core.history import compact_request_messages

                candidate = await compact_request_messages(
                    request_context.messages, role=self.role, target=self.target, force=True,
                    task_state=self.task_state() if self.task_state else "",
                )
                await self.persist_context(candidate)
                request_context.messages = candidate
                request_context = await self.before_model_request(ctx, request_context)
                ctx.messages[:] = request_context.messages

    async def on_run_error(self, ctx, *, error):
        """Retain cancelled request usage without turning control receipts into responses."""
        pending = self._pending_requests.pop(ctx.run_id, None)
        if pending and (isinstance(error, asyncio.CancelledError) or is_transport_interruption(error)):
            context, request, start = pending
            response = next((message for message in context.messages[start:]
                             if isinstance(message, ModelResponse)
                             and (message.metadata or {}).get("origin") != "execution_status"), None)
            if response is None:
                response = ModelResponse(parts=[], model_name=self.model.model_name,
                                         provider_name=self.model.system, timestamp=request.timestamp,
                                         state="interrupted", run_id=ctx.run_id,
                                         metadata={"usage_request_id": f"{ctx.run_id}:{context.run_step}"})
            await self.after_model_request(context, request_context=None, response=response)
        raise error

    async def on_model_request_error(self, ctx, *, request_context, error):
        cause = error
        while cause is not None:
            if isinstance(cause, InputLimitError):
                raise cause
            cause = cause.__cause__
        raise error


class GoalTextFilter:
    """Hold incomplete control comments across deltas before exposing speech."""

    MAX_PENDING = 512

    def __init__(self):
        self.pending = ""
        self._discarding_goal = False
        self._muted_prefix = 0

    def mute_pending(self):
        self._muted_prefix = len(self.pending)

    def _take(self, count):
        visible = self.pending[min(count, self._muted_prefix):count]
        self.pending = self.pending[count:]
        self._muted_prefix = max(0, self._muted_prefix - count)
        return visible

    @staticmethod
    def _could_complete_goal(comment):
        """Recognize a prefix of the goal marker without buffering other comments."""
        body = comment[4:].lstrip()

        def take(value, literal):
            width = min(len(value), len(literal))
            if value[:width].upper() != literal[:width]:
                return None, value
            return width == len(literal), value[width:]

        for literal in ("REDLOTUS_GOAL", ":"):
            complete, body = take(body, literal)
            if complete is None:
                return False
            if not complete:
                return True
            body = body.lstrip()
        for signal in ("CONTINUE", "DONE"):
            complete, suffix = take(body, signal)
            if complete is None:
                continue
            if not complete:
                return True
            if "-->".startswith(suffix.lstrip()):
                return True
        return False

    def feed(self, text, *, final=False):
        from redlotus.core.tasks import GOAL_MARKER_RE
        self.pending += text
        output = ""
        while self.pending:
            if self._discarding_goal:
                end = self.pending.find("-->")
                if end < 0:
                    self._take(max(0, len(self.pending) - 2))
                    break
                self._take(end + 3)
                self._discarding_goal = False
                continue
            start = self.pending.find("<!--")
            if start < 0:
                keep = 0 if final else next(
                    (n for n in (3, 2, 1) if self.pending.endswith("<!--"[:n])), 0)
                split = len(self.pending) - keep
                output += self._take(split)
                break
            output += self._take(start)
            end = self.pending.find("-->", 4)
            if end < 0:
                if self._could_complete_goal(self.pending):
                    if len(self.pending) > self.MAX_PENDING:
                        self._discarding_goal = True
                        self._take(len(self.pending) - 2)
                        continue
                    break
                output += self._take(1)
            else:
                comment = self.pending[:end + 3]
                if GOAL_MARKER_RE.fullmatch(comment):
                    self._take(end + 3)
                else:
                    output += self._take(1)
        if final:
            if not self._discarding_goal and not (self.pending.startswith("<!--") and self._could_complete_goal(self.pending)):
                output += self._take(len(self.pending))
            self._take(len(self.pending))
            self._discarding_goal = False
        return output


class CoordinatorStreamHandler:
    """Consume one SDK stream, independently feeding display, speech and a reply sink."""

    _reply_ids = count(1)

    def __init__(self, system, *, display=True):
        self.system, self.started = system, False
        self.display = display and system.presentation.supports_model_stream()
        identity = system.session_key, system._session.generation
        self._is_current = lambda: (system.session_key, system._session.generation) == identity
        turn_id = getattr(system._session, "turn_id", None)
        self._is_reply_current = lambda: self._is_current() and getattr(system._session, "turn_id", None) == turn_id
        self._is_reply_bound = lambda: (system.session_key == identity[0]
                                       and system._session.generation[0] == identity[1][0]
                                       and getattr(system._session, "turn_id", None) == turn_id)
        self._reply_output = getattr(system._session, "reply_output", None)
        self._reply_id = str(next(self._reply_ids))
        self._reply_started = self._reply_finished = False

    async def _publish_reply(self, phase, text):
        if self._reply_output is not None and self._is_reply_bound():
            try:
                await self._reply_output(reply_id=self._reply_id, phase=phase, text=text)
            except Exception as exc:
                self._reply_output = None
                logger.warning("桌宠联动失败，文字继续: %s", exc)

    async def _feed_reply(self, text):
        if not text or self._reply_output is None or self._reply_finished or not self._is_reply_current():
            return
        if not self._reply_started:
            self._reply_started = True
            await self._publish_reply("start", "")
        await self._publish_reply("delta", text)

    async def finish_reply(self, phase, text):
        if self._reply_finished:
            return
        if phase == "done":
            if not self._is_reply_current():
                return
            text = GoalTextFilter().feed(text, final=True)
            if text and not self._reply_started:
                self._reply_started = True
                await self._publish_reply("start", "")
        self._reply_finished = True
        if self._reply_started:
            await self._publish_reply(phase, text)

    async def __call__(self, _run_ctx, event_stream):
        from redlotus.TTS import SpeechBusy, SpeechTextParser
        from redlotus.ui.presentation import _text_from_stream_event
        system, response_started, reply, filtered = self.system, False, None, GoalTextFilter()
        reply_filter = GoalTextFilter()
        speech_text = SpeechTextParser()
        voice_failed = False

        def cancel_voice():
            nonlocal reply
            filtered.mute_pending()
            speech_text.mute_pending()
            active, reply = reply, None
            if active is not None:
                try:
                    active.cancel()
                except Exception as exc:
                    logger.warning("语音取消失败: %s", exc)

        def discard_cancelled_voice():
            task = getattr(reply, "task", None)
            if task is not None and (task.cancelling() or task.cancelled()):
                cancel_voice()

        async def fail_voice(exc):
            nonlocal voice_failed
            if voice_failed:
                return
            voice_failed = True
            cancel_voice()
            report = getattr(system._session, "voice_error", None)
            if report is None:
                logger.warning("语音输出失败，文字继续: %s", exc)
                return
            try:
                result = report(exc)
                if isinstance(result, Awaitable):
                    await result
            except Exception as report_error:
                logger.warning("语音输出失败 (%s)，错误通知失败: %s", exc, report_error)

        try:
            async for event in event_stream:
                kind, text = _text_from_stream_event(event)
                if not text or not self._is_current():
                    continue
                if self.display:
                    if not self.started:
                        system.presentation.update_output("begin_model_stream", "Coordinator 正在回复")
                        self.started = True
                    if not response_started:
                        system.presentation.update_output("begin_model_response")
                        response_started = True
                    system.presentation.update_output("append_model_stream_delta", text, kind)
                if kind == "text":
                    await self._feed_reply(reply_filter.feed(text))
                if kind == "text" and not voice_failed:
                    try:
                        discard_cancelled_voice()
                        speech_text.limit = max(0, getattr(reply, "text_capacity", 4096) - getattr(reply, "pending_chars", 0))
                        body = speech_text.feed(filtered.feed(text), audible=system._session.voice_enabled)
                        if not system._session.voice_enabled:
                            cancel_voice()
                        if system._session.voice_enabled:
                            if reply is None and body.strip():
                                reply = system._session.begin_voice(
                                    system.workspace, is_current=self._is_current)
                            speech_text.limit = getattr(reply, "text_capacity", 4096)
                            if speech_text.pending_chars + getattr(reply, "pending_chars", 0) + len(body) > speech_text.limit:
                                raise SpeechBusy("语音文本缓冲区已满，文字回复继续")
                            if reply is not None and body:
                                await reply.feed(body)
                    except Exception as exc:
                        await fail_voice(exc)
            await self._feed_reply(reply_filter.feed("", final=True))
            discard_cancelled_voice()
            if not voice_failed:
                try:
                    tail = speech_text.feed(filtered.feed("", final=True), audible=system._session.voice_enabled)
                    tail += speech_text.finish()
                    if tail and self._is_current() and system._session.voice_enabled:
                        if reply is None and tail.strip():
                            reply = system._session.begin_voice(system.workspace, is_current=self._is_current)
                        if reply is not None:
                            await reply.feed(tail)
                    if reply is not None:
                        await reply.finish()
                except Exception as exc:
                    await fail_voice(exc)
        except BaseException as exc:
            cancel_voice()
            tail = reply_filter.feed("", final=True)
            if tail and not self._reply_started:
                self._reply_started = True
                await self._publish_reply("start", "")
            if tail:
                await self._publish_reply("delta", tail)
            await self.finish_reply("cancelled" if isinstance(exc, asyncio.CancelledError) else "failed", "")
            raise
        finally:
            speech_text.clear()


def coordinator_stream_handler(system, *, display=True):
    return CoordinatorStreamHandler(system, display=display)


class AgentRunner:
    """The inner loop: finish a tool batch, assemble steering, request the model."""

    async def run(
        self,
        *,
        agent: Any,
        prompt: Any,
        message_history: list,
        usage_limits: Any,
        take_urgent: Callable[[], Awaitable[list]] | None = None,
        before_request: Callable[[Any, Any], Awaitable[None]] | None = None,
        on_node: Callable[[Any], Awaitable[None]] | None = None,
        on_complete: Callable[[], None] | None = None,
        event_stream_handler=None,
    ):
        original_history = list(message_history)
        response_received = False
        async with agent.iter(
            prompt, message_history=message_history, usage_limits=usage_limits
        ) as run:
            results = []
            prepared_request = None
            try:
                node = run.next_node
                while not agent.is_end_node(node):
                    if agent.is_model_request_node(node):
                        if take_urgent and node is not prepared_request:
                            node.request.parts.extend(
                                UserPromptPart(text) for text in await take_urgent()
                            )
                        prepared_request = None
                        if on_node:
                            # Audit the pending tool batch before a compressor changes the model view.
                            run.ctx.state.message_history.append(node.request)
                            await on_node(run)
                            run.ctx.state.message_history.pop()
                        if before_request:
                            await before_request(run, node)

                    results = []
                    if agent.is_model_request_node(node) or agent.is_call_tools_node(
                        node
                    ):
                        async with node.stream(run.ctx) as stream:

                            async def events():
                                async for event in stream:
                                    if isinstance(event, FunctionToolResultEvent):
                                        results.append(event.part)
                                    yield event

                            if event_stream_handler:
                                await event_stream_handler(run.ctx, events())
                            else:
                                async for _ in events():
                                    pass
                    next_node = await run.next(node)
                    if agent.is_model_request_node(node):
                        response_received = True
                    if results and agent.is_model_request_node(next_node):
                        ids = {p.tool_call_id for p in results}
                        other = [
                            p
                            for p in next_node.request.parts
                            if getattr(p, "tool_call_id", None) not in ids
                        ]
                        next_node.request.parts[:] = [*results, *other]
                    if on_node:
                        await on_node(run)
                    # Steering arriving during a final model response still belongs to this turn.
                    if agent.is_end_node(next_node) and take_urgent:
                        urgent = await take_urgent()
                        if urgent:
                            next_node = ModelRequestNode(
                                ModelRequest(parts=[UserPromptPart(t) for t in urgent])
                            )
                            prepared_request = next_node
                    if agent.is_end_node(next_node) and on_complete:
                        on_complete()  # Later input is a new turn, even while trace writes are draining.
                    node = next_node
            except BaseException as exc:
                try:
                    if on_complete:
                        on_complete()
                    self._close_interrupted_calls(
                        run.ctx.state.message_history, results, exc
                    )
                    if on_node:
                        await on_node(run)
                    if isinstance(exc, InputLimitError) and not response_received:
                        # Retain the rejected input only in the journal.
                        run.ctx.state.message_history[:] = original_history
                        if on_node:
                            await on_node(run)
                except BaseException as recording_error:
                    raise exc from recording_error
                raise
        return run.result

    @staticmethod
    def _close_interrupted_calls(messages, results, error):
        pending = pending_tool_calls(messages)
        completed = [part for part in results if part.tool_call_id in pending]
        for part in completed:
            pending.pop(part.tool_call_id, None)
        status = "cancelled" if isinstance(error, asyncio.CancelledError) else "failed"
        completed.extend(
            ToolReturnPart(
                tool_name=part.tool_name,
                tool_call_id=key,
                content={
                    "status": status,
                    "execution_outcome": "unknown",
                    "error": type(error).__name__,
                },
                outcome="failed",
                metadata={
                    "origin": "runtime_control",
                    "execution_outcome": "unknown",
                    "interruption_status": status,
                },
            )
            for key, (_, part) in pending.items()
        )
        if completed:
            messages.append(ModelRequest(parts=completed))
        messages.append(
            ModelResponse(
                parts=[
                    TextPart(json.dumps({"execution_status": status}))
                ],
                metadata={"origin": "execution_status", "status": status},
            )
        )


def create_function_toolset(
    tools: list,
    *,
    toolset_id: str = "default",
    instructions: str | None = None,
    defer_loading: bool = False,
) -> FunctionToolset:
    from redlotus.tools import registry as tool_telemetry

    wrapped_tools = tool_telemetry.wrap_tools_for_user_notify(list(tools))
    return FunctionToolset(
        wrapped_tools,
        id=toolset_id,
        instructions=instructions,
        defer_loading=defer_loading,
    )


def _validate_current_text(ctx: RunContext, output: str) -> str:
    # The SDK may recover pre-tool or previous-turn text after an empty response.
    if not any(
        isinstance(part, TextPart) and part.content.strip()
        for part in ctx.messages[-1].parts
    ):
        raise UnexpectedModelBehavior(
            "模型本次没有返回有效答复，不能将先前的回复当作当前结果。"
            "已有执行记录已保留，可继续当前任务；本次未自动重跑工具。"
        )
    return output


def create_agent(
    model_name: Any,
    parameter: dict | None = None,
    instructions: str | None = None,
    *,
    toolsets: list | None = None,
    capabilities: list | None = None,
    output_type: Any = str,
    role: str | None = None,
    usage_category: str = "agent",
    follow_config: bool = False,
    task_state=None,
    persist_context=None,
):
    model = (
        create_model(model_name, parameter)
        if isinstance(model_name, (str, ModelTarget))
        else model_name
    )

    capabilities = list(capabilities or [])
    if role and isinstance(model_name, ModelTarget):
        capabilities.insert(0,
            RequestPolicy(
                role,
                model_name,
                model,
                usage_category=usage_category,
                follow_config=follow_config,
                task_state=task_state,
                persist_context=persist_context,
            )
        )
    agent = Agent(
        model,
        output_type=output_type,
        toolsets=list(toolsets) if toolsets is not None else None,
        capabilities=capabilities,
        instructions=instructions or "",
    )
    if output_type is str:
        agent.output_validator(_validate_current_text)
    return agent


async def create_coordinator_agent(
    skills_manager: Any,
    memory_injection: str,
    routing_tools: Sequence[Any],
    worker_tools: Sequence[Any],
    task_state=None,
    *,
    instructions: str | None = None,
    persist_context=None,
):
    from redlotus.prompts.prompt import get_coordinator_system_prompt

    target = ModelTarget.for_role("coordinator")
    if instructions is None:
        instructions = await asyncio.to_thread(
            get_coordinator_system_prompt, skills_manager, memory_injection
        )
    toolsets = [
        create_function_toolset(list(tools), toolset_id=name)
        for name, tools in (("delegation", routing_tools), ("execution", worker_tools))
        if tools
    ]
    return create_agent(
        target,
        instructions=instructions,
        toolsets=toolsets,
        role="coordinator",
        usage_category="main",
        follow_config=True,
        task_state=task_state,
        persist_context=persist_context,
    )


async def complete_text(
    role: str, system_prompt: str, user_text: str, *, output_validator=None, output_type=str
):
    """Auxiliary calls use the foreground Agent's provider routing."""
    target = ModelTarget.for_role(role)
    agent = create_agent(target, instructions=system_prompt, role=role, output_type=output_type,
                         usage_category="auxiliary")
    if output_validator is not None:
        agent.output_validator(output_validator)

    async def save_usage(run):
        if record := current_usage_recorder():
            for message in run.all_messages():
                if isinstance(message, ModelResponse):
                    message.metadata = {"usage_category": "auxiliary", **(message.metadata or {})}
            await record(run.all_messages(), role=role, invocation=run.ctx.state.run_id,
                         cancelling=bool(asyncio.current_task().cancelling()))

    with agent_context(None):
        result = await AgentRunner().run(
            agent=agent, prompt=with_runtime_context(user_text), message_history=[],
            usage_limits=get_agent_usage_limits(), on_node=save_usage,
        )
    return result.output


class TaskTitle(BaseModel):
    """The only value accepted from the title model."""

    title: str = Field(min_length=1)

    @field_validator("title")
    @classmethod
    def single_line(cls, value: str) -> str:
        value = value.strip()
        if not value or "\n" in value or "\r" in value:
            raise ValueError("title must be a non-empty single line")
        return value


async def generate_task_title(user_text: str) -> str:
    """Generate a short task title with the dedicated configured title role."""
    fallback = next(
        (line.strip() for line in user_text.splitlines() if line.strip()),
        "",
    )
    try:
        result = await complete_text(
            "title", load_prompt("title_system.md"), user_text,
            output_type=PromptedOutput(TaskTitle),
        )
        return result.title
    except Exception as exc:
        logger.warning("LLM 标题生成失败，使用用户输入命名: %s", exc)
        return fallback
