"""The live diagnostic must count real usage, including cold and interrupted requests."""

import importlib.util
import json
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    "cli_state_acceptance", Path(__file__).parents[1] / "scripts/cli_state_acceptance.py"
)
acceptance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acceptance)


def test_cache_trace_detects_prefix_changes_without_altering_messages():
    journal = acceptance.RequestJournal(None)
    messages = [{"role": "system", "content": "fixed"}, {"role": "user", "content": "first"}]
    row = {"role": "coordinator", "agent": "main", "payload": {"messages": messages, "tools": []}}
    original = json.dumps(row)
    first = journal.request_evidence(row)
    assert first["cold_request"] and json.dumps(row) == original
    row = {**row, "payload": {**row["payload"], "messages": messages + [{"role": "assistant", "content": "done"}]}}
    second = journal.request_evidence(row)
    assert second["history_prefix_preserved"] and first["system_sha256"] == second["system_sha256"]
    row = {**row, "payload": {**row["payload"], "messages": messages[:1]}}
    assert not journal.request_evidence(row)["history_prefix_preserved"]


def test_cache_report_includes_cold_requests_and_separates_auxiliary_calls():
    journal = acceptance.RequestJournal(None)
    for role, hit in [("coordinator", 0), ("worker", 90), ("compressor", 5)]:
        response = {"model": "configured", "system_fingerprint": "real-fingerprint",
                    "usage": {"prompt_tokens": 100, "completion_tokens": 3, "prompt_cache_hit_tokens": hit}}
        cache = journal.response_evidence("data: " + json.dumps(response) + "\n\ndata: [DONE]\n")
        journal.rows.append({"role": role, "cache": cache})
    report = journal.cache_report()
    assert report["main_and_workers"]["cache_hit_ratio"] == .45
    assert report["compressor"]["cache_hit_ratio"] == .05
    assert journal.response_evidence("data: {\"usage\":")['input_tokens'] is None
    journal.rows.append({"role": "coordinator", "cache": {}})
    assert journal.cache_report()["main_and_workers"]["cache_hit_ratio"] is None
