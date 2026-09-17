"""Live acceptance for project-local storage, sessions, memory, and recovery.

The harness deliberately uses the application with the selected source config and
the normal dotenv fallback.  It neither substitutes a model nor manufactures a
memory result.  Each batch owns a supplied E: run directory for config copies,
project data, and a compact, non-conversational evidence report; native LanceDB
storage is routed to its stable evaluation namespace on the operator volume.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from uuid import uuid4


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
SOURCE = ROOT / "src"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
sys.path[:] = [
    str(SOURCE),
    *[
        path
        for path in sys.path
        if Path(path).resolve() != SOURCE
        and not (Path(path).name == "src" and (Path(path) / "redlotus").is_dir())
    ],
]

from session_acceptance import ApplicationDriver, Evidence, configure, write_json  # noqa: E402


DEADLINE_SECONDS = 600
DEFAULT_DEPENDENCIES = Path(r"D:\RedLotusBenchmark\runtime\dependencies")


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _run_directory(path: Path) -> Path:
    """Reject the system drive before the app or a dependency can create test data."""
    resolved = path.expanduser().resolve()
    if resolved.drive.casefold() == "c:":
        raise ValueError("Acceptance root must not be on C:")
    return resolved


def _remaining(evidence: Evidence, *, reserve: float = 20.0) -> float:
    return max(1.0, DEADLINE_SECONDS - reserve - (time.monotonic() - evidence.started))


def _records(payload: str) -> list[dict]:
    """Decode a real memory-reader response without treating a decode failure as a hit."""
    try:
        value = json.loads(payload)
    except (TypeError, json.JSONDecodeError):
        return []
    return [row for row in value.get("memories", []) if isinstance(row, dict)]


def _contains_anchor(rows: list[dict], anchor: str, *, scope: str) -> bool:
    return any(
        row.get("scope") == scope
        and anchor in json.dumps(row, ensure_ascii=False, sort_keys=True)
        for row in rows
    )


def _trace_agent_roles(trace: object) -> dict[str, str]:
    """Bind roles only when an invocation trace names the exact same agent."""
    roles: dict[str, str] = {}
    if not isinstance(trace, list):
        return roles
    for row in trace:
        if not isinstance(row, dict) or row.get("kind") != "invocation_start":
            continue
        agent_id = str(row.get("agent_id") or "")
        role = str(row.get("role") or "").strip()
        if agent_id and role and role != "unknown":
            roles[agent_id] = role
    return roles


def _request_role(row: dict, agent_roles: dict[str, str]) -> tuple[str, str]:
    """Prefer the request context, then an exact trace binding; otherwise say unknown."""
    role = str(row.get("role") or "").strip()
    if role and role != "unknown":
        return role, "execution_context"
    traced = agent_roles.get(str(row.get("agent") or ""))
    if traced:
        return traced, "trace_agent_binding"
    return "unknown", "unknown"


class ProjectEvidence(Evidence):
    """Reuse the live observers while retaining only a compact final report.

    ``Evidence`` normally persists command output so its long acceptance can be
    diagnosed.  This run's user data and replies must not survive in evidence,
    so this subclass keeps raw observer data in memory only and persists counts,
    hashes, timings, and explicit pass/fail checks.
    """

    def __init__(self, root: Path, batch: str) -> None:
        super().__init__(root)
        self.batch = batch
        self.checks: dict[str, bool] = {}
        self.turn_summaries: list[dict] = []
        self._agent_roles: dict[str, str] = {}
        self.config_sha256 = ""

    def check(self, name: str, condition: bool, description: str) -> bool:
        passed = bool(condition)
        self.checks[name] = passed
        if not passed:
            self.require(False, description)
        return passed

    def record_turn(self, label: str, result: dict, trace: list[dict]) -> None:
        traced_roles = _trace_agent_roles(trace)
        self._agent_roles.update(traced_roles)
        kinds = Counter(str(row.get("kind", "")) for row in trace)
        self.turn_summaries.append(
            {
                "label": label,
                "status": str(result.get("status", "rejected")),
                "seconds": round(float(result.get("seconds", 0.0)), 3),
                "trace_kinds": dict(sorted(kinds.items())),
                "trace_roles": sorted(set(traced_roles.values())),
                "trace_sha256": _digest(trace),
            }
        )
        self.save()

    def _report(self) -> dict:
        request_role_rows = [
            _request_role(row, self._agent_roles) for row in self.requests
        ]
        request_roles = Counter(role for role, _source in request_role_rows)
        request_role_sources = Counter(source for _role, source in request_role_rows)
        request_models = Counter(str(row.get("model") or "unknown") for row in self.requests)
        command_status = Counter(str(row.get("returncode")) for row in self.commands)
        rag_endpoints = Counter(str(row.get("endpoint") or "unknown") for row in self.rag_calls)
        resource_rows = [item for row in self.resources for item in row.get("processes", [])]
        return {
            "schema": 1,
            "batch": self.batch,
            "deadline_seconds": DEADLINE_SECONDS,
            "elapsed_seconds": round(time.monotonic() - self.started, 3),
            "timed_out": self.timed_out,
            "complete": self.complete,
            "config_sha256": self.config_sha256,
            "checks": self.checks,
            "failure_count": len(self.failures),
            "failures": [str(item).split(":", 1)[0] for item in self.failures],
            "warning_count": len(self.warnings),
            "request_count": len(self.requests),
            "requests_by_role": dict(sorted(request_roles.items())),
            "request_role_sources": dict(sorted(request_role_sources.items())),
            "requests_by_model": dict(sorted(request_models.items())),
            "http_stream_observation": _safe_stream_observation(self.requests),
            "rag_call_count": len(self.rag_calls),
            "rag_endpoints": dict(sorted(rag_endpoints.items())),
            "rag_error_count": sum(bool(row.get("error")) for row in self.rag_calls),
            "tool_command_count": len(self.commands),
            "tool_commands_by_returncode": dict(sorted(command_status.items())),
            "turns": self.turn_summaries,
            "session_summaries": self.sessions,
            "resource_samples": len(self.resources),
            "resource_peak_rss": max((int(row.get("rss", 0)) for row in resource_rows), default=0),
        }

    def save(self) -> None:
        write_json(self.root / "acceptance-evidence.json", self._report())

    def timeout(self) -> None:
        self.timed_out = True
        self.failures.append("600-second acceptance deadline exceeded")
        self.complete = False
        self.save()
        os._exit(124)


def prepare_short_materials(project: Path) -> tuple[str, str]:
    """Create user-supplied fixtures only; never source files or generated code."""
    from PIL import Image, ImageDraw

    project.mkdir(parents=True, exist_ok=True)
    image_code = uuid4().hex[:6].upper()
    anchor = "项目验收锚点-" + uuid4().hex[:12]
    image = Image.new("RGB", (720, 240), "white")
    ImageDraw.Draw(image).text((60, 80), image_code, fill="black", font_size=64)
    image.save(project / "ticket.png")
    (project / "报价.csv").write_text(
        "category,amount\ntrain,240\nhotel,600\nmeal,160\n", encoding="utf-8"
    )
    (project / "说明.md").write_text(
        "# 临时验收资料\n两人出行，预算 3500 元；报价不代表已付款或已预订。\n",
        encoding="utf-8",
    )
    return image_code, anchor


async def _turn(driver: ApplicationDriver, evidence: ProjectEvidence, label: str, prompt: str):
    from redlotus.core.agents import TRACE_STORE

    result = await driver.say(prompt)
    trace = TRACE_STORE.events_for_turn(result["input_id"])
    evidence.record_turn(label, result, trace)
    evidence.check(
        f"{label}_success",
        result["status"] == "success",
        f"{label} did not complete successfully",
    )
    if result["status"] != "success":
        raise RuntimeError(f"{label} rejected or failed")
    return result, trace


def _worker_tool_receipt(
    evidence: ProjectEvidence, trace: list[dict], turn_id: str, project: Path
) -> tuple[bool, bool]:
    worker_ids = {
        str(row.get("agent_id"))
        for row in trace
        if row.get("kind") == "invocation_start" and row.get("role") == "worker"
    }
    commands = [
        row
        for row in evidence.commands
        if row.get("turn_id") == turn_id and str(row.get("agent_id")) in worker_ids
    ]
    inside_project = all(
        Path(str(row.get("cwd") or project)).resolve().is_relative_to(project.resolve())
        for row in commands
    )
    return bool(worker_ids), bool(commands) and any(row.get("returncode") == 0 for row in commands) and inside_project


def _session_summary(session, usage, *, tag: str) -> dict:
    return {
        "tag": tag,
        "session_id": session.session_id,
        "completed_turns": session.completed_turns,
        "bytes": session.path.stat().st_size,
        "usage": asdict(usage.totals),
    }


def _check_session_log(
    evidence: ProjectEvidence, session, project: Path, *, tag: str
) -> None:
    """A task log is created with the session's stored task name on first input."""
    from redlotus.core import config as logger

    log_dir = project / ".redlotus" / "logs"
    task_name = str(session.metadata.get("task_name") or "")
    prefix = logger.safe_name(task_name, max_len=50, fallback="task")
    task_logs = list(log_dir.glob(f"{prefix}_*.log")) if task_name and log_dir.is_dir() else []
    evidence.check(
        f"{tag}_project_log_dir",
        logger.get_log_dir().resolve() == log_dir.resolve() and log_dir.is_dir(),
        f"{tag} logger did not route to its project-local log directory",
    )
    evidence.check(
        f"{tag}_session_task_log",
        bool(task_logs),
        f"{tag} session task log was not created in its project-local log directory",
    )


async def short_batch(args: argparse.Namespace, evidence: ProjectEvidence) -> None:
    """Five actual turns: dialogue, child tool, original image, memory/RAG, usage."""
    from redlotus.core.config import close_all_clients
    from redlotus.core.history import read_usage_messages, summarize_messages

    project = args.root / "projects" / "short-storage"
    image_code, anchor = prepare_short_materials(project)
    driver = ApplicationDriver(project, args.root)
    try:
        await _turn(
            driver,
            evidence,
            "dialogue",
            "请读取 @说明.md 和 @报价.csv，用一句话说明已知约束；不要创建、修改或删除文件。",
        )
        worker, trace = await _turn(
            driver,
            evidence,
            "worker_tool",
            "请调用一个子Agent，让它只用终端读取当前项目的 报价.csv 并计算 amount 合计。不得创建、修改或删除任何文件；收到真实工具回执后只报告合计。",
        )
        worker_started, worker_command = _worker_tool_receipt(
            evidence, trace, worker["input_id"], project
        )
        evidence.check("worker_started", worker_started, "No real worker invocation was observed")
        evidence.check("worker_tool_success", worker_command, "No successful in-project worker command was observed")
        evidence.check(
            "worker_total", "1000" in worker["output"], "Worker result did not report the fixture total"
        )
        picture, _ = await _turn(
            driver,
            evidence,
            "original_image",
            "读取 @ticket.png 的验证码，只回答图中看到的六个字符。",
        )
        evidence.check(
            "original_image_recognized",
            image_code in picture["output"],
            "The original image fixture was not recognized",
        )
        await _turn(
            driver,
            evidence,
            "project_memory",
            f"请调用 remember 工具并明确使用 scope=project，主动记住：{anchor} 是本项目的回归标识，只适用于当前项目。取得真实保存回执后再回答。",
        )
        idle = await driver.system._memory.wait_idle(timeout=_remaining(evidence))
        evidence.check("memory_idle", idle, "Project memory processing did not become idle")
        recalled = _records(await driver.system._memory.reader.search_memory(anchor))
        evidence.check(
            "project_memory_recalled",
            _contains_anchor(recalled, anchor, scope="project"),
            "Real project memory was not recalled",
        )
        await _turn(
            driver,
            evidence,
            "post_memory_dialogue",
            "根据本项目已知资料，简短说明报价是否代表已经付款；不要创建文件。",
        )
        session = driver.system._session_file
        evidence.check("session_created", session is not None, "No session file was created")
        if session is None:
            raise RuntimeError("session file missing")
        session_root = project / ".redlotus" / "sessions"
        evidence.check(
            "project_session_path",
            session.path.resolve().is_relative_to(session_root.resolve()),
            "Session was not stored under the project-local sessions directory",
        )
        evidence.check(
            "one_json_per_session",
            len(list(session.path.parent.glob("*.json"))) == 1
            and not list(session.path.parent.glob("*.jsonl")),
            "Session directory did not retain the expected single JSON transcript",
        )
        rows, _ = read_usage_messages(session.path)
        usage = summarize_messages(rows, price_resolver=lambda _model: None)
        evidence.check("usage_recorded", usage.totals.responses > 0, "No real model usage was persisted")
        _check_session_log(evidence, session, project, tag="short")
        evidence.sessions.append(_session_summary(session, usage, tag="short"))
        evidence.check("rag_used", bool(evidence.rag_calls), "No real RAG request was observed")
        evidence.check(
            "rag_succeeded",
            all(not row.get("error") for row in evidence.rag_calls),
            "At least one observed RAG request failed",
        )
    finally:
        await driver.close()
        await close_all_clients()


async def _cancel_load(driver: ApplicationDriver) -> None:
    from redlotus.core.console import SnapshotAction, SnapshotSelection

    async def choose(_snapshots):
        return SnapshotSelection(SnapshotAction.CANCEL)

    controller = driver.system._cli_controller
    controller.set_snapshot_picker(choose)
    try:
        await driver.system.process_cli_line("/load", driver.state, wait_for_turn=True)
    finally:
        controller.set_snapshot_picker(None)


async def lifecycle_batch(args: argparse.Namespace, evidence: ProjectEvidence) -> None:
    """Five actual turns around clear, cancelled load, restore, switch, recall and compression."""
    from redlotus.core.config import close_all_clients
    from redlotus.core.history import read_usage_messages, summarize_messages

    project_a = args.root / "projects" / "lifecycle-a"
    project_b = args.root / "projects" / "lifecycle-b"
    project_a.mkdir(parents=True, exist_ok=True)
    project_b.mkdir(parents=True, exist_ok=True)
    anchor = "跨项目验收锚点-" + uuid4().hex[:12]
    (project_a / "资料.md").write_text("这是仅供验收的用户资料。\n", encoding="utf-8")
    (project_b / "资料.md").write_text("这是第二个验收项目的用户资料。\n", encoding="utf-8")
    driver = ApplicationDriver(project_a, args.root)
    try:
        await _turn(
            driver,
            evidence,
            "project_a_dialogue",
            "请读取 @资料.md，用一句话说明这是当前项目资料；不要创建文件。",
        )
        await _turn(
            driver,
            evidence,
            "global_memory",
            f"请调用 remember 工具并明确使用 scope=global，主动记住：{anchor} 是跨项目验收流程编号，可在其他项目召回。取得真实保存回执后再回答。",
        )
        await _turn(
            driver,
            evidence,
            "project_a_followup",
            "继续当前项目，用一句话确认该资料没有要求创建任何文件。",
        )
        old_session = driver.system._session_file
        evidence.check("pre_clear_session", old_session is not None, "No source session before /clear")
        if old_session is None:
            raise RuntimeError("source session missing")
        old_path, old_id, old_turns = old_session.path, old_session.session_id, old_session.completed_turns
        _check_session_log(evidence, old_session, project_a, tag="project_a")
        await driver.system.process_cli_line("/clear", driver.state, wait_for_turn=True)
        evidence.check("clear_new_context", driver.state.is_first_input, "/clear did not open a new context")
        evidence.check("clear_released_session", driver.system.session_key is None, "/clear retained the old session")
        new_turn, _ = await _turn(
            driver,
            evidence,
            "post_clear_dialogue",
            "这是清空后的新上下文。只回复“已开始新上下文”，不要创建文件。",
        )
        new_session = driver.system._session_file
        evidence.check(
            "clear_created_new_session",
            new_session is not None and new_session.session_id != old_id,
            "/clear did not create a distinct session on the next real turn",
        )
        if new_session is None:
            raise RuntimeError("post-clear session missing")
        before_cancel = new_session.session_id
        before_requests = len(evidence.requests)
        await _cancel_load(driver)
        evidence.check(
            "load_cancel_kept_session",
            driver.system.session_key == before_cancel and driver.system._session_file.path == new_session.path,
            "Cancelled /load altered the active new session",
        )
        evidence.check(
            "load_cancel_no_request",
            len(evidence.requests) == before_requests,
            "Cancelled /load issued a model request",
        )
        await driver.restore(old_path)
        evidence.check("load_restored_id", driver.system.session_key == old_id, "/load did not restore the chosen session")
        evidence.check(
            "load_restored_turns",
            driver.system._session_file.completed_turns == old_turns,
            "/load changed the saved completed-turn count",
        )
        await driver.system._cli_controller.reset_session(driver.state.history, workspace=project_b)
        driver.state.is_first_input = True
        evidence.check(
            "workspace_switched",
            driver.system.workspace.root == project_b.resolve(),
            "Project switch did not adopt the target workspace",
        )
        evidence.check(
            "target_project_logs",
            (project_b / ".redlotus" / "logs").is_dir(),
            "Project switch did not prepare target project logs",
        )
        await _turn(
            driver,
            evidence,
            "project_b_global_recall",
            f"请从全局记忆查找 {anchor}，仅说明是否找到；不要创建文件。",
        )
        global_rows = _records(await driver.system._memory.reader.search_memory(anchor))
        evidence.check(
            "global_memory_recalled_in_project_b",
            _contains_anchor(global_rows, anchor, scope="global"),
            "The real global memory was not recalled after the project switch",
        )
        before_compression = len(evidence.requests)
        await driver.system.process_cli_line("/compress", driver.state, wait_for_turn=True)
        compressed = any(
            bool(history.compress_summary_state)
            for history in (driver.state.history, driver.system._manager_history)
        )
        evidence.check("compression_saved", compressed, "Forced /compress did not persist a context summary")
        evidence.check(
            "compression_requested",
            len(evidence.requests) > before_compression,
            "Forced /compress did not issue an observed model request",
        )
        session = driver.system._session_file
        evidence.check("project_b_session", session is not None, "No session was created in project B")
        if session is None:
            raise RuntimeError("project B session missing")
        _check_session_log(evidence, session, project_b, tag="project_b")
        for tag, path in (("project_a", old_path), ("project_b", session.path)):
            rows, _ = read_usage_messages(path)
            usage = summarize_messages(rows, price_resolver=lambda _model: None)
            loaded = type(session).load(path)
            evidence.check(f"{tag}_usage", usage.totals.responses > 0, f"No usage persisted for {tag}")
            evidence.sessions.append(_session_summary(loaded, usage, tag=tag))
    finally:
        await driver.close()
        await close_all_clients()


async def _run_live_batch(args: argparse.Namespace, evidence: ProjectEvidence) -> None:
    from redlotus.core.config import load_config

    sampler = None
    try:
        load_config()
        evidence.install()
        sampler = asyncio.create_task(evidence.sample())
        if args.batch == "short":
            await short_batch(args, evidence)
        elif args.batch == "lifecycle":
            await lifecycle_batch(args, evidence)
        else:
            raise ValueError(f"Unsupported live batch: {args.batch}")
    except BaseException as exc:
        # Do not store model output, request text, or secrets from an exception.
        evidence.failures.append(type(exc).__name__)
    finally:
        if sampler is not None:
            sampler.cancel()
            await asyncio.gather(sampler, return_exceptions=True)
        evidence.check("no_unexpected_warnings", not evidence.warnings, "Unexpected WARNING/ERROR recorded")
        evidence.check("resource_sampling", bool(evidence.resources), "Process resource sampling did not run")
        evidence.complete = not evidence.failures and not evidence.timed_out
        evidence.save()


def _child_command(args: argparse.Namespace, batch: str, root: Path) -> list[str]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--batch",
        batch,
        "--run-live",
        "--config",
        str(args.config),
        "--root",
        str(root),
    ]
    if args.dotenv is not None:
        command.extend(("--dotenv", str(args.dotenv)))
    if args.dependencies is not None:
        command.extend(("--dependencies", str(args.dependencies)))
    return command


_SAFE_FAILURE_MARKERS = (
    ("600-second acceptance deadline exceeded", "deadline_exceeded"),
    ("Expected one worker", "worker_count"),
    ("No successful worker script execution", "worker_execution"),
    ("Expected one independently readable totals.json", "worker_artifact"),
    ("Incorrect computed artifact", "worker_artifact_total"),
    ("Original image was not recognized", "original_image"),
    ("Six urgent messages did not remain", "urgent_message_grouping"),
    ("Urgent input order changed", "urgent_message_order"),
    ("Restore changed session ID", "restore_session_id"),
    ("Restore changed completed count", "restore_completed_turns"),
    ("Loading issued a model request", "restore_model_request"),
    ("Expected three automatic windows", "automatic_window_count"),
    ("Window position changed", "automatic_window_position"),
    ("Window did not consume", "automatic_window_turns"),
    ("Window overlap changed", "automatic_window_overlap"),
    ("Production/index backlog remains", "memory_index_backlog"),
    ("Extra JSONL transcript exists", "session_extra_jsonl"),
    ("Session has more than one JSON", "session_json_count"),
    ("Real produced project memory was not recalled", "project_memory_recall"),
    ("Unexpected WARNING/ERROR recorded", "unexpected_warning"),
    ("Process resource sampling did not run", "resource_sampling"),
    ("Agent thread cap exceeded", "agent_thread_cap"),
    ("Main/worker cache hit ratio did not exceed", "cache_hit_ratio"),
)


def _safe_failure_label(value: object) -> str:
    """Return a stable failure identifier without persisting request or reply text."""
    message = str(value)
    for marker, label in _SAFE_FAILURE_MARKERS:
        if marker in message:
            return label
    prefix = message.split(":", 1)[0].strip()
    if prefix == "AssertionError":
        return f"assertion_{hashlib.sha256(message.encode('utf-8')).hexdigest()[:12]}"
    if prefix.endswith(("Error", "Exception", "Timeout")) and prefix.isidentifier():
        return prefix
    return f"assertion_{hashlib.sha256(message.encode('utf-8')).hexdigest()[:12]}"


def _safe_exception_frames(value: object) -> list[dict[str, object]]:
    """Keep exception type and source locations, never its message or frame locals."""
    safe: list[dict[str, object]] = []
    if not isinstance(value, list):
        return safe
    for row in value:
        if not isinstance(row, dict):
            continue
        exception_type = str(row.get("type") or "UnknownException")
        if not exception_type.isidentifier():
            exception_type = "UnknownException"
        frames: list[dict[str, object]] = []
        source_frames = row.get("frames")
        if isinstance(source_frames, list):
            for frame in source_frames[:30]:
                if not isinstance(frame, dict):
                    continue
                source = str(frame.get("file") or "")
                normalized = source.replace("\\", "/")
                if not normalized.endswith((".py", ".pyi")):
                    normalized = "<unknown>"
                elif normalized.startswith("/") or ":/" in normalized:
                    normalized = Path(normalized).name
                try:
                    line = int(frame.get("line"))
                except (TypeError, ValueError):
                    continue
                function = str(frame.get("function") or "<unknown>")
                if not function.isidentifier():
                    function = "<unknown>"
                if line > 0:
                    frames.append(dict(file=normalized, line=line, function=function))
        safe.append(dict(type=exception_type, frames=frames))
    return safe


def _safe_identifier(value: object, *, fallback: str = "unknown") -> str:
    """Allow stable technical identifiers while dropping arbitrary recorded text."""
    text = str(value or "")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:/-")
    return text if 0 < len(text) <= 128 and set(text) <= allowed else fallback


def _safe_sha256(value: object) -> str | None:
    text = str(value or "").lower()
    return text if len(text) == 64 and all(char in "0123456789abcdef" for char in text) else None


def _safe_counter(rows: object, key: str, *, identifier: bool = True) -> dict[str, int]:
    values: Counter[str] = Counter()
    if not isinstance(rows, list):
        return {}
    for row in rows:
        if not isinstance(row, dict) or key not in row:
            continue
        value = _safe_identifier(row.get(key)) if identifier else str(row.get(key))
        values[value] += 1
    return dict(sorted(values.items()))


def _safe_int(value: object) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _safe_seconds(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return round(result, 6) if math.isfinite(result) and result >= 0 else None


def _safe_stream_observation(requests: object) -> dict[str, object]:
    """Keep only numerical stream timing linked to its request-row order."""
    rows = []
    if isinstance(requests, list):
        for index, row in enumerate(requests):
            if not isinstance(row, dict) or "stream_status" not in row:
                continue
            observed: dict[str, object] = {
                "request_index": index,
                "status": _safe_identifier(row.get("stream_status")),
            }
            for key in (
                "headers_seconds",
                "stream_first_byte_seconds",
                "stream_eof_seconds",
                "stream_closed_early_seconds",
                "stream_error_seconds",
                "stream_wait_seconds",
            ):
                if (seconds := _safe_seconds(row.get(key))) is not None:
                    observed[key] = seconds
            if row.get("stream_error_type"):
                observed["error_type"] = _safe_identifier(row.get("stream_error_type"))
            rows.append(observed)
    return {
        "note": (
            "stream_wait_seconds sums upstream iterator await time and excludes consumer chunk-hold time; "
            "request-to-EOF is observed end-to-end timing, not pure service latency."
        ),
        "requests": rows,
    }


def _safe_long_evidence(raw: object, provenance: object) -> dict[str, object]:
    """Extract only evidence needed to assess a long live batch after raw logs go away."""
    raw = raw if isinstance(raw, dict) else {}
    provenance = provenance if isinstance(provenance, dict) else {}
    source_hashes = provenance.get("source_sha256")
    source_hashes = source_hashes if isinstance(source_hashes, dict) else {}
    stable_sources = {
        _safe_identifier(path, fallback="source"): digest
        for path, value in source_hashes.items()
        if (digest := _safe_sha256(value)) is not None
    }
    requests = raw.get("requests") if isinstance(raw.get("requests"), list) else []
    trace_agent_roles: dict[str, str] = {}
    for turn in raw.get("turns", []):
        if isinstance(turn, dict):
            trace_agent_roles.update(_trace_agent_roles(turn.get("trace")))
    request_role_rows = [
        _request_role(row, trace_agent_roles)
        for row in requests
        if isinstance(row, dict)
    ]
    request_roles = Counter(
        _safe_identifier(role) for role, _source in request_role_rows
    )
    request_role_sources = Counter(
        source for _role, source in request_role_rows
    )
    status_rows = [row for row in requests if isinstance(row, dict) and _safe_int(row.get("status_code")) is not None]
    status_codes = Counter(str(_safe_int(row.get("status_code"))) for row in status_rows)
    sessions = [row for row in raw.get("sessions", []) if isinstance(row, dict)]
    session = sessions[-1] if sessions else {}
    windows = [row for row in session.get("windows", []) if isinstance(row, dict)]
    automatic_windows = []
    for window in windows:
        start, end = _safe_int(window.get("start_position")), _safe_int(window.get("end_position"))
        if start is None or end is None:
            continue
        automatic_windows.append(
            {
                "start_position": start,
                "end_position": end,
                "new_turn_count": len(window.get("new_turn_ids", [])) if isinstance(window.get("new_turn_ids"), list) else 0,
                "overlap_turn_count": len(window.get("overlap_turn_ids", [])) if isinstance(window.get("overlap_turn_ids"), list) else 0,
            }
        )
    ratio = session.get("foreground_cache_ratio")
    ratio = float(ratio) if isinstance(ratio, (int, float)) else None
    return {
        "provenance": {
            "config_sha256": _safe_sha256(provenance.get("sha256")),
            "source_tree_sha256": _digest(stable_sources) if stable_sources else None,
            "source_file_count": len(stable_sources),
        },
        "actual_api": {
            "request_count": len(requests),
            "response_count": len(status_rows),
            "successful_response_count": sum(200 <= _safe_int(row.get("status_code")) < 300 for row in status_rows),
            "models": _safe_counter(requests, "model"),
            "roles": dict(sorted(request_roles.items())),
            "status_codes": dict(sorted(status_codes.items())),
        },
        "http_stream_observation": _safe_stream_observation(requests),
        "request_role_sources": dict(sorted(request_role_sources.items())),
        "rag_api": {
            "call_count": len(raw.get("rag_calls", [])) if isinstance(raw.get("rag_calls"), list) else 0,
            "endpoints": _safe_counter(raw.get("rag_calls"), "endpoint"),
        },
        "tool_commands": {
            "count": len(raw.get("commands", [])) if isinstance(raw.get("commands"), list) else 0,
            "returncodes": _safe_counter(raw.get("commands"), "returncode", identifier=False),
        },
        "session": {
            "session_id": _safe_identifier(session.get("id")),
            "completed_turns": _safe_int(session.get("completed")),
            "bytes": _safe_int(session.get("bytes")),
            "pending_job_count": len(session.get("pending", [])) if isinstance(session.get("pending"), list) else 0,
            "foreground_cache_ratio": ratio,
        },
        "automatic_task_count": len(windows),
        "automatic_windows": automatic_windows,
    }


def _long_batch(args: argparse.Namespace) -> int:
    """Reuse the fixed 60-turn harness unchanged, then replace its verbose evidence."""
    runtime = args.root / "WorkDatabase" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.update(TEMP=str(runtime), TMP=str(runtime), UV_CACHE_DIR=str(runtime / "uv-cache"))
    command = [
        sys.executable,
        str(SCRIPT_DIR / "session_acceptance.py"),
        "--config",
        str(args.config),
        "--root",
        str(args.root),
    ]
    if args.dotenv is not None:
        command.extend(("--dotenv", str(args.dotenv)))
    if args.dependencies is not None:
        command.extend(("--dependencies", str(args.dependencies)))
    timed_out = False
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=DEADLINE_SECONDS,
            check=False,
        )
        exit_code = completed.returncode
    except subprocess.TimeoutExpired:
        timed_out, exit_code = True, 124
    raw_path = args.root / "live-evidence.json"
    provenance_path = args.root / "provenance.json"
    try:
        raw = json.loads(raw_path.read_text(encoding="utf-8")) if raw_path.is_file() else {}
    except (OSError, ValueError):
        raw = {}
    try:
        provenance = (
            json.loads(provenance_path.read_text(encoding="utf-8"))
            if provenance_path.is_file()
            else {}
        )
    except (OSError, ValueError):
        provenance = {}
    report = {
        "schema": 1,
        "batch": "long",
        "deadline_seconds": DEADLINE_SECONDS,
        "timed_out": timed_out or bool(raw.get("timed_out")),
        "complete": bool(raw.get("complete")) and exit_code == 0,
        "exit_code": exit_code,
        "failure_count": len(raw.get("failures", [])),
        "failure_labels": [_safe_failure_label(value) for value in raw.get("failures", [])],
        "exceptions": _safe_exception_frames(raw.get("exceptions")),
        "warning_count": len(raw.get("warnings", [])),
        "request_count": len(raw.get("requests", [])),
        "rag_call_count": len(raw.get("rag_calls", [])),
        "tool_command_count": len(raw.get("commands", [])),
        "turn_count": len(raw.get("turns", [])),
        "session_count": len(raw.get("sessions", [])),
        "resource_samples": len(raw.get("resources", [])),
        **_safe_long_evidence(raw, provenance),
    }
    write_json(args.root / "acceptance-evidence.json", report)
    # The imported harness records decoded terminal output while it runs.  Keep
    # only this concise report after the child exits.
    raw_path.unlink(missing_ok=True)
    provenance_path.unlink(missing_ok=True)
    return 0 if report["complete"] and report["warning_count"] == 0 else (exit_code or 1)


def _run_all(args: argparse.Namespace) -> int:
    reports = []
    for batch in ("short", "lifecycle", "long"):
        child_root = args.root / batch
        child_root.mkdir(parents=True, exist_ok=True)
        try:
            completed = subprocess.run(
                _child_command(args, batch, child_root),
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=DEADLINE_SECONDS,
                check=False,
            )
            exit_code, timed_out = completed.returncode, False
        except subprocess.TimeoutExpired:
            exit_code, timed_out = 124, True
        report_path = child_root / "acceptance-evidence.json"
        try:
            child = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            child = {}
        reports.append(
            {
                "batch": batch,
                "exit_code": exit_code,
                "timed_out": timed_out,
                "complete": bool(child.get("complete")),
                "failure_count": int(child.get("failure_count", 1)),
                "warning_count": int(child.get("warning_count", 0)),
            }
        )
    write_json(
        args.root / "acceptance-evidence.json",
        {
            "schema": 1,
            "batch": "all",
            "deadline_seconds_per_batch": DEADLINE_SECONDS,
            "children": reports,
        },
    )
    return int(any(row["exit_code"] or not row["complete"] or row["warning_count"] for row in reports))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", choices=("short", "lifecycle", "long", "all"), default="short")
    parser.add_argument("--run-live", action="store_true", help="permit real service and model calls")
    parser.add_argument("--config", type=Path, default=ROOT / "src" / "redlotus" / "config.json")
    parser.add_argument("--dotenv", type=Path, default=ROOT / ".env")
    parser.add_argument("--root", type=Path, required=True, help="isolated E: acceptance root")
    parser.add_argument("--dependencies", type=Path, default=DEFAULT_DEPENDENCIES)
    args = parser.parse_args()
    if not args.run_live:
        parser.error("Real acceptance is disabled until --run-live is supplied")
    args.root = _run_directory(args.root)
    args.root.mkdir(parents=True, exist_ok=True)
    args.config = args.config.expanduser().resolve()
    if not args.config.is_file():
        parser.error(f"config file does not exist: {args.config}")
    args.dotenv = args.dotenv.expanduser().resolve() if args.dotenv and args.dotenv.is_file() else None
    args.dependencies = (
        args.dependencies.expanduser().resolve() if args.dependencies and args.dependencies.is_dir() else None
    )
    if args.batch == "all":
        return _run_all(args)
    if args.batch == "long":
        return _long_batch(args)
    if args.dependencies is not None:
        sys.path.insert(0, str(args.dependencies))
    runtime = args.root / "WorkDatabase" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    os.environ["UV_CACHE_DIR"] = str(runtime / "uv-cache")
    tempfile.tempdir = str(runtime)
    evidence = ProjectEvidence(args.root, args.batch)
    timer = threading.Timer(DEADLINE_SECONDS, evidence.timeout)
    timer.daemon = True
    timer.start()
    try:
        _memory, evidence.config_sha256 = configure(args)
        asyncio.run(_run_live_batch(args, evidence))
    finally:
        timer.cancel()
        evidence.save()
    return int(bool(evidence.failures) or evidence.timed_out)


if __name__ == "__main__":
    raise SystemExit(main())
