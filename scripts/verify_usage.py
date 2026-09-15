"""Fixed usage gate: full regression plus four live turns, at most 600 seconds.

Run from the repository: python scripts/verify_usage.py
Timeouts and API failures fail the gate; no automatic reruns or extra scenarios.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import shlex
import sys
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def configure(args):
    original = args.config.read_bytes()
    config = deepcopy(json.loads(original))
    state = Path.home() / ".redlotus/evaluation" / args.root.name
    config["storage"].update(
        state_dir=str(state / "data"),
        sessions_dir=str(args.root / "sessions"),
        references_dir=str(args.root / "references"),
        compression_dir=str(args.root / "compression"),
        runtime_dir=str(args.root / "runtime"),
    )
    config["execution"].update(
        environment_dir=str(args.root / "runtime/environments/{project_id}"),
        cache_dir=str(args.root / "runtime/cache"),
    )
    config["short_term_memory"]["db_path"] = str(
        Path.home()
        / ".redlotus/r"
        / ("quick-" + hashlib.sha256(str(state).encode()).hexdigest()[:12])
    )
    save(state / "config.json", config)
    os.environ.update(
        REDLOTUS_CONFIG_FILE=str(state / "config.json"),
        REDLOTUS_CONFIG_DIR=str(state),
        REDLOTUS_DATA_DIR=str(state / "data"),
        REDLOTUS_DOTENV_FILE=str(args.config.parent / ".env"),
    )
    # Preserve effective model/RAG credentials; isolate only the test database.
    os.environ["RAG_DB_PATH"] = config["short_term_memory"]["db_path"]
    save(
        args.root / "config-fingerprint.json",
        dict(
            source=str(args.config),
            sha256=hashlib.sha256(original).hexdigest(),
            overrides="isolated storage and execution paths only",
        ),
    )


def runs_summary(content, root):
    metadata = content.split("\nstdout:\n", 1)[0]
    command = next(
        (
            line.removeprefix("Command: ")
            for line in metadata.splitlines()
            if line.startswith("Command: ")
        ),
        "",
    )
    args = [part.strip("\"'") for part in shlex.split(command, posix=False)]
    if len(args) < 2:
        return False
    program = Path(args[0].replace("\\", "/")).name.lower()
    script = Path(args[1].replace("\\", "/"))
    if not script.is_absolute():
        script = root / "project" / script
    return (
        "Return code: 0" in metadata.splitlines()
        and program in {"python", "python.exe", "python3", "python3.exe"}
        and script.resolve() == (root / "project/WorkDatabase/summary.py").resolve()
    )


def delivery_evidence(root, turn_id):
    workers, executions = set(), {}
    for path in (root / "sessions").glob("*/worker_*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row["meta"].get("turn_id") != turn_id:
                continue
            workers.add(row["meta"]["sub_id"])
            for part in row["message"]["parts"]:
                if (
                    part["part_kind"] == "tool-return"
                    and part["tool_name"] == "run_command"
                ):
                    content = part["content"]
                    if isinstance(content, str) and runs_summary(content, root):
                        executions[part["tool_call_id"]] = content
    assert len(workers) == 1, f"Expected exactly one Worker invocation, found {workers}"
    assert executions, "The Worker did not actually run summary.py successfully"
    return dict(worker_ids=sorted(workers), command_receipts=executions)


async def live(args):
    configure(args)
    sys.path.insert(0, str(REPO / "src"))
    from real_acceptance import ApplicationDriver, WireAudit
    from redlotus.infra.shared_http import close_all_clients

    WireAudit(args.root / "wire.jsonl").install()
    project = args.root / "project"
    project.mkdir()
    values = [secrets.randbelow(900) + 100 for _ in range(3)]
    (project / "数据.csv").write_text(
        "qty\n" + "\n".join(map(str, values)), encoding="utf-8"
    )
    (project / "要求.md").write_text(
        "汇总 qty 列；输出 JSON 包含 count、total。原输入只读。", encoding="utf-8"
    )
    hashes = {
        name: hashlib.sha256((project / name).read_bytes()).hexdigest()
        for name in ("数据.csv", "要求.md")
    }
    marker = secrets.token_hex(6)
    driver = ApplicationDriver(project, args.root / "conversation")
    try:
        await driver.start()
        await driver.system.bind_session("quick-usage")
        driver.state.is_first_input = False
        assert "可以开始" in await driver.say("你好。只回复：可以开始。")
        await driver.say(
            "根据 @数据.csv @要求.md，只委派一个 Worker 用标准库编写 WorkDatabase/summary.py，"
            "真实运行并生成 WorkDatabase/result.json。主 Agent 简短报告实际回执。不要安装依赖或查看框架源码。"
        )
        actual = json.loads(
            (project / "WorkDatabase/result.json").read_text(encoding="utf-8")
        )
        assert actual == dict(count=3, total=sum(values)), actual
        assert (project / "WorkDatabase/summary.py").is_file() and driver.thread_ids
        execution = delivery_evidence(args.root, driver.outputs[-1]["turn_id"])
        assert all(
            hashlib.sha256((project / name).read_bytes()).hexdigest() == digest
            for name, digest in hashes.items()
        )
        await driver.say(
            f"请调用 remember 主动记住本项目的验收标识 {marker}，只保存到项目范围，不进入全局画像，报告真实保存回执。"
        )
        memories = driver.system._memory.store.all("project")
        assert any(marker in row.text() and row.kind == "requested" for row in memories)
    finally:
        await driver.close()
        await close_all_clients()
    fresh = ApplicationDriver(project, args.root / "recall")
    try:
        await fresh.start()
        await fresh.system.bind_session("quick-usage-new-session")
        fresh.state.is_first_input = False
        assert not fresh.state.history.messages
        reply = await fresh.say(
            "请检索本项目已保存的主动记忆，回答验收标识及其记录 ID，不读取项目文件，不猜测。"
        )
        assert marker in reply
        assert any(
            row.id in reply
            for row in fresh.system._memory.store.all("project")
            if marker in row.text()
        )
        save(
            args.root / "live-result.json",
            dict(
                passed=True,
                user_turns=4,
                actual_artifact=actual,
                original_files_unchanged=True,
                worker_threads=sorted(driver.thread_ids),
                execution=execution,
                memory_records=[row.model_dump(mode="json") for row in memories],
                fresh_context_recall=reply,
            ),
        )
    finally:
        await fresh.close()
        await close_all_clients()


async def run_stage(command, env, log, seconds):
    from redlotus.infra.subprocess_runner import _terminate_process_tree

    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=REPO,
        env=env,
        stdout=log,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=os.name != "nt",
    )
    try:
        return await asyncio.wait_for(process.wait(), timeout=seconds)
    except (TimeoutError, asyncio.CancelledError):
        await _terminate_process_tree(process)
        raise


async def gate(args):
    started = time.monotonic()
    args.root.mkdir(parents=True, exist_ok=False)
    temporary = args.root / "runtime/tmp"
    temporary.mkdir(parents=True)
    env = dict(
        os.environ,
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONIOENCODING="utf-8",
        TEMP=str(temporary),
        TMP=str(temporary),
    )
    env["REDLOTUS_TEST_MEMORY_ROOT"] = str(
        Path.home()
        / ".redlotus/r"
        / ("tests-" + hashlib.sha256(str(args.root).encode()).hexdigest()[:12])
    )
    report = dict(limit_seconds=600, status="running", stages=[])
    commands = [
        (
            "regression",
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "--basetemp",
                str(args.root / "runtime/pytest"),
            ],
        ),
        (
            "live",
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--live-only",
                "--root",
                str(args.root),
                "--config",
                str(args.config),
            ],
        ),
    ]
    try:
        for name, command in commands:
            stage = dict(name=name, status="running")
            report["stages"].append(stage)
            stage_start = time.monotonic()
            print(
                f"{name}: started; remaining {600 - (stage_start - started):.0f}s",
                flush=True,
            )
            with (args.root / f"{name}.log").open("w", encoding="utf-8") as log:
                returncode = await run_stage(
                    command,
                    env,
                    log,
                    max(0.001, 590 - (time.monotonic() - started)),
                )
            stage.update(
                returncode=returncode,
                seconds=time.monotonic() - stage_start,
                status="passed" if returncode == 0 else "failed",
            )
            if returncode:
                raise RuntimeError(f"{name} failed; see {name}.log")
            if name == "live":
                lines = (args.root / "live.log").read_text(encoding="utf-8")
                if re.search(
                    r"^\d{2}:\d{2}:\d{2} \|\s*(WARNING|ERROR)\s*\|", lines, re.MULTILINE
                ):
                    raise RuntimeError("Unexpected WARNING/ERROR; see live.log")
            print(f"{name}: passed in {stage['seconds']:.1f}s", flush=True)
        report["status"] = "passed"
    except (TimeoutError, RuntimeError) as exc:
        report.update(
            status="failed",
            error="600-second deadline exceeded"
            if isinstance(exc, TimeoutError)
            else str(exc),
        )
        report["stages"][-1]["status"] = "failed"
    finally:
        report["seconds"] = time.monotonic() - started
        save(args.root / "result.json", report)
        print(json.dumps(report, ensure_ascii=False), flush=True)
    return int(report["status"] != "passed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path.home() / ".redlotus/config.json"
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO
        / "WorkDatabase/evaluation"
        / datetime.now().strftime("quick-usage-%Y%m%d-%H%M%S"),
    )
    parser.add_argument("--live-only", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    sys.path.insert(0, str(REPO / "src"))
    args.root, args.config = args.root.resolve(), args.config.resolve()
    if args.live_only:
        asyncio.run(live(args))
        return 0
    return asyncio.run(gate(args))


if __name__ == "__main__":
    raise SystemExit(main())
