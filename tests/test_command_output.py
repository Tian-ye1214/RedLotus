"""Regressions for real command evidence and localized output."""

import json
import sys
from copy import deepcopy

import pytest

from redlotus.tools import execution as runner
from redlotus.core.agents import WorkspaceContext
from redlotus.tools.toolkit import BasicToolkit


@pytest.mark.parametrize("encoding", ["utf-8", "gbk", "utf-16"])
async def test_native_stdout_and_stderr_keep_chinese(tmp_path, monkeypatch, encoding):
    config = runner._execution_config()
    config["output_encodings"] = ["utf-8-sig", "gbk"]
    monkeypatch.setattr(runner, "_execution_config", lambda: deepcopy(config))
    script = (
        "import sys; "
        f"sys.stdout.buffer.write('中文输出：核验完成'.encode({encoding!r})); "
        f"sys.stderr.buffer.write('中文错误：条件未满足'.encode({encoding!r})); "
        "sys.exit(7)"
    )
    receipt = await runner._run_owned_process(
        [sys._base_executable, "-c", script],
        shell=False,
        cwd=str(tmp_path),
        env=None,
        timeout=10,
    )
    assert (receipt.stdout, receipt.stderr, receipt.returncode) == (
        "中文输出：核验完成",
        "中文错误：条件未满足",
        7,
    )
    assert receipt.output_decoded


async def test_unknown_output_keeps_bytes_and_reports_decoding_failure(
    tmp_path, monkeypatch
):
    config = runner._execution_config()
    config["output_encodings"] = ["utf-8"]
    monkeypatch.setattr(runner, "_execution_config", lambda: deepcopy(config))
    receipt = await runner._run_owned_process(
        [
            sys._base_executable,
            "-c",
            "import sys; sys.stdout.buffer.write(bytes([255, 129]))",
        ],
        shell=False,
        cwd=str(tmp_path),
        env=None,
        timeout=10,
    )
    assert "decode" in receipt.stdout.lower() and r"\xff\x81" in receipt.stdout
    assert (
        "\ufffd" not in receipt.stdout
        and not receipt.stderr
        and receipt.returncode == 0
    )
    from redlotus.tools.registry import tool_result_succeeded

    assert not tool_result_succeeded(receipt.to_text())


async def test_command_reports_its_selected_environment_with_actual_evidence(
    tmp_path, monkeypatch
):
    config = runner._execution_config()
    config.update(
        environment_dir=str(tmp_path / "env"), cache_dir=str(tmp_path / "cache")
    )
    monkeypatch.setattr(runner, "_execution_config", lambda: deepcopy(config))
    toolkit = BasicToolkit(None, workspace=WorkspaceContext.from_path(tmp_path))
    try:
        text = await toolkit.run_command(
            'python -c "import sys; print(sys.executable)"'
        )
        environment = runner.get_execution_environment(cwd=tmp_path)
        assert f"Python on PATH: {environment.python}" in text
        assert f"Working directory: {tmp_path}" in text
        assert f"stdout:\n{environment.python}" in text
        assert "Return code: 0" in text
        # Native stdout verifies identity; metadata alone is not the assertion.
        assert (
            json.loads((environment.root / ".redlotus-environment.json").read_text())[
                "state"
            ]
            == "ready"
        )
    finally:
        await toolkit.close()
