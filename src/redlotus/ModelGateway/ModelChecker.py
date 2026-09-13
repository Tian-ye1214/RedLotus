"""上下文窗口探测与各角色历史压缩（中间段摘录 + 结构化 Markdown 摘要）。"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from redlotus.tools.memory.chat_history import ChatHistory

import httpx

from redlotus.config.app_config import (
    get_context_config,
    get_context_profile_roles,
    get_model_and_params,
    settings,
)
from redlotus.prompt import (
    load_prompt,
)

from redlotus.ModelGateway.usage_accounting import latest_usage_input_tokens

from redlotus.infra import logger

from pydantic_ai.messages import (
    TextContent,
    ModelRequest,
    ModelResponse,
    UserPromptPart,
)

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


def _save_compress_debug_artifacts(
    *,
    role: str,
    system_prompt: str,
    user_content: str,
    summary_md: str,
    messages_before: list,
    new_messages: list,
    head_end: int,
    tail_start: int,
) -> None:
    from redlotus.tools.memory.message_text import pydantic_messages_to_text

    root = logger.get_log_dir() / "context_compress_debug"
    root.mkdir(parents=True, exist_ok=True)
    run_dir = root / f"{int(time.time() * 1000)}_{role}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "before_messages.md").write_text(
        pydantic_messages_to_text(messages_before), encoding="utf-8"
    )
    before_llm = f"## compressor system\n\n{system_prompt}\n\n## compressor user\n\n{user_content}\n"
    (run_dir / "before_compress.md").write_text(before_llm, encoding="utf-8")
    (run_dir / "compressor_output.md").write_text(summary_md, encoding="utf-8")
    (run_dir / "after_context.md").write_text(
        pydantic_messages_to_text(new_messages), encoding="utf-8"
    )
    (run_dir / "slice_bounds.txt").write_text(
        f"role={role}\nhead_end={head_end}\ntail_start={tail_start}\n",
        encoding="utf-8",
    )
    logger.info("上下文压缩调试已保存: %s", run_dir)


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
    if body.startswith("{") or body.startswith("["):
        errors.append("压缩摘要必须是 Markdown，不得输出 JSON")

    pieces = re.split(r"(?m)^[ \t]*(## [^\n]+?)[ \t]*$", body)
    sections = dict(zip(pieces[1::2], pieces[2::2]))
    headings = list(sections)
    required = list(_COMPRESS_REQUIRED_HEADINGS)
    missing = [h for h in required if h not in headings]
    if missing:
        errors.append(f"压缩摘要缺少必需标题: missing={missing!r} actual={headings!r}")
    else:
        bodies = sections
        if not bodies["## 原始目标与当前目标"].strip():
            errors.append("`原始目标与当前目标` 不能为空")
        if not (bodies["## 已完成节点"].strip() or bodies["## 待完成节点"].strip()):
            errors.append("`已完成节点` / `待完成节点` 至少一个不能为空")

    if errors:
        raise CompressionValidationError("; ".join(errors))
    return body


def _call_compressor_llm(
    *,
    system_prompt: str,
    user_content: str,
) -> str:
    from redlotus.ModelGateway.gateway import complete_text_sync

    return complete_text_sync("compressor", system_prompt, user_content).strip()


def compress_history(
    history: ChatHistory,
    *,
    role: str,
    force: bool,
    task_state: str | None = None,
    retain_tail: bool = True,
    context: dict | None = None,
) -> bool:
    """
    压缩三步：1) 按阈值或 force 触发；2) 头尾保留，中间段展成 Markdown 摘录；
    3) 压缩模型输出固定结构的 Markdown，写入一条 User 摘要消息。
    """
    messages = list(history.messages)
    if len(messages) < 2:
        return False

    ctx = get_context_config(role) if context is None else context
    max_ctx = get_effective_max_context(role=role, context=ctx)
    used = latest_usage_input_tokens(messages)
    threshold = max_ctx * float(ctx["auto_compress_ratio"])

    if not force and (used is None or used < threshold):
        return False

    starts = [
        index
        for index, message in enumerate(messages)
        if any(isinstance(part, UserPromptPart) for part in message.parts)
    ]
    starts.append(len(messages))
    head_end = (
        starts[min(int(ctx["head_turns"]), len(starts) - 1)] if starts[:-1] else 0
    )
    tail_start = starts[max(0, len(starts) - 1 - int(ctx["tail_turns"]))]

    boundaries = _closed_boundaries(messages)
    head_end = max(index for index in boundaries if index <= head_end)
    tail_start = next(
        (index for index in boundaries if index >= tail_start), len(messages)
    )
    if not retain_tail:
        head_end, tail_start = 0, len(messages)
    elif head_end >= tail_start:
        candidates = [index for index in boundaries if 0 < index < len(messages)]
        if not candidates:
            return False
        head_end, tail_start = 0, candidates[-1]

    prev_summary = history.compress_summary_state
    from redlotus.tools.memory.message_text import pydantic_messages_to_text

    excerpt = pydantic_messages_to_text(
        messages[head_end:tail_start],
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
    _save_compress_debug_artifacts(
        role=role,
        system_prompt=system_prompt,
        user_content=user_content,
        summary_md=summary_md,
        messages_before=messages,
        new_messages=new_messages,
        head_end=head_end,
        tail_start=tail_start,
    )
    history.set_messages(new_messages)
    history.compress_summary_state = summary_md.strip()
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


async def compress_history_async(
    history: ChatHistory,
    *,
    role: str,
    force: bool,
    task_state: str | None = None,
    retain_tail: bool = True,
    context: dict | None = None,
) -> bool:
    return await asyncio.to_thread(
        compress_history,
        history,
        role=role,
        force=force,
        task_state=task_state,
        retain_tail=retain_tail,
        context=context,
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


def _estimate_text_tokens(text):
    return sum(1 if ord(char) > 127 else 0.3 for char in text)


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
    from redlotus.tools.memory.chat_history import ChatHistory

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
    boundaries = _closed_boundaries(combined)
    last_closed = max((i for i in boundaries if i < len(combined)), default=0)
    retain_tail = estimate_context_tokens(combined[last_closed:], tools=tools) < min(
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
