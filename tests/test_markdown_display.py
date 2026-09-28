"""Assistant Markdown reaches real Rich/Textual and isolated Qt renderers."""
import asyncio
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from textual.app import App
from textual.containers import VerticalScroll
from textual.widgets import Collapsible, Input, RichLog, Static

from redlotus.ui import presentation
from redlotus.ui.tui import RedLotusTui
from test_pets_desktop import WINDOW, probe, requires_qt


WEATHER = "**成都 · 今天 9月28日（周一）**\n\n- **天气**：阴，25°C\n- **湿度**：68%\n\n另外提醒：明天起转阴雨。"


def rendered_segments(panel):
    console = Console(width=76, color_system="truecolor", force_terminal=True)
    return list(console.render(panel))


def assert_markdown_panel(panel, source):
    assert isinstance(panel, Panel) and isinstance(panel.renderable, Markdown)
    assert panel.renderable.markup == source
    segments = rendered_segments(panel)
    text = "".join(segment.text for segment in segments)
    assert "**天气**" not in text and "天气" in text
    assert any("天气" in s.text and s.style and s.style.bold for s in segments)
    assert "•" in text


def test_panel_markdown_is_explicit_and_plain_user_text_is_unchanged():
    plain = presentation.user_text_panel(WEATHER, "用户")
    assert isinstance(plain.renderable, Text) and plain.renderable.plain == WEATHER
    formatted = presentation.user_text_panel(WEATHER, "助手", markdown=True, text_style="white")
    assert_markdown_panel(formatted, WEATHER)


class RecordedStatic(Static):
    def update(self, content="", **kwargs):
        self.last = content
        return super().update(content, **kwargs)


class RecordedLog(RichLog):
    def __init__(self, **kwargs):
        self.items = []
        super().__init__(**kwargs)

    def write(self, content, **kwargs):
        self.items.append(content)
        return super().write(content, **kwargs)


class MarkdownTui(App):
    """Production display methods with actual widgets, without Agent startup."""
    CSS = "#output { height: 12; } #stream-preview { height: 12; } #thinking-scroll { height: 4; }"
    _write_user_input = RedLotusTui._write_user_input
    _refresh_model_stream = RedLotusTui._refresh_model_stream
    begin_model_stream = RedLotusTui.begin_model_stream
    begin_model_response = RedLotusTui.begin_model_response
    append_model_stream_delta = RedLotusTui.append_model_stream_delta
    end_model_stream = RedLotusTui.end_model_stream
    clear_model_stream = RedLotusTui.clear_model_stream
    _show_loaded_conversation = RedLotusTui._show_loaded_conversation

    def __init__(self):
        super().__init__()
        self.system = SimpleNamespace(session_key="current", _session=SimpleNamespace(generation=(0, 0)))
        self._model_stream_text = self._model_stream_title = self._model_stream_thinking = ""
        self._model_response_count, self._stream_session = 0, None

    def compose(self):
        yield RecordedLog(id="output", wrap=True, markup=False, highlight=False)
        with Collapsible(title="思考", id="thinking-preview"):
            with VerticalScroll(id="thinking-scroll"):
                yield RecordedStatic(id="thinking-content")
        with VerticalScroll(id="stream-preview"):
            yield RecordedStatic(id="stream-content")
        yield Static(id="session-context")
        yield Input(id="input")

    def refresh_status(self):
        pass

    def call_ui(self, callback):
        callback()

    def _session_context_text(self):
        return Text("Test session")


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["已完成", "已取消", "失败"])
async def test_tui_stream_terminal_and_thinking_keep_their_formats(monkeypatch, terminal):
    app = MarkdownTui()
    async with app.run_test(size=(88, 40)) as pilot:
        log = app.query_one("#output", RecordedLog)
        monkeypatch.setattr(presentation, "_sink", presentation.TextualOutputSink(app, log))
        monkeypatch.setattr(presentation.logger, "info_file_only", lambda *args: None)
        app.begin_model_stream("Coordinator 正在回复")
        app.begin_model_response()
        for piece in [WEATHER[:1], WEATHER[1:9], WEATHER[9:26], WEATHER[26:]]:
            app.append_model_stream_delta(piece)
        assert_markdown_panel(app.query_one("#stream-content", RecordedStatic).last, WEATHER)
        app.append_model_stream_delta("**原始思考**", "thinking")
        thinking = app.query_one("#thinking-content", RecordedStatic).last
        assert isinstance(thinking, Text) and thinking.plain == "**原始思考**"
        if terminal == "已完成":
            presentation.finish_model_stream(WEATHER, title="Coordinator")
        else:
            app.end_model_stream(terminal)
        await pilot.pause()
        assert len(log.items) == 1
        assert_markdown_panel(log.items[0], WEATHER)
        assert not app.query_one("#stream-preview").display
        presentation.show_model_output("**原样汇总**", markdown=False, log=False)
        assert isinstance(log.items[-1].renderable, Text)
        assert log.items[-1].renderable.plain == "**原样汇总**"


@pytest.mark.asyncio
async def test_tui_restored_assistant_is_markdown_and_old_stream_cannot_return():
    app = MarkdownTui()
    async with app.run_test(size=(88, 40)) as pilot:
        app.begin_model_stream("Old response")
        app.append_model_stream_delta("**旧正文**")
        app.system.session_key = "restored"
        messages = [ModelRequest(parts=[UserPromptPart(WEATHER)]), ModelResponse(parts=[TextPart(WEATHER)])]
        app._show_loaded_conversation(SimpleNamespace(title="历史会话", turn_count_label="1 轮"), messages)
        app.append_model_stream_delta("late response")
        app.end_model_stream("失败")
        await pilot.pause()
        rows = app.query_one("#output", RecordedLog).items
        assert len(rows) == 3
        assert isinstance(rows[1].renderable, Text) and rows[1].renderable.plain == WEATHER
        assert_markdown_panel(rows[2], WEATHER)
        assert app._model_stream_text == "" and not app.query_one("#stream-preview").display


@pytest.mark.asyncio
async def test_tui_accepts_input_during_markdown_updates():
    app = MarkdownTui()
    async with app.run_test(size=(88, 40)) as pilot:
        app.query_one(Input).focus()
        app.begin_model_stream("Coordinator 正在回复")
        observed = []
        async def produce():
            for _ in range(70):
                app.append_model_stream_delta("\n\n" + WEATHER)
                observed.append(app.query_one(Input).value)
                await asyncio.sleep(.01)
        task = asyncio.create_task(produce())
        try:
            await pilot.press("h")
            assert app.query_one(Input).value == "h"
            await asyncio.wait_for(task, timeout=10)
            assert "h" in observed[:-1], "input was delayed until the stream finished"
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert_markdown_panel(app.query_one("#stream-content", RecordedStatic).last, app._model_stream_text)


QT_REPLY = WINDOW + """
import json
from PySide6.QtGui import QFont, QTextCursor, QTextDocument, QTextFormat, QTextTable
from PySide6.QtWidgets import QPushButton, QTextBrowser
bubble = window.bubble
def snapshot(text, seq=1, phase='streaming', reply='one'):
    bubble.apply_snapshot(dict(event='reply', reply_id=reply, seq=seq, phase=phase, text=text, truncated=False))
    app.processEvents()
def text_format(text):
    return bubble.text.document().find(text).charFormat()
"""


@requires_qt
@pytest.mark.parametrize("dpi", ["1", "1.25", "1.5", "2"])
def test_bubble_weather_has_real_markdown_formats_and_bounded_layout(dpi):
    assert probe(QT_REPLY + """
for seq, phase in enumerate(('streaming', 'done', 'cancelled', 'failed'), 1):
    snapshot(SOURCE, seq, phase, str(seq))
    assert isinstance(bubble.text, QTextBrowser)
    assert text_format('天气').fontWeight() == QFont.Weight.Bold
    assert text_format('湿度').fontWeight() == QFont.Weight.Bold
    assert bubble.text.document().find('天气').block().textList() is not None
    assert '**' not in bubble.text.toPlainText() and '25°C' in bubble.text.toPlainText()
    assert bubble.width() == 280 and bubble.height() <= 180
    close = bubble.findChild(QPushButton)
    assert bubble.text.geometry().right() < close.x() and close.y() == 12
    assert bubble.text.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert bubble._timer.isActive() == (phase != 'streaming')
print(json.dumps(True))
window.close()
""".replace("SOURCE", repr(WEATHER)), scale=dpi)


@requires_qt
def test_bubble_markdown_blocks_and_split_delimiters():
    source = "# Heading\n\n*italic* ~~removed~~ `x*2`\n\n> quote\n\n1. one\n   - nested\n\n```python\nprint('**literal**')\n```\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n\\*literal\\* <b>html</b>"
    assert probe(QT_REPLY + """
snapshot('**weather', 1)
assert bubble.text.toPlainText() == '**weather'
snapshot('**weather**', 2)
assert text_format('weather').fontWeight() == QFont.Weight.Bold
snapshot(SOURCE, 3)
document = bubble.text.document()
assert document.firstBlock().blockFormat().headingLevel() == 1
assert text_format('italic').fontItalic()
assert text_format('removed').fontStrikeOut()
assert 'x*2' in bubble.text.toPlainText() and "print('**literal**')" in bubble.text.toPlainText()
assert '*literal* <b>html</b>' in bubble.text.toPlainText()
assert document.find('nested').block().textList().format().indent() >= 2
assert any(isinstance(frame, QTextTable) for frame in document.rootFrame().childFrames())
for seq, source in enumerate(('```python\\nprint(1)', '```python\\nprint(1)\\n```'), 4):
    snapshot(source, seq)
    assert bubble.text.toPlainText() == 'print(1)'
snapshot('**short**', 6)
assert bubble.height() <= 64 and bubble.text.verticalScrollBar().maximum() == 0
print(json.dumps(True))
window.close()
""".replace("SOURCE", repr(source)))


@requires_qt
def test_bubble_links_only_open_web_urls_and_images_do_not_load(tmp_path):
    marker = tmp_path / "image.png"
    assert probe(QT_REPLY + """
from types import SimpleNamespace
from PySide6.QtCore import QUrl
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from redlotus.pets import desktop
opened = []
desktop.QDesktopServices = SimpleNamespace(openUrl=lambda url: opened.append(url.toString()) or True)
snapshot('[Qt](https://doc.qt.io/)', 1)
assert isinstance(bubble.text, QTextBrowser)
cursor = bubble.text.document().find('Qt')
cursor.setPosition(cursor.selectionStart() + 1)
QTest.mouseClick(bubble.text.viewport(), Qt.MouseButton.LeftButton, pos=bubble.text.cursorRect(cursor).center())
assert opened == ['https://doc.qt.io/']
before = bubble.text.toPlainText()
for url in ('file:///test', 'javascript:alert(1)', 'mailto:test@example.com', '#anchor', 'relative.md'):
    bubble.text.anchorClicked.emit(QUrl(url))
bubble.text.anchorClicked.emit(QUrl('http://example.com/'))
assert opened == ['https://doc.qt.io/', 'http://example.com/']
assert bubble.text.toPlainText() == before and bubble.text.source().isEmpty()
picture = QImage(1000, 800, QImage.Format.Format_ARGB32)
picture.fill(Qt.GlobalColor.red)
assert picture.save(MARKER)
for seq, url in enumerate((QUrl.fromLocalFile(MARKER).toString(), 'https://example.com/image.png'), 2):
    snapshot('![picture](' + url + ')', seq)
    value = bubble.text.document().resource(QTextDocument.ResourceType.ImageResource, QUrl(url))
    assert isinstance(value, QImage) and (value.width(), value.height()) == (12, 12)
    assert bubble.text.horizontalScrollBar().maximum() == 0
    assert bubble.height() <= 64
print(json.dumps(True))
window.close()
""".replace("MARKER", repr(str(marker))))


@requires_qt
@pytest.mark.parametrize("source", ["| " + " | ".join(["Long heading"] * 8) + " |\n|" + "---|" * 8 + "\n|" + "value|" * 8,
                                   "```\n" + "long_code_identifier_" * 35 + "\n```"])
def test_wide_markdown_can_scroll_without_exceeding_bubble_height(source):
    assert probe(QT_REPLY + """
snapshot(SOURCE)
bar = bubble.text.horizontalScrollBar()
assert bar.isVisible() and bar.maximum() > 0
assert bubble.height() <= 180
assert bubble.text.verticalScrollBar().maximum() == 0, 'horizontal bar must fit alongside the document'
bar.setValue(bar.maximum())
snapshot(SOURCE + '\\n\\nmore', 2)
assert bubble.height() <= 180
print(json.dumps(True))
window.close()
""".replace("SOURCE", repr(source)))


@requires_qt
def test_markdown_measurement_documents_are_released_after_updates():
    assert probe(QT_REPLY + """
from PySide6.QtCore import QCoreApplication
for seq in range(30):
    snapshot('**reply** ' + str(seq), seq)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
assert len(bubble.text.findChildren(QTextDocument)) == 1
print(json.dumps(True))
window.close()
""")
