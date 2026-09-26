import pytest


@pytest.fixture(autouse=True)
def no_live_models(monkeypatch):
    from pydantic_ai import models
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", False)


def pytest_sessionstart(session):
    """Check each application area before collecting wheel regressions."""
    import os
    from pathlib import Path

    if expected := os.environ.get("REDLOTUS_TEST_INSTALL_ROOT"):
        import importlib.util

        root = Path(expected).resolve()
        for module in ("runtime.config", "sessions.storage", "core.system", "tools.references",
                       "memory.records", "api.base", "ui.console", "prompts.prompt"):
            origin = Path(importlib.util.find_spec("redlotus." + module).origin).resolve()
            if not origin.is_relative_to(root):
                raise pytest.UsageError(f"Wheel check imported checkout/dependency package: {origin}")


def pytest_sessionfinish(session, exitstatus):
    """Also check every application module actually imported by the tests."""
    import os
    import sys
    from pathlib import Path

    if expected := os.environ.get("REDLOTUS_TEST_INSTALL_ROOT"):
        root = Path(expected).resolve()
        for name, module in tuple(sys.modules.items()):
            if name == "redlotus" or name.startswith("redlotus."):
                if origin := getattr(module, "__file__", None):
                    if not Path(origin).resolve().is_relative_to(root):
                        raise pytest.UsageError(f"Wheel test loaded {name} outside the installation: {origin}")


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Provide only synthetic settings; never discover the checkout's config."""
    from redlotus.runtime import config

    config_dir = tmp_path / "global-config"
    monkeypatch.setenv("REDLOTUS_CONFIG_FILE", str(tmp_path / "config.json"))
    monkeypatch.setenv("REDLOTUS_DOTENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setenv("REDLOTUS_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("REDLOTUS_DATA_DIR", str(tmp_path / "global-data"))
    values = {
        "models": {
            "main": {"name": "synthetic-model", "api_key": "synthetic-key"},
            "coordinator": {"name": "openai:test"},
        },
        "API_KEY": "synthetic-key",
        "BASE_URL": "http://unused.invalid/v1",
        "MODEL_HTTP_TIMEOUT": 1,
        "request_limit": None,
        "input_limits": {
            "max_input_chars": 1000,
            "defaults": {"max_files": 8, "max_file_bytes": 1048576},
        },
        "agent_run_policy": {"max_command_timeout_seconds": 30},
        "storage": {
            "project_dir": "WorkDatabase",
            "sessions_dir": "WorkDatabase/sessions",
            "references_dir": "WorkDatabase/references",
        },
    }
    monkeypatch.setattr(config, "_read_config", lambda path, *, dotenv: values)
    return values


@pytest.fixture
def phone(isolated_config, tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from redlotus.api.WeChat import WeChatAgentBot
    from redlotus.runtime.resources import WorkspaceContext
    from redlotus.tools.references import ReferenceStore

    monkeypatch.chdir(tmp_path)
    bot = WeChatAgentBot()
    state = bot._session("wx_owner")
    replies, calls = [], []

    async def send(text):
        replies.append(text)

    class Model:
        session_key = "synthetic"
        last_turn_error = None
        toolkit = SimpleNamespace(_references=ReferenceStore(WorkspaceContext.from_path(tmp_path)))

        def _start_user_turn(self, message, history, *, turn_id):
            async def run():
                await self.toolkit._references.prepare_message(message)
                calls.append(message)
                return "final answer"
            return asyncio.create_task(run())

        async def stop_current_turn(self):
            state.reset()
            await state.queue.cancel()

        async def shutdown(self):
            pass

    state.agent = Model()
    return bot, state, send, replies, calls
