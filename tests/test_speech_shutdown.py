"""Native speech shutdown waits for owners and drains active leases."""
import asyncio
import threading

import pytest

from redlotus.TTS import ModelKind, SpeechSettings
from redlotus.TTS.service import SpeechService


@pytest.mark.asyncio
async def test_concurrent_close_shared_waits_for_first_shutdown(tmp_path, monkeypatch):
    service = SpeechService(SpeechSettings(model_dir=tmp_path / "model"))
    monkeypatch.setattr(SpeechService, "_shared", service)
    entered, release = threading.Event(), threading.Event()

    class SlowModel:
        def close(self):
            entered.set()
            assert release.wait(2)

    service.engines[ModelKind.ASR].native = SlowModel()
    first = asyncio.create_task(SpeechService.close_shared())
    second = None
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        second = asyncio.create_task(SpeechService.close_shared())
        await asyncio.sleep(.02)
        assert not second.done(), "a second close must await the native shutdown already in progress"
    finally:
        release.set()
        await asyncio.gather(first, *(tuple([second]) if second else ()), return_exceptions=True)


@pytest.mark.asyncio
async def test_close_cancels_live_inference_lease_before_releasing_model(tmp_path):
    service = SpeechService(SpeechSettings(model_dir=tmp_path / "model"))
    closed = []

    class Model:
        def close(self):
            assert holder.cancelled(), "native model must outlive the cancelled inference lease"
            closed.append(True)

    service.engines[ModelKind.ASR].native = Model()
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold_lease():
        async with service.acquire(ModelKind.ASR):
            entered.set()
            await release.wait()

    holder = asyncio.create_task(hold_lease())
    await entered.wait()
    closing = asyncio.create_task(service.close())
    try:
        # Bound lease cancellation independently of the two heap-wide GC passes.
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(holder), .3)
        await asyncio.wait_for(asyncio.shield(closing), 3)
        assert holder.cancelled()
        assert closed == [True]
    finally:
        release.set()
        await asyncio.gather(holder, closing, return_exceptions=True)
