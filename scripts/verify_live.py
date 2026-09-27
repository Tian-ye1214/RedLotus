"""Opt-in real-service acceptance driver; never imported by pytest.

Run with the explicitly selected interpreter and --mode source or wheel.
Only storage paths and synthetic channel owner identities are overridden.
"""

import argparse
import asyncio
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
import traceback
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]


def safe_text(value):
    text = str(value)
    text = re.sub(r"https?://[^\s\"'<>]+", "<service-url>", text)
    return re.sub(r"(?i)(bearer\s+|(?:api[_-]?key|authorization|token)[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+", r"\1<redacted>", text)


class Audit:
    def __init__(self, directory):
        self.directory, self.case, self.events = directory, "setup", []
        self.lock = threading.Lock()
        self.before_gate = None

    def record(self, kind, **values):
        row = dict(time=time.time(), case=self.case, kind=kind)
        row.update(values)
        with self.lock:
            self.events.append(row)
            with (self.directory / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        return row

    def of(self, kind):
        return [row for row in self.events if row["case"] == self.case and row["kind"] == kind]

    def check(self, condition, description, **details):
        self.record("assertion", passed=bool(condition), description=description, **details)
        if not condition:
            raise AssertionError(description)

    def instrument(self):
        import httpx
        from redlotus.core.gateway import RequestPolicy
        from redlotus.tools import registry
        from redlotus.sessions.context import current_short_agent_id
        from pydantic_ai import BinaryContent, ToolReturn
        from pydantic_ai.messages import ToolReturnPart
        original_send = httpx.AsyncClient.send
        original_before = RequestPolicy.before_model_request
        original_after = RequestPolicy.after_model_request
        original_record = registry._record

        def record_tool(name, t0, success, result=None, error=None):
            original_record(name, t0, success, result=result, error=error)
            content = result.content if isinstance(result, ToolReturn) else []
            self.record("tool_execution", name=name, success=success,
                        actor=current_short_agent_id(),
                        result=safe_text(result.return_value if isinstance(result, ToolReturn) else result),
                        native_tool_return=isinstance(result, ToolReturn),
                        media=[dict(identifier=item.identifier, sha256=hashlib.sha256(item.data).hexdigest())
                               for item in content or [] if isinstance(item, BinaryContent)],
                        error=safe_text(error) if error else None)

        async def send(client, request, *args, **kwargs):
            endpoint = request.url.path.rstrip("/").rsplit("/", 1)[-1]
            category = endpoint if endpoint in {"embeddings", "rerank", "responses", "completions", "messages"} else "metadata"
            row = self.record("http_start", category=category, method=request.method)
            try:
                response = await original_send(client, request, *args, **kwargs)
            except BaseException as exc:
                self.record("http_error", category=category, error=type(exc).__name__)
                raise
            self.record("http_end", category=category, status=response.status_code, seconds=time.time() - row["time"])
            return response

        async def before(policy, ctx, request_context):
            result = await original_before(policy, ctx, request_context)
            tools = request_context.model_request_parameters.function_tools
            parts = [part for message in request_context.messages for part in message.parts]
            media = [item for part in parts for item in (part.content if isinstance(getattr(part, "content", None), list) else [])
                     if isinstance(item, BinaryContent)]
            self.record("model_request", role=policy.role, tools=[tool.name for tool in tools],
                        returns=[dict(name=part.tool_name, id=part.tool_call_id) for part in parts if isinstance(part, ToolReturnPart)],
                        media=[dict(identifier=item.identifier, sha256=hashlib.sha256(item.data).hexdigest()) for item in media])
            if self.before_gate:
                await self.before_gate(policy, request_context)
            return result

        async def after(policy, ctx, *, request_context, response):
            result = await original_after(policy, ctx, request_context=request_context, response=response)
            self.record("model_response", role=policy.role, usage=dataclasses.asdict(response.usage),
                        state=response.state, tools=[part.tool_name for part in response.parts if hasattr(part, "tool_name")])
            return result

        httpx.AsyncClient.send, RequestPolicy.before_model_request, RequestPolicy.after_model_request = send, before, after
        registry._record = record_tool

    def presentation(self):
        presentation = Mock()
        presentation.supports_model_stream.return_value = False
        presentation.show_file_diff.return_value = (0, 0, 0)
        presentation.format_user_log_text.side_effect = lambda message: message.text
        presentation.handle_turn_error.side_effect = lambda error: self.record("turn_error", error=safe_text(error))
        presentation.print_warning.side_effect = lambda message: self.record("warning", message=safe_text(message))
        presentation.show_model_output.side_effect = lambda text, **kw: self.record("answer", text=text)
        presentation.finish_model_stream.side_effect = lambda text, **kw: self.record("answer", text=text)
        return presentation


def configure(args, audit):
    if args.mode == "source":
        sys.path.insert(0, str(ROOT / "src"))
    from redlotus.runtime import config

    sources = config.config_sources()
    before = [hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None for path in sources]
    os.environ["REDLOTUS_CONFIG_FILE"] = str(sources[0])
    os.environ["REDLOTUS_DOTENV_FILE"] = str(sources[1])
    os.environ["REDLOTUS_DATA_DIR"] = str(audit.directory / "isolated-data")
    os.environ["RAG_DB_PATH"] = str(audit.directory / "isolated-data" / "lancedb")
    original = config.settings
    storage = dict(project_dir="data", sessions_dir="data/sessions", references_dir="data/references",
                   project_logs_dir="data/logs", runtime_dir=None)

    def isolated_settings():
        values = original()
        overrides = config.ConfigValues(storage.copy(), path=("storage",), sources=values.sources)
        if storage["runtime_dir"] is None:
            overrides.deleted.add("runtime_dir")
        values["storage"] = overrides
        values["bot"] = {"owner_channels": {"qq": ["live-owner"], "wechat": "live-owner"}}
        return values

    config.settings = isolated_settings
    audit.storage = storage
    audit.instrument()
    from redlotus.runtime import logging
    from redlotus.ui import presentation as _presentation
    logging._lg.configure(patcher=lambda record: record.update(message=safe_text(record["message"])))
    logging.activate_log_dir(logging.prepare_log_dir(__import__("redlotus.runtime.resources", fromlist=["WorkspaceContext"]).WorkspaceContext.from_path(audit.directory)))
    logging.console_sink = lambda message: audit.record("log", message=safe_text(message))
    logging._lg.add(lambda message: audit.record("diagnostic", severity=message.record["level"].name,
                    message=safe_text(message.record["message"])), level="WARNING", format="{message}")
    return sources, before


def check_imports(args, audit):
    expected = ROOT / "src" if args.mode == "source" else Path(args.install_root).resolve()
    origins = {name: str(Path(module.__file__).resolve()) for name, module in tuple(sys.modules.items())
               if (name == "redlotus" or name.startswith("redlotus.")) and getattr(module, "__file__", None)}
    audit.check(all(Path(origin).is_relative_to(expected) for origin in origins.values()),
                "Every imported application module belongs to the selected source/install", origins=origins)


async def run(args, audit):
    sys.path.insert(0, str(audit.directory / "scenario-sources"))
    from live_cases import CASES
    from redlotus.runtime.network import close_all_clients
    selected = args.cases.split(",") if args.cases else list(CASES)
    outcomes = []
    for name in selected:
        audit.case = name
        directory = audit.directory / name
        directory.mkdir()
        os.chdir(directory)
        os.environ["REDLOTUS_DATA_DIR"] = str(directory / "isolated-data")
        os.environ["RAG_DB_PATH"] = str(directory / "isolated-data" / "lancedb")
        audit.storage["runtime_dir"] = None
        started = time.monotonic()
        record_resources(audit, directory, "before")
        print(f"START {name}", flush=True)
        try:
            await asyncio.wait_for(CASES[name](audit, directory), args.timeout)
            check_imports(args, audit)
            status = "passed"
        except Exception as error:
            status = "failed"
            audit.record("failure", error_type=type(error).__name__, error=safe_text(error),
                         stack=safe_text("".join(traceback.format_exception(error))))
        finally:
            audit.before_gate = None
            await close_all_clients()
            record_resources(audit, directory, "after")
        diagnostics = classify_diagnostics(audit)
        audit.record("diagnostic_inventory", items=diagnostics)
        unexpected = [row for row in diagnostics if not row["expected"]]
        if unexpected:
            status = "failed"
            audit.record("assertion", passed=False, description="No unclassified warning/error may pass", unexpected=len(unexpected))
        row = dict(case=name, status=status, seconds=round(time.monotonic() - started, 2),
                   model_responses=sum(row.get("state") != "interrupted" for row in audit.of("model_response")),
                   interrupted_responses=sum(row.get("state") == "interrupted" for row in audit.of("model_response")),
                   http_requests=len(audit.of("http_start")),
                   usage=[dict(role=row["role"], state=row.get("state"), **row["usage"]) for row in audit.of("model_response")])
        outcomes.append(row)
        audit.record("case_result", **row)
        print(json.dumps({key: value for key, value in row.items() if key != "usage"}), flush=True)
    return outcomes


def record_resources(audit, directory, phase):
    import psutil
    process = psutil.Process()
    audit.record("resources", phase=phase, cpu=process.cpu_times()._asdict(),
                 memory_bytes=process.memory_info().rss, threads=process.num_threads(),
                 children=[dict(pid=child.pid, name=child.name()) for child in process.children(recursive=True)],
                 storage_bytes=sum(path.stat().st_size for path in directory.rglob("*") if path.is_file()))


def classify_diagnostics(audit):
    results = []
    rows = [*audit.of("diagnostic"), *audit.of("warning"), *audit.of("turn_error"), *audit.of("http_error")]
    for row in rows:
        message = row.get("message") or row.get("error") or ""
        expected = (audit.case == "channels" and "Agent 请求失败" in message and (
                        "synthetic attachment download failure" in message or "QQ失败.png" in message)
                    or audit.case == "send_failure" and "synthetic send failure" in message
                    or audit.case == "pause_resume" and row["kind"] == "http_error" and message == "CancelledError")
        results.append(dict(kind=row["kind"], message=message, expected=bool(expected)))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("source", "wheel"), required=True)
    parser.add_argument("--install-root")
    parser.add_argument("--cases", help="Comma-separated independent cases; default is the whole approved matrix")
    parser.add_argument("--timeout", type=int, default=300, help="Acceptance deadline per case, not a model setting")
    parser.add_argument("--output", type=Path, default=ROOT / ".test-runtime/release-post2/live")
    args = parser.parse_args()
    if args.mode == "wheel" and not args.install_root:
        parser.error("--install-root is required for wheel provenance checks")
    directory = args.output.resolve() / (args.mode + "-" + time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1000000:06}")
    directory.mkdir(parents=True, exist_ok=False)
    audit = Audit(directory)
    scenario_sources = directory / "scenario-sources"
    scenario_sources.mkdir()
    for path in (Path(__file__), Path(__file__).with_name("live_cases.py")):
        (scenario_sources / path.name).write_bytes(path.read_bytes())
    audit.record("scenario_sources", hashes={path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in scenario_sources.iterdir()})
    sources, before = configure(args, audit)
    outcomes = asyncio.run(run(args, audit))
    audit.case = "final"
    after = [hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None for path in sources]
    audit.check(before == after, "Private configuration sources are byte-identical", hashes=after)
    result = dict(mode=args.mode, interpreter=sys.executable, cases=outcomes,
                  boundaries=["Synthetic channel events; no QQ/WeChat account", "Services unchanged; actual model/embedding/rerank calls recorded individually",
                              "Missing runtime is intentional", "No historical-data migration or long-session/cache claim"])
    (directory / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(str(directory), flush=True)
    return int(any(row["status"] != "passed" for row in outcomes))


if __name__ == "__main__":
    raise SystemExit(main())
