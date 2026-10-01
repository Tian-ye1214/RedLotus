"""The shared Agent speech branch must only synthesize readable prose."""
import asyncio
import re
from types import SimpleNamespace

import pytest

from redlotus.core.gateway import coordinator_stream_handler


CASES = [
    ("# Title\n\n**bold** and *italic* and ~~old~~.", "Title bold and italic and old."),
    ("- First\n2. Second\n> Quoted", "First Second Quoted"),
    ("[docs](https://example.com/a_(b)) and ![a cat](cat.png)", "docs and a cat"),
    ("Use `C#` and `foo_bar`, 2 * 3 and -23.50.", "Use C# and foo_bar, 2 * 3 and -23.50."),
    ("Hello.\n```python\nprint(123)\n```\nWorld.", "Hello. World."),
    ("Before.\n~~~js\nconst a = 1;\n~~~\nAfter.", "Before. After."),
    ("Before.\n\n    secret command\n\nAfter.", "Before. After."),
    ("| Name | Value |\n| --- | ---: |\n| A | 3.14 |", "Name, Value, A, 3.14,"),
    ("<b>Hello</b> &amp; goodbye. <!-- hidden --> End.", "Hello & goodbye. End."),
    ("Read https://example.com/a and www.example.com now.", "Read and now."),
    ("Escaped \\*stars\\* and C# and foo_bar.", "Escaped *stars* and C# and foo_bar."),
    ("Text.\n---\n***\n___\nEnd.", "Text. End."),
    ("Text.\n```python\nnever read this", "Text."),
    ("Text **unfinished", "Text unfinished"),
    ("*outer **inner** outer* and **bold *inside* bold**.", "outer inner outer and bold inside bold."),
    ("**first\nsecond** next.", "first second next."),
    ("A <https://example.com> and [label][ref].\n\n[ref]: https://example.com", "A and label."),
    ("### Heading ###\nNext.", "Heading Next."),
    ("# 中文标题\n\n请运行 `uv run main.py`，版本是 **3.14**。", "中文标题 请运行 uv run main.py，版本是 3.14。"),
    ("- [x] Finished\n- [ ] Waiting", "Finished Waiting"),
    ("Read [the **guide**](https://example.com/incomplete", "Read the guide"),
    ("See ![a cat](https://example.com/incomplete", "See a cat"),
    ("Read [unfinished label", "Read unfinished label"),
    ("Before <span class=\"unfinished", "Before"),
    ("Before <https://example.com/incomplete", "Before"),
]


def parse(pieces):
    from redlotus.TTS import SpeechTextParser
    parser = SpeechTextParser()
    return "".join(parser.feed(piece) for piece in pieces) + parser.finish()


@pytest.mark.parametrize("source,expected", CASES)
def test_markdown_is_independent_of_delta_boundaries(source, expected):
    for pieces in ([source], list(source), *( [source[:i], source[i:]] for i in range(len(source) + 1))):
        spoken = re.sub(r"\s+([,.;])", r"\1", " ".join(parse(pieces).split()))
        assert spoken == expected, pieces


def test_long_code_is_discarded_before_it_can_fill_the_text_budget():
    from redlotus.TTS import SpeechTextParser
    parser = SpeechTextParser(limit=80)
    assert not parser.feed("```python\n")
    for _ in range(1000):
        assert not parser.feed("x" * 256)
        assert parser.pending_chars <= 80
    assert parser.feed("\n```\nHello.").strip() == "Hello."


def test_unclosed_inline_construct_is_bounded():
    from redlotus.TTS import SpeechBusy, SpeechTextParser
    parser = SpeechTextParser(limit=30)
    with pytest.raises(SpeechBusy):
        parser.feed("[" + "x" * 40)


def test_single_delta_cannot_create_an_unbounded_plain_text_result():
    from redlotus.TTS import SpeechBusy, SpeechTextParser
    with pytest.raises(SpeechBusy):
        SpeechTextParser(limit=30).feed("hello " * 1000)


def test_parser_clear_removes_previous_response_state():
    from redlotus.TTS import SpeechTextParser
    parser = SpeechTextParser()
    parser.feed("```python\nhidden")
    parser.clear()
    assert parser.feed("New response.") == "New response."


def test_plain_prose_is_available_before_the_response_finishes():
    from redlotus.TTS import SpeechTextParser
    parser = SpeechTextParser()
    assert parser.feed("Hello, the answer is ") == "Hello, the answer is "
    assert parser.feed("**impor") == ""
    assert parser.feed("tant**.") == "important."


def test_incomplete_formatting_does_not_leak_at_newline():
    from redlotus.TTS import SpeechTextParser
    parser = SpeechTextParser()
    assert parser.feed("**first\n") == ""
    assert parser.feed("second**.") == "first second."


async def run_reply(pieces):
    spoken, displayed = [], []

    class Reply:
        async def feed(self, text):
            spoken.append(text)

        async def finish(self):
            pass

        def cancel(self):
            pass

    session = SimpleNamespace(generation=(0, 0), voice_enabled=True,
                              begin_voice=lambda *args, **kwargs: Reply())
    presentation = SimpleNamespace(supports_model_stream=lambda: True,
                                   update_output=lambda *args: displayed.append(args))
    system = SimpleNamespace(session_key="one", _session=session,
                             presentation=presentation, workspace=None)

    async def events():
        for text in pieces:
            if isinstance(text, bool):
                session.voice_enabled = text
                continue
            yield SimpleNamespace(event_kind="part_delta", delta=SimpleNamespace(
                part_delta_kind="text", content_delta=text))

    await coordinator_stream_handler(system)(None, events())
    return "".join(spoken), displayed


@pytest.mark.asyncio
async def test_shared_speech_removes_markdown_without_changing_display():
    source = "# Title\n\nRead **important** [docs](https://example.com).\n```python\nprint(123)\n```\nDone."
    spoken, displayed = await run_reply([source])
    assert spoken.split() == "Title Read important docs. Done.".split()
    assert "".join(item[1] for item in displayed if item[0] == "append_model_stream_delta") == source


@pytest.mark.asyncio
async def test_enabling_voice_inside_a_code_fence_does_not_read_code():
    spoken, _ = await run_reply([False, "```python\n", True, "print(123)\n```\nNow speak."])
    assert spoken.strip() == "Now speak."


@pytest.mark.asyncio
async def test_unfinished_markup_from_disabled_voice_is_not_replayed():
    spoken, _ = await run_reply(["Before. ", False, "**old", True, "** New."])
    assert "old" not in spoken
    assert "*" not in spoken
    assert "New." in spoken


@pytest.mark.asyncio
async def test_only_code_does_not_start_a_player():
    def unexpected(*args, **kwargs):
        raise AssertionError("An empty speech response must not open audio output")

    errors = []
    state = SimpleNamespace(generation=(0, 0), voice_enabled=True, begin_voice=unexpected,
                            voice_error=errors.append)
    system = SimpleNamespace(session_key="one", _session=state, workspace=None,
                             presentation=SimpleNamespace(supports_model_stream=lambda: False))

    async def events():
        yield SimpleNamespace(event_kind="part_delta", delta=SimpleNamespace(
            part_delta_kind="text", content_delta="```python\nprint(1)\n```"))

    await coordinator_stream_handler(system)(None, events())
    assert not errors


@pytest.mark.asyncio
async def test_finish_can_start_speech_for_a_previously_unclosed_span():
    spoken, _ = await run_reply(["**unfinished"])
    assert spoken == "unfinished"


@pytest.mark.asyncio
async def test_parser_and_pending_reply_share_one_budget_without_stopping_text():
    from redlotus.TTS import SpeechBusy

    class Reply:
        text_capacity = 24
        pending_chars = 0
        cancelled = False

        async def feed(self, text):
            self.pending_chars += len(text)

        def cancel(self):
            self.cancelled = True

    reply = Reply()
    errors, displayed = [], []
    state = SimpleNamespace(generation=(0, 0), voice_enabled=True,
                            begin_voice=lambda *args, **kwargs: reply, voice_error=errors.append)
    system = SimpleNamespace(session_key="one", _session=state, workspace=None,
                             presentation=SimpleNamespace(supports_model_stream=lambda: True,
                                                          update_output=lambda *args: displayed.append(args)))
    deltas = ["Already queued. ", "[unfinished label", "](https://example.com) Text continues."]

    async def events():
        for delta in deltas:
            yield SimpleNamespace(event_kind="part_delta", delta=SimpleNamespace(
                part_delta_kind="text", content_delta=delta))

    await coordinator_stream_handler(system)(None, events())
    assert reply.cancelled
    assert len(errors) == 1 and isinstance(errors[0], SpeechBusy)
    assert "".join(item[1] for item in displayed if item[0] == "append_model_stream_delta") == "".join(deltas)
