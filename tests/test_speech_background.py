from test_speech_service import CatalogFixture, NativeFixture
"""Regression gates for background model initialization, not audio acceptance."""
import asyncio
import threading
from contextlib import asynccontextmanager

import httpx
import pytest

from redlotus.TTS import SpeechSettings, service
from test_speech_service import REAL_BOOTSTRAP, tiny_archive


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
