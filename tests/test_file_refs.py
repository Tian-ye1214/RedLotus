import os

import pytest
from PIL import Image
from prompt_toolkit.document import Document
from textual.app import App

from redlotus.cli.completer import AgentCompleter, input_completions
from redlotus.cli.completion import completion_for_input
from redlotus.cli.file_ref import parse_file_paths
from redlotus.cli.tui import AgentInput, AgentInputSuggester
from redlotus.runtime.context import WorkspaceContext, workspace_context


@pytest.fixture
def reference_files(tmp_path):
    document = tmp_path / "ACA_ICLR2027_审稿意见.md"
    document.write_text("# Review\nActual document body", encoding="utf-8")
    picture = tmp_path / "QQ20260914-003427.png"
    Image.new("RGB", (3, 2), "red").save(picture)
    return document, picture


def test_screenshot_input_resolves_both_references(reference_files, tmp_path):
    text = (
        "@ACA_ICLR2027_审稿意见.md解读一下这个文档，"
        "@QQ20260914-003427.png这个图片中我修改了一点东西，告诉我修改了什么"
    )
    assert parse_file_paths(text, root=tmp_path) == list(reference_files)


@pytest.mark.parametrize(
    "separator", ["", "，", ",", ";", "。", ".", "\n", "说明，", "说明."]
)
def test_adjacent_unquoted_references(reference_files, tmp_path, separator):
    document, picture = reference_files
    text = f"@{document.name}{separator}@{picture.name}请比较"
    assert parse_file_paths(text, root=tmp_path) == [document, picture]


def test_completion_and_submission_agree_without_spaces(reference_files, tmp_path):
    document, picture = reference_files
    with workspace_context(WorkspaceContext.from_path(tmp_path)):
        text = "@ACA_ICLR2027_"
        first = next(input_completions(text))
        text = text[: len(text) + first.start_position] + first.text
        text += "解读一下这个文档，@QQ20260914"
        second = next(input_completions(text))
        text = text[: len(text) + second.start_position] + second.text
        text += "这个图片中我修改了一点东西，告诉我修改了什么"
        assert parse_file_paths(text) == [document, picture]


@pytest.mark.parametrize("opener,closer", [('"', '"'), ("'", "'"), ("{", "}")])
def test_quoted_completion_preserves_at_inside_filename(tmp_path, opener, closer):
    target = tmp_path / "picture@draft.png"
    target.write_bytes(b"parser fixture")
    text = f"说明 @{opener}picture@dr"
    with workspace_context(WorkspaceContext.from_path(tmp_path)):
        context = completion_for_input(text)
        assert context is not None
        assert context.prefix == f"{opener}picture@dr"
        completion = next(input_completions(text))
        completed = text[: len(text) + completion.start_position] + completion.text
        assert completed == f"说明 @{opener}picture@draft.png{closer}"
        assert parse_file_paths(completed) == [target]
        assert completion_for_input(completed) is None


@pytest.mark.parametrize(
    "name", ["图片 文件.png", "picture@draft.png", "picture,draft.png", "picture's.png"]
)
def test_completion_quotes_ambiguous_paths_automatically(tmp_path, name):
    target = tmp_path / name
    target.write_bytes(b"parser fixture")
    with workspace_context(WorkspaceContext.from_path(tmp_path)):
        completion = next(input_completions("@"))
        assert completion.text == f'"{name}"'
        assert parse_file_paths("@" + completion.text) == [target]


def test_completion_only_replaces_reference_before_cursor(tmp_path):
    target = tmp_path / "picture.png"
    target.write_bytes(b"parser fixture")
    before, after = "前面的说明，@pic", " 后面的说明保持原样"
    with workspace_context(WorkspaceContext.from_path(tmp_path)):
        document = Document(before + after, cursor_position=len(before))
        completion = next(AgentCompleter().get_completions(document, None))
        completed = (
            before[: len(before) + completion.start_position] + completion.text + after
        )
        assert completed == "前面的说明，@picture.png 后面的说明保持原样"
        assert parse_file_paths(completed) == [target]


def test_longest_existing_filename_wins_without_truncating_unicode(tmp_path):
    shorter, longer = tmp_path / "review.md", tmp_path / "review.md中文"
    for target in (shorter, longer):
        target.write_text("reference", encoding="utf-8")
    assert parse_file_paths("@review.md中文", root=tmp_path) == [longer]
    assert parse_file_paths("@review.md中文请解读", root=tmp_path) == [longer]
    assert parse_file_paths("@review.md.backup", root=tmp_path) == [
        tmp_path / "review.md.backup"
    ]


def test_mixed_paths_keep_first_appearance_order_and_deduplicate(
    reference_files, tmp_path
):
    document, picture = reference_files
    # An unmarked media path retains its existing whitespace-delimited syntax.
    text = f'{picture.name} @{document.name}，@"{picture}"，@{{{document.name}}}'
    assert parse_file_paths(text, root=tmp_path) == [picture, document]


def test_exact_unquoted_path_can_contain_at(tmp_path):
    target = tmp_path / "picture@draft.png"
    target.write_bytes(b"parser fixture")
    assert parse_file_paths("@picture@draft.png", root=tmp_path) == [target]


@pytest.mark.parametrize("shorter_exists", [False, True])
def test_unquoted_at_filename_followed_by_prose_and_another_reference(
    tmp_path, shorter_exists
):
    picture = tmp_path / "picture@draft.png"
    document = tmp_path / "review.md"
    picture.write_bytes(b"parser fixture")
    document.write_text("reference", encoding="utf-8")
    if shorter_exists:
        (tmp_path / "picture").write_text("shorter file", encoding="utf-8")
    assert parse_file_paths(
        "@picture@draft.png请分析，@review.md解读", root=tmp_path
    ) == [picture, document]


def test_email_in_prose_after_reference_is_not_another_file(tmp_path):
    target = tmp_path / "review.md"
    target.write_text("reference", encoding="utf-8")
    assert parse_file_paths("@review.md联系人user@example.com", root=tmp_path) == [
        target
    ]


def test_email_addresses_are_not_references(reference_files, tmp_path):
    document, picture = reference_files
    text = f"mail user@example.com，@{document.name}说明，@{picture.name}"
    assert parse_file_paths(text, root=tmp_path) == [document, picture]
    assert completion_for_input("mail user@example.com") is None


@pytest.mark.skipif(os.name != "nt", reason="Windows path normalization")
def test_windows_absolute_paths_and_case_aliases(reference_files, tmp_path):
    document, picture = reference_files
    text = f"@{document}说明，@{picture}，@{str(picture).upper()}"
    assert parse_file_paths(text, root=tmp_path) == [document, picture]


async def test_tui_tab_completion_then_chinese_prose_resolves_both_files(
    reference_files, tmp_path
):
    document, picture = reference_files

    class CompletionApp(App):
        def compose(self):
            yield AgentInput(
                suggester=AgentInputSuggester(case_sensitive=True, use_cache=False)
            )

    with workspace_context(WorkspaceContext.from_path(tmp_path)):
        app = CompletionApp()
        async with app.run_test() as pilot:
            field = app.query_one(AgentInput)
            for prefix, completed in (
                ("@ACA_ICLR2027_", f"@{document.name}"),
                (
                    f"@{document.name}解读一下这个文档，@QQ20260914",
                    f"@{document.name}解读一下这个文档，@{picture.name}",
                ),
            ):
                field.value = prefix
                field.cursor_position = len(prefix)
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.press("tab")
                assert field.value == completed
            assert parse_file_paths(field.value + "这个图片中我修改了一点东西") == [
                document,
                picture,
            ]
