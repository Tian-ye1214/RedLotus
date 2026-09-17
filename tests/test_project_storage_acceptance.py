"""Compact evidence must retain acceptance facts without conversation bodies."""

from __future__ import annotations

import asyncio
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "project_storage_acceptance.py"
SESSION_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "session_acceptance.py"


def _module():
    spec = importlib.util.spec_from_file_location("project_storage_acceptance", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _session_module():
    spec = importlib.util.spec_from_file_location("session_acceptance_stream_test", SESSION_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stable_root_hash(root: Path) -> str:
    return hashlib.sha256(str(root.resolve()).casefold().encode("utf-8")).hexdigest()[:16]


def test_configure_keeps_state_on_run_volume_and_lancedb_in_stable_evaluation_root(
    tmp_path, monkeypatch
):
    module = _module()
    source = module.ROOT / "src" / "redlotus" / "config.json"
    baseline = json.loads(source.read_text(encoding="utf-8"))
    run_root = tmp_path / "e-run"
    home = tmp_path / "c-user"
    global_config_dir = tmp_path / "operator-config"
    global_config = global_config_dir / "config.json"
    global_config_dir.mkdir()
    global_config.write_text('{"operator": "fallback"}', encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("REDLOTUS_CONFIG_DIR", str(global_config_dir))
    monkeypatch.delenv("RAG_DB_PATH", raising=False)
    for name in ("REDLOTUS_CONFIG_FILE", "REDLOTUS_DATA_DIR", "TEMP", "TMP"):
        monkeypatch.setenv(name, os.environ.get(name, ""))

    args = SimpleNamespace(config=source, root=run_root, dotenv=None)
    memory, fingerprint = module.configure(args)

    expected_lancedb = (
        home
        / ".redlotus"
        / "evaluation"
        / "storage-final-live"
        / _stable_root_hash(run_root)
    )
    selected = json.loads((memory / "config.json").read_text(encoding="utf-8"))
    assert memory == run_root / "global-state"
    assert Path(os.environ["REDLOTUS_DATA_DIR"]) == memory
    assert Path(os.environ["RAG_DB_PATH"]) == expected_lancedb
    assert expected_lancedb.is_dir()
    assert fingerprint == hashlib.sha256(source.read_bytes()).hexdigest()
    assert (memory / "config-source" / "config.json").read_bytes() == global_config.read_bytes()
    assert (memory / "config.json").is_relative_to(run_root)
    for section, value in baseline.items():
        if section != "storage":
            assert selected[section] == value
    assert selected["storage"]["state_dir"] == str(memory)
    assert selected["storage"]["project_dir"] == ".redlotus"
    assert selected["storage"]["sessions_dir"] == ".redlotus/sessions"
    assert selected["storage"]["project_logs_dir"] == ".redlotus/logs"
    assert selected["storage"]["references_dir"] == "WorkDatabase/references"
    assert selected["storage"]["runtime_dir"] == "WorkDatabase/runtime"

    sentinel = expected_lancedb / "restart-sentinel"
    sentinel.write_text("preserve", encoding="utf-8")
    monkeypatch.setenv("REDLOTUS_CONFIG_DIR", str(global_config_dir))
    repeated_memory, _ = module.configure(args)
    assert repeated_memory == memory
    assert Path(os.environ["RAG_DB_PATH"]) == expected_lancedb
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_timed_response_stream_passes_bytes_and_marks_eof_early_close_and_error():
    module = _session_module()

    class Stream:
        def __init__(self, chunks=(), error=None):
            self.chunks, self.error, self.close_calls = chunks, error, 0

        async def __aiter__(self):
            for chunk in self.chunks:
                await asyncio.sleep(0)
                yield chunk
            if self.error:
                raise self.error

        async def aclose(self):
            self.close_calls += 1

    def observed(stream):
        started = time.monotonic()
        row = {"at": 0.0}
        return module.TimedResponseStream(
            stream, row, elapsed=lambda: time.monotonic() - started, lock=threading.RLock()
        ), row

    async def exercise():
        complete_source = Stream((b"one", b"two"))
        complete, complete_row = observed(complete_source)
        complete_body = b"".join([chunk async for chunk in complete])
        await complete.aclose()

        early_source = Stream((b"keep", b"discard"))
        early, early_row = observed(early_source)
        iterator = early.__aiter__()
        early_chunk = await iterator.__anext__()
        await iterator.aclose()
        await early.aclose()

        error_source = Stream(error=RuntimeError("fixture"))
        failed, failed_row = observed(error_source)
        try:
            async for _chunk in failed:
                pass
        except RuntimeError:
            pass
        else:
            raise AssertionError("stream error was not propagated")
        await failed.aclose()
        return (
            complete_body,
            complete_source,
            complete_row,
            early_chunk,
            early_source,
            early_row,
            error_source,
            failed_row,
        )

    (
        complete_body,
        complete_source,
        complete_row,
        early_chunk,
        early_source,
        early_row,
        error_source,
        failed_row,
    ) = asyncio.run(exercise())

    assert complete_body == b"onetwo"
    assert complete_source.close_calls == 1
    assert complete_row["stream_status"] == "eof"
    assert complete_row["stream_first_byte_seconds"] >= 0
    assert complete_row["stream_eof_seconds"] >= complete_row["stream_first_byte_seconds"]
    assert complete_row["stream_wait_seconds"] >= 0

    assert early_chunk == b"keep"
    assert early_source.close_calls == 1
    assert early_row["stream_status"] == "closed_early"
    assert "stream_eof_seconds" not in early_row

    assert error_source.close_calls == 1
    assert failed_row["stream_status"] == "error"
    assert failed_row["stream_error_type"] == "RuntimeError"
    assert "stream_eof_seconds" not in failed_row


def test_project_evidence_uses_only_explicit_or_exact_trace_bound_request_roles(tmp_path):
    module = _module()
    evidence = module.ProjectEvidence(tmp_path, "short")
    evidence.requests.extend(
        [
            {"model": "model-a", "role": None, "agent": "session:worker"},
            {"model": "model-b", "role": None, "agent": "unbound-agent"},
            {"model": "model-c", "role": "manager", "agent": "session:worker"},
        ]
    )
    evidence.record_turn(
        "role-evidence",
        {"status": "success", "seconds": 0.01},
        [
            {
                "kind": "invocation_start",
                "agent_id": "session:worker",
                "role": "worker",
            }
        ],
    )

    report = evidence._report()
    assert report["requests_by_role"] == {"manager": 1, "unknown": 1, "worker": 1}
    assert report["request_role_sources"] == {
        "execution_context": 1,
        "trace_agent_binding": 1,
        "unknown": 1,
    }
    assert report["turns"][0]["trace_roles"] == ["worker"]


def test_long_evidence_summary_retains_verifiable_facts_without_raw_conversation():
    module = _module()
    config_hash = "a" * 64
    source_hash = "b" * 64
    raw = {
        "requests": [
            {
                "model": "acceptance-model",
                "role": "coordinator",
                "agent": "session:coordinator",
                "status_code": 200,
                "headers_seconds": 0.1,
                "stream_status": "eof",
                "stream_first_byte_seconds": 0.2,
                "stream_eof_seconds": 0.8,
                "stream_wait_seconds": 0.7,
                "instructions": "RAW CONVERSATION MUST NOT PERSIST",
            },
            {
                "model": "worker-model",
                "role": None,
                "agent": "session:worker",
                "status_code": 201,
                "stream_status": "closed_early",
                "stream_closed_early_seconds": 0.4,
                "stream_wait_seconds": 0.3,
            },
            {
                "model": "unbound-model",
                "role": None,
                "agent": "unbound-agent",
                "status_code": 500,
                "stream_status": "error",
                "stream_error_seconds": 0.6,
                "stream_wait_seconds": 0.5,
                "stream_error_type": "RuntimeError",
            },
        ],
        "turns": [
            {
                "trace": [
                    {
                        "kind": "invocation_start",
                        "agent_id": "session:worker",
                        "role": "worker",
                    }
                ]
            }
        ],
        "rag_calls": [{"endpoint": "/embeddings", "model": "embedding-model"}],
        "commands": [{"returncode": 0, "output_decoded": "RAW TOOL OUTPUT MUST NOT PERSIST"}],
        "sessions": [
            {
                "path": "RAW SESSION PATH MUST NOT PERSIST",
                "id": "session-safe-id",
                "completed": 60,
                "bytes": 1234,
                "pending": [{"user_text": "RAW PENDING BODY MUST NOT PERSIST"}],
                "foreground_cache_ratio": 0.95,
                "windows": [
                    {"start_position": 0, "end_position": 20, "new_turn_ids": ["a"] * 20, "overlap_turn_ids": []},
                    {"start_position": 20, "end_position": 40, "new_turn_ids": ["b"] * 20, "overlap_turn_ids": ["x"] * 3},
                    {"start_position": 40, "end_position": 60, "new_turn_ids": ["c"] * 20, "overlap_turn_ids": ["y"] * 3},
                ],
            }
        ],
    }
    provenance = {
        "sha256": config_hash,
        "source_sha256": {"redlotus/core/system.py": source_hash},
        "memory": "RAW CONFIG PATH MUST NOT PERSIST",
    }

    summary = module._safe_long_evidence(raw, provenance)
    encoded = json.dumps(summary, ensure_ascii=False)

    assert summary["provenance"]["config_sha256"] == config_hash
    assert len(summary["provenance"]["source_tree_sha256"]) == 64
    assert summary["provenance"]["source_file_count"] == 1
    assert summary["actual_api"] == {
        "request_count": 3,
        "response_count": 3,
        "successful_response_count": 2,
        "models": {"acceptance-model": 1, "unbound-model": 1, "worker-model": 1},
        "roles": {"coordinator": 1, "unknown": 1, "worker": 1},
        "status_codes": {"200": 1, "201": 1, "500": 1},
    }
    assert summary["request_role_sources"] == {
        "execution_context": 1,
        "trace_agent_binding": 1,
        "unknown": 1,
    }
    assert summary["http_stream_observation"]["requests"] == [
        {
            "request_index": 0,
            "status": "eof",
            "headers_seconds": 0.1,
            "stream_first_byte_seconds": 0.2,
            "stream_eof_seconds": 0.8,
            "stream_wait_seconds": 0.7,
        },
        {
            "request_index": 1,
            "status": "closed_early",
            "stream_closed_early_seconds": 0.4,
            "stream_wait_seconds": 0.3,
        },
        {
            "request_index": 2,
            "status": "error",
            "stream_error_seconds": 0.6,
            "stream_wait_seconds": 0.5,
            "error_type": "RuntimeError",
        },
    ]
    assert summary["automatic_task_count"] == 3
    assert summary["automatic_windows"] == [
        {"start_position": 0, "end_position": 20, "new_turn_count": 20, "overlap_turn_count": 0},
        {"start_position": 20, "end_position": 40, "new_turn_count": 20, "overlap_turn_count": 3},
        {"start_position": 40, "end_position": 60, "new_turn_count": 20, "overlap_turn_count": 3},
    ]
    assert summary["session"] == {
        "session_id": "session-safe-id",
        "completed_turns": 60,
        "bytes": 1234,
        "pending_job_count": 1,
        "foreground_cache_ratio": 0.95,
    }
    assert "RAW CONVERSATION" not in encoded
    assert "RAW TOOL OUTPUT" not in encoded
    assert "RAW SESSION PATH" not in encoded
    assert "RAW CONFIG PATH" not in encoded
