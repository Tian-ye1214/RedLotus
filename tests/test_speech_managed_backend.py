"""Managed TTS selection keeps installed families distinct."""
import hashlib
import io
import tarfile

import pytest

from redlotus.TTS import ModelKind, ModelSpec, SpeechSettings, SpeechUnavailable, TTSBackend
from redlotus.TTS import service


def _archive(tmp_path, family, compression="bz2"):
    root = f"tiny-{family}"
    archive = tmp_path / f"{root}.tar.{compression}"
    names = ("gpt_encoder.onnx", "gpt_step.onnx", "sovits.onnx", "bert.onnx",
             "config.json", "mambo.gsppkg", "frontend/tokenizer.json", "model.json") if family == "mambo" else (
             "model.onnx", "voices.bin", "tokens.txt", "model.json")
    with tarfile.open(archive, f"w:{compression}") as tar:
        for name in names:
            data = name.encode()
            member = tarfile.TarInfo(f"{root}/{name}")
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
    backend = TTSBackend.MAMBO if family == "mambo" else TTSBackend.SHERPA
    spec = ModelSpec.from_dict(ModelKind.TTS, {
        "archive": archive.name, "url": f"https://example.test/{archive.name}",
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "root": root,
        "required": list(names[:-2]) + (["frontend/", "model.json"] if family == "mambo" else list(names[-2:])),
        "archive_limit": 100000, "unpack_limit": 100000,
        "runtime": {"runtime": backend.value},
    })
    return archive, spec


def _managed(tmp_path, monkeypatch, backend, specs):
    speech = service.SpeechService(SpeechSettings(model_dir=tmp_path / "models", tts_backend=backend))
    speech.catalog._models = {ModelKind.TTS: specs, ModelKind.ASR: ()}
    monkeypatch.setattr(service.ModelFactory, "require_runtime", lambda: None)
    monkeypatch.setattr(service.ModelFactory, "available", lambda backend=None: True)
    return speech


def test_tts_backend_setting_is_typed_and_selectable(tmp_path):
    assert SpeechSettings(model_dir=tmp_path).tts_backend is TTSBackend.MAMBO
    chosen = SpeechSettings.read({"speech": {"tts_backend": TTSBackend.SHERPA.value}})
    assert chosen.tts_backend is TTSBackend.SHERPA
    with pytest.raises(ValueError):
        SpeechSettings(model_dir=tmp_path, tts_backend="unknown")


def test_catalog_groups_managed_tts_and_retains_all_specs(tmp_path):
    _, mambo = _archive(tmp_path, "mambo")
    _, kokoro = _archive(tmp_path, "kokoro")
    catalog = service.ModelCatalog(TTSBackend.MAMBO)
    catalog._models = {ModelKind.TTS: (mambo, kokoro), ModelKind.ASR: ()}
    assert catalog.specs(ModelKind.TTS) == (mambo,)
    assert catalog.all_specs(ModelKind.TTS) == (mambo, kokoro)
    assert service.ModelCatalog(TTSBackend.SHERPA).tts_backend is TTSBackend.SHERPA
    assert mambo.backend is TTSBackend.MAMBO and kokoro.backend is TTSBackend.SHERPA


def test_bundled_catalog_selects_complete_mambo_onnx_group():
    catalog = service.ModelCatalog()
    selected = catalog.specs(ModelKind.TTS)
    assert selected and all(spec.backend is TTSBackend.MAMBO for spec in selected)
    assert {"model.json", "gpt_encoder.onnx", "gpt_step.onnx", "sovits.onnx", "bert.onnx",
            "config.json", "mambo.gsppkg", "frontend/"}.issubset(selected[0].required)
    assert TTSBackend.SHERPA in {spec.backend for spec in catalog.all_specs(ModelKind.TTS)}


@pytest.mark.asyncio
async def test_managed_mambo_installs_gzip_archive(tmp_path, monkeypatch):
    archive, mambo = _archive(tmp_path, "mambo", "gz")
    speech = _managed(tmp_path, monkeypatch, TTSBackend.MAMBO, (mambo,))
    try:
        await speech.prepare(ModelKind.TTS, archive)
        assert speech._known_record(ModelKind.TTS, mambo.version) is not None
    finally:
        await speech.close()


@pytest.mark.asyncio
async def test_managed_mambo_rejects_missing_native_before_download(tmp_path, monkeypatch):
    _, mambo = _archive(tmp_path, "mambo")
    speech = _managed(tmp_path, monkeypatch, TTSBackend.MAMBO, (mambo,))
    monkeypatch.setattr(service.ModelFactory, "available", lambda backend=None: False)
    monkeypatch.setattr(speech, "_download", lambda *args: pytest.fail("download attempted without native worker"))
    try:
        with pytest.raises(SpeechUnavailable, match="曼波"):
            await speech.prepare(ModelKind.TTS)
        assert not (speech.root / "tts").exists()
    finally:
        await speech.close()


@pytest.mark.asyncio
async def test_existing_kokoro_migrates_to_selected_mambo_without_cross_family_rollback(tmp_path, monkeypatch):
    mambo_archive, mambo = _archive(tmp_path, "mambo")
    kokoro_archive, kokoro = _archive(tmp_path, "kokoro")
    specs = (mambo, kokoro)
    speech = _managed(tmp_path, monkeypatch, TTSBackend.SHERPA, specs)
    await speech.prepare(ModelKind.TTS, kokoro_archive)
    assert speech._state().active[ModelKind.TTS] == kokoro.version
    await speech.close()

    speech = _managed(tmp_path, monkeypatch, TTSBackend.MAMBO, specs)
    downloads = []
    warmed = []
    class Native:
        @classmethod
        def retained_root(cls):
            return None
        def __init__(self, root):
            self.root = root
        def warmup(self):
            warmed.append(self.root.name)
        def close(self):
            pass
    def download(kind, spec):
        downloads.append(spec.version)
        return mambo_archive
    monkeypatch.setattr(speech, "_download", download)
    monkeypatch.setattr(service.ModelFactory, "create", lambda kind, root, threads: Native(root))
    try:
        await speech.prepare(ModelKind.TTS, warm=True)
        assert downloads == [mambo.version]
        assert warmed == [mambo.version]
        assert speech._state().active[ModelKind.TTS] == mambo.version
        assert speech._known_record(ModelKind.TTS, kokoro.version) is not None
        speech._write_state(lambda state: state.previous.__setitem__(ModelKind.TTS, kokoro.version))
        with pytest.raises(SpeechUnavailable, match="回滚"):
            await speech.rollback(ModelKind.TTS)
        assert speech._state().active[ModelKind.TTS] == mambo.version
    finally:
        await speech.close()

    speech = _managed(tmp_path, monkeypatch, TTSBackend.SHERPA, specs)
    monkeypatch.setattr(speech, "_download", lambda *args: pytest.fail("installed Kokoro must be reused"))
    try:
        await speech.prepare(ModelKind.TTS)
        assert speech._usable(ModelKind.TTS)[0] == kokoro.version
        assert speech._state().active[ModelKind.TTS] == kokoro.version
        assert speech._state().previous[ModelKind.TTS] == kokoro.version
        with pytest.raises(SpeechUnavailable, match="回滚"):
            await speech.rollback(ModelKind.TTS)
        assert speech._state().active[ModelKind.TTS] == kokoro.version
    finally:
        await speech.close()
