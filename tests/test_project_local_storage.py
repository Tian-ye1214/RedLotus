"""Project files follow the selected folder, including child-agent contexts."""

from contextvars import copy_context
from copy import deepcopy
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from redlotus.core import config
from redlotus.core.agents import WorkspaceContext, workspace_context
from redlotus.prompts import prompt
from redlotus.tools.references import ReferenceStore


class _PromptSkills:
    def __init__(self, root):
        self.skills_dir = root

    def get_skills_summary(self):
        return ""


@pytest.fixture
def storage_policy(tmp_path, monkeypatch):
    values = config.settings()
    values["storage"].update(
        state_dir=str(tmp_path / "global"),
        project_dir=".redlotus",
        sessions_dir=".redlotus/sessions",
        project_logs_dir=".redlotus/logs",
        references_dir="WorkDatabase/references",
        runtime_dir="WorkDatabase/runtime",
    )
    monkeypatch.delenv("REDLOTUS_DATA_DIR", raising=False)
    monkeypatch.setattr(config, "settings", lambda: deepcopy(values))
    return values


def test_project_sessions_belong_to_selected_folder(tmp_path, storage_policy):
    project = WorkspaceContext.from_path(tmp_path / "项目 A")
    assert config.project_data_dir(project) == project.root / ".redlotus"
    assert config.session_data_dir(project) == project.root / ".redlotus/sessions"
    assert not project.root.exists(), "Resolving paths must not initialize files"


def test_runtime_references_and_logs_follow_active_project(tmp_path, storage_policy):
    first = WorkspaceContext.from_path(tmp_path / "项目 A")
    second = WorkspaceContext.from_path(tmp_path / "项目 B")
    for project in (first, second, first):
        with workspace_context(project):
            assert config.references_dir() == project.root / "WorkDatabase/references"
            assert config.runtime_dir() == project.root / "WorkDatabase/runtime"
            assert config.logs_dir() == project.root / ".redlotus/logs"
    assert ReferenceStore(second).root == second.root / "WorkDatabase/references"
    assert config.runtime_dir(second) == second.root / "WorkDatabase/runtime"
    assert config.user_skills_dir(second) == second.root / "WorkDatabase/runtime/skills"
    assert config.memory_dir() == tmp_path / "global/LongTermMemory"


def test_project_log_switch_routes_unbound_session_log_to_new_project(
    tmp_path, storage_policy, monkeypatch
):
    first = WorkspaceContext.from_path(tmp_path / "项目 A")
    second = WorkspaceContext.from_path(tmp_path / "项目 B")
    first_log_dir = config.prepare_log_dir(first)
    second_log_dir = config.prepare_log_dir(second)

    monkeypatch.setattr(config, "_configured_dir", first_log_dir)
    monkeypatch.setattr(config, "prune_old_logs", lambda: None)

    config.activate_log_dir(second_log_dir)

    class SessionMessage:
        record = {"extra": {"session": "new-project-session"}}

        def __str__(self):
            return "new project log entry\n"

    config._session_sink(SessionMessage())
    assert config.get_log_dir() == second_log_dir
    assert (second_log_dir / "new-project-session.log").read_text(encoding="utf-8") == (
        "new project log entry\n"
    )
    assert not (first_log_dir / "new-project-session.log").exists()


def test_background_session_log_stays_with_its_project_after_another_activation(
    tmp_path, storage_policy
):
    """A copied background context must not follow a later global log activation."""
    first = WorkspaceContext.from_path(tmp_path / "项目 A")
    second = WorkspaceContext.from_path(tmp_path / "项目 B")
    first_log_dir = config.prepare_log_dir(first)
    second_log_dir = config.prepare_log_dir(second)
    previous_dir = config._configured_dir
    release = threading.Event()
    worker = None

    try:
        config.activate_log_dir(first_log_dir)
        with config.session_log_context("first-background"):
            background_context = copy_context()

        def emit_from_background():
            release.wait(5)
            background_context.run(
                lambda: config.info_file_only("background from project A")
            )

        worker = threading.Thread(target=emit_from_background)
        worker.start()
        config.activate_log_dir(second_log_dir)
        release.set()
        worker.join(5)

        assert not worker.is_alive()
        assert "background from project A" in (
            first_log_dir / "first-background.log"
        ).read_text(encoding="utf-8")
        assert not (second_log_dir / "first-background.log").exists()
    finally:
        release.set()
        if worker is not None:
            worker.join(5)
        if previous_dir is None:
            config._configured_dir = None
        else:
            config.activate_log_dir(previous_dir)


def test_active_workspace_routes_session_log_after_another_project_activates(
    tmp_path, storage_policy
):
    first = WorkspaceContext.from_path(tmp_path / "项目 A")
    second = WorkspaceContext.from_path(tmp_path / "项目 B")
    first_log_dir = config.prepare_log_dir(first)
    second_log_dir = config.prepare_log_dir(second)
    previous_dir = config._configured_dir

    try:
        config.activate_log_dir(first_log_dir)
        config.activate_log_dir(second_log_dir)
        with workspace_context(first), config.session_log_context("first-active"):
            config.info_file_only("session after project B activation")

        assert "session after project B activation" in (
            first_log_dir / "first-active.log"
        ).read_text(encoding="utf-8")
        assert not (second_log_dir / "first-active.log").exists()
    finally:
        if previous_dir is None:
            config._configured_dir = None
        else:
            config.activate_log_dir(previous_dir)


def test_background_task_log_stays_with_its_project_after_another_activation(
    tmp_path, storage_policy
):
    """A task sink must follow the copied owner context, not the latest project."""
    first = WorkspaceContext.from_path(tmp_path / "项目 A")
    second = WorkspaceContext.from_path(tmp_path / "项目 B")
    first_log_dir = config.prepare_log_dir(first)
    second_log_dir = config.prepare_log_dir(second)
    previous_dir = config._configured_dir
    release = threading.Event()
    worker = None

    try:
        config.activate_log_dir(first_log_dir)
        with workspace_context(first):
            config.setup_task_logger("first-task")
            background_context = copy_context()
        first_task_log = next(first_log_dir.glob("first-task_*.log"))

        def emit_from_background():
            release.wait(5)
            background_context.run(
                lambda: config.info_file_only("background task from project A")
            )

        worker = threading.Thread(target=emit_from_background)
        worker.start()
        config.activate_log_dir(second_log_dir)
        with workspace_context(second):
            config.setup_task_logger("second-task")
        second_task_log = next(second_log_dir.glob("second-task_*.log"))
        release.set()
        worker.join(5)

        assert not worker.is_alive()
        assert "background task from project A" in first_task_log.read_text(
            encoding="utf-8"
        )
        assert "background task from project A" not in second_task_log.read_text(
            encoding="utf-8"
        )
    finally:
        release.set()
        if worker is not None:
            worker.join(5)
        if previous_dir is None:
            config._configured_dir = None
        else:
            config.activate_log_dir(previous_dir)


def test_active_workspace_replaces_stale_task_context(tmp_path, storage_policy):
    first = WorkspaceContext.from_path(tmp_path / "项目 A")
    second = WorkspaceContext.from_path(tmp_path / "项目 B")
    first_log_dir = config.prepare_log_dir(first)
    second_log_dir = config.prepare_log_dir(second)
    previous_dir = config._configured_dir

    try:
        config.activate_log_dir(first_log_dir)
        with workspace_context(first):
            config.setup_task_logger("first-task")
            first_context = copy_context()
        first_task_log = next(first_log_dir.glob("first-task_*.log"))

        config.activate_log_dir(second_log_dir)
        with workspace_context(second):
            config.setup_task_logger("second-task")
        second_task_log = next(second_log_dir.glob("second-task_*.log"))

        def emit_from_stale_context():
            with workspace_context(second):
                config.info_file_only("active project B task log")

        first_context.run(emit_from_stale_context)

        assert "active project B task log" not in first_task_log.read_text(
            encoding="utf-8"
        )
        assert "active project B task log" in second_task_log.read_text(
            encoding="utf-8"
        )
    finally:
        if previous_dir is None:
            config._configured_dir = None
        else:
            config.activate_log_dir(previous_dir)


def test_project_paths_do_not_escape_to_global_cache(tmp_path, storage_policy):
    project = WorkspaceContext.from_path(tmp_path / "project")
    storage_policy["storage"]["runtime_dir"] = str(tmp_path / "outside")
    with workspace_context(project):
        with pytest.raises(config.ConfigError, match="runtime_dir"):
            config.runtime_dir()


def test_new_prompt_snapshots_project_agent_file(tmp_path, storage_policy):
    root = tmp_path / "project"
    agent_file = root / ".redlotus/AGENT.md"
    agent_file.parent.mkdir(parents=True)
    agent_file.write_text("仅在本项目使用的约束 A", encoding="utf-8")
    project = WorkspaceContext.from_path(root)
    skills = _PromptSkills(tmp_path)

    with workspace_context(project):
        first = prompt.get_coordinator_system_prompt(skills)

    agent_file.write_text("仅在本项目使用的约束 B", encoding="utf-8")
    restored = prompt.session_prompt_from_history(
        [SimpleNamespace(instructions=first)]
    )
    with workspace_context(project):
        second = prompt.get_coordinator_system_prompt(skills)

    assert "仅在本项目使用的约束 A" in first
    assert restored == first
    assert "仅在本项目使用的约束 B" in second
    assert "仅在本项目使用的约束 A" not in second


def test_missing_project_agent_file_does_not_initialize_project_directory(
    tmp_path, storage_policy
):
    project = WorkspaceContext.from_path(tmp_path / "empty-project")

    with workspace_context(project):
        prompt.get_coordinator_system_prompt(_PromptSkills(tmp_path))

    assert not project.root.exists()


def test_skills_overlay_uses_explicit_target_before_switch(tmp_path, storage_policy):
    from redlotus.tools.registry import SkillsManager

    previous = WorkspaceContext.from_path(tmp_path / "previous")
    target = WorkspaceContext.from_path(tmp_path / "target")
    with workspace_context(previous):
        manager = SkillsManager(workspace=target)
    assert manager.skills_dir == target.root / "WorkDatabase/runtime/skills"
    assert not previous.root.exists()


def test_command_environment_resolves_against_explicit_project(tmp_path, storage_policy):
    from redlotus.tools.execution import get_execution_environment

    previous = WorkspaceContext.from_path(tmp_path / "previous")
    target = WorkspaceContext.from_path(tmp_path / "target")
    with workspace_context(previous):
        environment = get_execution_environment(workspace=target, python_required=False)
    assert environment.root == target.root / "WorkDatabase/runtime/venv"
    assert environment.cache == target.root / "WorkDatabase/runtime/cache"
    assert Path(environment.variables["TEMP"]) == target.root / "WorkDatabase/runtime/tmp"


async def test_initial_agent_activates_the_selected_project_log_directory(tmp_path, storage_policy):
    from redlotus.core.system import AgentSystem

    previous = WorkspaceContext.from_path(tmp_path / "previous")
    target = WorkspaceContext.from_path(tmp_path / "target")
    config.activate_log_dir(config.prepare_log_dir(previous))
    system = AgentSystem(workspace=target)
    try:
        assert config.get_log_dir() == target.root / ".redlotus/logs"
        assert config.get_log_dir().is_dir()
    finally:
        await system.shutdown()
