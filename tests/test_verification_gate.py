"""The quick gate must preserve routing and require real execution evidence."""

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def gate(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("verify_usage")


@pytest.mark.parametrize("workers,executed", [(1, True), (2, True), (1, False)])
def test_delivery_requires_one_worker_and_successful_program_execution(
    gate, tmp_path, workers, executed
):
    directory = tmp_path / "sessions/project"
    directory.mkdir(parents=True)
    for index in range(workers):
        row = dict(
            meta=dict(turn_id="delivery", sub_id=f"worker-{index}"),
            message=dict(
                parts=[
                    dict(
                        part_kind="tool-return",
                        tool_name="run_command",
                        tool_call_id=f"run-{index}",
                        content="Return code: 0\nCommand: python WorkDatabase/summary.py\n",
                    )
                ]
                if executed
                else []
            ),
        )
        (directory / f"worker_{index}.jsonl").write_text(
            json.dumps(row), encoding="utf-8"
        )
    if workers == 1 and executed:
        result = gate.delivery_evidence(tmp_path, "delivery")
        assert result["worker_ids"] == ["worker-0"]
        assert set(result["command_receipts"]) == {"run-0"}
    else:
        with pytest.raises(AssertionError):
            gate.delivery_evidence(tmp_path, "delivery")


def test_test_storage_isolation_preserves_effective_gateway_environment(
    gate, tmp_path, monkeypatch
):
    monkeypatch.setattr(gate.os, "environ", dict(gate.os.environ))
    source = tmp_path / "config.json"
    baseline = dict(
        storage={},
        execution={},
        short_term_memory=dict(db_path="personal-db"),
        models=dict(worker=dict(name="unchanged")),
    )
    source.write_text(json.dumps(baseline), encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    for name in ("BASE_URL", "API_KEY", "SILICONFLOW_BASE", "SILICONFLOW_KEY"):
        monkeypatch.setenv(name, f"original-{name}")
    gate.configure(SimpleNamespace(config=source, root=tmp_path / "run"))
    for name in ("BASE_URL", "API_KEY", "SILICONFLOW_BASE", "SILICONFLOW_KEY"):
        assert gate.os.environ[name] == f"original-{name}"
    isolated = json.loads(
        Path(gate.os.environ["REDLOTUS_CONFIG_FILE"]).read_text(encoding="utf-8")
    )
    assert isolated["models"] == baseline["models"]
    assert (
        isolated["short_term_memory"]["db_path"]
        != baseline["short_term_memory"]["db_path"]
    )
    assert json.loads(source.read_text(encoding="utf-8")) == baseline
