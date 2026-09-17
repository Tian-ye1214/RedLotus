"""The quick gate must preserve routing and require real execution evidence."""

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def gate(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("session_acceptance")


@pytest.mark.parametrize(
    "workers,command",
    [
        (1, "python WorkDatabase/summary.py"),
        (2, "python WorkDatabase/summary.py"),
        (1, ""),
        (1, "echo summary.py"),
        (1, "type WorkDatabase/summary.py"),
        (1, "python -c \"print('summary.py')\""),
    ],
)
def test_delivery_requires_one_worker_and_successful_program_execution(
    gate, tmp_path, workers, command
):
    trace, commands = [], []
    for index in range(workers):
        trace.append(dict(kind="invocation_start", role="worker", agent_id=f"worker-{index}"))
        if command:
            commands.append(dict(turn_id="delivery", agent_id=f"worker-{index}",
                                 command=command, returncode=0))
    if workers == 1 and command == "python WorkDatabase/summary.py":
        result = gate.delivery_evidence(trace, commands, "delivery", "summary.py")
        assert result["worker_ids"] == ["worker-0"]
        assert set(result["command_receipts"]) == {"0"}
    else:
        with pytest.raises(AssertionError):
            gate.delivery_evidence(trace, commands, "delivery", "summary.py")


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
    assert isolated["short_term_memory"] == baseline["short_term_memory"]
    assert Path(isolated["storage"]["state_dir"]).is_relative_to(tmp_path / "run")
    assert isolated["storage"]["sessions_dir"] == ".redlotus/sessions"
    assert isolated["storage"]["runtime_dir"] == "WorkDatabase/runtime"
    assert isolated["storage"]["references_dir"] == "WorkDatabase/references"
    assert json.loads(source.read_text(encoding="utf-8")) == baseline


@pytest.mark.parametrize("command", [
    'cd /d "E:\\项目 资料" && python WorkDatabase/summary.py',
    'cmd /c "cd /d E:\\project && python WorkDatabase/summary.py"',
])
def test_delivery_accepts_the_observed_directory_and_shell_wrappers(gate, command):
    trace = [dict(kind="invocation_start", role="worker", agent_id="worker")]
    commands = [dict(turn_id="delivery", agent_id="worker", command=command, returncode=0)]
    assert gate.delivery_evidence(trace, commands, "delivery", "summary.py")["command_receipts"]
