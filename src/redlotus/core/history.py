"""SDK context integrity, compression, and retained usage accounting."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable

from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextContent,
    UserPromptPart,
)

from redlotus.prompts.prompt import load_prompt
from redlotus.runtime import logging as logger
from redlotus.runtime.config import (
    ConfigError,
    get_context_config,
    get_context_profile_roles,
    get_model_and_params,
)
from redlotus.runtime.network import (
    _lookup_openrouter_meta,
    lookup_model_context,
)
from redlotus.sessions.context import ChatHistory, _context_summary_metadata
from redlotus.sessions.storage import SessionFile, _response_id, _response_usage


def compression_summary_headings() -> list[str]:
    """Validate the same section names the compressor actually receives."""
    return re.findall(r"(?m)^## .+$", load_prompt("context_compress_structured_system.md"))


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


def get_effective_max_context(
    model_name: str | None = None,
    *,
    role,
    context: dict | None = None,
) -> int:
    """Resolve the selected role's explicit capacity or its model metadata."""
    r: str = role if role is not None else get_context_profile_roles()[0]
    ctx = get_context_config(r) if context is None else context
    raw_max = ctx.get("max_context_windows")
    if raw_max is not None:
        if isinstance(raw_max, int) and not isinstance(raw_max, bool) and raw_max > 0:
            return raw_max
        raise ConfigError(f"无效配置 models.{r}.max_context_windows；应为正整数或 null。")

    mid = model_name if model_name is not None else get_model_and_params(r)[0]
    looked = lookup_model_context(mid)
    if looked:
        return looked
    raise ConfigError(
        f"无法获取模型 {mid} 的上下文容量；请配置 models.{r}.max_context_windows。"
    )


def _lint_compression_summary(summary_md: str) -> str:
    body = (summary_md or "").strip()
    errors: list[dict] = []
    if not body:
        errors.append({"error": "compression_empty"})
    if "```" in body:
        errors.append({"error": "compression_fence"})
    if body.startswith(("{", "[")):
        errors.append({"error": "compression_json"})

    pieces = re.split(r"(?m)^[ \t]*(## [^\n]+?)[ \t]*$", body)
    sections = dict(zip(pieces[1::2], pieces[2::2]))
    headings = list(sections)
    required = compression_summary_headings()
    missing = [h for h in required if h not in headings]
    if missing:
        errors.append({"error": "compression_headings", "missing": missing, "actual": headings})
    else:
        for heading in required:
            if not sections[heading].strip(" \t\r\n-*#>"):
                errors.append({"error": "compression_section_empty", "heading": heading})

    if errors:
        raise CompressionValidationError(json.dumps(errors, ensure_ascii=False))
    return body


async def _call_compressor_llm(
    *,
    system_prompt: str,
    user_content: str,
) -> str:
    from pydantic_ai import ModelRetry

    from redlotus.core.gateway import complete_text

    def validate(output):
        try:
            return _lint_compression_summary(output)
        except CompressionValidationError as exc:
            raise ModelRetry(str(exc)) from exc

    return await complete_text(
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
        starts[min(int(context["compress_head_turns"]), len(starts) - 1)] if starts[:-1] else 0
    )
    tail_start = starts[max(0, len(starts) - 1 - int(context["compress_tail_turns"]))]
    boundaries = _closed_boundaries(messages)
    head_end = max(index for index in boundaries if index <= head_end)
    tail_start = next(
        (index for index in boundaries if index >= tail_start), len(messages)
    )
    if head_end >= tail_start:
        candidates = [index for index in boundaries if 0 < index < len(messages)]
        return (0, candidates[-1]) if candidates else None
    return head_end, tail_start


async def prepare_compression(
    history: ChatHistory,
    *,
    role: str,
    force: bool,
    task_state: str | None = None,
    retain_tail: bool = True,
    context: dict | None = None,
) -> ChatHistory | None:
    """Build a detached candidate; native async model I/O shares cancellation and usage."""
    messages, summary_state = list(history.messages), history.compress_summary_state
    if len(messages) < 2:
        return None

    ctx = get_context_config(role) if context is None else context
    max_ctx = await get_effective_max_context_async(role=role, context=ctx)
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

    middle_messages = messages[head_end:tail_start]
    if prev_summary:
        # The prior checkpoint is supplied separately below; keep real user text even
        # if it happens to equal that checkpoint.
        middle_messages = [
            message
            for message in middle_messages
            if _context_summary_metadata(message) is None
        ]
    excerpt = pydantic_messages_to_text(middle_messages)

    system_prompt = load_prompt("context_compress_structured_system.md")
    user_parts = {"transcript": excerpt}
    if prev_summary:
        user_parts["previous_summary"] = prev_summary
    if task_state and task_state.strip():
        user_parts["task_state"] = task_state.strip()
    user_content = json.dumps(user_parts, ensure_ascii=False)

    summary_md = await _call_compressor_llm(
        system_prompt=system_prompt, user_content=user_content
    )
    summary_md = _lint_compression_summary(summary_md)
    retained = messages[:head_end] + messages[tail_start:]
    metadata = {
        "origin": "context_summary", "summary": summary_md,
        "prior_usage_responses": [_response_id(message) for message in retained if isinstance(message, ModelResponse)],
    }
    from redlotus.prompts.prompt import session_prompt_from_history

    summary_msg = ModelRequest(
        parts=[
            UserPromptPart(
                content=[
                    TextContent(
                        summary_md,
                        metadata=metadata,
                    )
                ]
            )
        ],
        metadata=metadata,
        instructions=session_prompt_from_history(messages),
    )
    new_messages = messages[:head_end] + [summary_msg] + messages[tail_start:]
    logger.info("上下文压缩完成: role=%s messages=%d→%d", role, len(messages), len(new_messages))
    candidate = ChatHistory()
    candidate.set_messages(new_messages)
    return candidate


async def compress_histories(sources, *, task_state, persist, is_current):
    """Prepare role candidates together and adopt only after their durable commit."""
    revisions = {role: source.revision for role, source in sources.items()}

    def current():
        return is_current() and all(source.revision == revisions[role] for role, source in sources.items())

    candidates = {}
    for role, source in sources.items():
        candidates[role] = await prepare_compression(
            source, role=role, force=True, retain_tail=False, task_state=task_state,
        )
        if not current():
            return ["会话已改变，压缩候选已丢弃。"]
    if not any(candidates.values()):
        return ["当前上下文无需压缩。"]
    await persist(candidates)
    if not current():
        return ["会话已改变，压缩候选不再应用。"]
    for role, candidate in candidates.items():
        if candidate is not None:
            sources[role].set_messages(candidate.messages)
    return [f"{role}: {'已压缩并保存' if candidate else '无需压缩'}" for role, candidate in candidates.items()]


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




async def compact_request_messages(
    combined, *, role: str, task_state: str = "", target=None, tools=()
) -> list:
    """Build the bounded model view; original trace persistence belongs to the runner."""
    request = combined[-1]
    context = target.context if target else get_context_config(role)
    limit = await get_effective_max_context_async(
        model_name=target.name if target else None, role=role, context=context
    )
    threshold = limit * float(context["auto_compress_ratio"])
    recent_tokens = latest_usage_input_tokens(combined)
    if recent_tokens is None or recent_tokens < threshold:
        return combined
    history = ChatHistory()
    history.set_messages(combined)
    candidate = await prepare_compression(
        history,
        role=role,
        force=True,
        task_state=task_state,
        context={**context, "max_context_windows": limit},
    )
    if candidate is None:
        raise CompressionValidationError(
            "Context has no safe compaction boundary; original messages are retained."
        )
    compacted = candidate.messages
    if request.instructions is not None:
        compacted[-1].instructions = request.instructions
    return compacted


MODEL_MESSAGES_GLOB = "model_messages.json"
USAGE_CATEGORY_LABELS = {"main": "主 Agent", "agent": "普通子 Agent", "auxiliary": "辅助角色", "unknown": "旧记录／身份未分类"}


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
    by_category: dict[str, UsageTotals] = field(default_factory=dict)
    content: ContentTokenStats = field(default_factory=ContentTokenStats)


@dataclass
class UsageReport:
    files: list[UsageFileSummary] = field(default_factory=list)
    totals: UsageTotals = field(default_factory=UsageTotals)
    by_model: dict[str, ModelUsageSummary] = field(default_factory=dict)
    by_agent: dict[str, UsageTotals] = field(default_factory=dict)
    by_category: dict[str, UsageTotals] = field(default_factory=dict)


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
        metadata = {key: row.pop(key, None) for key in ("role", "category", "turn_id", "agent_id", "invocation")}
        rows.append({**row, "kind": "response", "parts": [], "metadata": metadata})
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
    recent = list(reversed(list(messages)))
    response = next((message for message in recent if _response_usage(message) is not None), None)
    if response is None:
        return None
    checkpoint = next((info for message in recent if (info := _context_summary_metadata(message))), {})
    if _response_id(response) in checkpoint.get("prior_usage_responses", []):
        return None
    return response.usage.input_tokens or None


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
        if _response_usage(message) is None:
            continue
        summary.totals.responses += 1
        role = (message.metadata or {}).get("role", "coordinator")
        agent_totals = summary.by_agent.setdefault(role, UsageTotals())
        category = (message.metadata or {}).get("category") or "unknown"
        category_totals = summary.by_category.setdefault(category, UsageTotals())
        for totals in (agent_totals, category_totals):
            totals.responses += 1
        usage = message.usage
        if not usage.has_values():
            summary.totals.missing_usage_responses += 1
            for totals in (agent_totals, category_totals):
                totals.missing_usage_responses += 1
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
        category_totals.add_usage(usage, billable)
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
    return UsageReport(files=files, totals=total.totals, by_model=total.by_model,
                       by_agent=total.by_agent, by_category=total.by_category)


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
