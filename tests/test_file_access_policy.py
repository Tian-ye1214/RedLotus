import asyncio
from pathlib import Path

import pytest

from redlotus.runtime.resources import WorkspaceContext
from redlotus.tools.base_tools import BasicToolkit
from redlotus.tools.registry import SkillsManager


@pytest.fixture
def toolkit(tmp_path, isolated_config):
    workspace = WorkspaceContext.from_path(tmp_path / "project")
    workspace.root.mkdir()
    return BasicToolkit(SkillsManager(workspace=workspace), workspace=workspace,
                        show_diff=lambda *args, **kwargs: (0, 0, 0))


@pytest.mark.asyncio
async def test_workdatabase_write_edit_delete_need_no_confirmation(toolkit):
    async def unexpected(question):
        pytest.fail("WorkDatabase operations must not prompt")
    toolkit.set_ask_user_handler(unexpected)
    assert "Saved" in await toolkit.write_file("WorkDatabase/example.txt", content="before")
    assert "Saved" in await toolkit.edit_file("WorkDatabase/example.txt", "before", "after")
    assert toolkit.read_file("WorkDatabase/example.txt").return_value == "after"
    assert "Deleted" in await toolkit.delete_file("WorkDatabase/example.txt")
    assert not (toolkit.workspace.root / "WorkDatabase/example.txt").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["yes", "no"])
async def test_external_write_requires_confirmation_before_file_changes(toolkit, answer):
    target = toolkit.workspace.root / "src/example.txt"
    target.parent.mkdir()
    target.write_text("original", encoding="utf-8")
    prompts = []
    async def confirm(question):
        assert target.read_text(encoding="utf-8") == "original"
        prompts.append(question)
        return answer
    toolkit.set_ask_user_handler(confirm)
    await toolkit.edit_file(str(target), "original", "updated")
    assert len(prompts) == 1
    assert str(target) in prompts[0] and "original" in prompts[0] and "updated" in prompts[0]
    assert target.read_text(encoding="utf-8") == ("updated" if answer == "yes" else "original")


@pytest.mark.asyncio
async def test_change_during_confirmation_is_not_overwritten(toolkit):
    target = toolkit.workspace.root / "example.txt"
    target.write_text("original", encoding="utf-8")
    async def confirm(question):
        target.write_text("user changed", encoding="utf-8")
        return "yes"
    toolkit.set_ask_user_handler(confirm)
    result = await toolkit.write_file(str(target), content="model changed")
    assert "Error" in result
    assert target.read_text(encoding="utf-8") == "user changed"


def test_external_reads_are_allowed(toolkit, tmp_path):
    target = tmp_path / "outside.txt"
    target.write_text("external evidence", encoding="utf-8")
    assert toolkit.read_file(str(target)).return_value == "external evidence"
    assert "outside.txt" in toolkit.list_files(str(tmp_path))
    assert "external evidence" in toolkit.search_in_files("external", directory=str(tmp_path))


@pytest.mark.asyncio
async def test_cancelled_confirmation_never_writes(toolkit):
    target = toolkit.workspace.root / "external.txt"
    entered = asyncio.Event()
    async def confirm(question):
        entered.set()
        await asyncio.Event().wait()
    toolkit.set_ask_user_handler(confirm)
    task = asyncio.create_task(toolkit.write_file(str(target), content="new"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not target.exists()


@pytest.mark.asyncio
async def test_session_change_invalidates_confirmed_write(toolkit):
    state = [1]
    toolkit.access_policy.scope = lambda: state[0]
    async def confirm(question):
        state[0] = 2
        return "yes"
    toolkit.set_ask_user_handler(confirm)
    result = await toolkit.write_file("outside.txt", content="new")
    assert "Error" in result
    assert not (toolkit.workspace.root / "outside.txt").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [r"WorkDatabase\rg.cmd", "rg", r"type WorkDatabase\%TARGET%", "python WorkDatabase/script.py", "mkdir 'WorkDatabase'", r"mkdir WorkDatabase\inside,outer", r"mkdir WorkDatabase\inside=outer", "mkdir WorkDatabase/inside\voutside", "mkdir WorkDatabase/inside\foutside", "copy WorkDatabase/seed.txt", "move WorkDatabase/seed.txt"])
async def test_opaque_or_expanding_commands_require_confirmation(toolkit, command):
    prompts = []
    async def deny(question):
        prompts.append(question)
        return "no"
    toolkit.set_ask_user_handler(deny)
    with pytest.raises(PermissionError):
        await toolkit.access_policy.authorize_command(command, str(toolkit.workspace.root))
    assert len(prompts) == 1 and command in prompts[0]


@pytest.mark.asyncio
async def test_binary_overwrite_discloses_previous_file(toolkit):
    target = toolkit.workspace.root / "screen.png"
    target.write_bytes(b"previous bytes")
    proposal = toolkit.access_policy.plan_output(str(target), b"new image")
    assert "覆盖已有文件" in proposal.preview
    assert "14 字节" in proposal.preview and "SHA256" in proposal.preview


@pytest.mark.asyncio
async def test_delete_invalidates_pending_review(toolkit):
    toolkit.review_store.activate(lambda: None)
    await toolkit.write_file("WorkDatabase/edit.txt", content="first")
    assert toolkit.review_store.entries()
    await toolkit.delete_file("WorkDatabase/edit.txt")
    assert not toolkit.review_store.entries()
    assert "Saved" in await toolkit.write_file("WorkDatabase/edit.txt", content="new")


@pytest.mark.asyncio
async def test_junction_outside_workdatabase_and_retarget_during_confirmation(isolated_config):
    import os
    import subprocess
    import tempfile
    if os.name != "nt":
        pytest.skip("Windows junction acceptance")
    # The checkout can live on exFAT; Windows junctions require an NTFS fixture.
    with tempfile.TemporaryDirectory(prefix="redlotus-junction-") as directory:
        root = Path(directory).resolve()
        assert root.is_relative_to(Path(tempfile.gettempdir()).resolve())
        project, outside, replacement = root / "project", root / "outside", root / "replacement"
        for folder in (project, outside, replacement):
            folder.mkdir()
        workspace = WorkspaceContext.from_path(project)
        toolkit = BasicToolkit(SkillsManager(workspace=workspace), workspace=workspace,
                               show_diff=lambda *args, **kwargs: (0, 0, 0))
        target = outside / "item.txt"
        target.write_text("original")
        (replacement / "item.txt").write_text("other")
        link = project / "WorkDatabase" / "link"
        link.parent.mkdir()
        subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(link), str(outside)],
                       check=True, capture_output=True)
        prompts = []
        async def confirm(question):
            prompts.append(question)
            link.rmdir()
            subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(link), str(replacement)],
                           check=True, capture_output=True)
            return "yes"
        toolkit.set_ask_user_handler(confirm)
        try:
            result = await toolkit.write_file("WorkDatabase/link/item.txt", content="changed")
            assert "Error" in result and prompts
            assert str(target) in prompts[0]
            assert target.read_text() == "original"
            assert (replacement / "item.txt").read_text() == "other"
        finally:
            link.rmdir()


@pytest.mark.asyncio
async def test_old_turn_cannot_capture_new_turn_permission(toolkit):
    from types import SimpleNamespace
    from redlotus.core.system import AgentSystem
    from redlotus.sessions.context import turn_context
    system = AgentSystem.__new__(AgentSystem)
    system._session = SimpleNamespace(active=True, turn_id="new", generation=(0, 1))
    toolkit.access_policy.scope = system._file_access_scope
    with turn_context("old"):
        result = await toolkit.write_file("WorkDatabase/stale.txt", content="late")
    assert "Error" in result
    assert not (toolkit.workspace.root / "WorkDatabase/stale.txt").exists()


@pytest.mark.asyncio
async def test_skill_script_uses_same_confirmation_gate(toolkit):
    from types import SimpleNamespace
    directory = toolkit.workspace.root / "WorkDatabase/skill"
    directory.mkdir(parents=True)
    script = directory / "writer.py"
    script.write_text("from pathlib import Path; Path('written.txt').write_text('data')")
    toolkit.skills_manager.skills["writer"] = SimpleNamespace(path=directory)
    prompts = []
    async def deny(question):
        prompts.append(question)
        return "no"
    toolkit.set_ask_user_handler(deny)
    result = await toolkit.skills_manager.execute_skill_script("writer", "writer.py")
    assert "已取消" in result and str(script) in prompts[0]
    assert not (directory / "written.txt").exists()


@pytest.mark.asyncio
async def test_identical_approved_operation_does_not_repeat_confirmation(toolkit):
    prompts = []
    async def confirm(question):
        prompts.append(question)
        return "yes"
    toolkit.set_ask_user_handler(confirm)
    first = toolkit.access_policy.plan_write("external.txt", lambda _: "content")
    await toolkit.access_policy.authorize(first)
    await toolkit.access_policy.authorize(first)
    assert len(prompts) == 1
    assert "Saved" in await toolkit.write_file("external.txt", content="content")
    assert "Unchanged" in await toolkit.write_file("external.txt", content="content")
    assert len(prompts) == 1
