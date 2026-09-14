from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic_ai import ModelRequestNode
from pydantic_ai.messages import (
    FunctionToolResultEvent,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolReturnPart,
    UserPromptPart,
)
import asyncio

from redlotus.ModelGateway.input_policy import InputLimitError


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
                            try:
                                await on_node(run)
                            finally:
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
                if on_complete:
                    on_complete()
                self._close_interrupted_calls(
                    run.ctx.state.message_history, results, exc
                )
                if on_node:
                    await on_node(run)
                if isinstance(exc, InputLimitError) and not response_received:
                    # The rejected input remains in the journal, outside the next request's view.
                    run.ctx.state.message_history[:] = original_history
                    if on_node:
                        await on_node(run)
                raise
        return run.result

    @staticmethod
    def _close_interrupted_calls(messages, results, error):
        pending = {}
        for message in messages:
            for part in message.parts:
                kind = getattr(part, "part_kind", "")
                if kind == "tool-call":
                    pending[part.tool_call_id] = part
                elif kind in ("tool-return", "retry-prompt"):
                    pending.pop(getattr(part, "tool_call_id", ""), None)
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
                    "error": "Execution interrupted; completion is unverified.",
                },
            )
            for key, part in pending.items()
        )
        if completed:
            messages.append(ModelRequest(parts=completed))
        messages.append(
            ModelResponse(
                parts=[
                    TextPart(
                        f"Execution {status}. Unfinished actions are unverified; await the next user instruction."
                    )
                ],
                metadata={"origin": "execution_status", "status": status},
            )
        )
