"""Models usage responsibilities."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable

from pydantic_ai.messages import ModelMessagesTypeAdapter

import redlotus.models.context as context
from redlotus.storage.session import SessionFile, _response_usage


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
    by_agent: dict[str, UsageTotals] = field(default_factory=dict)


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
        return sorted(p.rglob(context.MODEL_MESSAGES_GLOB), key=lambda item: str(item))
    return []


def session_model_message_files(
    conversations_root_path: Path, session_key: str
) -> list[Path]:
    root = Path(conversations_root_path).resolve()
    path = (root / session_key / "model_messages.json").resolve()
    return [path] if path.is_relative_to(root) and path.is_file() else []


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
    return UsageReport(files=files, totals=total.totals, by_model=total.by_model, by_agent=total.by_agent)


def resolve_token_price(model_name: str) -> ResolvedTokenPrice | None:
    from decimal import InvalidOperation

    pricing = (context._lookup_openrouter_meta(model_name) or {}).get("pricing") or {}
    try:
        return ResolvedTokenPrice(
            model_name,
            Decimal(str(pricing["prompt"])),
            Decimal(str(pricing["completion"])),
            "openrouter",
        )
    except (KeyError, InvalidOperation):
        return None
