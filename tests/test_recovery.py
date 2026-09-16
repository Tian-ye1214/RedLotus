import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path


import pytest
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)

from redlotus.tools.execution import ensure_execution_environment
from redlotus.tools.execution import get_execution_environment
from redlotus.tools.execution import run_subprocess
from redlotus.core.agents import WorkspaceContext




def test_legacy_session_archives_are_not_loaded_or_migrated(tmp_path):
    from redlotus.core.session import list_workspace_snapshots
    from redlotus.core.session import SessionFile

    old = tmp_path / "coordinator_old_ModelMessages.json"
    old.write_text('{"meta":{"agent":"coordinator"},"model_messages":[]}', encoding="utf-8")
    session = SessionFile.create(tmp_path, "project", session_id="new")
    snapshots = list_workspace_snapshots(root=tmp_path)
    assert [row.path for row in snapshots] == [session.path]
    assert not list(tmp_path.rglob("migration*"))

def process_alive(pid):
    if sys.platform == "win32":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return (
            re.search(
                rb'^"[^"]*","' + str(pid).encode("ascii") + rb'",', result.stdout, re.M
            )
            is not None
        )
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


async def test_cancel_terminates_external_process_tree(tmp_path):
    # Provisioning is covered separately; this deadline measures process-tree cancellation.
    environment = await asyncio.to_thread(get_execution_environment, cwd=tmp_path)
    await ensure_execution_environment(environment)
    pid_file = tmp_path / "child.pid"
    script = tmp_path / "parent.py"
    script.write_text(
        "import subprocess,sys,time,os\nfrom pathlib import Path\n"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
        "creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0)\n"
        "ready=Path(sys.argv[1]); temporary=ready.with_suffix('.tmp')\n"
        "temporary.write_text(str(os.getpid())+','+str(child.pid))\n"
        "temporary.replace(ready)\nchild.wait()\n",
        encoding="utf-8",
    )
    task = asyncio.create_task(
        run_subprocess(
            [str(environment.python), str(script), str(pid_file)],
            shell=False,
            cwd=str(tmp_path),
            timeout=30,
        )
    )
    async with asyncio.timeout(8):
        while not pid_file.is_file():
            await asyncio.sleep(0.02)
        parent, child = map(int, pid_file.read_text().split(","))
        assert process_alive(parent) and process_alive(child)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        while process_alive(parent) or process_alive(child):
            await asyncio.sleep(0.02)
