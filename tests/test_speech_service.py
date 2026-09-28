"""Model lifecycle and native-work cancellation with tiny local assets only."""
import asyncio
import hashlib
import io
import json
import tarfile
import threading
from types import SimpleNamespace

import pytest
from filelock import FileLock

from redlotus.TTS import SpeechBusy, SpeechSettings, SpeechUnavailable
from redlotus.TTS import service
from redlotus.runtime import resources

REAL_BOOTSTRAP = service.SpeechService.bootstrap


class CatalogFixture:
    @classmethod
    def install(cls, monkeypatch, spec):
        original = service.ModelCatalog.specs
        monkeypatch.setattr(service.ModelCatalog, "specs", lambda self, kind:
            tuple(service.ModelSpec.from_dict("asr", row) for row in (spec, *spec.get("compatible", [])))
            if kind == "asr" else original(self, kind))


class NativeFixture:
    def __init__(self, value=None, warm=None):
        self.value, self.warm = value, warm

    def warmup(self):
        if self.warm is not None:
            self.warm()

    def close(self):
        self.value = None

    @property
    def name(self):
        return self.value.name

    def __eq__(self, other):
        return self.value == other


def tiny_archive(tmp_path, monkeypatch, *, root="tiny-asr", extra=None):
    entries = {"model.bin": root.encode(), "tokens.txt": b"tokens", **(extra or {})}
    source = tmp_path / f"{root}.tar.bz2"
    with tarfile.open(source, "w:bz2") as tar:
        for name, data in entries.items():
            info = tarfile.TarInfo(f"{root}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    spec = {"archive": source.name, "url": "https://example.test/tiny-asr.tar.bz2",
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "root": root,
            "required": ["model.bin", "tokens.txt"], "archive_limit": 100000,
            "unpack_limit": 100000}
    CatalogFixture.install(monkeypatch, spec)
    return source, spec


@pytest.fixture
def speech(tmp_path, monkeypatch):
    monkeypatch.setattr(service.ModelFactory, "require_runtime", lambda: None)
    return service.SpeechService(SpeechSettings(model_dir=tmp_path / "model", queue_size=1))


@pytest.mark.asyncio
async def test_import_archive_then_reuse_offline(tmp_path, monkeypatch, speech):
    archive, spec = tiny_archive(tmp_path, monkeypatch)
    assert not speech.root.exists()
    first = await speech.prepare("asr", archive)
    assert (first["asr"].stage, first["asr"].active) == ("installed", service.ModelSpec.from_dict("asr", spec).version)
    assert not (speech.root / ".downloads").exists()
    archive.unlink()
    await speech.prepare("asr")
    await speech.close()


@pytest.mark.asyncio
async def test_prepare_reuses_compatible_active_without_download(tmp_path, monkeypatch, speech):
    old_archive, old = tiny_archive(tmp_path, monkeypatch, root="compatible-old")
    await speech.prepare("asr", old_archive)
    _, current = tiny_archive(tmp_path, monkeypatch, root="recommended-new")
    current["compatible"] = [old]
    def no_download(*args):
        raise AssertionError("prepare must reuse the installed compatible model")
    monkeypatch.setattr(speech, "_download", no_download)
    result = await speech.prepare("asr")
    assert result["asr"].active == service.ModelSpec.from_dict("asr", old).version
    await speech.close()


@pytest.mark.asyncio
async def test_foreign_active_uses_own_compatible_install(tmp_path, monkeypatch, speech):
    old_archive, old = tiny_archive(tmp_path, monkeypatch, root="compatible-old")
    await speech.prepare("asr", old_archive)
    _, current = tiny_archive(tmp_path, monkeypatch, root="recommended-new")
    current["compatible"] = [old]
    speech._write_state(lambda state: state.active.__setitem__("asr", "future-app-version"))
    def no_download(*args):
        raise AssertionError("foreign active must not force a download")
    monkeypatch.setattr(speech, "_download", no_download)
    monkeypatch.setattr(service.ModelFactory, "create", lambda kind, root, threads: NativeFixture(root.name))
    await speech.prepare("asr")
    async with speech.acquire("asr") as native:
        assert native == service.ModelSpec.from_dict("asr", old).version
    assert speech._state().active["asr"] == "future-app-version"
    await speech.close()


@pytest.mark.asyncio
async def test_first_install_preserves_foreign_active_but_loads_own_model(tmp_path, monkeypatch, speech):
    archive, spec = tiny_archive(tmp_path, monkeypatch)
    speech.root.mkdir()
    speech._write_state(lambda state: state.active.__setitem__("asr", "future-app-version"))
    result = await speech.prepare("asr", archive)
    assert result["asr"].active == "future-app-version"
    monkeypatch.setattr(service.ModelFactory, "create", lambda kind, root, threads: NativeFixture(root.name))
    async with speech.acquire("asr") as native:
        assert native == service.ModelSpec.from_dict("asr", spec).version
    assert speech.status()["asr"].loaded == service.ModelSpec.from_dict("asr", spec).version
    assert speech.status()["asr"].active == "future-app-version"
    await speech.close()


@pytest.mark.asyncio
async def test_update_loads_current_installed_candidate(tmp_path, monkeypatch, speech):
    archive, spec = tiny_archive(tmp_path, monkeypatch)
    await speech.prepare("asr", archive)
    loaded = []
    monkeypatch.setattr(service.ModelFactory, "create", lambda kind, root, threads: NativeFixture(loaded.append(root.name) or object()))
    await speech.update("asr")
    assert loaded == [service.ModelSpec.from_dict("asr", spec).version]
    await speech.close()


@pytest.mark.asyncio
async def test_update_installs_recommended_after_reusing_compatible(tmp_path, monkeypatch, speech):
    old_archive, old = tiny_archive(tmp_path, monkeypatch, root="compatible-old")
    await speech.prepare("asr", old_archive)
    new_archive, current = tiny_archive(tmp_path, monkeypatch, root="recommended-new")
    current["compatible"] = [old]
    downloaded = []
    def local_download(kind, spec):
        downloaded.append(kind)
        return new_archive
    monkeypatch.setattr(speech, "_download", local_download)
    monkeypatch.setattr(service.ModelFactory, "create", lambda kind, root, threads: NativeFixture(root.name))
    result = await speech.update("asr")
    assert downloaded == ["asr"]
    assert result["asr"].active == service.ModelSpec.from_dict("asr", current).version
    assert result["asr"].previous == service.ModelSpec.from_dict("asr", old).version
    assert speech.engines["asr"].native == service.ModelSpec.from_dict("asr", current).version
    await speech.close()


@pytest.mark.asyncio
async def test_loaded_native_reuse_does_not_rehash_models(tmp_path, monkeypatch, speech):
    archive, _ = tiny_archive(tmp_path, monkeypatch)
    await speech.prepare("asr", archive)
    monkeypatch.setattr(service.ModelFactory, "create", lambda kind, root, threads: NativeFixture(object()))
    async with speech.acquire("asr"):
        assert await speech.run("asr", lambda: 1) == 1
    def forbidden_hash(path):
        raise AssertionError("unexpected model hash")
    monkeypatch.setattr(resources, "file_sha256", forbidden_hash)
    async with speech.acquire("asr"):
        assert await speech.run("asr", lambda: 2) == 2
    await speech.close()


@pytest.mark.asyncio
async def test_asr_acquire_wakes_before_tts_bootstrap_finishes(tmp_path, monkeypatch, speech):
    archive, spec = tiny_archive(tmp_path, monkeypatch)
    monkeypatch.setattr(service.ModelFactory, "available", lambda: True)
    monkeypatch.setattr(service.SpeechService, "bootstrap", REAL_BOOTSTRAP)
    monkeypatch.setattr(service.ModelFactory, "create", lambda kind, root, threads: NativeFixture(root.name))
    asr_started, asr_release = threading.Event(), threading.Event()
    tts_started, tts_release = threading.Event(), threading.Event()
    original_prepare = speech._prepare_one
    def prepare_one(kind, source, *, force=False):
        if kind == "asr":
            asr_started.set()
            assert asr_release.wait(5)
            return original_prepare(kind, archive, force=force)
        tts_started.set()
        assert tts_release.wait(5)
    monkeypatch.setattr(speech, "_prepare_one", prepare_one)
    bootstrap = speech.bootstrap()
    async def infer():
        async with speech.acquire("asr") as native:
            return native
    acquisition = asyncio.create_task(infer())
    try:
        assert await asyncio.to_thread(asr_started.wait, 5)
        assert await asyncio.to_thread(tts_started.wait, 5)
        asr_release.set()
        assert await asyncio.wait_for(acquisition, 2) == service.ModelSpec.from_dict("asr", spec).version
    finally:
        tts_release.set()
        await bootstrap
        await speech.close()


@pytest.mark.asyncio
async def test_cancelled_bootstrap_wakes_acquire_and_close(tmp_path, monkeypatch, speech):
    monkeypatch.setattr(service.ModelFactory, "available", lambda: True)
    monkeypatch.setattr(service.SpeechService, "bootstrap", REAL_BOOTSTRAP)
    started = {kind: threading.Event() for kind in ("asr", "tts")}
    drained = {kind: threading.Event() for kind in ("asr", "tts")}
    release = threading.Event()
    def blocked_prepare(kind, archive, *, force=False):
        started[kind].set()
        try:
            while not release.wait(0.01):
                resources.check_thread_cancel()
        finally:
            drained[kind].set()
    monkeypatch.setattr(speech, "_prepare_one", blocked_prepare)
    bootstrap = speech.bootstrap()
    async def infer():
        async with speech.acquire("asr"):
            pass
    acquisition = asyncio.create_task(infer())
    try:
        assert await asyncio.to_thread(started["asr"].wait, 2)
        assert await asyncio.to_thread(started["tts"].wait, 2)
        bootstrap.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(bootstrap, 2)
        assert all(event.is_set() for event in drained.values())
        with pytest.raises(SpeechUnavailable):
            await asyncio.wait_for(acquisition, 2)
        await asyncio.wait_for(speech.close(), 2)
    finally:
        release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["prepare", "update"])
async def test_close_cancels_and_drains_manual_model_work(tmp_path, monkeypatch, speech, operation):
    if operation == "update":
        archive, old = tiny_archive(tmp_path, monkeypatch, root="old")
        await speech.prepare("asr", archive)
        _, current = tiny_archive(tmp_path, monkeypatch, root="new")
        current["compatible"] = [old]
    else:
        tiny_archive(tmp_path, monkeypatch)
    started, drained, release = threading.Event(), threading.Event(), threading.Event()
    def blocked_download(kind, spec):
        started.set()
        try:
            while not release.wait(0.01):
                resources.check_thread_cancel()
        finally:
            drained.set()
    monkeypatch.setattr(speech, "_download", blocked_download)
    task = asyncio.create_task(speech.update("asr") if operation == "update" else speech.prepare("asr"))
    assert await asyncio.to_thread(started.wait, 2)
    try:
        await asyncio.wait_for(speech.close(), 2)
        assert task.cancelled() and drained.is_set()
    finally:
        release.set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_clean_in_other_process_cannot_remove_update_candidate(tmp_path, monkeypatch, speech):
    old_archive, old = tiny_archive(tmp_path, monkeypatch, root="old")
    await speech.prepare("asr", old_archive)
    new_archive, current = tiny_archive(tmp_path, monkeypatch, root="new")
    current["compatible"] = [old]
    def local_download(kind, spec):
        return new_archive
    monkeypatch.setattr(speech, "_download", local_download)
    monkeypatch.setattr(service.ModelFactory, "create", lambda kind, root, threads: NativeFixture(object()))
    entered, release = asyncio.Event(), asyncio.Event()
    original_switch = speech._switch
    async def delayed_switch(kind, candidate):
        entered.set()
        await release.wait()
        return await original_switch(kind, candidate)
    monkeypatch.setattr(speech, "_switch", delayed_switch)
    task = asyncio.create_task(speech.update("asr"))
    await asyncio.wait_for(entered.wait(), 2)
    other = service.SpeechService(speech.config)
    candidate = speech.root / "asr" / service.ModelSpec.from_dict("asr", current).version
    try:
        await other.clean()
        assert candidate.is_dir()
    finally:
        release.set()
        try:
            await task
        except Exception:
            pass
        await other.close()
        await speech.close()


@pytest.mark.asyncio
async def test_clean_recovers_only_owned_idle_staging_directories(tmp_path, monkeypatch, speech):
    _, spec = tiny_archive(tmp_path, monkeypatch)
    staging = speech.root / ".staging"
    def abandoned(name, *, foreign=False, marked=True):
        stage = staging / f"prepare-{name}"
        package = stage / spec["root"]
        package.mkdir(parents=True)
        (package / "model.bin").write_bytes(b"partial")
        if marked:
            marker = {"schema": 1, "kind": "asr", "version": service.ModelSpec.from_dict("asr", spec).version,
                      "archive_sha256": spec["sha256"],
                      "members": [spec["root"], f"{spec['root']}/model.bin", f"{spec['root']}/tokens.txt"]}
            (stage / ".redlotus-stage.json").write_text(json.dumps(marker))
        if foreign:
            (package / "notes.txt").write_text("keep")
        return stage
    owned = abandoned("owned")
    unknown = abandoned("unknown", marked=False)
    mixed = abandoned("mixed", foreign=True)
    await speech.clean()
    assert not owned.exists() and unknown.exists() and mixed.exists()
    busy = abandoned("busy")
    lock_path = speech.root / ".locks" / f"download-asr-{service.ModelSpec.from_dict("asr", spec).version}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(lock_path))
    lock.acquire()
    try:
        await speech.clean()
        assert busy.exists()
    finally:
        lock.release()
    await speech.clean()
    assert not busy.exists()
    await speech.close()


@pytest.mark.asyncio
async def test_archive_rejects_traversal_without_install(tmp_path, monkeypatch, speech):
    archive, spec = tiny_archive(tmp_path, monkeypatch)
    with tarfile.open(archive, "w:bz2") as tar:
        for name, data in (("tiny-asr/model.bin", b"model"),
                           ("tiny-asr/tokens.txt", b"tokens"),
                           ("tiny-asr/../escape.txt", b"escape")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    spec["sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
    with pytest.raises(SpeechUnavailable, match="越界"):
        await speech.prepare("asr", archive)
    assert not (speech.root / "escape.txt").exists()
    assert not (speech.root / "installed.json").exists()
    await speech.close()


@pytest.mark.asyncio
async def test_cancelled_model_load_waits_for_native_worker_and_releases_use_lock(tmp_path, monkeypatch, speech):
    archive, _ = tiny_archive(tmp_path, monkeypatch)
    await speech.prepare("asr", archive)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    def load_model(root, threads, **kwargs):
        started.set()
        try:
            assert release.wait(5)
            return object()
        finally:
            finished.set()
    monkeypatch.setattr(service.ModelFactory, "create", lambda kind, root, threads: NativeFixture(load_model(root, threads)))
    async def acquire_once():
        async with speech.acquire("asr"):
            pass
    task = asyncio.create_task(acquire_once())
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not finished.is_set()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
    assert speech.engines["asr"].native is None
    assert speech.engines["asr"].lease is None
    await speech.close()


@pytest.mark.asyncio
async def test_cancelled_native_work_keeps_lease_until_worker_stops(tmp_path, monkeypatch, speech):
    archive, _ = tiny_archive(tmp_path, monkeypatch)
    await speech.prepare("asr", archive)
    monkeypatch.setattr(service.ModelFactory, "create", lambda kind, root, threads: NativeFixture(object()))
    started, release = threading.Event(), threading.Event()
    def native_work():
        started.set()
        release.wait(5)
    async def infer():
        async with speech.acquire("asr"):
            await speech.run("asr", native_work)
    task = asyncio.create_task(infer())
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    assert speech.engines["asr"].lock.locked()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not speech.engines["asr"].lock.locked()
    await speech.close()


@pytest.mark.asyncio
async def test_waiting_admission_is_bounded(tmp_path, monkeypatch, speech):
    archive, _ = tiny_archive(tmp_path, monkeypatch)
    await speech.prepare("asr", archive)
    monkeypatch.setattr(service.ModelFactory, "create", lambda kind, root, threads: NativeFixture(object()))
    hold = asyncio.Event()
    async def owner():
        async with speech.acquire("asr"):
            await hold.wait()
    first = asyncio.create_task(owner())
    await asyncio.sleep(0.05)
    second = asyncio.create_task(owner())
    await asyncio.sleep(0.05)
    with pytest.raises(SpeechBusy):
        async with speech.acquire("asr"):
            pass
    hold.set()
    await asyncio.gather(first, second)
    await speech.close()


@pytest.mark.asyncio
async def test_child_task_cannot_use_inherited_native_lease(tmp_path, monkeypatch, speech):
    archive, _ = tiny_archive(tmp_path, monkeypatch)
    await speech.prepare("asr", archive)
    monkeypatch.setattr(service.ModelFactory, "create", lambda kind, root, threads: NativeFixture(object()))
    release = asyncio.Event()
    async def child():
        await release.wait()
        return await speech.run("asr", lambda: "illegal")
    async with speech.acquire("asr"):
        task = asyncio.create_task(child())
    release.set()
    with pytest.raises(SpeechUnavailable):
        await task
    await speech.close()


@pytest.mark.asyncio
async def test_unknown_installed_schema_is_not_rewritten(tmp_path, monkeypatch, speech):
    archive, _ = tiny_archive(tmp_path, monkeypatch)
    speech.root.mkdir()
    state = speech.root / "installed.json"
    state.write_text('{"schema": 99}')
    with pytest.raises(SpeechUnavailable, match="安装状态"):
        await speech.prepare("asr", archive)
    assert state.read_text() == '{"schema": 99}'
    await speech.close()


@pytest.mark.asyncio
async def test_damaged_managed_version_is_reinstalled(tmp_path, monkeypatch, speech):
    archive, spec = tiny_archive(tmp_path, monkeypatch)
    await speech.prepare("asr", archive)
    model = speech.root / "asr" / service.ModelSpec.from_dict("asr", spec).version / "model.bin"
    model.write_bytes(b"damaged")
    await speech.prepare("asr", archive)
    assert model.read_bytes() == spec["root"].encode()
    assert speech.status()["asr"].active == service.ModelSpec.from_dict("asr", spec).version
    await speech.close()


@pytest.mark.asyncio
async def test_failed_candidate_restores_previous_native(tmp_path, monkeypatch, speech):
    old_archive, old = tiny_archive(tmp_path, monkeypatch, root="old")
    await speech.prepare("asr", old_archive)
    old_version = service.ModelSpec.from_dict("asr", old).version
    new_archive, new = tiny_archive(tmp_path, monkeypatch, root="new")
    new["compatible"] = [old]
    await speech.prepare("asr", new_archive)
    events = []
    fail = True
    block = False
    started, release = threading.Event(), threading.Event()
    class Model:
        def __init__(self, name):
            self.name = name
        def __del__(self):
            events.append(("drop", self.name))
    def load(root, threads, **kwargs):
        events.append(("load", root.name))
        if root.name == service.ModelSpec.from_dict("asr", new).version and fail:
            raise RuntimeError("candidate failed")
        if root.name == service.ModelSpec.from_dict("asr", new).version and block:
            started.set()
            release.wait(5)
        return Model(root.name)
    monkeypatch.setattr(service.ModelFactory, "create", lambda kind, root, threads: NativeFixture(load(root, threads)))
    async with speech.acquire("asr"):
        assert speech.engines["asr"].native.name == old_version
    with pytest.raises(RuntimeError, match="candidate failed"):
        await speech._switch("asr", service.ModelSpec.from_dict("asr", new).version)
    assert speech.engines["asr"].native.name == old_version
    assert speech.status()["asr"].active == old_version
    fail = False
    block = True
    switching = asyncio.create_task(speech._switch("asr", service.ModelSpec.from_dict("asr", new).version))
    assert await asyncio.to_thread(started.wait, 5)
    switching.cancel()
    await asyncio.sleep(0)
    assert speech.engines["asr"].lock.locked()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await switching
    assert speech.engines["asr"].native.name == old_version
    assert speech.status()["asr"].active == old_version
    block = False
    await speech.update("asr")
    assert speech.status()["asr"].previous == old_version
    assert events.index(("drop", old_version)) < events.index(("load", service.ModelSpec.from_dict("asr", new).version))
    await speech.rollback("asr")
    assert speech.status()["asr"].active == old_version
    await speech.close()


@pytest.mark.asyncio
async def test_clean_preserves_unknown_files_and_prior_version(tmp_path, monkeypatch, speech):
    archive1, spec1 = tiny_archive(tmp_path, monkeypatch, root="first")
    await speech.prepare("asr", archive1)
    monkeypatch.setattr(service.ModelFactory, "create", lambda kind, root, threads: NativeFixture(object()))
    prior = [spec1]
    for name in ("second", "third"):
        archive, spec = tiny_archive(tmp_path, monkeypatch, root=name)
        spec["compatible"] = list(prior)
        await speech.prepare("asr", archive)
        await speech._switch("asr", service.ModelSpec.from_dict("asr", spec).version)
        prior.insert(0, spec)
    first = speech.root / "asr" / service.ModelSpec.from_dict("asr", spec1).version
    previous = speech.root / "asr" / service.ModelSpec.from_dict("asr", prior[1]).version
    foreign = first / "user-note.txt"
    foreign.write_text("keep")
    await speech.clean()
    assert first.exists() and foreign.exists() and previous.exists()
    foreign.unlink()
    await speech.clean()
    assert not first.exists() and previous.exists()
    await speech.close()
