from test_speech_service import CatalogFixture, NativeFixture
"""Speech archive download, resume, and storage preflight contracts."""
import hashlib
import io
import json
import tarfile
from types import SimpleNamespace

import httpx
import pytest

from redlotus.TTS import SpeechSettings, SpeechUnavailable
from redlotus.TTS import service


def tiny_archive(tmp_path, monkeypatch):
    root = "tiny-asr"
    source = tmp_path / f"{root}.tar.bz2"
    with tarfile.open(source, "w:bz2") as tar:
        for name, data in (("model.bin", root.encode()), ("tokens.txt", b"tokens")):
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
@pytest.mark.parametrize("matching", [True, False])
async def test_space_preflight_credits_only_matching_partial_archive(tmp_path, monkeypatch, speech, matching):
    archive, spec = tiny_archive(tmp_path, monkeypatch)
    part, metadata = speech._download_paths(service.ModelSpec.from_dict("asr", spec))
    part.parent.mkdir(parents=True)
    part.write_bytes(archive.read_bytes()[:archive.stat().st_size // 2])
    metadata.write_text(json.dumps({"sha256": spec["sha256"] if matching else "other", "url": spec["url"]}))
    free = spec["unpack_limit"] + spec["archive_limit"] - part.stat().st_size + 1
    monkeypatch.setattr(service.shutil, "disk_usage", lambda path: SimpleNamespace(free=free))
    def local_download(kind, selected):
        return archive
    monkeypatch.setattr(speech, "_download", local_download)
    if matching:
        assert (await speech.prepare("asr"))["asr"].active == service.ModelSpec.from_dict("asr", spec).version
    else:
        with pytest.raises(SpeechUnavailable, match="冲突"):
            await speech.prepare("asr")
        assert part.exists() and metadata.exists()
    await speech.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 206, 416])
async def test_resume_http_semantics(tmp_path, monkeypatch, speech, status):
    archive, spec = tiny_archive(tmp_path, monkeypatch)
    payload = archive.read_bytes()
    part, meta = speech._download_paths(service.ModelSpec.from_dict("asr", spec))
    part.parent.mkdir(parents=True)
    prefix = payload if status == 416 else payload[:len(payload) // 2]
    part.write_bytes(prefix)
    meta.write_text(json.dumps({"sha256": spec["sha256"], "url": spec["url"], "total": len(payload), "etag": '"fixed"'}))
    requests = []
    def reply(request):
        requests.append(request)
        assert request.headers["range"] == f"bytes={len(prefix)}-"
        if status == 416:
            return httpx.Response(416)
        if status == 200:
            return httpx.Response(200, content=payload, headers={"content-length": str(len(payload)), "etag": '"fixed"'})
        return httpx.Response(206, content=payload[len(prefix):], headers={
            "content-range": f"bytes {len(prefix)}-{len(payload)-1}/{len(payload)}", "etag": '"fixed"'})
    client = httpx.Client(transport=httpx.MockTransport(reply))
    monkeypatch.setattr(service.httpx, "Client", lambda **kwargs: client)
    try:
        result = await speech._background(speech._download, "asr", service.ModelSpec.from_dict("asr", spec))
        assert result.read_bytes() == payload
        assert len(requests) == 1
    finally:
        client.close()
    await speech.close()
