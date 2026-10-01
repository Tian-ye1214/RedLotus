"""Model packages are selected without changing speech callers or dependencies."""
from pathlib import Path
import threading

import pytest

from redlotus.TTS import ModelKind, SpeechSettings, SpeechUnavailable
from redlotus.TTS import service


def test_package_selection_uses_existing_config_without_creating_files(tmp_path):
    package = tmp_path / "voice-package"
    settings = SpeechSettings.read({"speech": {"tts_package": str(package)}})
    assert settings.tts_package == package.resolve()
    assert not package.exists()


def test_default_model_selection_remains_available(tmp_path):
    settings = SpeechSettings(model_dir=tmp_path / "models")
    assert settings.tts_package is None
    assert settings.model_dir == tmp_path / "models"


@pytest.fixture
def selected_package(tmp_path, monkeypatch):
    package_root = tmp_path / "voice-package"
    package_root.mkdir()
    events = []
    owner_thread = threading.get_ident()

    class Bundle:
        root = package_root
        version = "package-0123456789abcdef"
        resources = {"fixture": object()}

        @classmethod
        def read(cls, path):
            assert threading.get_ident() != owner_thread
            assert path == package_root
            events.append("read")
            return cls()

        def verify(self):
            assert threading.get_ident() != owner_thread
            events.append("verify")
            return self

    class Native:
        @classmethod
        def retained_root(cls):
            return None

        def warmup(self):
            assert threading.get_ident() != owner_thread
            events.append("warm")

        def close(self):
            events.append("close")

    def create(kind, root, threads):
        assert kind == ModelKind.TTS
        assert root == package_root
        assert threading.get_ident() != owner_thread
        events.append("load")
        return Native()

    def unexpected_catalog(*args):
        pytest.fail("Explicit model packages must not trigger default downloads")

    monkeypatch.setattr(service, "TTSBundle", Bundle, raising=False)
    monkeypatch.setattr(service.ModelFactory, "create", create)
    monkeypatch.setattr(service.ModelFactory, "require_runtime", lambda: None)
    monkeypatch.setattr(service.ModelFactory, "implementation", lambda kind: Native)
    monkeypatch.setattr(service.ModelCatalog, "specs", unexpected_catalog)
    speech = service.SpeechService(SpeechSettings(model_dir=tmp_path / "models", tts_package=package_root))
    return speech, events, package_root


async def test_selected_package_loads_once_without_default_model_storage(selected_package):
    speech, events, package = selected_package
    try:
        await speech.prepare(ModelKind.TTS, warm=True)
        async with speech.acquire(ModelKind.TTS):
            pass
        async with speech.acquire(ModelKind.TTS):
            pass
        assert events == ["read", "verify", "load", "warm"]
        assert speech.status()[ModelKind.TTS].stage == "ready"
        assert speech.status()[ModelKind.TTS].target == str(package)
        assert not (speech.root / "installed.json").exists()
        assert not (speech.root / "tts").exists()
    finally:
        await speech.close()
    assert events[-1] == "close"
    assert list(package.iterdir()) == []
    assert not list((speech.root / ".locks").glob("*.lock"))


@pytest.mark.parametrize("operation", ["update", "rollback"])
async def test_external_package_switch_requires_new_start_not_default_fallback(selected_package, operation):
    speech, events, _ = selected_package
    try:
        with pytest.raises(SpeechUnavailable, match="模型包.*重启"):
            await getattr(speech, operation)(ModelKind.TTS)
        assert not events
        assert not speech.root.exists()
    finally:
        await speech.close()


async def test_broken_explicit_package_reports_failure_without_default_fallback(selected_package, monkeypatch):
    speech, events, package = selected_package

    def broken(_bundle):
        raise SpeechUnavailable("模型资源校验失败")

    monkeypatch.setattr(service.TTSBundle, "verify", broken)
    try:
        with pytest.raises(SpeechUnavailable, match="校验失败"):
            await speech.prepare(ModelKind.TTS, warm=True)
        assert events == ["read"]
        assert speech.status()[ModelKind.TTS].stage == "failed"
        assert speech.status()[ModelKind.TTS].target == str(package)
        assert not speech.root.exists()
    finally:
        await speech.close()


async def test_explicit_package_cannot_use_unverified_catalog_fallback(selected_package, monkeypatch):
    speech, events, _ = selected_package
    monkeypatch.setattr(service.TTSBundle, "resources", None)
    try:
        with pytest.raises(SpeechUnavailable, match="清单"):
            await speech.prepare(ModelKind.TTS, warm=True)
        assert events == ["read"]
        assert not speech.root.exists()
    finally:
        await speech.close()


async def test_catalog_named_directory_without_manifest_is_not_an_external_package(tmp_path, monkeypatch):
    spec = service.ModelCatalog().specs(ModelKind.TTS)[0]
    directory = tmp_path / spec.version
    directory.mkdir()
    assert service.TTSBundle.read(directory).resources is None
    monkeypatch.setattr(service.ModelFactory, "require_runtime", lambda: None)
    speech = service.SpeechService(SpeechSettings(model_dir=tmp_path / "cache", tts_package=directory))
    try:
        with pytest.raises(SpeechUnavailable, match="清单"):
            await speech.prepare(ModelKind.TTS, warm=True)
        assert speech.status()[ModelKind.TTS].stage == "failed"
        assert not speech.root.exists()
    finally:
        await speech.close()
