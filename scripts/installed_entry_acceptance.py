"""Drive installed CLI/TUI processes through a real Windows pseudoterminal."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import threading
import time
from pathlib import Path

from winpty import PtyProcess
import pyte

from acceptance_assets import make_assets
from real_acceptance import read_json as read_unlocked_json, write_json
from redlotus.infra.persist_utils import file_lock


def read_json(path):
    # Observe mutable application files through the same cross-process lock as writers.
    with file_lock(path):
        return read_unlocked_json(path)


class TerminalSession:
    def __init__(
        self,
        executable,
        project,
        state,
        evidence,
        *,
        python,
        tui,
        environment=None,
        transcript_root=None,
    ):
        self.project, self.state, self.evidence = project.resolve(), state, evidence
        evidence.mkdir(parents=True, exist_ok=True)
        identity = hashlib.sha256(
            os.path.normcase(str(self.project)).encode()
        ).hexdigest()[:24]
        self.storage = state / "projects" / identity
        self.conversations = (
            (transcript_root / identity) if transcript_root else self.storage
        )
        self.screen = pyte.Screen(140, 44)
        self.stream = pyte.Stream(self.screen)
        self.text, self.lock = "", threading.Lock()
        env = dict(os.environ)
        for key in (
            "REDLOTUS_CONFIG_FILE",
            "REDLOTUS_CONFIG_DIR",
            "REDLOTUS_DOTENV_FILE",
        ):
            env.pop(key, None)
        env.update(
            REDLOTUS_DATA_DIR=str(state),
            REDLOTUS_LEGACY_CLI="0" if tui else "1",
            TERM="xterm-256color",
        )
        env["PATH"] = str(python.parent) + os.pathsep + env["PATH"]
        env.update(environment or {})
        arguments = (
            [str(arg) for arg in executable]
            if isinstance(executable, (list, tuple))
            else [str(executable)]
        )
        self.process = PtyProcess.spawn(
            arguments, cwd=str(self.project), env=env, dimensions=(44, 140)
        )
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            with (self.evidence / "terminal.ansi").open(
                "w", encoding="utf-8"
            ) as output:
                while True:
                    value = self.process.read(8192)
                    output.write(value)
                    output.flush()
                    with self.lock:
                        self.text += value
                        self.stream.feed(value)
                        if "\x1b[6n" in value:
                            self.process.write(
                                f"\x1b[{self.screen.cursor.y + 1};{self.screen.cursor.x + 1}R"
                            )
        except (EOFError, OSError):
            pass

    def wait(self, predicate, timeout=900):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            if not self.process.isalive():
                raise RuntimeError(
                    "Installed process exited before the requested operation completed"
                )
            time.sleep(0.15)
        self.snapshot("timeout")
        raise TimeoutError("Installed terminal operation did not complete")

    def start(self):
        self.wait(lambda: "输入" in self.text or "RedLotus" in self.text, timeout=120)
        time.sleep(1)
        self.snapshot("startup")

    def send(self, text):
        with (self.evidence / "submitted.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps({"time": time.time(), "text": text}, ensure_ascii=False)
                + "\n"
            )
        self.process.write(text + "\r")

    def snapshot(self, name):
        with self.lock:
            lines = list(self.screen.display)
        body = "".join(
            f'<text x="12" y="{24 + index * 19}">{html.escape(line)}</text>'
            for index, line in enumerate(lines)
        )
        (self.evidence / (name + ".svg")).write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="1460" height="860"><rect width="100%" height="100%" fill="#18151f"/>'
            '<g fill="#f0e9fa" font-family="Consolas,monospace" font-size="16" xml:space="preserve">'
            + body
            + "</g></svg>",
            encoding="utf-8",
        )

    def _event(self, prompt, previous=()):
        for path in (self.storage / "memory/turns").glob("*.json"):
            value = read_json(path)
            if (
                value["id"] not in previous
                and value.get("finished_at")
                and value["user_inputs"][0] == prompt
            ):
                return value
        return None

    def _messages(self):
        paths = list(self.conversations.glob("coordinator*_ModelMessages.json"))
        return (
            read_json(max(paths, key=lambda p: p.stat().st_mtime_ns))["model_messages"]
            if paths
            else []
        )

    def say(self, prompt):
        previous = {
            path.stem for path in (self.storage / "memory/turns").glob("*.json")
        }
        self.send(prompt)
        event = self.wait(lambda: self._event(prompt, previous))
        assert event["status"] == "success", event
        responses = [m for m in self._messages() if m["kind"] == "response"]
        response = responses[-1]
        assert response.get("provider_response_id"), (
            "No actual provider response was saved"
        )
        assert response["usage"]["input_tokens"] > 0, response["usage"]
        text = "\n".join(
            p["content"] for p in response["parts"] if p["part_kind"] == "text"
        )
        self.snapshot(
            "turn-" + str(len(list((self.storage / "memory/turns").glob("*.json"))))
        )
        write_json(
            self.evidence / (event["id"] + ".json"),
            dict(input=prompt, output=text, event=event, usage=response["usage"]),
        )
        return text

    def exit(self):
        self.send("/exit")
        deadline = time.monotonic() + 900
        while self.process.isalive() and time.monotonic() < deadline:
            time.sleep(0.2)
        assert not self.process.isalive(), "Exit left the application running"
        self.reader.join(timeout=5)
        self.process.close()
        problems = []
        for path in (self.state / "logs").rglob("*.log"):
            problems.extend(
                re.findall(
                    r"^.*\|\s*(?:WARNING|ERROR|CRITICAL)\s*\|.*$",
                    path.read_text(encoding="utf-8"),
                    flags=re.M,
                )
            )
        assert not problems, problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--tui", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    project = root / "中文 空格项目"
    truth = make_assets(project, root / "truth.json")
    # A working-directory credential file must not override the installed global source.
    (project / ".env").write_text(
        "API_KEY=wrong-project-test-only\nBASE_URL=http://127.0.0.1:1\n",
        encoding="utf-8",
    )
    session = TerminalSession(
        args.executable,
        project,
        root / "state",
        root / "terminal",
        python=args.python,
        tui=args.tui,
    )
    report = dict(executable=str(args.executable), tui=args.tui, status="running")
    started = time.monotonic()
    try:
        session.start()
        assert session.say("你好，用一句话说明你可以如何协助本地项目。")
        answer = session.say(
            '请直接识别引用图片中的数字和形状，不运行代码或OCR：@"图片 样例.png"'
        )
        assert truth["image_code"] in answer, answer
        answer = session.say(
            "请逐份读取引用资料，核对地区金额，并报告 Word 的表格数字、PDF 的合同金额、Excel 的公式和 PPT 的备注（公式与备注原样引用）：@sales.csv @sales.xlsx @reference.docx @reference.pptx @reference.pdf"
        )
        assert all(
            value in answer
            for value in ("35", "40", "37", "93", "SUM", "second source")
        ), answer
        session.say(
            "请用两个子 Agent 独立核对 sales.csv 中 North 和 South 的总额，并生成标准库脚本 WorkDatabase/verify.py，真实运行后把地区总额写到 WorkDatabase/totals.json。只操作本项目，不安装依赖。"
        )
        assert read_json(project / "WorkDatabase/totals.json") == truth["totals"]
        answer = session.say(
            f"请主动记住全局验收资料：本次安装验收的部署别名是 {truth['memo']}，以后跨项目查询要能找到它。它不是用户画像，不进入 MEMORY.md。请使用记忆工具真实保存。"
        )
        assert truth["memo"] in answer
        jobs = [
            read_json(path) for path in (session.storage / "memory/jobs").glob("*.json")
        ]
        assert any(
            job["done"]
            and job.get("request")
            and job["records"]
            and job["result"]["request_authorized"]
            for job in jobs
        )
        session.send("/usage")
        time.sleep(1)
        session.snapshot("usage")
        session.exit()
        report.update(
            status="passed",
            image=True,
            documents=True,
            subagents=True,
            artifacts=True,
            memory=True,
        )
    except Exception as exc:
        import traceback

        report.update(status="failed", error=str(exc), traceback=traceback.format_exc())
        session.snapshot("failure")
        if session.process.isalive():
            session.process.terminate(force=True)
    report["seconds"] = time.monotonic() - started
    write_json(root / "report.json", report)
    print(json.dumps(report, ensure_ascii=True), flush=True)
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
