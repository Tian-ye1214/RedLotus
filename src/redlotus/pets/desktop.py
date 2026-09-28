"""Optional Qt child: cached sprite drawing, local pointer input and parent ownership."""
from __future__ import annotations

import asyncio
import json
import math
import os
import stat
import sys
import threading
import time

try:
    from PySide6.QtCore import QEvent, QMetaObject, QPoint, QRect, Qt, QTimer, Signal, Slot
    from PySide6.QtGui import QColor, QCursor, QDesktopServices, QImage, QPainter, QPen, QPixmap, QPolygon, QTextDocument, QTextOption
    from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QMenu, QPushButton, QTextBrowser, QVBoxLayout, QWidget
except (ImportError, OSError) as exc:
    _QT_ERROR = str(exc)
    QWidget = QTextBrowser = object
else:
    _QT_ERROR = ""


class ParentPipe:
    """Stoppable raw-pipe workers; no worker ever locks Python's standard streams."""

    def __init__(self, stream, output=None):
        self._input_fd, self._output_fd = stream.fileno(), (output or sys.stdout).fileno()
        self._modes = {}
        self.closed = threading.Event()
        self.finished = threading.Event()
        self._stop, self._cancel_output = threading.Event(), threading.Event()
        self.application = self.window = self._pending = None
        self._lock, self._output, self._seq = threading.Condition(), {}, -1
        self._scheduled = self._stopping = False
        self._writer = threading.Thread(target=self._write, name="pets-parent-writer", daemon=False)
        self._reader = threading.Thread(target=self._watch, name="pets-parent-pipe", daemon=False)

    def start(self):
        for fd in (self._input_fd, self._output_fd):
            if stat.S_ISFIFO(os.fstat(fd).st_mode):
                self._modes[fd] = os.get_blocking(fd)
                os.set_blocking(fd, False)
        self._writer.start()
        self._reader.start()

    def attach(self, window):
        window._reply_pipe = self
        window.reply_available.connect(window._receive_reply, Qt.ConnectionType.QueuedConnection)
        with self._lock:
            self.window = window
            if self._pending is not None:
                self._scheduled = True
                window.reply_available.emit()

    def _deliver(self):
        with self._lock:
            snapshot, self._pending, self._scheduled = self._pending, None, False
        if snapshot is not None and not self.closed.is_set():
            self.window.bubble.apply_snapshot(snapshot)

    def send(self, record):
        with self._lock:
            self._output[record["event"]] = record
            self._lock.notify()

    def _write(self):
        try:
            while True:
                with self._lock:
                    self._lock.wait_for(lambda: self._output or self._stopping)
                    if not self._output:
                        return
                    record = self._output.pop(next(iter(self._output)))
                data = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
                while data and not self._cancel_output.is_set():
                    try:
                        count = os.write(self._output_fd, data)
                    except BlockingIOError:
                        count = 0
                    data = data[count:]
                    if not count:
                        self._cancel_output.wait(.01)
                if self._cancel_output.is_set():
                    return
        except (OSError, ValueError):
            self._disconnect()

    def finish(self):
        self._stop.set()
        with self._lock:
            self._stopping = True
            self._lock.notify()
        self._reader.join()
        self._writer.join(timeout=1)
        self._cancel_output.set()
        self._writer.join()
        for fd, blocking in self._modes.items():
            os.set_blocking(fd, blocking)
        self.finished.set()

    def _receive(self, line):
        try:
            value = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError):
            return
        if (not isinstance(value, dict) or value.get("event") != "reply"
                or type(value.get("seq")) is not int or value["seq"] <= self._seq
                or not isinstance(value.get("reply_id"), str) or not isinstance(value.get("text"), str)
                or type(value.get("truncated")) is not bool
                or value.get("phase") not in ("streaming", "done", "cancelled", "failed", "clear")):
            return
        value["truncated"] |= len(value["text"]) > 32768
        value["text"] = value["text"][-32768:]
        with self._lock:
            self._pending, self._seq = value, value["seq"]
            if self.window is not None and not self._scheduled:
                self._scheduled = True
                self.window.reply_available.emit()

    def _watch(self):
        if self._input_fd not in self._modes:
            return  # An interactive terminal or redirected file has no owning parent.
        pending, discard = b"", False
        try:
            while not self._stop.is_set():
                try:
                    chunk = os.read(self._input_fd, 4096)
                except BlockingIOError:
                    self._stop.wait(.01)
                    continue
                if not chunk:
                    break
                lines = (pending + chunk).split(b"\n")
                pending = lines.pop()
                for line in lines:
                    if not discard and len(line) < 262144:
                        self._receive(line)
                    discard = False
                if len(pending) >= 262144:
                    pending, discard = b"", True
        except (OSError, ValueError):
            pass
        finally:
            self._disconnect()

    def _disconnect(self):
        if self._stop.is_set():
            return
        self.closed.set()
        try:
            if self.application is not None:
                QMetaObject.invokeMethod(self.application, "quit", Qt.ConnectionType.QueuedConnection)
        except RuntimeError:
            pass  # Qt may already be destroying the application during shutdown.
        if not self._stop.wait(2):
            os._exit(0)


class OverlayWindow(QWidget):
    def __init__(self):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)


class ReplyText(QTextBrowser):
    """Render in-memory Markdown; only explicit web-link clicks leave the bubble."""

    def __init__(self):
        super().__init__(openLinks=False, openExternalLinks=False, focusPolicy=Qt.FocusPolicy.NoFocus,
                         wordWrapMode=QTextOption.WrapMode.WordWrap)
        self.anchorClicked.connect(self.open_link)

    def loadResource(self, kind, name):
        placeholder = QImage(12, 12, QImage.Format.Format_ARGB32)
        placeholder.fill(QColor("#dac9ac"))
        return placeholder  # A non-null resource prevents Qt's fallback file loading.

    def open_link(self, url):
        if url.isValid() and url.scheme() in {"http", "https"} and url.host():
            QDesktopServices.openUrl(url)


class ReplyBubble(OverlayWindow):
    """One replaceable Markdown reply, separate from the pet's pointer surface."""

    def __init__(self, pet):
        super().__init__()
        self.pet, self._reply_id, self._hidden_id, self._seq = pet, None, None, -1
        self._phase, self._remaining, self._hover = "streaming", 15000, False
        self._tail = "bottom"
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setFixedWidth(280)
        self.setMaximumHeight(180)
        self.setStyleSheet("QWidget { color: #3b3228; font-size: 13px; } "
                           "QTextEdit, QLabel, QPushButton { background: transparent; border: none; }")
        layout = QHBoxLayout(self, spacing=6)
        layout.setContentsMargins(12, 12, 12, 16)
        body = QVBoxLayout(spacing=4)
        layout.addLayout(body, 1)
        close = QPushButton("×", focusPolicy=Qt.FocusPolicy.NoFocus, clicked=self.close)
        close.setFixedSize(18, 18)
        layout.addWidget(close, 0, Qt.AlignmentFlag.AlignTop)
        self.banner = QLabel("较早内容请在终端查看")
        body.addWidget(self.banner)
        self.banner.hide()
        self.text = ReplyText()
        body.addWidget(self.text)
        self._timer = QTimer(self, singleShot=True, timeout=self.close)

    def apply_snapshot(self, snapshot):
        if snapshot["seq"] <= self._seq:
            return
        self._seq = snapshot["seq"]
        fresh = snapshot["reply_id"] != self._reply_id
        if fresh:
            self._reply_id, self._hidden_id = snapshot["reply_id"], None
        phase, text = snapshot["phase"], snapshot["text"]
        if phase == "clear" or not text or self._hidden_id == self._reply_id:
            self._phase = phase
            self._timer.stop()
            self.hide()
            return
        bar = self.text.verticalScrollBar()
        old, bottom = bar.value(), fresh or bar.value() >= bar.maximum() - 2
        self.ensurePolished()
        self.text.document().setMarkdown(text[-32768:], QTextDocument.MarkdownDialectGitHub | QTextDocument.MarkdownNoHTML)
        document = self.text.document().clone(self.text)
        # Reserve the 18px close button, 6px gap and 24px horizontal margins.
        document.setTextWidth(self.width() - 48 - bar.sizeHint().width())
        horizontal = self.text.horizontalScrollBar().sizeHint().height() if document.size().width() > document.textWidth() else 0
        truncated = snapshot["truncated"] or len(text) > 32768
        available = 152 - (self.banner.sizeHint().height() + 4 if truncated else 0)
        self.text.setFixedHeight(min(available, max(28, math.ceil(document.size().height()) + horizontal + 6)))
        document.deleteLater()
        self.banner.setVisible(truncated)
        self.layout().activate()
        self.resize(self.width(), self.sizeHint().height())
        self.follow()
        self.show()
        bar.setValue(bar.maximum() if bottom else old)
        if fresh or phase != self._phase:
            self._timer.stop()
            self._remaining = 15000
            if phase != "streaming" and not self._hover:
                self._timer.start(self._remaining)
        self._phase = phase

    def follow(self):
        area, pet = self.pet.screen().availableGeometry(), self.pet.geometry()
        self.setFixedWidth(min(280, area.width()))
        x = max(area.left(), min(pet.center().x() - self.width() // 2, area.right() - self.width() + 1))
        y = max(area.top(), min(pet.center().y() - self.height() // 2, area.bottom() - self.height() + 1))
        candidates = [(x, pet.top() - self.height(), "bottom"), (x, pet.bottom() + 1, "top"),
                      (pet.right() + 1, y, "left"), (pet.left() - self.width(), y, "right")]
        x, y, self._tail = next((c for c in candidates if area.contains(QRect(c[0], c[1], self.width(), self.height()))), candidates[0])
        self.move(max(area.left(), min(x, area.right() - self.width() + 1)),
                  max(area.top(), min(y, area.bottom() - self.height() + 1)))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QColor("#dac9ac"))
        painter.setBrush(QColor("#fff6e5"))
        painter.drawRoundedRect(self.rect().adjusted(8, 8, -8, -8), 12, 12)
        x = max(16, min(self.width() - 16, self.pet.geometry().center().x() - self.x()))
        y = max(16, min(self.height() - 16, self.pet.geometry().center().y() - self.y()))
        if self._tail in ("left", "right"):
            x, direction = (8, -1) if self._tail == "left" else (self.width() - 8, 1)
            points = [QPoint(x, y - 6), QPoint(x + direction * 8, y), QPoint(x, y + 6)]
        else:
            y, direction = (8, -1) if self._tail == "top" else (self.height() - 8, 1)
            points = [QPoint(x - 6, y), QPoint(x, y + direction * 8), QPoint(x + 6, y)]
        painter.drawPolygon(QPolygon(points))

    def enterEvent(self, event):
        self._hover = True
        if self._timer.isActive():
            self._remaining = max(1, self._timer.remainingTime())
            self._timer.stop()

    def leaveEvent(self, event):
        self._hover = False
        if self._phase != "streaming" and self.isVisible():
            self._timer.start(self._remaining)

    def closeEvent(self, event):
        self._hidden_id = self._reply_id
        self._hover = False
        self._timer.stop()
        self.hide()
        event.ignore()


class PetWindow(OverlayWindow):
    """All Qt objects and painting live on this child process's main thread."""

    TICK_MS = 16
    GRIP_SIZE, GRIP_MARGIN, GRIP_DELAY = 22, 12, .6
    if not _QT_ERROR:
        scale_changed = Signal(float)
        reply_available = Signal()

        @Slot()
        def _receive_reply(self):
            self._reply_pipe._deliver()

    def __init__(self, model):
        super().__init__()
        self.model = model
        self.scale, self._resizing = 1.0, None
        self._frame_id = model.frame_id
        self._press = None
        self._dragging = False
        self._pointer_inside = False
        self._grip_visible, self._grip_hover, self._grip_until = False, False, 0.
        self._placing = False
        self._offset = QPoint()
        self.setWindowTitle("RedLotus Pet")
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose)
        self.setMouseTracking(True)
        self.bubble = ReplyBubble(self)
        width, height = model.size
        self._pixmaps = {
            name: QPixmap.fromImage(QImage(pixels, width, height, width * 4,
                                          QImage.Format.Format_RGBA8888).copy())
            for name, pixels in model.frames.items()
        }
        self._menu = QMenu(self)
        self._menu.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus)
        self._menu.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._menu.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._menu.addAction("退出桌宠", self.close)
        self._menu.aboutToHide.connect(self._menu_closed)
        self.winId()
        self.windowHandle().screenChanged.connect(self._fit)
        app = QApplication.instance()
        for screen in app.screens():
            self._track_screen(screen)
        app.screenAdded.connect(self._track_screen)
        app.screenRemoved.connect(self._screen_removed)
        screen = app.screenAt(QCursor.pos()) or app.primaryScreen()
        if screen is None:
            raise RuntimeError("No screen is available for the desktop pet")
        self._place(screen.availableGeometry().bottomRight(), screen)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._advance)
        self._timer.start(self.TICK_MS)

    def _track_screen(self, screen):
        screen.availableGeometryChanged.connect(self._fit)
        screen.geometryChanged.connect(self._fit)
        screen.logicalDotsPerInchChanged.connect(self._fit)
        screen.physicalDotsPerInchChanged.connect(self._fit)

    def _screen_removed(self, removed):
        screens = [screen for screen in QApplication.screens() if screen is not removed]
        if screens:
            screen = self.windowHandle().screen()
            self._place(self.pos(), screen if screen in screens else screens[0])

    def _fit(self, *unused):
        if self._placing:
            return
        screen = self.windowHandle().screen()
        if screen is not None:
            self._place(self.pos(), screen)

    def _place(self, point, screen):
        self._placing = True
        try:
            handle = self.windowHandle()
            if handle.screen() is not screen:
                handle.setScreen(screen)
            dpr = handle.devicePixelRatio()
            area = screen.availableGeometry()
            ratio = min(self.scale / dpr, area.width() / self.model.size[0], area.height() / self.model.size[1])
            width, height = (max(1, math.floor(size * ratio)) for size in self.model.size)
            self.setFixedSize(width, height)
            self.move(max(area.left(), min(point.x(), area.right() - width + 1)),
                      max(area.top(), min(point.y(), area.bottom() - height + 1)))
        finally:
            self._placing = False
        self.bubble.follow()

    def set_scale(self, scale):
        if not math.isfinite(scale):
            raise ValueError("Desktop pet scale must be finite")
        self.scale = max(.5, min(3., scale))
        self._fit()
        self.update()

    def _grip_corner(self):
        if self._resizing:
            return self._resizing[:2]
        area, box = self.screen().availableGeometry(), self.geometry()
        room = 300 / self.devicePixelRatioF() - self.width()
        return (-1 if area.right() - box.right() < room and box.left() - area.left() > area.right() - box.right() else 1,
                -1 if area.bottom() - box.bottom() < room and box.top() - area.top() > area.bottom() - box.bottom() else 1)

    def _grip_rect(self):
        x, y = self._grip_corner()
        size = min(self.GRIP_SIZE, self.width() // 2, self.height() // 2)
        return QRect(self.width() - size if x > 0 else 0, self.height() - size if y > 0 else 0, size, size)

    def event(self, event):
        result = super().event(event)
        if event.type() == QEvent.Type.DevicePixelRatioChange and hasattr(self, "model"):
            self._fit()
        return result

    def _advance(self):
        now, point = time.monotonic(), self.mapFromGlobal(QCursor.pos())
        # Native leave events also occur over transparent pixels on Windows.
        if (self.rect().adjusted(-self.GRIP_MARGIN, -self.GRIP_MARGIN,
                                self.GRIP_MARGIN, self.GRIP_MARGIN).contains(point) or self._resizing):
            self._grip_until = now + self.GRIP_DELAY
        visible = self.isVisible() and now < self._grip_until
        hover = visible and (self._resizing is not None or self._grip_rect().contains(point))
        sx, sy = self._grip_corner()
        self.setCursor((Qt.CursorShape.SizeFDiagCursor if sx == sy else Qt.CursorShape.SizeBDiagCursor)
                       if hover else Qt.CursorShape.ArrowCursor)
        frame_id = self.model.advance(now)
        if frame_id != self._frame_id or (visible, hover) != (self._grip_visible, self._grip_hover):
            self._frame_id, self._grip_visible, self._grip_hover = frame_id, visible, hover
            self.update()

    def _interact(self, event):
        self.model.interact(event, time.monotonic())
        self._advance()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, self.scale < 1.)
        painter.drawPixmap(self.rect(), self._pixmaps[self._frame_id])
        if self._grip_visible:
            rect, (sx, sy) = self._grip_rect(), self._grip_corner()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor("#53483b"), 1.4))
            painter.setBrush(QColor("#ffe0a3" if self._grip_hover else "#fff6e5"))
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 3, 3)
            painter.translate(rect.center())
            painter.scale(rect.width() / self.GRIP_SIZE, sx * sy * rect.height() / self.GRIP_SIZE)
            painter.drawLine(-5, -5, 5, 5)
            for direction in (-1, 1):
                painter.drawPolyline(QPolygon([QPoint(direction * 5, 0), QPoint(direction * 5, direction * 5),
                                               QPoint(0, direction * 5)]))

    def enterEvent(self, event):
        if not self._menu.isVisible():
            self._set_pointer(True)

    def leaveEvent(self, event):
        if not self._menu.isVisible():
            self._set_pointer(False)

    def _set_pointer(self, inside):
        if inside != self._pointer_inside:
            self._pointer_inside = inside
            self._interact("pointer_enter" if inside else "pointer_leave")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press = event.globalPosition().toPoint()
            self._offset = self._press - self.pos()
            self._dragging = False
            if self._grip_visible and self._grip_rect().contains(event.position().toPoint()):
                sx, sy = self._grip_corner()
                anchor = self.pos() + QPoint(self.width() - 1 if sx < 0 else 0, self.height() - 1 if sy < 0 else 0)
                self._resizing = (sx, sy, anchor, self.scale)
            self.grabMouse()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        self.mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._press is None or not event.buttons() & Qt.MouseButton.LeftButton:
            return
        point = event.globalPosition().toPoint()
        if self._resizing:
            sx, sy, anchor, scale = self._resizing
            delta = point - self._press
            change = (sx * delta.x() / self.model.size[0] + sy * delta.y() / self.model.size[1]) / 2
            self.set_scale(scale + change * self.devicePixelRatioF())
            self._place(anchor - QPoint(self.width() - 1 if sx < 0 else 0, self.height() - 1 if sy < 0 else 0), self.screen())
            event.accept()
            return
        if not self._dragging:
            if (point - self._press).manhattanLength() < QApplication.startDragDistance():
                return
            self._dragging = True
            self._interact("drag_start")
        screen = QApplication.screenAt(point) or self.windowHandle().screen()
        self._place(point - self._offset, screen)
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or self._press is None:
            return
        self.releaseMouse()
        point = self.mapFromGlobal(event.globalPosition().toPoint())
        inside = self.rect().contains(point)
        self._set_pointer(inside)
        if self._resizing:
            self._resizing = None
            self.scale_changed.emit(self.scale)
        elif self._dragging:
            self._interact("drag_end")
        elif inside:
            self._interact("primary_click")
        self._press = None
        self._dragging = False
        event.accept()

    def contextMenuEvent(self, event):
        self._menu.popup(event.globalPos())
        event.accept()

    def _menu_closed(self):
        inside = self.rect().contains(self.mapFromGlobal(QCursor.pos()))
        self._set_pointer(inside)

    def closeEvent(self, event):
        self._timer.stop()
        self.bubble.close()
        if self._press is not None:
            self.releaseMouse()
        self._menu.close()
        super().closeEvent(event)
        QApplication.quit()


class PetApplication:
    """A JSON-lines child entry point that never initializes the Agent or speech."""

    @staticmethod
    def run(argv: list[str]) -> int:
        parent = ParentPipe(sys.stdin)
        parent.start()
        try:
            if _QT_ERROR:
                raise RuntimeError(f'Install desktop support with: pip install "RedLotus[pets]" ({_QT_ERROR})')
            from .factory import PetFactory
            from .model import CHARACTERS

            scale = 1.
            if "--scale" in argv:
                index = argv.index("--scale")
                scale = float(argv[index + 1])
                argv = argv[:index] + argv[index + 2:]
            if len(argv) > 1 or (argv and argv[0] not in CHARACTERS):
                raise ValueError("Desktop pet character must be charcoal or ivory")
            if not math.isfinite(scale) or not .5 <= scale <= 3:
                raise ValueError("Desktop pet scale must be between 0.5 and 3")
            character = argv[0] if argv else "charcoal"
            app = QApplication(["redlotus-pets"])
            app.setQuitOnLastWindowClosed(True)
            parent.application = app
            model = asyncio.run(PetFactory.model(character))
            if parent.closed.is_set():
                return 0
            window = PetWindow(model)
            window.set_scale(scale)
            window.scale_changed.connect(lambda value: parent.send({"event": "scale", "scale": value}))
            parent.attach(window)
            app.aboutToQuit.connect(window.close)
            window.show()
            app.processEvents()
            if parent.closed.is_set() or not window.isVisible():
                return 0
            parent.send({"event": "ready", "character": character})
            return app.exec()
        except Exception as exc:
            parent.send({"event": "error", "error": str(exc)})
            return 1
        finally:
            parent.finish()


if __name__ == "__main__":
    raise SystemExit(PetApplication.run(sys.argv[1:]))
