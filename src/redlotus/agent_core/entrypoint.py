from __future__ import annotations

import asyncio
import os
import signal
import threading

from redlotus.infra.shared_http import close_all_clients


class ExitDeadline:
    """Bound process exit even when a native call ignores task cancellation."""

    def __init__(self, seconds: float):
        self._timer = threading.Timer(seconds, os._exit, args=(0,))
        self._timer.daemon = True

    def start(self):
        self._timer.start()

    def close(self):
        self._timer.cancel()


def install_stop_handlers(stop_event: asyncio.Event) -> None:
    """Map process signals to the interactive runner's stop event."""
    loop = asyncio.get_running_loop()

    def request_stop(*_args: object) -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop)
        except (NotImplementedError, ValueError):
            signal.signal(sig, request_stop)


async def run_cli(system=None):
    """Run the interactive RedLotus CLI/TUI."""
    from redlotus.agent_core.system import AgentSystem
    from redlotus.config.app_config import initialize_config

    if system is None:
        initialize_config()
        system = AgentSystem()
    stop_event = asyncio.Event()
    install_stop_handlers(stop_event)
    try:
        await system.run_interactive(stop_event=stop_event)
    finally:
        await system.shutdown()
        await close_all_clients()
    return system


def main() -> None:
    """CLI entrypoint used by root ``main.py``."""
    from redlotus.agent_core.system import AgentSystem
    from redlotus.config.app_config import initialize_config, settings

    initialize_config()
    deadline = ExitDeadline(settings()["lifecycle"]["shutdown_grace_seconds"])
    system = AgentSystem(exit_deadline=deadline)
    try:
        asyncio.run(run_cli(system))
    finally:
        deadline.close()
