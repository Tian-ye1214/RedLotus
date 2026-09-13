import asyncio

from redlotus.cli.file_ref import load_file_refs
from redlotus.infra.paths import user_data_dir
from redlotus.references.readers import DocumentReader
from redlotus.runtime.context import WorkspaceContext, workspace_context
from test_system import configured_system


async def test_all_originals_are_captured_before_slow_parsing(tmp_path, monkeypatch):
    files = []
    for number in range(5):
        path = tmp_path / f"reference-{number}.txt"
        path.write_text(f"original {number}", encoding="utf-8")
        files.append(path)
    started, release = asyncio.Event(), asyncio.Event()
    original_read = DocumentReader.read

    async def slow_read(reader, source, directory):
        started.set()
        await release.wait()
        return await original_read(reader, source, directory)

    monkeypatch.setattr(DocumentReader, "read", slow_read)
    with workspace_context(WorkspaceContext.from_path(tmp_path)):
        task = asyncio.create_task(load_file_refs(" ".join(f'@"{p}"' for p in files)))
        try:
            await asyncio.wait_for(started.wait(), 5)
            saved = list((user_data_dir() / "references/blobs").glob("*/source.txt"))
            assert len(saved) == 5
            files[-1].write_text("changed after admission", encoding="utf-8")
        finally:
            release.set()
            references = await task
    assert references[-1].parts[0].text == "original 4"


async def test_queued_input_captures_references_while_previous_turn_is_waiting(
    tmp_path, monkeypatch
):
    system = configured_system(tmp_path, monkeypatch)
    cli, hold, seen = system._cli_controller, asyncio.Event(), []
    state = cli.new_session_state()
    original = tmp_path / "queued.txt"
    original.write_text("value at admission", encoding="utf-8")

    async def start(text, state, *, references, **kwargs):
        seen.extend(await references)

    monkeypatch.setattr(cli, "_start_user_turn_from_raw_input", start)
    monkeypatch.setattr(
        "redlotus.agent_core.cli_controller.app_config.missing_main_api_keys",
        lambda: (),
    )
    system._session.queue.submit(hold.wait)
    try:
        await cli.process_line(f'分析 @"{original}"', state, wait_for_turn=False)
        async with asyncio.timeout(5):
            while not list((user_data_dir() / "references/blobs").glob("*/source.txt")):
                await asyncio.sleep(0.01)
        original.write_text("changed while queued", encoding="utf-8")
    finally:
        hold.set()
        await system._session.queue.join()
        await system.shutdown()
    assert seen[0].parts[0].text == "value at admission"
