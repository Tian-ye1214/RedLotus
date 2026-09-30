from test_speech_service import CatalogFixture, NativeFixture
"""Regression gates for background model initialization, not audio acceptance."""
import asyncio
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from redlotus.TTS import SpeechSettings, service
from test_speech_service import REAL_BOOTSTRAP, tiny_archive


@pytest.fixture
def preparation_diagnostics(tmp_path, isolated_config, monkeypatch):
    from redlotus.runtime import logging as logger, resources

    isolated_config["storage"].update(runtime_dir="runtime", project_logs_dir="logs")
    workspace = resources.WorkspaceContext.from_path(tmp_path)
    console, writers = [], []
    written = threading.Event()
    original_sink = logger._session_sink

    def record(message):
        writers.append(threading.get_ident())
        original_sink(message)
        written.set()

    monkeypatch.setattr(logger, "_configured", False)
    monkeypatch.setattr(logger, "_configured_dir", None)
    monkeypatch.setattr(logger, "_task_log_paths", {})
    monkeypatch.setattr(logger, "console_sink", console.append)
    monkeypatch.setattr(logger, "_session_sink", record)
    speech = service.SpeechService(SpeechSettings(model_dir=tmp_path / "model"))
    monkeypatch.setattr(service.SpeechService, "_shared", speech)
    return SimpleNamespace(workspace=workspace, speech=speech, written=written,
                           console=console, writers=writers, log=tmp_path / "logs/speech.log")


@pytest.mark.asyncio
async def test_startup_failure_is_logged_before_the_other_model_finishes(preparation_diagnostics, monkeypatch):
    from redlotus.api import base
    from redlotus.runtime import resources

    probe = preparation_diagnostics
    release = threading.Event()
    monkeypatch.setattr(service.SpeechService, "bootstrap", REAL_BOOTSTRAP)
    monkeypatch.setattr(service.ModelFactory, "available", lambda: True)

    def prepare(kind, archive):
        if kind == "tts":
            try:
                raise OSError("synthetic installation failure")
            except OSError as exc:
                raise service.SpeechUnavailable("model preparation failed") from exc
        assert release.wait(5)

    async def load(engine, verified):
        engine.native = NativeFixture()

    monkeypatch.setattr(probe.speech, "_prepare_one", prepare)
    monkeypatch.setattr(probe.speech, "_ensure_loaded", load)
    try:
        with resources.workspace_context(probe.workspace):
            assert await base.start_speech() is probe.speech
        assert await asyncio.to_thread(probe.written.wait, 1)
        assert not probe.speech._bootstrap_task.done()
        assert probe.speech.status()["tts"].error == "model preparation failed"
        log = probe.log.read_text(encoding="utf-8")
        assert "synthetic installation failure" in log and "Traceback" in log
        assert log.count("ERROR") == 1
        assert not probe.console
        assert probe.writers and all(identity != threading.get_ident() for identity in probe.writers)
    finally:
        release.set()
        await probe.speech.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/voice prepare", "/voice prepare tts"])
async def test_prepare_command_logs_the_original_failure_once(preparation_diagnostics, monkeypatch, command):
    from redlotus.sessions.control import SessionController
    from redlotus.ui import cli_commands

    probe = preparation_diagnostics
    warnings = []

    def prepare(kind, archive):
        if kind == "tts":
            raise OSError("synthetic preparation error")

    async def load(engine, verified):
        engine.native = NativeFixture()

    monkeypatch.setattr(probe.speech, "_prepare_one", prepare)
    monkeypatch.setattr(probe.speech, "_ensure_loaded", load)
    monkeypatch.setattr(cli_commands, "print_warning", warnings.append)
    monkeypatch.setattr(cli_commands, "print_panel", lambda *args, **kwargs: None)
    controller = SimpleNamespace(system=SimpleNamespace(workspace=probe.workspace))
    try:
        await cli_commands.SlashCommands(controller, SessionController(), command).voice()
        log = probe.log.read_text(encoding="utf-8")
        assert "synthetic preparation error" in log and "Traceback" in log
        assert log.count("ERROR") == 1
        assert warnings and not probe.console
    finally:
        await probe.speech.close()


@pytest.mark.asyncio
async def test_status_never_touches_model_storage(tmp_path, monkeypatch):
    speech = service.SpeechService(SpeechSettings(model_dir=tmp_path / "model"))
    def forbidden(*args):
        raise AssertionError("UI status must only read memory")
    monkeypatch.setattr(speech, "_state", forbidden)
    monkeypatch.setattr(speech, "_download_paths", forbidden)
    try:
        assert speech.status()["tts"].stage == "missing"
    finally:
        await speech.close()


@pytest.mark.asyncio
async def test_bootstrap_prewarms_both_models_without_waiting_for_other_kind(tmp_path, monkeypatch):
    speech = service.SpeechService(SpeechSettings(model_dir=tmp_path / "model"))
    monkeypatch.setattr(service.ModelFactory, "available", lambda: True)
    monkeypatch.setattr(service.SpeechService, "bootstrap", REAL_BOOTSTRAP)
    asr_gate, tts_warm = threading.Event(), asyncio.Event()
    warmed = []
    def prepare(kind, archive):
        if kind == "asr":
            assert asr_gate.wait(2)
    async def load(engine, active=None):
        warmed.append(engine.kind)
        if engine.kind == "tts":
            tts_warm.set()
    monkeypatch.setattr(speech, "_prepare_one", prepare)
    monkeypatch.setattr(speech, "_ensure_loaded", load)
    task = speech.bootstrap()
    try:
        assert speech.bootstrap() is task
        await asyncio.wait_for(tts_warm.wait(), 1)
        assert warmed == ["tts"]
        asr_gate.set()
        await task
        assert warmed == ["tts", "asr"]
    finally:
        asr_gate.set()
        await asyncio.gather(task, return_exceptions=True)
        await speech.close()


@pytest.mark.asyncio
async def test_prepare_warm_reuses_verified_files_and_runs_native_load_off_loop(tmp_path, monkeypatch):
    speech = service.SpeechService(SpeechSettings(model_dir=tmp_path / "model"))
    archive, _ = tiny_archive(tmp_path, monkeypatch)
    monkeypatch.setattr(service.ModelFactory, "require_runtime", lambda: None)
    await speech.prepare("asr", archive)
    checks, loads = [], []
    valid = speech._valid
    def verify(kind, record):
        checks.append(kind)
        return valid(kind, record)
    def load(*args):
        loads.append(threading.get_ident())
        return NativeFixture()
    monkeypatch.setattr(speech, "_valid", verify)
    monkeypatch.setattr(service.ModelFactory, "create", load)
    try:
        await speech.prepare("asr", warm=True)
        assert checks == ["asr"]
        assert loads and loads[0] != threading.get_ident()
        assert speech.status()["asr"].stage == "ready"
        async with speech.acquire("asr"):
            pass
        assert len(loads) == 1
    finally:
        await speech.close()


@pytest.mark.asyncio
async def test_model_download_uses_sync_client_off_loop(tmp_path, monkeypatch):
    speech = service.SpeechService(SpeechSettings(model_dir=tmp_path / "model"))
    archive, spec = tiny_archive(tmp_path, monkeypatch)
    data = archive.read_bytes()
    owner = threading.get_ident()
    threads = []
    def receive(request):
        threads.append(threading.get_ident())
        return httpx.Response(200, content=data)
    real_client = httpx.Client
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kwargs: pytest.fail("download must run in sync worker"))
    monkeypatch.setattr(service.httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(receive)))
    monkeypatch.setattr(service.ModelFactory, "require_runtime", lambda: None)
    try:
        await speech.prepare("asr")
        assert threads and all(thread != owner for thread in threads)
        assert speech.status()["asr"].stage == "installed"
    finally:
        await speech.close()


@pytest.mark.asyncio
async def test_cancelled_update_restores_old_native_outside_cancelled_worker(tmp_path, monkeypatch):
    from redlotus.runtime import resources
    speech = service.SpeechService(SpeechSettings(model_dir=tmp_path / "model"))
    monkeypatch.setattr(service.ModelFactory, "require_runtime", lambda: None)
    archive, old = tiny_archive(tmp_path, monkeypatch, root="old")
    monkeypatch.setattr(service.ModelFactory, "create", lambda kind, root, threads: NativeFixture(root.name))
    await speech.prepare("asr", archive, warm=True)
    archive, new = tiny_archive(tmp_path, monkeypatch, root="new")
    new["compatible"] = [old]
    await speech.prepare("asr", archive)
    started, release = threading.Event(), threading.Event()
    def load(root, threads, **kwargs):
        if root.name == service.ModelSpec.from_dict("asr", new).version:
            started.set()
            assert release.wait(5)
            resources.check_thread_cancel()
        return root.name
    monkeypatch.setattr(service.ModelFactory, "create", lambda kind, root, threads: NativeFixture(load(root, threads)))
    task = asyncio.create_task(speech._switch("asr", service.ModelSpec.from_dict("asr", new).version))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(.01)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert speech.engines["asr"].native == service.ModelSpec.from_dict("asr", old).version
        assert speech.status()["asr"].stage == "ready"
        assert speech.status()["asr"].active == service.ModelSpec.from_dict("asr", old).version
    finally:
        release.set()
        await speech.close()


@pytest.mark.asyncio
async def test_cancelled_bootstrap_waits_for_both_native_workers(monkeypatch, tmp_path):
    from redlotus.runtime import resources
    speech = service.SpeechService(SpeechSettings(model_dir=tmp_path / "model"))
    monkeypatch.setattr(service.ModelFactory, "available", lambda: True)
    monkeypatch.setattr(service.SpeechService, "bootstrap", REAL_BOOTSTRAP)
    started = {kind: threading.Event() for kind in ("asr", "tts")}
    drained = {kind: threading.Event() for kind in ("asr", "tts")}
    release_tts = threading.Event()
    def blocked_prepare(kind, archive, *, force=False):
        started[kind].set()
        try:
            while not release_tts.wait(.01):
                resources.check_thread_cancel()
        finally:
            if kind == "tts":
                release_tts.wait(5)
            drained[kind].set()
    monkeypatch.setattr(speech, "_prepare_one", blocked_prepare)
    bootstrap = speech.bootstrap()
    try:
        for event in started.values():
            assert await asyncio.to_thread(event.wait, 2)
        bootstrap.cancel()
        assert await asyncio.to_thread(drained["asr"].wait, 2)
        await asyncio.sleep(.02)
        assert not bootstrap.done(), "ASR drained but TTS still owns native preparation work"
    finally:
        release_tts.set()
        await asyncio.gather(bootstrap, return_exceptions=True)
        await speech.close()
    assert all(event.is_set() for event in drained.values())
