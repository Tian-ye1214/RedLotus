from __future__ import annotations

import asyncio
import json
import copy
import hashlib
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic_ai.messages import ModelMessagesTypeAdapter
from redlotus.infra.paths import session_data_dir

from redlotus.infra.persist_utils import (
    atomic_write_json,
    file_lock,
    iso_utc_now,
    safe_segment,
)
from redlotus.workspace.workspace import (
    MODEL_MESSAGES_SUFFIX,
    conversations_root,
    snapshot_base_from_loadable,
    snapshot_basename,
)


def read_saved_model_messages_file(path: Path) -> tuple[list[Any], dict[str, Any]]:
    """从 `*_ModelMessages.json` 读取 `model_messages`，校验并还原为 pydantic-ai 消息对象。"""
    path = Path(path)
    with file_lock(path):
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    raw = data.get("model_messages")
    if not isinstance(raw, list):
        raise ValueError("文件格式无效：缺少 model_messages 数组")
    meta_raw = data.get("meta")
    meta: dict[str, Any] = dict(meta_raw) if isinstance(meta_raw, dict) else {}
    meta["saved_at"] = data.get("saved_at")
    messages = ModelMessagesTypeAdapter.validate_python(raw)
    return messages, meta


class ConversationLog:
    """Append original events to JSONL and save the separate loadable model-context view."""

    def __init__(
        self,
        name: str,
        date: str | None,
        topic: str | None,
        *,
        sub_id: str | None = None,
        existing_run_base: Path | None = None,
        workspace=None,
    ) -> None:
        self._name = safe_segment(name, 40)
        self._date = safe_segment(date or "", 16)
        self._topic = safe_segment(topic or "", 80)
        self._sub_id = safe_segment(sub_id, 60) if sub_id else None
        self._root = (
            session_data_dir(workspace)
            if workspace is not None
            else conversations_root()
        )
        self._run_base: Path | None = existing_run_base
        self._init_lock = threading.Lock()
        self._journal_seen: set[str] | None = None

    def model_messages_path(self) -> Path | None:
        if self._run_base is None:
            return None
        return self._run_base.with_name(self._run_base.name + MODEL_MESSAGES_SUFFIX)

    async def save(
        self, model_messages: list[Any], *, extra: dict[str, Any] | None = None
    ) -> None:
        """模型返回后调用；异步落盘，不阻塞事件循环。同一会话多次调用覆盖同一对文件。"""
        with self._init_lock:
            if self._run_base is None:
                if not self._date or not self._topic:
                    return
                self._root.mkdir(parents=True, exist_ok=True)
                stem = snapshot_basename(
                    self._name,
                    self._date,
                    self._topic,
                    sub_id=self._sub_id,
                )
                self._run_base = self._root / stem
        base = self._run_base
        if base is None:
            return
        snap = copy.deepcopy(model_messages)

        write = asyncio.create_task(
            asyncio.to_thread(self._write, base, snap, dict(extra) if extra else None)
        )
        try:
            await asyncio.shield(write)
        except asyncio.CancelledError:
            await write
            raise

    def _append_journal(self, base: Path, raw: list[dict], meta: dict) -> None:
        """Keep every original message version, even when the model view is compacted."""
        path = base.with_name(base.name + ".jsonl")
        with file_lock(path):
            if self._journal_seen is None:
                self._journal_seen = set()
                if path.exists():
                    for line in path.read_text(encoding="utf-8").splitlines():
                        try:
                            self._journal_seen.add(
                                self._message_id(json.loads(line)["message"])
                            )
                        except (ValueError, KeyError):
                            continue
            entries = []
            new_ids = set()
            for message in raw:
                digest = self._message_id(message)
                if digest not in self._journal_seen and digest not in new_ids:
                    origin = (message.get("metadata") or {}).get("origin")
                    entries.append(
                        json.dumps(
                            dict(
                                event_id=digest,
                                saved_at=iso_utc_now(),
                                meta={**meta, **({"origin": origin} if origin else {})},
                                message=message,
                            ),
                            ensure_ascii=False,
                        )
                    )
                    new_ids.add(digest)
            if entries:
                with path.open("a", encoding="utf-8") as stream:
                    stream.write("\n".join(entries) + "\n")
                    stream.flush()
                self._journal_seen.update(new_ids)

    @staticmethod
    def _message_id(message: dict) -> str:
        # The SDK attaches request/run metadata after submission. That is not a new message.
        identity = dict(kind=message["kind"], parts=message["parts"])
        if message["kind"] == "response":
            identity["timestamp"] = message.get("timestamp")
        encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _write(
        self, base: Path, model_messages: list[Any], extra: dict[str, Any] | None
    ) -> None:
        raw = ModelMessagesTypeAdapter.dump_python(model_messages, mode="json")
        saved_at = iso_utc_now()
        meta: dict[str, Any] = {
            "agent": self._name,
            "date": self._date,
            "topic": self._topic,
        }
        if self._sub_id:
            if self._name == "worker":
                meta["sub_id"] = self._sub_id
            else:
                meta["session_id"] = self._sub_id
        if extra:
            meta.update(extra)
        model_path = self.model_messages_path()
        model_path.parent.mkdir(parents=True, exist_ok=True)
        self._append_journal(base, raw, meta)
        with file_lock(model_path):
            atomic_write_json(
                model_path,
                {"saved_at": saved_at, "meta": meta, "model_messages": raw},
            )


class SessionConversationLogs:
    """一次用户任务内各 Agent 的落盘日志；共享同一日期和主题目录，按角色名懒创建 ConversationLog。

    on_activate(date, topic) -- 首次 ensure() 成功时调用
    on_reset()               -- reset() 时调用
    """

    __slots__ = ("_date", "_topic", "_session_id", "_logs", "_on_activate", "_on_reset")

    def __init__(
        self,
        on_activate: Any = None,
        on_reset: Any = None,
    ) -> None:
        self._date: str | None = None
        self._topic: str | None = None
        self._session_id: str | None = None
        self._logs: dict[str, ConversationLog] = {}
        self._on_activate = on_activate
        self._on_reset = on_reset

    def ensure(self, topic_hint: str) -> None:
        """绑定本次任务的日期与主题；在 reset() 之前重复调用无效。首次绑定后触发 on_activate。"""
        if self._date is not None:
            return
        self._date = safe_segment(datetime.now().strftime("%Y%m%d"), 16)
        self._topic = safe_segment((topic_hint.strip() or "default")[:200], 80)
        self._session_id = safe_segment(datetime.now().strftime("%H%M%S%f"), 16)
        if self._on_activate:
            self._on_activate(self._date, self._topic)

    def reset(self) -> None:
        self._date = None
        self._topic = None
        self._session_id = None
        self._logs.clear()
        if self._on_reset:
            self._on_reset()

    def bind_loaded_snapshot(
        self, agent_name: str, load_path: Path, meta: dict[str, Any]
    ) -> None:
        """将会话日志绑定到 /load 的原始快照文件，后续保存继续覆盖该文件。"""
        p = Path(load_path)
        base = snapshot_base_from_loadable(p)
        date = safe_segment(str(meta.get("date") or ""), 16)
        topic = safe_segment(str(meta.get("topic") or ""), 80)
        if not date:
            date = safe_segment(datetime.now().strftime("%Y%m%d"), 16)
        if not topic:
            topic = safe_segment(base.name, 80)
        if (self._date, self._topic) != (date, topic):
            self._logs.clear()
        self._date = date
        self._topic = topic
        key = safe_segment(agent_name, 40)
        instance_raw = meta.get("session_id") or meta.get("sub_id")
        instance_id = safe_segment(str(instance_raw), 60) if instance_raw else None
        self._session_id = instance_id
        self._logs[key] = ConversationLog(
            key,
            self._date,
            self._topic,
            sub_id=instance_id,
            existing_run_base=base,
        )
        if self._on_activate:
            self._on_activate(self._date, self._topic)

    def session_key(self) -> str | None:
        if self._date is not None and self._topic is not None:
            return f"{self._date}/{self._topic}"
        return None

    def for_agent(self, name: str) -> ConversationLog:
        key = safe_segment(name, 40)
        if key not in self._logs:
            self._logs[key] = ConversationLog(
                key,
                self._date,
                self._topic,
                sub_id=self._session_id,
            )
        return self._logs[key]
