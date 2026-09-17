"""SDK context integrity, compression, and retained usage accounting."""

from __future__ import annotations

import asyncio
import json
import re
import threading
import httpx
from pathlib import Path
from typing import Any, Callable, Iterable
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextContent,
    ToolReturnPart,
    UserPromptPart,
    ModelMessagesTypeAdapter,
)
from redlotus.core.config import (
    get_context_config,
    get_context_profile_roles,
    get_model_and_params,
    settings,
)
from redlotus.core import config as logger
from redlotus.prompts.prompt import load_prompt
from dataclasses import dataclass, field
from decimal import Decimal
from redlotus.core.session import SessionFile


def _part_kind(part) -> str:
    return str(getattr(part, "part_kind", "") or "")


def _tool_key(part) -> str:
    return str(
        getattr(part, "tool_call_id", None) or getattr(part, "tool_name", "") or ""
    )


def _has_user_prompt(message) -> bool:
    return any(
        _part_kind(part) == "user-prompt"
        for part in getattr(message, "parts", ()) or ()
    )


def messages_safe_for_new_prompt(messages: list) -> list:
    pending: dict[str, int] = {}
    for index, message in enumerate(messages):
        for part in getattr(message, "parts", ()) or ():
            kind = _part_kind(part)
            key = _tool_key(part)
            if kind in ("tool-return", "retry-prompt"):
                pending.pop(key, None)
            elif kind == "tool-call":
                pending[key] = index
    if not pending:
        return list(messages)

    cut = min(pending.values())
    for index in range(cut, -1, -1):
        if _has_user_prompt(messages[index]):
            cut = index
            break
    return list(messages[:cut])


def repair_interrupted_tool_calls(messages: list) -> list:
    """Close only persisted tool calls that have no recorded result."""
    pending: dict[str, Any] = {}
    for message in messages:
        for part in getattr(message, "parts", ()) or ():
            kind, key = _part_kind(part), _tool_key(part)
            if kind == "tool-call" and key:
                pending[key] = part
            elif kind in ("tool-return", "retry-prompt") and key:
                pending.pop(key, None)
    if not pending:
        return list(messages)
    metadata = {"origin": "runtime_control", "execution_outcome": "unknown"}
    returns = [
        ToolReturnPart(
            str(getattr(part, "tool_name", "")),
            {
                "status": "unknown",
                "message": "Original tool execution outcome is unknown; no result was persisted.",
            },
            tool_call_id=key,
            outcome="failed",
            metadata={**metadata, "tool_call_id": key},
        )
        for key, part in pending.items()
    ]
    return [*messages, ModelRequest(parts=returns, metadata=metadata)]


class ChatHistory:
    __slots__ = (
        "_messages",
        "_compress_summary_state",
        "_revision",
    )

    def __init__(self):
        self._messages: list = []
        self._compress_summary_state: str | None = None
        self._revision = 0

    def update(self, result) -> None:
        """从 RunResult / StreamedRunResult 提取完整消息列表并保存。"""
        self._messages = list(result.all_messages())
        self._revision += 1

    def reset(self) -> None:
        self._messages = []
        self._compress_summary_state = None
        self._revision += 1

    def set_messages(self, messages: list) -> None:
        """直接替换消息列表（供上下文压缩等使用）。"""
        self._messages = list(messages)
        self._compress_summary_state = None
        self._revision += 1
        for message in reversed(self._messages):
            candidates = [getattr(message, "metadata", None) or {}]
            for part in message.parts:
                if _part_kind(part) == "user-prompt" and isinstance(part.content, list):
                    candidates.extend(
                        getattr(item, "metadata", None) or {} for item in part.content
                    )
            for metadata in candidates:
                if metadata.get("origin") == "context_summary":
                    self._compress_summary_state = metadata["summary"]
                    return

    @property
    def compress_summary_state(self) -> str | None:
        """上一轮压缩模型产出的 Markdown 摘要文本，供下次压缩合并。"""
        return self._compress_summary_state

    @compress_summary_state.setter
    def compress_summary_state(self, value: str | None) -> None:
        self._compress_summary_state = value

    @property
    def messages(self) -> list:
        """传入 agent.run(message_history=...) 的只读引用。"""
        return self._messages

    @property
    def revision(self) -> int:
        return self._revision


















_COMPRESS_PREFIX = "[CONTEXT_COMPRESSION_SUMMARY]"
_COMPRESS_MARKER = "<<COMPRESS_SUMMARY>>"
_COMPRESS_REQUIRED_HEADINGS = (
    "## 原始目标与当前目标",
    "## 已完成节点",
    "## 待完成节点",
    "## 工具调用与关键结果",
    "## 当前状态",
    "## 未解决问题与阻塞",
    "## 用户约束与已做决策",
    "## 恢复后下一步",
)


class CompressionValidationError(RuntimeError):
    """压缩摘要或写回消息不满足可恢复检查点契约。"""


def context_usage_breakdown(
    role: str,
    history_messages: list,
) -> dict[str, Any]:
    """基于最近一次真实模型 usage 的上下文占用。没有真实 usage 时不回退估算。"""
    ctx = get_context_config(role)
    max_tokens = get_effective_max_context(role=role)
    used = latest_usage_input_tokens(history_messages)
    total = int(used or 0)
    threshold = int(max_tokens * float(ctx["auto_compress_ratio"]))
    percent = 0.0 if max_tokens <= 0 else min(100.0, total * 100.0 / max_tokens)
    return {
        "has_usage": used is not None,
        "input": total,
        "total": total,
        "max": max_tokens,
        "threshold": threshold,
        "percent": percent,
    }


_OPENROUTER_LOCK = threading.Lock()
_OPENROUTER_META_MAP = None


def _openrouter_cache_path() -> Path:
    return logger.get_log_dir() / "cache/openrouter_models.json"


def _ensure_openrouter_maps() -> None:
    global _OPENROUTER_META_MAP
    with _OPENROUTER_LOCK:
        if _OPENROUTER_META_MAP is not None:
            return
        path = _openrouter_cache_path()
        try:
            if path.exists():
                raw = json.loads(path.read_text(encoding="utf-8"))
            else:
                metadata = settings()["model_metadata"]
                with httpx.Client(timeout=metadata["timeout"]) as client:
                    response = client.get(metadata["url"])
                    response.raise_for_status()
                    raw = response.json()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            _OPENROUTER_META_MAP = {}
            for row in raw["data"]:
                name = row["id"].lower()
                _OPENROUTER_META_MAP[name] = row
                _OPENROUTER_META_MAP.setdefault(name.rsplit("/", 1)[-1], row)
        except (OSError, ValueError, httpx.HTTPError) as exc:
            _OPENROUTER_META_MAP = {}
            logger.warning("模型元数据不可用，使用配置中的上下文容量：%s", exc)


def _lookup_openrouter_meta(name: str) -> dict | None:
    _ensure_openrouter_maps()
    rows = _OPENROUTER_META_MAP or {}
    return rows.get(name.lower()) or rows.get(name.lower().rsplit("/", 1)[-1])


def lookup_model_context(model_name: str) -> int | None:
    row = _lookup_openrouter_meta(model_name) or {}
    return row.get("top_provider", {}).get("context_length") or row.get(
        "context_length"
    )


def lookup_model_max_output_tokens(model_name: str) -> int | None:
    row = _lookup_openrouter_meta(model_name) or {}
    return row.get("top_provider", {}).get("max_completion_tokens")


def get_effective_max_context(
    model_name: str | None = None,
    *,
    role,
    context: dict | None = None,
) -> int:
    """有效上下文上限：config 覆盖 > 缓存 > 多源查找 > default_context_tokens。"""
    r: str = role if role is not None else get_context_profile_roles()[0]
    ctx = get_context_config(r) if context is None else context
    # Auxiliary roles may define a model without their own context profile. Only the
    # fallback budget is shared; model metadata and explicit role limits still win.
    fallback = ctx.get("default_context_tokens")
    if fallback is None:
        fallback = get_context_config("coordinator")["default_context_tokens"]
    fallback = int(fallback)
    mid = model_name if model_name is not None else get_model_and_params(r)[0]

    raw_max = ctx.get("max_context_tokens")
    if isinstance(raw_max, int) and raw_max > 0:
        return raw_max

    looked = lookup_model_context(mid)
    if looked:
        return looked

    return fallback


def _build_compress_user_body(summary_md: str) -> str:
    body = summary_md.strip()
    return (
        f"{_COMPRESS_PREFIX}\n"
        "以下内容为此前对话的压缩摘要（Markdown）。请结合后续消息继续推理。\n"
        f"{_COMPRESS_MARKER}\n"
        f"{body}"
    )


def _lint_compression_summary(summary_md: str) -> str:
    body = (summary_md or "").strip()
    errors: list[str] = []
    if not body:
        errors.append("压缩摘要为空")
    if "```" in body:
        errors.append("压缩摘要不能包含 Markdown code fence")
    if body.startswith(("{", "[")):
        errors.append("压缩摘要必须是 Markdown，不得输出 JSON")

    pieces = re.split(r"(?m)^[ \t]*(## [^\n]+?)[ \t]*$", body)
    sections = dict(zip(pieces[1::2], pieces[2::2]))
    headings = list(sections)
    required = list(_COMPRESS_REQUIRED_HEADINGS)
    missing = [h for h in required if h not in headings]
    if missing:
        errors.append(f"压缩摘要缺少必需标题: missing={missing!r} actual={headings!r}")
    else:
        for heading in required:
            if not sections[heading].strip(" \t\r\n-*#>"):
                errors.append(f"{heading} 缺少正文；摘要可能被截断")

    if errors:
        raise CompressionValidationError("; ".join(errors))
    return body


def _call_compressor_llm(
    *,
    system_prompt: str,
    user_content: str,
) -> str:
    from pydantic_ai import ModelRetry

    from redlotus.core.gateway import complete_text_sync

    def validate(output):
        try:
            return _lint_compression_summary(output)
        except CompressionValidationError as exc:
            raise ModelRetry(str(exc)) from exc

    return complete_text_sync(
        "compressor", system_prompt, user_content, output_validator=validate
    )


def _compression_bounds(messages, context, *, retain_tail=True):
    """Choose complete message groups to retain around the summary."""
    if not retain_tail:
        return 0, len(messages)
    starts = [
        index
        for index, message in enumerate(messages)
        if any(isinstance(part, UserPromptPart) for part in message.parts)
    ]
    starts.append(len(messages))
    head_end = (
        starts[min(int(context["head_turns"]), len(starts) - 1)] if starts[:-1] else 0
    )
    tail_start = starts[max(0, len(starts) - 1 - int(context["tail_turns"]))]
    boundaries = _closed_boundaries(messages)
    head_end = max(index for index in boundaries if index <= head_end)
    tail_start = next(
        (index for index in boundaries if index >= tail_start), len(messages)
    )
    if head_end >= tail_start:
        candidates = [index for index in boundaries if 0 < index < len(messages)]
        return (0, candidates[-1]) if candidates else None
    return head_end, tail_start


def _compression_candidate(
    messages: list,
    summary_state: str | None,
    *,
    role: str,
    force: bool,
    task_state: str | None = None,
    retain_tail: bool = True,
    context: dict | None = None,
) -> ChatHistory | None:
    if len(messages) < 2:
        return None

    ctx = get_context_config(role) if context is None else context
    max_ctx = get_effective_max_context(role=role, context=ctx)
    used = latest_usage_input_tokens(messages)
    threshold = max_ctx * float(ctx["auto_compress_ratio"])

    if not force and (used is None or used < threshold):
        return None

    bounds = _compression_bounds(messages, ctx, retain_tail=retain_tail)
    if bounds is None:
        return None
    head_end, tail_start = bounds

    prev_summary = summary_state
    from redlotus.prompts.message_text import pydantic_messages_to_text

    excerpt = pydantic_messages_to_text(
        messages[head_end:tail_start],
        include_reference_content=False,
        tool_args_max_chars=int(
            settings()["context"]["compression"]["middle_tool_args_max_chars"]
        ),
    )

    system_prompt = load_prompt("context_compress_structured_system.md")
    user_parts: list[str] = []
    if prev_summary:
        user_parts.append(
            "## 上轮压缩摘要（必须合并更新，不能丢失仍有效信息）\n\n" + prev_summary
        )
    if task_state and task_state.strip():
        user_parts.append("## 当前结构化任务状态（权威）\n\n" + task_state.strip())
    user_parts.append("## 本轮待压缩中间段\n\n" + (excerpt or "unknown"))
    user_content = "\n\n".join(user_parts)

    summary_md = _call_compressor_llm(
        system_prompt=system_prompt, user_content=user_content
    )
    summary_md = _lint_compression_summary(summary_md)
    new_body = _build_compress_user_body(summary_md)

    summary_msg = ModelRequest(
        parts=[
            UserPromptPart(
                content=[
                    TextContent(
                        new_body,
                        metadata={"origin": "context_summary", "summary": summary_md},
                    )
                ]
            )
        ],
        metadata={"origin": "context_summary", "summary": summary_md},
    )
    new_messages = messages[:head_end] + [summary_msg] + messages[tail_start:]
    logger.info("上下文压缩完成: role=%s messages=%d→%d", role, len(messages), len(new_messages))
    candidate = ChatHistory()
    candidate.set_messages(new_messages)
    return candidate


def compress_history(
    history: ChatHistory,
    *,
    role: str,
    force: bool,
    task_state: str | None = None,
    retain_tail: bool = True,
    context: dict | None = None,
) -> bool:
    """Synchronously build and apply a context-compression candidate."""
    candidate = _compression_candidate(
        list(history.messages),
        history.compress_summary_state,
        role=role,
        force=force,
        task_state=task_state,
        retain_tail=retain_tail,
        context=context,
    )
    if candidate is None:
        return False
    history.set_messages(candidate.messages)
    return True


async def get_effective_max_contexts_by_role_async(*, roles=None) -> dict[str, int]:
    roles = tuple(roles) if roles is not None else get_context_profile_roles()
    limits = await asyncio.gather(
        *(get_effective_max_context_async(role=role) for role in roles)
    )
    return dict(zip(roles, limits))


async def prewarm_effective_max_contexts_by_role_async(
    *, reason: str = "startup"
) -> dict[str, int]:
    """并行预取三角色有效上下文并写入缓存；在启动与切换模型后调用。返回各角色 max token。"""
    d = await get_effective_max_contexts_by_role_async()
    log_values = ", ".join(f"{role}={value}" for role, value in d.items())
    logger.info(
        "各角色有效上下文 token 上限（%s）: %s",
        reason,
        log_values,
    )
    return d


async def get_effective_max_context_async(
    model_name: str | None = None,
    *,
    role: str | None = None,
    context: dict | None = None,
) -> int:
    return await asyncio.to_thread(
        lambda: get_effective_max_context(model_name, role=role, context=context)
    )


async def prepare_compression(
    history: ChatHistory,
    *,
    role: str,
    force: bool,
    task_state: str | None = None,
    retain_tail: bool = True,
    context: dict | None = None,
) -> ChatHistory | None:
    """Build a detached compression candidate without changing ``history``."""
    messages = list(history.messages)
    summary_state = history.compress_summary_state
    context = dict(context) if context is not None else None
    return await asyncio.to_thread(
        _compression_candidate,
        messages,
        summary_state,
        role=role,
        force=force,
        task_state=task_state,
        retain_tail=retain_tail,
        context=context,
    )


async def compress_history_async(
    history: ChatHistory,
    *,
    role: str,
    force: bool,
    task_state: str | None = None,
    retain_tail: bool = True,
    context: dict | None = None,
) -> bool:
    revision = history.revision
    candidate = await prepare_compression(
        history,
        role=role,
        force=force,
        task_state=task_state,
        retain_tail=retain_tail,
        context=context,
    )
    if candidate is None or history.revision != revision:
        return False
    history.set_messages(candidate.messages)
    return True


def _closed_boundaries(messages: list) -> list[int]:
    pending: set[str] = set()
    boundaries = [0]
    for index, message in enumerate(messages):
        for part in getattr(message, "parts", ()):
            kind = getattr(part, "part_kind", "")
            key = getattr(part, "tool_call_id", "")
            if kind == "tool-call":
                pending.add(key)
            elif kind in ("tool-return", "retry-prompt"):
                pending.discard(key)
        if not pending:
            boundaries.append(index + 1)
    return boundaries


def _estimate_text_tokens(text):
    # Dense decimal data can tokenize digit by digit. Keep the prose estimate
    # for ASCII punctuation/spacing so ordinary documents are not overcounted.
    return sum(1 if ord(char) > 127 or char.isdigit() else 0.3 for char in text)


def estimate_context_tokens(
    messages: list, *, tools=(), include_instructions=True
) -> int:
    # Include full tool outputs: the display transcript deliberately elides them.
    from pydantic_ai import TextContent

    tokens = 0.0
    for message in messages:
        for part in getattr(message, "parts", ()):
            value = getattr(part, "content", getattr(part, "args", ""))
            if isinstance(value, (list, tuple)):
                texts = [
                    item
                    if isinstance(item, str)
                    else item.content
                    if isinstance(item, TextContent)
                    else "[media]"
                    for item in value
                ]
                tokens += sum(
                    1024 for item in value if not isinstance(item, (str, TextContent))
                )
                value = "\n".join(texts)
            elif not isinstance(value, str):
                value = str(value)
            tokens += _estimate_text_tokens(value) + 8
    if include_instructions:
        instructions = next(
            (
                m.instructions
                for m in reversed(messages)
                if getattr(m, "instructions", None)
            ),
            "",
        )
        tokens += _estimate_text_tokens(instructions)
    if tools:
        schema = [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters_json_schema,
            }
            for tool in tools
        ]
        tokens += _estimate_text_tokens(json.dumps(schema, ensure_ascii=False))
    return int(tokens + 0.999)


async def prepare_model_request(run, node, *, role: str, task_state: str = "") -> None:
    """Check capacity before every request, including requests within one tool chain."""
    compacted = await compact_request_messages(
        [*run.ctx.state.message_history, node.request], role=role, task_state=task_state
    )
    run.ctx.state.message_history[:] = compacted[:-1]
    node.request = compacted[-1]


async def compact_request_messages(
    combined, *, role: str, task_state: str = "", target=None, tools=()
) -> list:
    """Build the bounded model view; original trace persistence belongs to the runner."""
    messages, request = combined[:-1], combined[-1]
    context = target.context if target else get_context_config(role)
    limit = await get_effective_max_context_async(
        model_name=target.name if target else None, role=role, context=context
    )
    threshold = limit * float(context["auto_compress_ratio"])
    parameters = target.settings if target else get_model_and_params(role)[1]
    input_budget = limit - int(parameters.get("max_tokens") or 0)
    if input_budget <= 0:
        raise CompressionValidationError(
            "Configured output budget leaves no input capacity; check context and max_tokens in config.json."
        )
    # Last model usage excludes its newly generated tools and the pending tool responses.
    recent_tokens = estimate_context_tokens(combined, tools=tools)
    for index in range(len(messages) - 1, -1, -1):
        if (
            isinstance(messages[index], ModelResponse)
            and messages[index].usage.input_tokens
        ):
            recent_tokens = max(
                recent_tokens,
                messages[index].usage.input_tokens
                + estimate_context_tokens(combined[index:], include_instructions=False),
            )
            break
    if recent_tokens < threshold and recent_tokens < input_budget:
        return combined
    history = ChatHistory()
    history.set_messages(combined)
    bounds = _compression_bounds(combined, context)
    retained = [*combined[: bounds[0]], *combined[bounds[1] :]] if bounds else combined
    retain_tail = estimate_context_tokens(retained, tools=tools) < min(
        threshold, input_budget
    )
    changed = await compress_history_async(
        history,
        role=role,
        force=True,
        task_state=task_state,
        retain_tail=retain_tail,
        context={**context, "max_context_tokens": limit},
    )
    if not changed:
        raise CompressionValidationError(
            "Context has no safe compaction boundary; original messages are retained."
        )
    compacted = history.messages
    if request.instructions is not None:
        compacted[-1].instructions = request.instructions
    if not retain_tail:
        # All calls in this batch have completed; their summary can replace the entire batch.
        latest_inputs = [
            part for part in request.parts if isinstance(part, UserPromptPart)
        ]
        compacted[-1].parts.extend(latest_inputs)
    if estimate_context_tokens(compacted, tools=tools) >= input_budget:
        raise CompressionValidationError(
            "The current input exceeds the context window; use a smaller input."
        )
    return compacted


MODEL_MESSAGES_GLOB = "model_messages.json"


@dataclass(frozen=True)
class BillableTokens:
    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True)
class ResolvedTokenPrice:
    model_name: str
    prompt_usd_per_token: Decimal
    completion_usd_per_token: Decimal
    source: str


@dataclass
class PriceEstimate:
    prompt_usd_per_token: Decimal
    completion_usd_per_token: Decimal
    source: str
    input_usd: Decimal = Decimal("0")
    output_usd: Decimal = Decimal("0")
    total_usd: Decimal = Decimal("0")

    def add(self, billable: BillableTokens) -> None:
        input_cost = Decimal(billable.prompt_tokens) * self.prompt_usd_per_token
        output_cost = (
            Decimal(billable.completion_tokens) * self.completion_usd_per_token
        )
        self.input_usd += input_cost
        self.output_usd += output_cost
        self.total_usd += input_cost + output_cost


@dataclass
class UsageTotals:
    responses: int = 0
    missing_usage_responses: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    prompt_billable_tokens: int = 0
    completion_billable_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0

    def add_usage(self, usage: Any, billable: BillableTokens) -> None:
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.reasoning_tokens += usage.details.get("reasoning_tokens", 0)
        self.prompt_billable_tokens += billable.prompt_tokens
        self.completion_billable_tokens += billable.completion_tokens
        details = usage.details
        if (
            "prompt_cache_hit_tokens" in details
            and "prompt_cache_miss_tokens" in details
        ):
            self.cache_hit_tokens += details["prompt_cache_hit_tokens"]
            self.cache_miss_tokens += details["prompt_cache_miss_tokens"]
        elif usage.cache_read_tokens:
            self.cache_hit_tokens += usage.cache_read_tokens
            self.cache_miss_tokens += usage.input_tokens - usage.cache_read_tokens

    def add_totals(self, other: "UsageTotals") -> None:
        for name in UsageTotals.__dataclass_fields__:
            setattr(self, name, getattr(self, name) + getattr(other, name))


@dataclass
class ModelUsageSummary:
    model_name: str
    totals: UsageTotals = field(default_factory=UsageTotals)
    price: PriceEstimate | None = None
    price_unavailable_responses: int = 0

    def add_usage(
        self,
        usage: Any,
        billable: BillableTokens,
        resolved_price: ResolvedTokenPrice | None,
    ) -> None:
        self.totals.responses += 1
        self.totals.add_usage(usage, billable)
        if resolved_price is None:
            self.price_unavailable_responses += 1
            return
        if self.price is None:
            self.price = PriceEstimate(
                prompt_usd_per_token=resolved_price.prompt_usd_per_token,
                completion_usd_per_token=resolved_price.completion_usd_per_token,
                source=resolved_price.source,
            )
        elif self.price.source != resolved_price.source:
            sources = sorted({self.price.source, resolved_price.source})
            self.price.source = ", ".join(sources)
        self.price.add(billable)


@dataclass
class ContentTokenStats:
    """New content counters; user input is estimated, generation is reported usage."""

    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    unmetered_attachments: int = 0
    incomplete_sessions: int = 0
    missing_reasoning_responses: int = 0
    missing_usage_responses: int = 0

    def add(self, other):
        for name in self.__dataclass_fields__:
            setattr(self, name, getattr(self, name) + getattr(other, name))

    @property
    def complete(self):
        return not (self.unmetered_attachments or self.incomplete_sessions
                    or self.missing_reasoning_responses or self.missing_usage_responses)


@dataclass
class UsageFileSummary:
    path: Path
    meta: dict[str, Any]
    totals: UsageTotals = field(default_factory=UsageTotals)
    by_model: dict[str, ModelUsageSummary] = field(default_factory=dict)
    by_agent: dict[str, UsageTotals] = field(default_factory=dict)
    content: ContentTokenStats = field(default_factory=ContentTokenStats)


@dataclass
class UsageReport:
    files: list[UsageFileSummary] = field(default_factory=list)
    totals: UsageTotals = field(default_factory=UsageTotals)
    by_model: dict[str, ModelUsageSummary] = field(default_factory=dict)


PriceResolver = Callable[[str], ResolvedTokenPrice | None]


def billable_tokens_from_usage(usage: Any) -> BillableTokens:
    prompt_cache_total = usage.details.get(
        "prompt_cache_hit_tokens", 0
    ) + usage.details.get("prompt_cache_miss_tokens", 0)
    prompt_tokens = max(usage.input_tokens, prompt_cache_total)
    completion_tokens = usage.output_tokens

    return BillableTokens(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


def read_usage_messages(path: Path):
    """Read retained counters even when old message bodies have been pruned."""
    session = SessionFile.load(path)
    rows = []
    for row in session.usage_responses():
        row = dict(row)
        role = row.pop("role", "coordinator")
        row.pop("turn_id", None)
        rows.append({**row, "kind": "response", "parts": [], "metadata": {"role": role}})
    return ModelMessagesTypeAdapter.validate_python(rows), session.info()


def model_message_files_for_path(path: Path) -> list[Path]:
    p = Path(path).expanduser()
    if p.is_file():
        return [p]
    if p.is_dir():
        return sorted(p.rglob(MODEL_MESSAGES_GLOB), key=lambda item: str(item))
    return []


def session_model_message_files(
    conversations_root_path: Path, session_key: str
) -> list[Path]:
    root = Path(conversations_root_path).resolve()
    path = (root / session_key / "model_messages.json").resolve()
    return [path] if path.is_relative_to(root) and path.is_file() else []


def latest_usage_input_tokens(messages: Iterable[Any]) -> int | None:
    for message in reversed(list(messages)):
        if isinstance(message, ModelResponse):
            usage = message.usage
            if usage.has_values():
                return usage.input_tokens
    return None


def summarize_messages(
    messages: Iterable[Any],
    *,
    meta: dict[str, Any] | None = None,
    path: Path | None = None,
    price_resolver: PriceResolver | None = None,
) -> UsageFileSummary:
    resolver = price_resolver or resolve_token_price
    summary = UsageFileSummary(
        path=Path(path) if path is not None else Path(), meta=meta or {}
    )
    summary.content = ContentTokenStats(**summary.meta.get("input_usage", {"incomplete_sessions": 1}))
    for message in messages:
        if (
            not isinstance(message, ModelResponse)
            or (message.metadata or {}).get("origin") == "execution_status"
        ):
            continue
        summary.totals.responses += 1
        role = (message.metadata or {}).get("role", "coordinator")
        agent_totals = summary.by_agent.setdefault(role, UsageTotals())
        agent_totals.responses += 1
        usage = message.usage
        if not usage.has_values():
            summary.totals.missing_usage_responses += 1
            agent_totals.missing_usage_responses += 1
            summary.content.missing_usage_responses += 1
            summary.content.missing_reasoning_responses += 1
            continue
        summary.content.output_tokens += usage.output_tokens
        reasoning = usage.details.get("reasoning_tokens")
        if reasoning is None or not 0 <= reasoning <= usage.output_tokens:
            summary.content.missing_reasoning_responses += 1
        else:
            summary.content.reasoning_tokens += reasoning
        model_name = str(getattr(message, "model_name", "") or "unknown")
        billable = billable_tokens_from_usage(usage)
        summary.totals.add_usage(usage, billable)
        agent_totals.add_usage(usage, billable)
        model_summary = summary.by_model.setdefault(
            model_name, ModelUsageSummary(model_name=model_name)
        )
        model_summary.add_usage(usage, billable, resolver(model_name))
    return summary


def summarize_usage_files(paths: Iterable[Path], *, price_resolver=None) -> UsageReport:
    files, messages = [], []
    for path in paths:
        batch, meta = read_usage_messages(Path(path))
        files.append(
            summarize_messages(
                batch, meta=meta, path=path, price_resolver=price_resolver
            )
        )
        messages.extend(batch)
    total = summarize_messages(messages, price_resolver=price_resolver)
    return UsageReport(files=files, totals=total.totals, by_model=total.by_model)


def resolve_token_price(model_name: str) -> ResolvedTokenPrice | None:
    from decimal import InvalidOperation

    pricing = (_lookup_openrouter_meta(model_name) or {}).get("pricing") or {}
    try:
        return ResolvedTokenPrice(
            model_name,
            Decimal(str(pricing["prompt"])),
            Decimal(str(pricing["completion"])),
            "openrouter",
        )
    except (KeyError, InvalidOperation):
        return None
