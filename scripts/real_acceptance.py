"""Live acceptance through the application's real CLI controller and Textual UI.

No fake models, responses, tool results, vectors or pre-populated memories.
Only test storage and fixture paths are isolated; configured model settings stay intact.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

from acceptance_assets import make_assets


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )


async def until(predicate, timeout=600):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.1)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def isolate_environment(root: Path, *, config_mode="global") -> dict:
    import platformdirs

    config_dir = Path(platformdirs.user_config_dir("RedLotus", appauthor=False))
    development_root = Path(__file__).resolve().parents[1]
    config = (
        config_dir / "config.json"
        if config_mode == "global"
        else development_root / "src/redlotus/config.json"
    )
    for key in ("REDLOTUS_CONFIG_FILE", "REDLOTUS_CONFIG_DIR", "REDLOTUS_DOTENV_FILE"):
        os.environ.pop(key, None)
    if config_mode == "source":
        os.environ["REDLOTUS_CONFIG_FILE"] = str(config)
        os.environ["REDLOTUS_DOTENV_FILE"] = str(development_root / ".env")
    from redlotus.config.app_config import initialize_config

    initialize_config()
    data_dir = Path(
        os.environ.get("REDLOTUS_DATA_DIR")
        or platformdirs.user_data_dir("RedLotus", appauthor=False)
    )
    raw = config.read_bytes()
    values = json.loads(raw)
    test_data = root / "state"
    cached = data_dir / "logs" / "cache" / "openrouter_models.json"
    if cached.is_file():
        destination = test_data / "logs" / "cache" / cached.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cached, destination)
    os.environ["REDLOTUS_DATA_DIR"] = str(test_data)
    os.environ["PATH"] = (
        str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]
    )
    frozen = dict(
        config_mode=config_mode,
        config_path=str(config),
        config_hash=hashlib.sha256(raw).hexdigest(),
        models=values["models"],
        context=values["context"],
        rag_models=values["RAG_models"],
    )
    write_json(root / "configuration-baseline.json", frozen)
    return frozen


class WireAudit:
    """Attach HTTPX request observers without changing requests or responses."""

    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()
        self.sequence = 0

    def write(self, row):
        with self.lock:
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    def install(self):
        import httpx

        original = httpx.AsyncClient.__init__
        audit = self

        def initialize(client, *args, **kwargs):
            hooks = dict(kwargs.get("event_hooks") or {})
            hooks["request"] = [*hooks.get("request", []), audit.request]
            hooks["response"] = [*hooks.get("response", []), audit.response]
            kwargs["event_hooks"] = hooks
            original(client, *args, **kwargs)

        httpx.AsyncClient.__init__ = initialize

    async def response(self, response):
        row = dict(
            time=time.time(),
            path=response.request.url.path,
            status=response.status_code,
            request_id=response.request.extensions.get("audit_id"),
        )
        if "text/event-stream" in response.headers.get("content-type", ""):
            import httpx

            original_stream, audit = response.stream, self

            class ObservedStream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    buffer, usage = b"", {}
                    try:
                        async for chunk in original_stream:
                            buffer += chunk
                            while b"\n" in buffer:
                                line, buffer = buffer.split(b"\n", 1)
                                if not line.startswith(b"data:"):
                                    continue
                                try:
                                    event = json.loads(line[5:].strip())
                                except ValueError:
                                    continue
                                found = (
                                    event.get("usage")
                                    or event.get("response", {}).get("usage")
                                    or event.get("message", {}).get("usage")
                                )
                                if found:
                                    usage.update(found)
                            yield chunk
                    finally:
                        audit.write({**row, "kind": "usage", "usage": usage or None})

                async def aclose(self):
                    await original_stream.aclose()

            response.stream = ObservedStream()
        else:
            await response.aread()
            if response.is_success:
                row["usage"] = response.json().get("usage")
        self.write(row)

    async def request(self, request):
        try:
            payload = json.loads(request.content)
        except (ValueError, UnicodeDecodeError):
            return
        with self.lock:
            self.sequence += 1
            request.extensions["audit_id"] = self.sequence
        native = []

        def inspect(value):
            if isinstance(value, dict):
                if value.get("type") in (
                    "image_url",
                    "video_url",
                    "file",
                    "input_image",
                    "input_file",
                ):
                    native.append(value["type"])
                if "inlineData" in value or "inline_data" in value:
                    native.append("google_inline")
                for item in value.values():
                    inspect(item)
            elif isinstance(value, list):
                for item in value:
                    inspect(item)

        inspect(payload)
        row = dict(
            time=time.time(),
            request_id=request.extensions["audit_id"],
            path=request.url.path,
            model=payload.get("model"),
            native=native,
            encoded_bytes=len(request.content),
            output_format=payload.get("response_format", {}).get("type"),
            tools_count=len(payload.get("tools", [])),
            system_sha=hashlib.sha256(
                json.dumps(
                    [
                        m
                        for m in payload.get("messages", [])
                        if m.get("role") == "system"
                    ],
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode()
            ).hexdigest(),
            tools_sha=hashlib.sha256(
                json.dumps(
                    payload.get("tools", []), ensure_ascii=False, sort_keys=True
                ).encode()
            ).hexdigest(),
            input_count=len(payload["input"])
            if isinstance(payload.get("input"), list)
            else None,
            settings={
                k: payload[k]
                for k in (
                    "temperature",
                    "max_tokens",
                    "max_completion_tokens",
                    "reasoning_effort",
                    "top_p",
                    "thinking",
                    "parallel_tool_calls",
                )
                if k in payload
            },
        )
        self.write(row)


class ApplicationDriver:
    def __init__(self, project: Path, records: Path):
        from redlotus.agent_core.system import AgentSystem
        from redlotus.runtime.context import WorkspaceContext
        from redlotus.workspace.workspace import set_workspace

        set_workspace(project)
        self.project, self.records = project, records
        self.system = AgentSystem(workspace=WorkspaceContext.from_path(project))
        self.state = self.system.new_cli_session_state()
        self.system._cli_controller._active_session_state = self.state
        self.outputs = []
        self.thread_ids = set()

        async def answer(question):
            write_json(records / "attention.json", dict(question=question))
            return "只授权当前测试项目内的文件操作和命令执行；请采用不需要删除目录或外发消息的方式完成任务。"

        self.system.set_ask_user_handler(answer)

    async def start(self):
        missing = await self.system.prepare_cli_session()
        if missing:
            raise RuntimeError("Existing model configuration is unavailable")

    async def idle(self):
        async with asyncio.timeout(900):
            while (
                self.system.has_current_turn
                or self.system._session.queue.pending
                or self.system._session.queue.current
                or self.system._cli_controller._preparing
            ):
                self.thread_ids.update(
                    h.thread.ident
                    for h in self.system._orchestrator.factory.handles
                    if h.thread.ident
                )
                await asyncio.sleep(0.1)

    async def say(self, text, *, goal=False):
        self.records.mkdir(parents=True, exist_ok=True)
        before = len(self.state.history.messages)
        await self.system.process_cli_line(
            text, self.state, wait_for_turn=False, goal_mode=goal
        )
        await self.idle()
        if getattr(self.system, "last_turn_error", None) is not None:
            raise RuntimeError(str(self.system.last_turn_error))
        messages = self.state.history.messages
        last = next(
            (m for m in reversed(messages) if getattr(m, "kind", "") == "response"),
            None,
        )
        output = (
            "\n".join(
                p.content for p in last.parts if getattr(p, "part_kind", "") == "text"
            )
            if last
            else ""
        )
        self.outputs.append(
            dict(
                input=text if len(text) < 1000 else f"[long input: {len(text)} chars]",
                output=output,
                messages_before=before,
                messages_after=len(messages),
            )
        )
        write_json(self.records / "dialogue.json", self.outputs)
        print("TURN", len(self.outputs), output[:180].replace("\n", " "), flush=True)
        return output

    async def close(self):
        await self.system.shutdown()
        memory = self.system._memory
        assert not (
            memory.last_error or memory.store.last_error or memory.store.retrieval_error
        ), (memory.last_error, memory.store.last_error, memory.store.retrieval_error)


class AcceptanceSuite:
    def __init__(self, args, frozen):
        self.args, self.root, self.frozen = args, args.root, frozen
        self.results = {}

    async def case(self, name, execute):
        from loguru import logger as capture
        from redlotus.infra.logger import ensure_configured

        ensure_configured()
        problems = []
        sink = capture.add(
            lambda message: problems.append(str(message)), level="WARNING"
        )
        print("CASE START", name, flush=True)
        started = time.time()
        try:
            details = await execute()
            assert not problems, "Unexpected WARNING/ERROR: " + "\n".join(problems[:5])
            result = dict(status="passed", details=details)
        except Exception as exc:
            import traceback

            print(traceback.format_exc(), flush=True)
            message = f"{type(exc).__name__}: {exc}"
            blocked = "Insufficient Balance" in message or "status_code: 402" in message
            result = dict(status="blocked" if blocked else "failed", error=message)
        finally:
            capture.remove(sink)
        result["warning_error_count"] = len(problems)
        result["seconds"] = round(time.time() - started, 2)
        self.results[name] = result
        write_json(self.root / "report.json", self.results)
        print("CASE END", name, result["status"], result.get("error", ""), flush=True)

    async def driver(self, name):
        project = self.root / "projects" / name
        truth = make_assets(project, self.root / "truth" / f"{name}.json")
        driver = ApplicationDriver(project, self.root / "evidence" / name)
        await driver.start()
        return driver, truth

    async def conversation(self):
        driver, _ = await self.driver("conversation")
        try:
            assert (await driver.say("你好，请用一句话介绍自己。")).strip()
            await driver.say(
                "本轮对话中的临时核对词是 FERN-284，稍后会问你。无需保存长期记忆。只回复收到。"
            )
            assert "FERN-284" in await driver.say("刚才的临时核对词是什么？")
            queued = ["只回复 FIFO-ONE。", "只回复 FIFO-TWO。", "只回复 FIFO-THREE。"]
            await asyncio.gather(
                *(
                    driver.system.process_cli_line(
                        text, driver.state, wait_for_turn=False
                    )
                    for text in queued
                )
            )
            await driver.idle()
            events = driver.system._memory.observations.read(
                driver.system._memory.observations.order()[-3:]
            )
            assert [event.user_inputs[0] for event in events] == queued
            before = len(driver.state.history.messages)
            await driver.system.process_cli_line(
                "请直接使用 run_command 运行 Python，等待 20 秒再打印 COMMAND-DONE。不要委派子 Agent。完成后报告命令输出。",
                driver.state,
                wait_for_turn=False,
            )
            await until(
                lambda: any(
                    (
                        getattr(part, "tool_name", "") == "run_command"
                        for message in driver.state.history.messages[before:]
                        for part in message.parts
                    )
                ),
                timeout=300,
            )
            urgent = "URGENT-" + secrets_token()
            await driver.system.process_cli_line(
                "/urgent 命令结束后请在最终回复中包含 "
                + urgent
                + "，同时报告实际命令输出。",
                driver.state,
                wait_for_turn=False,
            )
            await driver.idle()
            responses = [
                part.content
                for message in driver.state.history.messages[before:]
                for part in message.parts
                if getattr(part, "part_kind", "") == "text"
            ]
            assert any(urgent in text and "COMMAND-DONE" in text for text in responses)
            await driver.system.process_cli_line(
                "请直接运行一个等待 30 秒的 Python 命令。",
                driver.state,
                wait_for_turn=False,
            )
            await asyncio.sleep(2)
            await driver.system.process_cli_line(
                "/stop", driver.state, wait_for_turn=False
            )
            await driver.idle()
            assert (await driver.say("现在只回复：可以继续。")).strip()
            return dict(
                turns=len(driver.outputs),
                fifo=True,
                urgent_with_tool_result=True,
                stopped=True,
            )
        finally:
            await driver.close()

    async def general(self):
        driver, truth = await self.driver("general")
        try:
            await driver.say(
                "请读取当前项目的 sales.csv。用两个子 Agent 分别独立核对 North 和 South 的总额，然后生成标准库 Python 脚本 WorkDatabase/sum_sales.py，真实运行，把各地区总额保存为 WorkDatabase/totals.json。不要安装依赖，不要操作当前项目以外的数据。最终简短报告实际执行结果。"
            )
            actual = read_json(driver.project / "WorkDatabase/totals.json")
            assert actual == truth["totals"], actual
            assert (driver.project / "WorkDatabase/sum_sales.py").is_file()
            assert len(driver.thread_ids) >= 2, (
                "two real child threads were not observed"
            )
            browser_code, skill_code = secrets_token(), secrets_token()
            (driver.project / "browser.html").write_text(
                '<title>Interactive evidence</title><input id="value"><button id="apply" '
                "onclick=\"document.querySelector('output').textContent='"
                + browser_code
                + "-'+document.querySelector('input').value\">Apply</button><output></output>",
                encoding="utf-8",
            )
            from redlotus.infra.paths import user_skills_dir

            skill = user_skills_dir() / "live-evidence"
            skill.mkdir(parents=True, exist_ok=True)
            (skill / "SKILL.md").write_text(
                "---\nname: live-evidence\ndescription: Local acceptance script\n---\n"
                "Use execute_skill_script to run verify.py; report its actual stdout.",
                encoding="utf-8",
            )
            (skill / "verify.py").write_text(
                "print(" + repr(skill_code) + ")\n", encoding="utf-8"
            )
            answer = await driver.say(
                "使用浏览器工具打开当前项目 browser.html，向 #value 填入 lotus，点击 #apply，读取页面结果并保存 WorkDatabase/browser.png 截图。"
                "然后通过 Skills 工具加载 live-evidence 的指令，真实执行它的 verify.py，报告两个输出。"
            )
            assert browser_code + "-lotus" in answer and skill_code in answer, answer
            assert (driver.project / "WorkDatabase/browser.png").is_file()
            return dict(
                totals=actual,
                child_threads=len(driver.thread_ids),
                browser=True,
                skills=True,
            )
        finally:
            await driver.close()

    async def memory(self):
        driver, truth = await self.driver("memory-a")
        try:
            await driver.say("你好。")
            await driver.say("你能处理哪些类型的本地任务？一句话回答。")
            await driver.say(
                f"请主动记住一条全局长期资料：验收项目的部署别名是 {truth['memo']}。它需要跨项目检索，但不是用户画像，不要放进 MEMORY.md。请通过记忆工具真实保存。"
            )
            assert (
                truth["memo"] not in await driver.system._memory.long_term.list_memory()
            ), "detailed project data entered core profile"
            for index in range(4, 46):
                if index == 5:
                    prompt = '请直接识别引用图片中的数字、形状与颜色，作为本任务的多模态核验记录：@"图片 样例.png"。不要使用代码或 OCR。'
                elif index == 22 and self.args.include_video:
                    prompt = '请直接观看这份原始视频，按顺序报告三个阶段的数字，作为本任务的多模态核验记录：@"视频 样例.mp4"。不要抽帧或运行代码。'
                elif index in (10, 25, 35, 45):
                    prompt = f"继续这个验收任务：请实际读取 sales.csv 核对地区总额，并把阶段 {index} 的结果保存到 WorkDatabase/check-{index}.json。用一句话报告实际核验结果。"
                else:
                    prompt = f"补充当前验收任务的第 {index} 项约束：报告需要保留地区字段和整数金额，阶段编号为 {index}。这是本任务上下文，不是长期偏好。只需一句话确认。"
                await driver.say(prompt)
                if index in (24, 25, 44, 45):
                    await driver.system.wait_for_memory_quiescent(timeout=600)
                    receipts = [
                        read_json(path)
                        for path in driver.system._memory.jobs_dir.glob("*.json")
                    ]
                    windows = [
                        row["window"]
                        for row in receipts
                        if row.get("window") and row.get("done")
                    ]
                    authorized = [
                        row
                        for row in receipts
                        if row.get("request")
                        and (row.get("result") or {}).get("request_authorized")
                    ]
                    assert len(authorized) == 1, (
                        "Ordinary context became explicit memory"
                    )
                    expected = {24: 0, 25: 1, 44: 1, 45: 2}[index]
                    assert len(windows) == expected, (
                        f"window count at turn {index}: {windows}"
                    )
                    if index == 45:
                        windows.sort(key=lambda row: row["start_position"])
                        assert (
                            len(windows[1]["new_turn_ids"]) == 20
                            and len(windows[1]["overlap_turn_ids"]) == 5
                        )
            await driver.system.wait_for_memory_quiescent(timeout=600)
            known_jobs = set(driver.system._memory.jobs_dir.glob("*.json"))
            project_marker = "PROJECT-" + secrets_token()
            active = asyncio.create_task(
                driver.say(
                    f"请主动记住项目资料：本项目验收标识是 {project_marker}，今后的本项目会话要使用。只保存到项目范围。"
                )
            )
            await until(
                lambda: set(driver.system._memory.jobs_dir.glob("*.json")) != known_jobs
            )
            replay = asyncio.create_task(driver.system._memory.process_pending())
            await active
            await replay
            receipts = [
                read_json(path)
                for path in set(driver.system._memory.jobs_dir.glob("*.json"))
                - known_jobs
            ]
            requested = [row for row in receipts if row.get("request")]
            assert len(requested) == 1 and requested[0]["done"]
            assert len(requested[0]["usage"]) == 1, (
                "Concurrent recovery repeated LLM production"
            )
            await driver.close()
            other, _ = await self.driver("memory-b")
            try:
                answer = await other.say(
                    "请调用记忆检索工具，查找我之前让你全局保存的验收项目部署别名。返回别名及记忆记录 ID。"
                )
                assert truth["memo"] in answer, answer
                assert project_marker not in await other.system._memory.search_memory(
                    project_marker
                )
                replacement = "UPDATED-" + secrets_token()
                await other.say(
                    f"请把验收项目的全局部署别名更正为 {replacement}，旧别名不再有效。仍然只存长期资料，不放入 MEMORY.md。"
                )
                corrected = await other.say(
                    "请重新检索当前有效的验收项目部署别名，只回答有效值。"
                )
                assert replacement in corrected and truth["memo"] not in corrected
                await other.say("请忘记这条验收项目部署别名记录，之后不要再召回它。")
                await other.say("/clear")
                await other.system.wait_for_memory_quiescent(timeout=600)
                forgotten = await other.say(
                    "请用记忆检索工具检查验收项目部署别名是否仍是有效记忆，找不到就说明找不到。"
                )
                assert replacement not in forgotten and truth["memo"] not in forgotten
                secret = "TEST-ONLY-" + secrets_token()
                await other.say(
                    f"请把这条验收专用凭据放入长期记忆：password: {secret}。"
                )
                await other.system.wait_for_memory_quiescent(timeout=600)
                assert secret not in await other.system._memory.long_term.list_memory()
                assert all(
                    secret not in (row.subject + row.text())
                    for row in other.system._memory.store.all()
                )
                return dict(
                    turns=45,
                    cross_project_recall=True,
                    project_isolation=True,
                    one_production_during_replay=True,
                    correction=True,
                    forgotten=True,
                    credentials_excluded=True,
                    window_counts=[0, 1, 1, 2],
                )
            finally:
                await other.close()
        finally:
            await driver.close()

    async def multimodal(self):
        driver, truth = await self.driver("multimodal")
        errors = []
        try:
            image = await driver.say(
                '请识别这个引用图片中的六位数字以及左侧形状、颜色：@"图片 样例.png"。不要通过编写代码或 OCR 工具读取它。'
            )
            if truth["image_code"] not in image:
                errors.append("image recognition failed")
            if self.args.include_video:
                video = await driver.say(
                    '请直接观看引用的原始视频，按时间顺序报告三个场景中出现的四位数字：@"视频 样例.mp4"。不要抽帧或通过代码读取它。'
                )
                positions = [video.find(code) for code in truth["video_codes"]]
                if any(p < 0 for p in positions) or positions != sorted(positions):
                    errors.append("native video recognition failed")
            from check_references import validate_references

            local_report, names = await validate_references(
                driver.project, truth, include_video=self.args.include_video
            )
            write_json(driver.records / "reference-checks.json", local_report)
            names = [name for name in names if not name.endswith((".png", ".mp4"))]
            docs = await driver.say(
                "读取所有引用文件。逐文件报告 marker 和图片中的六位数字；表格报告地区总额及公式，幻灯片包含备注，Word 包含表格数字。保留来源文件名。"
                + " ".join('@"' + name + '"' for name in names)
            )
            if not all(marker in docs for marker in truth["document_markers"]):
                errors.append("document references incomplete")
            assert all(code in docs for code in truth["document_codes"].values()), (
                "Embedded document images were not recognized: " + docs
            )
            assert all(
                token in docs
                for token in ("37", "SUM", "35", "40", ".doc", ".ppt", ".xls")
            ), docs
            refs = " ".join("@ref-%02d.txt" % i for i in range(20))
            answer = await driver.say(
                "请报告引用文件的数量、最早和最后的编号，以及这些编号之和。" + refs
            )
            assert "20" in answer and "19" in answer and "190" in answer, answer
            assert not errors, "; ".join(errors)
            return dict(
                image=True,
                video="tested" if self.args.include_video else "excluded by user",
                documents=True,
            )
        finally:
            await driver.close()

    async def interface(self):
        from redlotus.agent_core.system import AgentSystem
        from redlotus.cli.tui import RedLotusTui, AgentInput
        from redlotus.cli.output import set_output_sink
        from redlotus.runtime.context import WorkspaceContext
        from redlotus.workspace.workspace import set_workspace
        from textual.widgets import Input

        project = self.root / "projects" / "interface"
        make_assets(project, self.root / "truth" / "interface.json")
        (project / "review.txt").write_text("original\n", encoding="utf-8")
        set_workspace(project)
        system = AgentSystem(workspace=WorkspaceContext.from_path(project))
        app = RedLotusTui(system)
        try:
            async with app.run_test(size=(120, 38)) as pilot:
                await pilot.pause()
                inp = app.query_one("#input", AgentInput)

                async def send(text):
                    await app.on_input_submitted(Input.Submitted(inp, text))

                await send(
                    "请使用 write_file 工具把 review.txt 改为 changed 加一个换行。只做这个文件修改，不要运行命令。"
                )
                await until(lambda: system.review_store.entries(), timeout=600)
                await pilot.press("ctrl+r")
                await pilot.pause()
                app.save_screenshot(
                    filename="diff.svg", path=str(self.root / "evidence")
                )
                await pilot.press("n")
                assert (project / "review.txt").read_text(
                    encoding="utf-8"
                ) == "original\n", "TUI undo did not restore original file"
                await pilot.press("escape")
                await until(lambda: not system.has_current_turn, timeout=600)
                await send("请用 write_file 把 review.txt 写为 kept 加换行。")
                await until(lambda: system.review_store.entries(), timeout=600)
                await pilot.press("ctrl+r", "y", "escape")
                assert (project / "review.txt").read_text(
                    encoding="utf-8"
                ) == "kept\n", "TUI keep changed file contents"
                await until(lambda: not system.has_current_turn, timeout=600)
                await send("/panel")
                await until(
                    lambda: (
                        app._panel_refresh_task is not None
                        and app._panel_refresh_task.done()
                    )
                )
                app.save_screenshot(
                    filename="panel.svg", path=str(self.root / "evidence")
                )
                await pilot.press("escape")
                await send("/api")
                await until(lambda: app._ask_future is not None, timeout=30)
                await pilot.press("escape")
                await pilot.pause()
                assert (
                    hashlib.sha256(
                        Path(self.frozen["config_path"]).read_bytes()
                    ).hexdigest()
                    == self.frozen["config_hash"]
                )
                for command in (
                    "/config",
                    "/skills",
                    "/status",
                    "/context",
                    "/LTM show",
                    "/STM show",
                    "/tasks",
                    "/agent",
                    "/effort",
                ):
                    await app.on_input_submitted(Input.Submitted(inp, command))
                    await pilot.pause()
                app.save_screenshot(
                    filename="status.svg", path=str(self.root / "evidence")
                )
                await pilot.press("shift+tab", "shift+tab")
                assert app._run_mode.value == "goal", (
                    "Shift+Tab did not enter goal mode"
                )
                await send(
                    "目标：读取 sales.csv，计算全部金额，写入 WorkDatabase/goal.txt，再实际读取该文件确认。必须完成后才结束。"
                )
                await asyncio.sleep(0.2)
                await until(
                    lambda: not (system.has_current_turn or app._active_line_handlers),
                    timeout=600,
                )
                assert "75" in (project / "WorkDatabase/goal.txt").read_text(
                    encoding="utf-8"
                )
                app.save_screenshot(
                    filename="goal.svg", path=str(self.root / "evidence")
                )
                await send("/clear")
                await pilot.pause()
                await until(lambda: not app._active_line_handlers, timeout=120)
                assert not app.state.history.messages, "Clear retained history"
                await send("/load")
                await pilot.pause()
                await until(lambda: not app._active_line_handlers, timeout=120)
                assert app.state.history.messages, "Load did not restore history"
                other = self.root / "projects" / "interface-other"
                other.mkdir(parents=True, exist_ok=True)
                await send('/cd "' + str(other) + '"')
                await until(lambda: not app._active_line_handlers, timeout=120)
                assert system.workspace.root == other.resolve(), (
                    "CD did not switch project"
                )
                return dict(
                    diff_undo=True,
                    diff_keep=True,
                    goal_completed=True,
                    clear=True,
                    load=True,
                    cd=True,
                    screenshots=["diff.svg", "status.svg", "goal.svg"],
                )
        finally:
            set_output_sink(None)
            await system.shutdown()

    async def stress(self):
        from redlotus.ModelGateway.ModelChecker import get_effective_max_context_async
        from redlotus.ModelGateway.usage_accounting import latest_usage_input_tokens

        driver, _ = await self.driver("stress")
        first, middle = "START-" + secrets_token(), "MID-" + secrets_token()
        usages = []
        try:
            await driver.say(
                f"这是一项长对话测试。最初约束码是 {first}。在最后追问前只回复 ACK。不要把这些临时测试材料写入长期记忆。"
            )
            limit = await get_effective_max_context_async(role="coordinator")
            for turn in range(1, 129):
                needle = (
                    f"重要的任务中途约束码：{middle}。请在压缩后仍保留这个约束。\n"
                    if turn == 2
                    else ""
                )
                data = "\n".join(
                    f"记录{turn}-{row}: 此条是合成业务日志，任务要求保留审计和结果依据。值={(row * 31 + turn) % 997}。"
                    for row in range(1800)
                )
                await driver.say(
                    f"长文本批次 {turn}，以下均为本任务引用材料。{needle}\n{data}\n只回复 ACK，不执行工具。"
                )
                used = latest_usage_input_tokens(driver.state.history.messages) or 0
                usages.append(used)
                write_json(
                    self.root / "evidence" / "stress-usage.json",
                    dict(limit=limit, usage=usages),
                )
                compressed = driver.state.history.compress_summary_state or any(
                    (getattr(message, "metadata", None) or {}).get("origin")
                    == "context_summary"
                    for message in driver.state.history.messages
                )
                if compressed:
                    answer = await driver.say(
                        "只根据当前上下文回答最初约束码和中途约束码，不要检索记忆或读取文件。"
                    )
                    assert first in answer and middle in answer, answer
                    return dict(
                        context_limit=limit,
                        peak_input=max(usages),
                        turns=turn,
                        compression=True,
                        anchors_preserved=True,
                    )
            raise AssertionError(
                "automatic compression did not occur within 128 real long turns"
            )
        finally:
            await driver.close()

    async def run(self):
        methods = [
            ("conversation", self.conversation),
            ("general", self.general),
            ("memory", self.memory),
            ("multimodal", self.multimodal),
            ("interface", self.interface),
            ("stress", self.stress),
        ]
        for name, method in methods:
            if not self.args.only or name in self.args.only:
                if any(row["status"] == "blocked" for row in self.results.values()):
                    self.results[name] = dict(
                        status="blocked",
                        error="Original model service unavailable; no further model requests were sent.",
                    )
                    write_json(self.root / "report.json", self.results)
                else:
                    await self.case(name, method)
        current = Path(self.frozen["config_path"]).read_bytes()
        assert hashlib.sha256(current).hexdigest() == self.frozen["config_hash"], (
            "Operator configuration changed"
        )
        from redlotus.infra.shared_http import close_all_clients

        await close_all_clients()
        return all(row["status"] == "passed" for row in self.results.values())


def secrets_token():
    import secrets

    return secrets.token_hex(4).upper()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, required=True, help="Fresh isolated NTFS test directory"
    )
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--config-mode", choices=("global", "source"), default="global")
    parser.add_argument(
        "--include-video",
        action="store_true",
        help="Enable video cases when the configured gateway supports native video",
    )
    args = parser.parse_args()
    args.root = args.root.resolve()
    args.root.mkdir(parents=True, exist_ok=True)
    frozen = isolate_environment(args.root, config_mode=args.config_mode)
    WireAudit(args.root / "wire.jsonl").install()
    raise SystemExit(0 if asyncio.run(AcceptanceSuite(args, frozen).run()) else 1)


if __name__ == "__main__":
    main()
