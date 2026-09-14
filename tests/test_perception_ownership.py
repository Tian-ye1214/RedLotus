"""Thread and evidence ownership checks, separate from live acceptance."""

import asyncio
import json
import subprocess
import sys
import threading
from pathlib import Path

from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelResponse, TextPart

from redlotus.references.store import ReferenceStore
from redlotus.runtime.context import WorkspaceContext
from redlotus.runtime.subagents import SubagentFactory, SubagentSpec
from redlotus.tools.memory.evidence import EvidenceReader
from redlotus.tools.memory.models import ObservedTurn


async def test_perception_reads_main_trace_not_child_internals(tmp_path):
    workspace = WorkspaceContext.from_path(tmp_path)
    journal = tmp_path / "trace.jsonl"
    message = ModelMessagesTypeAdapter.dump_python(
        [ModelResponse(parts=[TextPart("child detail")])], mode="json"
    )[0]
    journal.write_text(
        "\n".join(
            json.dumps(
                dict(
                    event_id=role,
                    meta=dict(agent=role, turn_id="turn"),
                    message=message,
                )
            )
            for role in ("worker", "manager", "coordinator")
        ),
        encoding="utf-8",
    )
    event = ObservedTurn(
        id="event",
        project_id=workspace.project_id,
        session_id="session",
        turn_id="turn",
        user_inputs=["request"],
        evidence_paths=[str(journal)],
    )
    packets, _, _ = await EvidenceReader(ReferenceStore(workspace)).collect([event])
    assert [row["id"] for row in packets[0]["operations"]] == ["event:coordinator:0"]


async def test_factory_close_cancels_background_work_without_waiting_for_completion(
    tmp_path,
):
    factory = SubagentFactory(1)
    release = threading.Event()
    started = threading.Event()

    async def execute():
        started.set()
        try:
            while not release.is_set():
                await asyncio.sleep(0.01)
        finally:
            release.set()

    spec = SubagentSpec(
        "memory", None, WorkspaceContext.from_path(tmp_path), role="perception"
    )
    handle = factory.start_background(spec, execute)
    try:
        assert await asyncio.to_thread(started.wait, 2)
        await asyncio.wait_for(factory.close(), 1)
        assert not handle.thread.is_alive()
        assert handle._future.cancelled()
        assert release.is_set()
    finally:
        release.set()
        await handle.close()


def test_cli_exit_deadline_applies_only_after_exit_is_requested(tmp_path):
    import time

    code = """
import asyncio
import threading
import time
from redlotus.agent_core.entrypoint import main
from redlotus.agent_core.system import AgentSystem
from redlotus.config.app_config import settings
from redlotus.runtime.subagents import SubagentSpec
started = threading.Event()
def blocking():
    started.set()
    time.sleep(12)
async def work():
    await asyncio.to_thread(blocking)
async def interaction(self, **kwargs):
    self._memory_factory.start_background(SubagentSpec('memory', None, self.workspace, role='perception'), work)
    assert await asyncio.to_thread(started.wait, 5)
    await asyncio.sleep(settings()['lifecycle']['shutdown_grace_seconds'] + .1)
    print('EXIT_AT:' + str(time.time()), flush=True)
    await self.shutdown()
AgentSystem.run_interactive = interaction
main()
print('UNEXPECTED_NORMAL_RETURN', flush=True)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    exit_at = next(
        float(line.split(":", 1)[1])
        for line in result.stdout.splitlines()
        if line.startswith("EXIT_AT:")
    )
    assert time.time() - exit_at < 5, result.stdout
    assert "UNEXPECTED_NORMAL_RETURN" not in result.stdout
