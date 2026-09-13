import asyncio
import json
import os
import sys
from pathlib import Path

import lancedb
import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    UserPromptPart,
    TextPart,
    ModelMessagesTypeAdapter,
)

from redlotus.infra.subprocess_runner import run_subprocess
from redlotus.runtime.context import WorkspaceContext
from redlotus.tools.memory.migration import migrate_observations
from redlotus.tools.memory.observations import ObservationStore


async def test_legacy_migration_is_idempotent_and_excludes_unknown_projects(tmp_path):
    project, db_path = tmp_path / "project", Path(os.environ["RAG_DB_PATH"])
    root = project / ".redlotus"
    root.mkdir(parents=True)
    for role in ("coordinator", "manager", "worker"):
        messages = [
            ModelRequest(parts=[UserPromptPart("修复数据库")]),
            ModelResponse(parts=[TextPart("结果待核验")]),
        ]
        (root / f"{role}_ModelMessages.json").write_text(
            json.dumps(
                dict(
                    meta=dict(agent=role, date="20250101", topic="迁移"),
                    model_messages=ModelMessagesTypeAdapter.dump_python(
                        messages, mode="json"
                    ),
                ),
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    db = lancedb.connect(str(db_path))
    db.create_table(
        "conversation_turns",
        data=[
            dict(
                source=str(root / "coordinator_ModelMessages.json") + "#old",
                text="重复的旧索引",
                vector=[1.0, 0.0],
            ),
            dict(source="unowned.json#old", text="无法确定项目", vector=[1.0, 0.0]),
            dict(
                source=str(tmp_path / "another" / ".redlotus" / "old.json") + "#old",
                text="别人的项目",
                vector=[1.0, 0.0],
            ),
            dict(
                source=str(root / "deleted.json") + "#hash#c0",
                text="仅存在索引的第一块",
                vector=[1.0, 0.0],
            ),
            dict(
                source=str(root / "deleted.json") + "#hash#c1",
                text="仅存在索引的第二块",
                vector=[1.0, 0.0],
            ),
        ],
    )
    store = ObservationStore(WorkspaceContext.from_path(project))
    await migrate_observations(store, rag_config=dict(db_path=str(db_path)))
    before = {p.name: p.read_bytes() for p in store.turns.glob("*.json")}
    await migrate_observations(store, rag_config=dict(db_path=str(db_path)))
    assert before == {p.name: p.read_bytes() for p in store.turns.glob("*.json")}
    assert len(before) == 2
    episodes = [json.loads(body) for body in before.values()]
    assert any(len(ep["evidence_paths"]) >= 1 for ep in episodes)
    assert all(ep["status"] == "unverified" for ep in episodes)
    assert not any(
        "别人的项目" in json.dumps(ep, ensure_ascii=False)
        or "重复的旧索引" in json.dumps(ep, ensure_ascii=False)
        for ep in episodes
    )
    assert (
        store.root / "migration_backup" / "coordinator_ModelMessages.json"
    ).is_file()
    assert (
        store.root / "migration_backup" / "unassigned_legacy_vectors.json"
    ).is_file()
    assert (db_path / "migration_backup" / "conversation_turns.lance").is_dir()
    assert db.open_table("conversation_turns").count_rows() == 5


def process_alive(pid):
    if sys.platform == "win32":
        import ctypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong()
        try:
            kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
            return code.value == 259
        finally:
            kernel.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


async def test_cancel_terminates_external_process_tree(tmp_path):
    pid_file = tmp_path / "child.pid"
    script = tmp_path / "parent.py"
    script.write_text(
        "import subprocess,sys,time,os\nfrom pathlib import Path\n"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
        "creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0)\n"
        "Path(sys.argv[1]).write_text(str(os.getpid())+','+str(child.pid))\ntime.sleep(60)\n",
        encoding="utf-8",
    )
    task = asyncio.create_task(
        run_subprocess(
            [sys.executable, str(script), str(pid_file)],
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
