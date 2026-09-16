import fitz

from redlotus.core.agents import WorkspaceContext
from redlotus.tools.toolkit import BasicToolkit
from redlotus.tools.registry import SkillsManager


async def test_pdf_text_and_images_stay_in_active_project(tmp_path):
    project = WorkspaceContext.from_path(tmp_path)
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "PDF regression evidence")
    pixels = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 8, 8), False)
    pixels.clear_with(100)
    page.insert_image(fitz.Rect(72, 100, 112, 140), pixmap=pixels)
    document.save(str(tmp_path / "sample.pdf"))
    document.close()
    toolkit = BasicToolkit(SkillsManager(), workspace=project)
    output = await toolkit.extract_text("sample.pdf")
    assert any(
        "PDF regression evidence" in p for p in output.content if isinstance(p, str)
    )
    assert any(getattr(p, "media_type", "") == "image/png" for p in output.content)
    await toolkit.close()


async def test_project_file_tools_and_skills_capabilities_survive(tmp_path):
    from redlotus.core.gateway import create_worker_toolsets_and_capabilities

    toolkit = BasicToolkit(
        SkillsManager(), workspace=WorkspaceContext.from_path(tmp_path)
    )
    groups = toolkit.worker_tool_groups(include_browser=True)
    assert {
        "skills",
        "browser",
        "memory",
        "media",
        "execution",
        "file_mutation",
        "core",
    } >= set(groups)
    toolsets, capabilities = create_worker_toolsets_and_capabilities(groups)
    assert toolsets and any(c.id == "worker_skills" for c in capabilities)
    toolkit.write_file("answer.txt", "project file")
    assert (tmp_path / "answer.txt").read_text(encoding="utf-8") == "project file"
    assert not (tmp_path / "WorkDatabase" / "answer.txt").exists()
    await toolkit.close()


def test_quoted_reference_completion_preserves_spaces_and_unicode(tmp_path):
    from redlotus.core.console import completion_for_input
    from redlotus.core.console import _iter_file_completions
    from redlotus.tools.interaction import parse_file_paths
    from redlotus.core.session import set_workspace

    set_workspace(tmp_path)
    target = tmp_path / "图片 文件.png"
    target.write_bytes(b"fixture")
    context = completion_for_input('请看 @"图片 文')
    assert context is not None and context.prefix == '"图片 文'
    completion = next(_iter_file_completions(context.prefix, at_mode=True))
    completed = "请看 @" + completion.text
    assert parse_file_paths(completed) == [target.resolve()]
    assert completion_for_input("mail user@example.com") is None


def test_panel_total_does_not_count_reasoning_twice():
    from redlotus.core.history import UsageTotals
    from redlotus.core.presentation import _session_total_tokens

    totals = UsageTotals(input_tokens=100, output_tokens=30, reasoning_tokens=20)
    assert _session_total_tokens(totals) == 130


async def test_usage_survives_compaction_without_double_counting_display_updates(
    tmp_path,
):
    from dataclasses import replace
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        TextPart,
        UserPromptPart,
    )
    from pydantic_ai.usage import RequestUsage
    from redlotus.core.session import SessionFile
    from redlotus.core.history import summarize_usage_files

    log = SessionFile.create(tmp_path, WorkspaceContext.from_path(tmp_path).project_id)
    first = ModelResponse(
        parts=[TextPart("first")],
        provider_response_id="one",
        usage=RequestUsage(input_tokens=100, output_tokens=10),
    )
    second = ModelResponse(
        parts=[TextPart("second")],
        provider_response_id="two",
        usage=RequestUsage(input_tokens=200, output_tokens=20),
    )
    log.save_context([first, second], turn_id="one")
    log.save_context(
        [
            ModelRequest(parts=[UserPromptPart("summary")]),
            replace(second, parts=[TextPart("cleaned display")]),
        ], turn_id="one"
    )
    report = summarize_usage_files(
        [log.path], price_resolver=lambda model: None
    )
    assert report.totals.responses == 2
    assert report.totals.input_tokens == 300 and report.totals.output_tokens == 30


def test_review_uses_latest_snapshot_and_preserves_external_changes(tmp_path):
    import threading
    import pytest
    from redlotus.tools.interaction import PendingReviewStore

    path = tmp_path / "code.py"
    path.write_text("original\n", encoding="utf-8")
    store = PendingReviewStore(threading.Lock())
    store.activate(lambda: None)
    store.write(path, path.name, lambda old: "first\n")
    first = store.get(str(path))
    store.write(path, path.name, lambda old: "second\n")
    assert not store.decide(first, 0, True)
    current = store.get(str(path))
    assert store.decide(current, 0, True)
    assert path.read_text(encoding="utf-8") == "original\n"
    path.write_text("manual edit\n", encoding="utf-8")
    with pytest.raises(ValueError):
        store.decide(current, 0, False)
    assert path.read_text(encoding="utf-8") == "manual edit\n"
    new_file = tmp_path / "new.py"
    store.write(new_file, new_file.name, lambda old: "created\n")
    assert store.decide(store.get(str(new_file)), 0, True)
    assert not new_file.exists()


def test_legacy_console_measures_visible_ansi_text():
    from io import StringIO
    from rich.console import Console
    from redlotus.core.presentation import LegacyOutputSink

    stream = StringIO()
    sink = LegacyOutputSink(Console(file=stream, width=80, force_terminal=False))
    sink.emit("\x1b[38;2;255;80;60mR\x1b[0m" * 60)
    assert stream.getvalue() == "R" * 60 + "\n"
