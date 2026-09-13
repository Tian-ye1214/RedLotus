from __future__ import annotations


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


class ChatHistory:
    __slots__ = (
        "_messages",
        "_compress_summary_state",
    )

    def __init__(self):
        self._messages: list = []
        self._compress_summary_state: str | None = None

    def update(self, result) -> None:
        """从 RunResult / StreamedRunResult 提取完整消息列表并保存。"""
        self._messages = list(result.all_messages())

    def reset(self) -> None:
        self._messages = []
        self._compress_summary_state = None

    def set_messages(self, messages: list) -> None:
        """直接替换消息列表（供上下文压缩等使用）。"""
        self._messages = list(messages)
        self._compress_summary_state = None
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
