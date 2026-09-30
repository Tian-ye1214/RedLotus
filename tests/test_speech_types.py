"""Typed speech boundaries and construction contracts."""
import inspect
import hashlib
import json
from pathlib import Path

import pytest


def test_pcm_annotations_resolve_to_concrete_numpy_type():
    from typing import get_type_hints
    from redlotus.TTS import PCMChunk
    assert "numpy.ndarray" in str(get_type_hints(PCMChunk)["samples"])


def test_native_resource_lease_is_retained_once_until_process_exit(tmp_path):
    from redlotus.TTS import ModelLease
    from filelock import FileLock, Timeout

    root = tmp_path / "model"
    first = ModelLease(root, tmp_path / "first.lock")
    second = ModelLease(root, tmp_path / "second.lock")
    try:
        first.close(retain=True)
        second.close(retain=True)
        assert not second.path.exists()
        with pytest.raises(Timeout):
            FileLock(str(first.path)).acquire(timeout=0)
        assert list(ModelLease._retained.values()).count(first) == 1
    finally:
        ModelLease.release_retained()
    assert not first.path.exists()

from redlotus import TTS


def test_encoded_audio_owns_bytes_instead_of_a_file():
    assert "data" in TTS.AudioSegment.__dataclass_fields__
    clip = TTS.AudioSegment(b"encoded", "silk", 1.0, 24000)
    assert clip.data == b"encoded" and not hasattr(clip, "path")
    with pytest.raises((TypeError, ValueError)):
        TTS.AudioSegment(Path("voice.silk"), "silk", 1.0, 24000)


def test_models_require_distinct_asr_and_tts_contracts():
    assert hasattr(TTS, "ASRModel") and hasattr(TTS, "TTSModel")
    assert inspect.isabstract(TTS.ASRModel)
    assert inspect.isabstract(TTS.TTSModel)
    with pytest.raises(TypeError):
        TTS.ASRModel()


def test_installed_state_preserves_schema_without_shared_defaults():
    assert hasattr(TTS, "InstalledState")
    first, second = TTS.InstalledState(), TTS.InstalledState()
    first.active[TTS.ModelKind.ASR] = "example"
    assert not second.active
    restored = TTS.InstalledState.from_dict(first.to_dict())
    assert restored.active[TTS.ModelKind.ASR] == "example"
    with pytest.raises(ValueError):
        TTS.InstalledState.from_dict({"schema": 999})


def test_text_backlog_limit_is_finite_and_at_most_4096(tmp_path):
    assert "text_chars" in TTS.SpeechSettings.__dataclass_fields__
    assert TTS.SpeechSettings(model_dir=tmp_path).text_chars == 4096
    for invalid in (0, 4097, True):
        with pytest.raises(ValueError):
            TTS.SpeechSettings(model_dir=tmp_path, text_chars=invalid)


def _external_bundle(tmp_path):
    names = ("gpt_encoder.onnx", "gpt_step.onnx", "sovits.onnx", "bert.onnx", "config.json",
             "mambo.gsppkg", "frontend/bert_tokenizer.json")
    resources = {}
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        content = name.encode()
        path.write_bytes(content)
        resources[name] = {"size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    manifest = {
        "schema": 1, "kind": "tts", "runtime": "mambo-onnx", "native_family": "gpt_sovits_v2",
        "base_model_id": "mambo-fixture", "sample_rate": 32000, "cache_layout": "growing",
        "native_model_files": {
            "gpt_encoder": "gpt_encoder.onnx", "gpt_step": "gpt_step.onnx", "sovits": "sovits.onnx",
            "bert": "bert.onnx", "config": "config.json", "voices": "mambo.gsppkg", "data_dir": "frontend",
        },
        "generation": {"mode": "features"}, "voice": {"id": "mambo"}, "resources": resources,
    }
    (tmp_path / "model.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def test_external_tts_bundle_verifies_every_declared_resource(tmp_path):
    _external_bundle(tmp_path)
    bundle = TTS.TTSBundle.read(tmp_path)
    assert bundle.verify() is bundle
    assert bundle.version == hashlib.sha256((tmp_path / "model.json").read_bytes()).hexdigest()
    assert bundle.profile.mode == "features"
    (tmp_path / "gpt_encoder.onnx").write_bytes(b"tampered")
    with pytest.raises(TTS.SpeechUnavailable, match="校验"):
        bundle.verify()


def test_external_tts_bundle_rejects_unlisted_or_escaping_native_resource(tmp_path):
    manifest = _external_bundle(tmp_path)
    manifest["native_model_files"]["gpt_encoder"] = "../outside.onnx"
    (tmp_path / "model.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(TTS.SpeechUnavailable):
        TTS.TTSBundle.read(tmp_path)
    manifest["native_model_files"]["gpt_encoder"] = "missing.onnx"
    (tmp_path / "model.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(TTS.SpeechUnavailable):
        TTS.TTSBundle.read(tmp_path)


def test_external_tts_bundle_rejects_link_inside_native_data_dir(tmp_path, monkeypatch):
    _external_bundle(tmp_path)
    bundle = TTS.TTSBundle.read(tmp_path)
    another = tmp_path / "another"
    another.mkdir()
    link = tmp_path / "frontend" / "linked"
    try:
        link.symlink_to(another, target_is_directory=True)
    except OSError:
        link.mkdir()
        original = Path.is_symlink
        monkeypatch.setattr(Path, "is_symlink", lambda path: path == link or original(path))
    with pytest.raises(TTS.SpeechUnavailable, match="链接"):
        bundle.verify()
