from test_speech_service import CatalogFixture, NativeFixture
"""Owned speech intermediates are reclaimed without losing resumable downloads."""

import asyncio
import hashlib
import io
import json
import tarfile
import threading

import httpx
import pytest

from redlotus.TTS import SpeechSettings, SpeechUnavailable
from redlotus.TTS import audio, service
from redlotus.runtime import resources


def tiny_archive(tmp_path, monkeypatch, *, root="tiny-asr"):
    source = tmp_path / f"{root}.tar.bz2"
    with tarfile.open(source, "w:bz2") as tar:
        for name, data in (("model.bin", b"weights"), ("tokens.txt", b"tokens")):
            member = tarfile.TarInfo(f"{root}/{name}")
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
    spec = {"archive": source.name, "url": "https://example.test/model.tar.bz2",
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "root": root,
            "required": ["model.bin", "tokens.txt"], "archive_limit": 100000,
            "unpack_limit": 100000}
    CatalogFixture.install(monkeypatch, spec)
    return source, spec


@pytest.fixture
def speech(tmp_path, monkeypatch):
    monkeypatch.setattr(service.ModelFactory, "require_runtime", lambda: None)
    return service.SpeechService(SpeechSettings(model_dir=tmp_path / "model"))


def owned_download(speech, spec, payload):
    part, metadata = speech._download_paths(service.ModelSpec.from_dict("asr", spec))
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(payload)
    metadata.write_text(json.dumps({"sha256": spec["sha256"], "url": spec["url"]}))
    return part, metadata


@pytest.mark.asyncio
async def test_unknown_length_oversize_response_leaves_no_poisoned_partial(tmp_path, monkeypatch, speech):
    spec = {"archive": "large.tar.bz2", "url": "https://example.test/large.tar.bz2",
            "sha256": "a" * 64, "archive_limit": 4, "root": "large",
            "required": ["model.bin"], "unpack_limit": 100000}

    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            yield b"abcd"
            yield b"efgh"

    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=Chunks()),
    ))
    monkeypatch.setattr(service.httpx, "Client", lambda **kwargs: client)
    with pytest.raises(SpeechUnavailable, match="大小上限"):
        await speech._background(speech._download, "asr", service.ModelSpec.from_dict("asr", spec))
    part, metadata = speech._download_paths(service.ModelSpec.from_dict("asr", spec))
    assert not part.exists() and not metadata.exists()
    assert speech._partial(service.ModelSpec.from_dict("asr", spec)) == ({}, 0)
    await speech.close()


@pytest.mark.asyncio
async def test_complete_online_archive_is_removed_when_install_fails(tmp_path, monkeypatch, speech):
    source, spec = tiny_archive(tmp_path, monkeypatch)
    part, metadata = owned_download(speech, spec, source.read_bytes())
    monkeypatch.setattr(speech, "_download", lambda kind, selected: part)

    def fail_install(*args):
        raise RuntimeError("unpack failed")

    monkeypatch.setattr(speech, "_install", fail_install)
    with pytest.raises(RuntimeError, match="unpack failed"):
        await speech.prepare("asr")
    assert not part.exists() and not metadata.exists()
    assert source.exists()
    await speech.close()


@pytest.mark.asyncio
async def test_online_install_hashes_archive_once(tmp_path, monkeypatch, speech):
    source, spec = tiny_archive(tmp_path, monkeypatch)
    part, metadata = owned_download(speech, spec, source.read_bytes())
    monkeypatch.setattr(speech, "_download", lambda kind, selected: part)
    hashed = []
    original_sha256 = resources.file_sha256
    original_digest = resources.hashlib.file_digest

    def counted_sha256(path):
        if path == part:
            hashed.append("install")
        return original_sha256(path)

    def counted_digest(stream, algorithm):
        if stream.name == str(part):
            hashed.append("cleanup")
        return original_digest(stream, algorithm)

    monkeypatch.setattr(resources, "file_sha256", counted_sha256)
    monkeypatch.setattr(resources.hashlib, "file_digest", counted_digest)
    await speech.prepare("asr")
    assert hashed == ["install"]
    assert not part.exists() and not metadata.exists()
    await speech.close()


@pytest.mark.asyncio
async def test_cancellation_after_full_download_removes_completed_archive(tmp_path, monkeypatch, speech):
    source, spec = tiny_archive(tmp_path, monkeypatch)
    part, metadata = owned_download(speech, spec, source.read_bytes())
    monkeypatch.setattr(speech, "_download", lambda kind, selected: part)
    entered = threading.Event()
    release = threading.Event()

    def interrupted_install(*args):
        entered.set()
        release.wait(2)
        resources.check_thread_cancel()

    monkeypatch.setattr(speech, "_install", interrupted_install)
    task = asyncio.create_task(speech.prepare("asr"))
    assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), 2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not part.exists() and not metadata.exists()
    await speech.close()


@pytest.mark.asyncio
async def test_completed_archive_left_after_install_is_removed_on_next_prepare(tmp_path, monkeypatch, speech):
    source, spec = tiny_archive(tmp_path, monkeypatch)
    await speech.prepare("asr", source)
    part, metadata = owned_download(speech, spec, source.read_bytes())
    await speech.prepare("asr")
    assert not part.exists() and not metadata.exists()
    assert source.exists()
    assert (speech.root / "asr" / service.ModelSpec.from_dict("asr", spec).version).exists()
    await speech.close()


@pytest.mark.asyncio
async def test_complete_archive_for_newer_uninstalled_version_is_preserved(tmp_path, monkeypatch, speech):
    old_source, old_spec = tiny_archive(tmp_path, monkeypatch, root="old")
    await speech.prepare("asr", old_source)
    new_source, new_spec = tiny_archive(tmp_path, monkeypatch, root="new")
    new_spec["compatible"] = [old_spec]
    part, metadata = owned_download(speech, new_spec, new_source.read_bytes())
    await speech.prepare("asr")
    assert part.exists() and metadata.exists()
    assert speech._state().active["asr"] == service.ModelSpec.from_dict("asr", old_spec).version
    await speech.close()


@pytest.mark.asyncio
async def test_failed_download_keeps_valid_incomplete_progress(tmp_path, monkeypatch, speech):
    source, spec = tiny_archive(tmp_path, monkeypatch)
    prefix = source.read_bytes()[:10]
    part, metadata = owned_download(speech, spec, prefix)

    def fail_download(*args):
        raise RuntimeError("network interrupted")

    monkeypatch.setattr(speech, "_download", fail_download)
    with pytest.raises(RuntimeError, match="network interrupted"):
        await speech.prepare("asr")
    assert part.read_bytes() == prefix and metadata.exists()
    await speech.close()


@pytest.mark.asyncio
async def test_clean_preserves_owned_incomplete_download_for_resume(tmp_path, monkeypatch, speech):
    source, spec = tiny_archive(tmp_path, monkeypatch)
    prefix = source.read_bytes()[:10]
    part, metadata = owned_download(speech, spec, prefix)

    await speech.clean()

    assert part.read_bytes() == prefix and metadata.exists()
    await speech.close()


@pytest.mark.asyncio
async def test_clean_removes_only_complete_owned_download(tmp_path, monkeypatch, speech):
    source, spec = tiny_archive(tmp_path, monkeypatch)
    part, metadata = owned_download(speech, spec, source.read_bytes())

    await speech.clean()

    assert not part.exists() and not metadata.exists()
    assert source.exists()
    await speech.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_silk_decode_failure_or_cancel_never_writes_files(monkeypatch, cancel):
    import builtins
    import sys
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace
    from redlotus.TTS import SpeechError

    entered, release = threading.Event(), threading.Event()
    original_open = Path.open
    original_file_open = builtins.open

    def guard_open(path, mode="r", *args, **kwargs):
        if any(flag in mode for flag in "wax+"):
            pytest.fail("SILK conversion attempted a disk write")
        return original_open(path, mode, *args, **kwargs)

    def no_tempfile(*args, **kwargs):
        pytest.fail("SILK conversion attempted a temporary file")

    def file_open(file, mode="r", *args, **kwargs):
        if any(flag in mode for flag in "wax+"):
            pytest.fail("SILK conversion attempted a disk write")
        return original_file_open(file, mode, *args, **kwargs)

    def decode(source, output, sample_rate):
        entered.set()
        release.wait(2)
        if cancel:
            output.write(b"\0\0")
        else:
            raise ValueError("broken codec")

    monkeypatch.setattr(Path, "open", guard_open)
    monkeypatch.setattr(tempfile, "mkstemp", no_tempfile)
    monkeypatch.setattr(builtins, "open", file_open)
    monkeypatch.setitem(sys.modules, "pysilk", SimpleNamespace(decode=decode))

    async def consume():
        return [chunk async for chunk in audio.AudioIO.parse_input(b"#!SILK_V3bad")]

    task = asyncio.create_task(consume())
    assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), 2)
    if cancel:
        task.cancel()
        await asyncio.sleep(.02)
        assert not task.done()
    release.set()
    if cancel:
        with pytest.raises(asyncio.CancelledError): await task
    else:
        with pytest.raises(SpeechError, match="SILK decode failed") as failed: await task
        assert isinstance(failed.value.__cause__, ValueError)
