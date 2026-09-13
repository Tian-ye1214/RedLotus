"""Start the real application with a read-only wire observer for evaluation."""

import argparse
import io
import sys
import threading
from pathlib import Path


def record_legacy_output(path):
    """Observe render events; Windows console writes can bypass sys.stdout."""
    from rich.console import Console

    from redlotus.cli.output import LegacyOutputSink

    original, lock = LegacyOutputSink.emit, threading.Lock()

    def emit(sink, renderable):
        buffer = io.StringIO()
        Console(file=buffer, width=120).print(renderable)
        with lock, path.open("a", encoding="utf-8") as record:
            record.write(buffer.getvalue())
        original(sink, renderable)

    LegacyOutputSink.emit = emit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wire", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    args = parser.parse_args()
    if Path.cwd().resolve() != args.project.resolve():
        parser.error("Run the real entry from the explicitly selected test project.")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from real_acceptance import WireAudit

    args.wire.parent.mkdir(parents=True, exist_ok=True)
    WireAudit(args.wire).install()
    record_legacy_output(args.wire.with_name("rendered.txt"))
    from redlotus.agent_core.entrypoint import main as run

    run()


if __name__ == "__main__":
    main()
