from test_speech_service import CatalogFixture, NativeFixture
"""Speech startup leaves the interactive loop responsive while it initializes."""

import asyncio
import sys
import threading
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_speech_import_probe_and_construction_run_off_loop(monkeypatch):
    from redlotus.api import base

    loop_thread = threading.get_ident()
    work_threads = []
    bootstrap_threads = []

    class Service:
        def bootstrap(self):
            bootstrap_threads.append(threading.get_ident())

    def load(name):
        assert name == "redlotus.TTS.service"
        work_threads.append(threading.get_ident())

        def available():
            work_threads.append(threading.get_ident())
            return True

        def get_service():
            work_threads.append(threading.get_ident())
            return Service()

        return SimpleNamespace(ModelFactory=SimpleNamespace(available=available), SpeechService=SimpleNamespace(shared=get_service))

    monkeypatch.setattr(base, "importlib", SimpleNamespace(import_module=load), raising=False)
    await base.start_speech()
    assert len(work_threads) == 3
    assert all(thread != loop_thread for thread in work_threads)
    assert bootstrap_threads == [loop_thread]


@pytest.mark.asyncio
async def test_cancelled_speech_initialization_drains_worker(monkeypatch):
    from redlotus.api import base

    began, release, finished = threading.Event(), threading.Event(), threading.Event()

    def load(_name):
        began.set()
        release.wait(2)
        finished.set()
        return SimpleNamespace(ModelFactory=SimpleNamespace(available=lambda: False))

    monkeypatch.setattr(base, "importlib", SimpleNamespace(import_module=load), raising=False)
    task = asyncio.create_task(base.start_speech())
    assert await asyncio.to_thread(began.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    result = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)
    assert finished.is_set()


@pytest.mark.asyncio
async def test_cli_starts_ui_while_speech_initialization_is_pending(isolated_config, monkeypatch):
    import redlotus.core.system  # Exclude unrelated first-import cost from the startup scheduling timeout.
    from redlotus.api import base
    from redlotus.TTS import service as speech_service
    from redlotus.ui import console

    initializing, release, entered_ui = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def start_speech():
        initializing.set()
        await release.wait()

    class Controller:
        def __init__(self, system):
            self.pets = SimpleNamespace(close=close)

        async def run_interactive(self, *, stop_event):
            entered_ui.set()
            await asyncio.wait_for(initializing.wait(), 1)
            release.set()

    class System:
        async def shutdown(self):
            pass

    async def close():
        pass

    monkeypatch.setattr(base, "start_speech", start_speech)
    monkeypatch.setattr(base, "install_stop_handlers", lambda _: None)
    monkeypatch.setattr(base, "close_all_clients", close)
    monkeypatch.setattr(speech_service.SpeechService, "close_shared", close)
    monkeypatch.setattr(console, "AgentCliController", Controller)
    await asyncio.wait_for(base.run_cli(System()), 2)
    assert entered_ui.is_set()


@pytest.mark.asyncio
async def test_qq_connection_awaits_shared_speech_startup(monkeypatch):
    from redlotus.api import base
    from redlotus.api.QQ import QQBot

    events = []

    async def speech():
        events.append("speech")

    async def connect():
        events.append("connect")

    async def release():
        events.append("release")

    monkeypatch.setattr(base, "start_speech", speech)
    bot = object.__new__(QQBot)
    monkeypatch.setattr(bot, "release_all_resources_async", release)
    await bot._run_connection(connect)
    assert events == ["speech", "connect", "release"]


@pytest.mark.asyncio
async def test_wechat_connection_awaits_shared_speech_startup(monkeypatch):
    from redlotus.api import base
    from redlotus.api.WeChat import WeChatAgentBot

    events = []

    class FakeBot:
        def __init__(self, **kwargs):
            pass

        async def login(self):
            events.append("login")

        def on_message(self, callback):
            pass

        async def start(self):
            events.append("start")

        def stop(self):
            events.append("stop")

    async def speech():
        events.append("speech")

    async def release():
        events.append("release")

    monkeypatch.setitem(sys.modules, "wechatbot", SimpleNamespace(WeChatBot=FakeBot))
    monkeypatch.setattr(base, "start_speech", speech)
    bot = WeChatAgentBot()
    monkeypatch.setattr(bot, "release_all_resources_async", release)
    await bot._async_main()
    assert events == ["login", "speech", "start", "release", "stop"]
