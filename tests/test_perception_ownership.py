"""Thread and evidence ownership checks, separate from live acceptance."""

import asyncio
import json
import subprocess
import sys
import threading
from pathlib import Path

from pydantic_ai.messages import ModelResponse, TextPart

from redlotus.tools.references import ReferenceStore
from redlotus.core.agents import WorkspaceContext
from redlotus.core.agents import SubagentFactory
from redlotus.core.agents import SubagentSpec
from redlotus.memory.records import EvidenceReader
from redlotus.memory.records import ObservedTurn


async def test_perception_reads_main_trace_not_child_internals(tmp_path):
    workspace = WorkspaceContext.from_path(tmp_path)
    from memory_helpers import session_file
    session = session_file(workspace)
    session.save_context([ModelResponse(parts=[TextPart("main evidence")])], turn_id="turn")
    for role in ("worker", "manager"):
        session.record_usage([ModelResponse(parts=[TextPart("child detail")])], role=role, invocation=role)
    event = ObservedTurn(
        id="event",
        project_id=workspace.project_id,
        session_id="session",
        turn_id="turn",
        user_inputs=["request"],
        evidence_paths=[str(session.path)],
    )
    reader = EvidenceReader(ReferenceStore(workspace))
    reader.session = session
    packets, _, _ = await reader.collect([event])
    assert [row["text"] for row in packets[0]["operations"]] == ["main evidence"]
    assert "child detail" not in session.path.read_text(encoding="utf-8")


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
import sys
from pathlib import Path
sys.path[:] = [str(Path.cwd() / 'src'), *[p for p in sys.path if not (Path(p).name == 'src' and (Path(p) / 'redlotus').is_dir())]]
from redlotus.core.config import main
from redlotus.core.system import AgentSystem
from redlotus.core.config import settings
from redlotus.core.agents import SubagentSpec
started = threading.Event()
def blocking():
    started.set()
    time.sleep(12)
async def work():
    await asyncio.to_thread(blocking)
async def interaction(self, **kwargs):
    self._factory.start_background(SubagentSpec('memory', None, self.workspace, role='perception'), work)
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
        # Startup imports are outside the exit deadline asserted below.
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    exit_at = next(
        float(line.split(":", 1)[1])
        for line in result.stdout.splitlines()
        if line.startswith("EXIT_AT:")
    )
    assert time.time() - exit_at < 5, result.stdout
    assert "UNEXPECTED_NORMAL_RETURN" not in result.stdout
