"""Fixed ten-minute live session acceptance; no fake model or manufactured memory."""

from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"
sys.path[:] = [str(SOURCE), *[
    path for path in sys.path
    if Path(path).resolve() != SOURCE
    and not (Path(path).name == "src" and (Path(path) / "redlotus").is_dir())
]]


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def delivery_evidence(trace, commands, turn_id, script_name):
    """Require an actual worker and a successful script launch, independent of its prose."""
    from redlotus.tools.execution import _command_invocations, _program_name, _unquote_shell_word

    workers = {row["agent_id"] for row in trace
               if row["kind"] == "invocation_start" and row.get("role") == "worker"}
    assert len(workers) == 1, f"Expected one worker, got {len(workers)}"
    receipts = {}
    for index, row in enumerate(commands):
        if row["turn_id"] != turn_id or row["agent_id"] not in workers or row["returncode"] != 0:
            continue
        invocations = list(_command_invocations(row["command"]))
        if any(_program_name(argv[0]) not in ("cd", "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe")
               for argv in invocations[:-1]):
            continue
        argv = [_unquote_shell_word(str(item)) for item in invocations[-1]] if invocations else []
        executable = _program_name(argv[0]) if argv else ""
        if executable in ("python", "python.exe", "python3", "python3.exe") and len(argv) > 1:
            if Path(argv[1]).name == script_name and argv[1].endswith(".py"):
                receipts[str(index)] = row
    assert receipts, "No successful worker script execution"
    return dict(worker_ids=sorted(workers), command_receipts=receipts)


def configure(args):
    """Change storage destinations only; never overwrite the source configuration."""
    config = json.loads(args.config.read_text(encoding="utf-8"))
    baseline = deepcopy(config)
    memory = Path.home() / ".redlotus" / "e" / hashlib.sha256(str(args.root).encode()).hexdigest()[:8]
    runtime = args.root / "runtime"
    for directory in (memory, runtime):
        directory.mkdir(parents=True, exist_ok=True)
    config.setdefault("storage", {}).update(
        state_dir=str(memory), sessions_dir=str(args.root / "sessions"),
        references_dir=str(args.root / "references"), runtime_dir=str(runtime),
    )
    config["short_term_memory"]["db_path"] = str(memory / "rag")
    selected = memory / "config.json"
    write_json(selected, config)
    for section in ("models", "model_presets", "gateways", "RAG_models", "context"):
        assert config.get(section) == baseline.get(section), section
    os.environ.update(
        REDLOTUS_CONFIG_FILE=str(selected), REDLOTUS_DATA_DIR=str(memory),
        RAG_DB_PATH=str(memory / "rag"),
    )
    if getattr(args, "dotenv", None):
        os.environ["REDLOTUS_DOTENV_FILE"] = str(args.dotenv)
    return memory, hashlib.sha256(args.config.read_bytes()).hexdigest()


class ApplicationDriver:
    """Submit unmodified CLI input and correlate returned text by the actual turn ID."""

    def __init__(self, project, records):
        from redlotus.core.system import AgentSystem
        from redlotus.core.agents import WorkspaceContext

        self.system = AgentSystem(workspace=WorkspaceContext.from_path(project))
        self.state = self.system.new_cli_session_state()
        self.outputs, self.results = [], {}
        self.records = Path(records)
        original = self.system.run_agent_system

        async def observed(*args, **kwargs):
            identity = kwargs.get("turn_id")
            try:
                history, output = await original(*args, **kwargs)
                self.results[identity] = dict(output=output, status="success")
                return history, output
            except BaseException as exc:
                self.results[identity] = dict(
                    output="", status="cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                    error=f"{type(exc).__name__}: {exc}",
                )
                raise

        self.system.run_agent_system = observed

    async def say(self, text, *, goal=False):
        identity, started = uuid4().hex, time.monotonic()
        error = None
        try:
            await self.system.process_cli_line(
                text, self.state, wait_for_turn=True, goal_mode=goal, input_id=identity,
            )
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
            error = "cancelled"
        result = dict(input=text, input_id=identity, seconds=time.monotonic() - started)
        result.update(self.results.pop(identity, dict(output="", status=error or "rejected")))
        self.outputs.append(result)
        return result

    async def restore(self, path):
        async def choose(snapshots):
            return next(row for row in snapshots if row.path == path)

        controller = self.system._cli_controller
        controller.set_snapshot_picker(choose)
        await self.system.process_cli_line("/load", self.state, wait_for_turn=True)
        assert self.system._session_file.path == path

    async def close(self):
        await self.system.shutdown()


class Evidence:
    """Observe real requests and owned process resources without replacing responses."""

    def __init__(self, root):
        self.root, self.requests, self.resources, self.failures = root, [], [], []
        self.rag_calls = []
        self.commands = []
        self.warnings, self.turns, self.sessions = [], [], []
        self.started = time.monotonic()
        self.lock = threading.RLock()
        self.timed_out = False
        self.complete = False

    def install(self):
        from redlotus.core import config as logger, gateway as model_factory
        from redlotus.core.agents import current_execution_role
        from redlotus.core.agents import current_agent_id

        original = model_factory.create_async_http_client

        def client(**kwargs):
            value = original(**kwargs)

            async def request(req):
                data = json.loads(req.content)
                instructions = [row for row in data.get("messages", []) if row.get("role") in ("system", "developer")]
                row = dict(
                    at=time.monotonic() - self.started, model=data.get("model"),
                    role=current_execution_role(), agent=current_agent_id(),
                    bytes=len(req.content), parallel_tool_calls=data.get("parallel_tool_calls"),
                    instructions_sha256=hashlib.sha256(json.dumps(instructions, sort_keys=True).encode()).hexdigest(),
                )
                with self.lock:
                    self.requests.append(row)
                req.extensions["acceptance_row"] = row

            async def response(res):
                row = res.request.extensions["acceptance_row"]
                with self.lock:
                    row.update(status_code=res.status_code, headers_seconds=time.monotonic() - self.started - row["at"])

            value.event_hooks["request"].append(request)
            value.event_hooks["response"].append(response)
            return value

        model_factory.create_async_http_client = client
        from redlotus.tools import toolkit as BasicTools
        from redlotus.core.agents import current_turn_id

        original_command = BasicTools.run_subprocess

        async def command(*args, **kwargs):
            result = await original_command(*args, **kwargs)
            with self.lock:
                self.commands.append(dict(
                    turn_id=current_turn_id(), agent_id=current_agent_id(),
                    command=result.command, cwd=result.cwd, returncode=result.returncode,
                    output_decoded=result.output_decoded,
                    stdout_sha256=hashlib.sha256(result.stdout.encode()).hexdigest(),
                ))
            return result

        BasicTools.run_subprocess = command
        from redlotus.memory import retrieval

        original_rag = retrieval._rag_api_post

        async def rag(endpoint, body):
            started = time.monotonic()
            row = dict(endpoint=endpoint, model=body.get("model"))
            try:
                return await original_rag(endpoint, body)
            except Exception as exc:
                row["error"] = type(exc).__name__
                raise
            finally:
                row["seconds"] = time.monotonic() - started
                self.rag_calls.append(row)

        retrieval._rag_api_post = rag
        logger.ensure_configured()
        self.log_sink = logger._lg.add(lambda message: self.warnings.append(str(message)), level="WARNING")

    def save(self):
        with self.lock:
            write_json(self.root / "live-evidence.json", dict(
                elapsed=time.monotonic() - self.started, timed_out=self.timed_out,
                complete=self.complete, failures=self.failures, requests=self.requests,
                warnings=self.warnings, resources=self.resources, turns=self.turns, sessions=self.sessions,
                rag_calls=self.rag_calls,
                commands=self.commands,
            ))

    def timeout(self):
        self.timed_out = True
        self.failures.append("600-second acceptance deadline exceeded")
        self.save()
        os._exit(124)

    async def sample(self):
        import psutil

        process = psutil.Process()
        while True:
            rows = []
            for item in [process, *process.children(recursive=True)]:
                try:
                    rows.append(dict(pid=item.pid, rss=item.memory_info().rss,
                                     cpu_seconds=sum(item.cpu_times()[:2]), threads=item.num_threads()))
                except psutil.NoSuchProcess:
                    pass
            self.resources.append(dict(
                at=time.monotonic() - self.started, processes=rows,
                agent_threads=sum(thread.name.startswith("subagent-") for thread in threading.enumerate()),
            ))
            await asyncio.sleep(1)

    def require(self, condition, description):
        if not condition:
            self.failures.append(description)


def prepare_materials(project):
    """Only prepare user-supplied data, never the project implementation or final memory."""
    from PIL import Image, ImageDraw

    project.mkdir(parents=True, exist_ok=True)
    code = uuid4().hex[:6].upper()
    image = Image.new("RGB", (720, 240), "white")
    ImageDraw.Draw(image).text((60, 80), code, fill="black", font_size=64)
    image.save(project / "ticket.png")
    (project / "报价 一.csv").write_text("category,amount\ntrain,240\nhotel,600\nmeal,160\n", encoding="utf-8")
    (project / "约束.md").write_text("# 临时项目\n两人；总预算 3500 元；只使用给定的报价，不假定已经预订。\n", encoding="utf-8")
    return code


def scenario(number):
    stage = (number - 1) // 20 + 1
    questions = [
        "这次是临时测试项目：两人出行，总预算3500元。请区分题设与永久偏好。用一句话复述目标。",
        '请读取 @"报价 一.csv" @约束.md，计算目前三项报价合计，保留实际计算依据。',
        "请调用一个子Agent，在本项目 WorkDatabase 内编写并运行 summary.py，读取报价CSV并汇总金额，生成 totals.json，用 total 字段记录合计。你负责验收，只汇报结果和文件路径。",
        "请主动记住：本测试项目别名为青竹验收，适用范围仅本项目。说明真实保存结果。",
        "当前报价是否就是已经支付？请依据已有信息区分已知事实与待确认事项。",
        "如果酒店报价调整为680元，仅更新计划中的报价，不改原始材料。总预算仍是3500元。复述有效条件。",
        "新的三项报价合计是多少？本次最多用两句话解释计算和剩余预算。",
        "给同行人写一条简短说明，准确使用最新酒店价格，不声称已预订。",
        "列出现在还需要确认的两件事，不要自行补充具体日期。",
        "检查目标、预算和人数是否自洽，简短回答。",
        "现在取消刚才酒店调整，恢复原始600元报价。用一句话列出有效三项报价。",
        "我刚才的改价和撤销，哪个要求现在有效？",
        "把当前计划浓缩为一条待办，不新建文件。",
        "假如增加100元的临时交通备用金，总支出和余款分别是多少？不要改写真实报价。",
        "把备用金方案与原方案做两项简短对比。",
        "这道假设题是否意味着已经发生了额外消费？明确回答。",
        "请以当前最终条件写一条不超过40字的总结。",
        "本项目有哪些不能推断为我的永久偏好的信息？举两个已有例子。",
        "用一句话保留下一步待办：核验三个报价，再由用户确认是否预订。",
        "检查最终预算和尚未预订的状态，用一句话回答。",
    ]
    text = questions[(number - 1) % 20]
    if stage > 1:
        text = f"第{stage}阶段继续核对同一临时项目；不要重复创建已完成的代码。" + text
        if (number - 1) % 20 in (2, 3):
            text = "继续同一项目：请确认已完成的脚本和项目别名，只简短回答，不重复执行或另存记忆。"
    return text


async def live(args, evidence):
    from redlotus.core.session import SessionFile
    from redlotus.core.config import close_all_clients
    from redlotus.core.history import summarize_messages, read_usage_messages
    from redlotus.core.agents import TRACE_STORE

    project = args.root / "projects" / "青竹 验收"
    picture_code = prepare_materials(project)
    driver = ApplicationDriver(project, args.root)
    try:
        for number in range(1, 61):
            prompt = scenario(number)
            if number == 8:
                prompt = "读取 @ticket.png 的验证码，只回答看到的六个字符。"
            if number == 10:
                pending = asyncio.create_task(driver.say(prompt))
                while not driver.system._session.active and not pending.done():
                    await asyncio.sleep(0.01)
                for item in range(6):
                    await driver.system.process_cli_line(
                        f"补充{item + 1}：最终核对时请按收到顺序列出编号{item + 1}。",
                        driver.state, wait_for_turn=False, urgent=True,
                    )
                result = await pending
            else:
                result = await driver.say(prompt)
            evidence.turns.append({key: value for key, value in result.items() if key not in ("input", "output")})
            evidence.turns[-1]["trace"] = TRACE_STORE.events_for_turn(result["input_id"])
            evidence.require(result["status"] == "success", f"turn {number}: {result}")
            session = driver.system._session_file
            evidence.require(session is not None and session.completed_turns == number, f"turn count at {number}")
            if number == 3:
                totals = list((project / "WorkDatabase").rglob("totals.json"))
                evidence.require(len(totals) == 1, "Expected one independently readable totals.json")
                if len(totals) == 1:
                    total = json.loads(totals[0].read_text(encoding="utf-8")).get("total")
                    evidence.require(total == 1000, f"Incorrect computed artifact: {total}")
                try:
                    delivery_evidence(evidence.turns[-1]["trace"], evidence.commands,
                                      result["input_id"], "summary.py")
                except AssertionError as exc:
                    evidence.failures.append(str(exc))
            if number == 8:
                evidence.require(picture_code in result["output"], "Original image was not recognized")
            if number == 10:
                inputs = session.pending_turns(9)[0]["user_inputs"]
                evidence.require(len(inputs) == 7, "Six urgent messages did not remain in the same outer turn")
                evidence.require(all(f"补充{i}" in inputs[i] for i in range(1, 7)), "Urgent input order changed")
            if number == 19:
                saved_id, saved_path = session.session_id, session.path
                await driver.close()
                request_count = len(evidence.requests)
                driver = ApplicationDriver(project, args.root)
                await driver.restore(saved_path)
                evidence.require(driver.system.session_key == saved_id, "Restore changed session ID")
                evidence.require(driver.system._session_file.completed_turns == 19, "Restore changed completed count")
                evidence.require(request_count == len(evidence.requests), "Loading issued a model request")
            evidence.save()
        await driver.system._memory.wait_idle(timeout=max(0, 570 - (time.monotonic() - evidence.started)))
        session = driver.system._session_file
        stored = SessionFile.load(session.path)
        jobs = list(stored._jobs.values())
        automatic = [row for row in jobs if row.get("window")]
        evidence.require(len(automatic) == 3, f"Expected three automatic windows, got {len(automatic)}")
        for index, row in enumerate(sorted(automatic, key=lambda row: row["window"]["start_position"])):
            window = row["window"]
            evidence.require((window["start_position"], window["end_position"]) == (20 * index, 20 * (index + 1)), "Window position changed")
            evidence.require(len(window["new_turn_ids"]) == 20, "Window did not consume twenty new turns")
            evidence.require(len(window["overlap_turn_ids"]) == (3 if index else 0), "Window overlap changed")
        evidence.require(all(row["done"] and row["indexed"] for row in automatic), "Production/index backlog remains")
        evidence.require(not list(session.path.parent.glob("*.jsonl")), "Extra JSONL transcript exists")
        evidence.require(len(list(session.path.parent.glob("*.json"))) == 1, "Session has more than one JSON")
        rows, _ = read_usage_messages(session.path)
        usage = summarize_messages(rows, price_resolver=lambda model: None)
        recall = json.loads(await driver.system._memory.reader.search_episodes("青竹 报价 预算"))
        evidence.require(bool(recall.get("episodes")), "Real produced project memory was not recalled")
        evidence.sessions.append(dict(path=str(session.path), id=session.session_id,
            completed=stored.completed_turns, windows=[row["window"] for row in automatic],
            records=[row["records"] for row in automatic], bytes=session.path.stat().st_size,
            usage=asdict(usage.totals), by_agent={key: asdict(value) for key, value in usage.by_agent.items()},
            pending=stored.pending_jobs(), recall_ids=[row.get("id") for row in recall.get("episodes", [])]))
        evidence.require(not evidence.warnings, "Unexpected WARNING/ERROR recorded")
        evidence.require(bool(evidence.resources), "Process resource sampling did not run")
        evidence.require(all(row["agent_threads"] <= 16 for row in evidence.resources), "Agent thread cap exceeded")
        foreground = [row for role, row in usage.by_agent.items() if role in ("coordinator", "worker")]
        hits = sum(row.cache_hit_tokens for row in foreground)
        total = sum(row.cache_hit_tokens + row.cache_miss_tokens for row in foreground)
        evidence.sessions[-1]["foreground_cache_ratio"] = hits / total if total else None
        evidence.require(bool(total) and hits / total > .9, "Main/worker cache hit ratio did not exceed 90%")
        evidence.complete = True
    finally:
        await driver.close()
        await close_all_clients()


async def run(args, evidence):
    from redlotus.core.config import load_config

    load_config()
    evidence.install()
    sampler = asyncio.create_task(evidence.sample())
    try:
        await live(args, evidence)
    except Exception as exc:
        evidence.failures.append(f"{type(exc).__name__}: {exc}")
        raise
    finally:
        sampler.cancel()
        await asyncio.gather(sampler, return_exceptions=True)
        evidence.save()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--dotenv", type=Path)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--dependencies", type=Path)
    args = parser.parse_args()
    args.root = args.root.resolve()
    args.root.mkdir(parents=True, exist_ok=True)
    if args.dependencies:
        sys.path.insert(0, str(args.dependencies))
    evidence = Evidence(args.root)
    deadline = threading.Timer(600, evidence.timeout)
    deadline.daemon = True
    deadline.start()
    try:
        memory, fingerprint = configure(args)
        write_json(args.root / "provenance.json", dict(
            config_source=str(args.config), sha256=fingerprint, memory=str(memory),
            source=str(SOURCE), deadline_seconds=600,
            source_sha256={str(path.relative_to(SOURCE)): hashlib.sha256(path.read_bytes()).hexdigest()
                           for path in SOURCE.rglob("*.py") if "skills" not in path.parts},
        ))
        asyncio.run(run(args, evidence))
    finally:
        deadline.cancel()
        evidence.save()
    return int(bool(evidence.failures))


if __name__ == "__main__":
    raise SystemExit(main())
