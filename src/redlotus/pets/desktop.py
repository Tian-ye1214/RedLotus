"""Optional Qt child: cached sprite drawing, local pointer input and parent ownership."""
from __future__ import annotations

import asyncio
import json
import math
import os
import sys
import threading
import time

try:
    from PySide6.QtCore import QEvent, QMetaObject, QPoint, Qt, QTimer
    from PySide6.QtGui import QCursor, QImage, QPainter, QPixmap
    from PySide6.QtWidgets import QApplication, QMenu, QWidget
except (ImportError, OSError) as exc:
    _QT_ERROR = str(exc)
    QWidget = object
else:
    _QT_ERROR = ""


class ParentPipe:
    """A daemon owns EOF detection even while loading or the GUI thread is stuck."""

    def __init__(self, stream):
        self.stream = stream
        self.closed = threading.Event()
        self.finished = threading.Event()
        self.application = None

    def start(self):
        threading.Thread(target=self._watch, name="pets-parent-pipe", daemon=True).start()

    def _watch(self):
        try:
            self.stream.read()
        except (OSError, ValueError):
            pass
        if self.finished.is_set():
            return
        self.closed.set()
        try:
            if self.application is not None:
                QMetaObject.invokeMethod(self.application, "quit", Qt.ConnectionType.QueuedConnection)
        except RuntimeError:
            pass  # Qt may already be destroying the application during shutdown.
        if not self.finished.wait(2):
            os._exit(0)


class PetWindow(QWidget):
    """All Qt objects and painting live on this child process's main thread."""

    TICK_MS = 16

    def __init__(self, model):
        flags = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                 | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        super().__init__(None, flags)
        self.model = model
        self._frame_id = model.frame_id
        self._press = None
        self._dragging = False
        self._pointer_inside = False
        self._placing = False
        self._offset = QPoint()
        self.setWindowTitle("RedLotus Pet")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)
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
            width = max(1, min(area.width(), math.floor(self.model.size[0] / dpr)))
            height = max(1, min(area.height(), math.floor(self.model.size[1] / dpr)))
            self.setFixedSize(width, height)
            self.move(max(area.left(), min(point.x(), area.right() - width + 1)),
                      max(area.top(), min(point.y(), area.bottom() - height + 1)))
        finally:
            self._placing = False

    def event(self, event):
        result = super().event(event)
        if event.type() == QEvent.Type.DevicePixelRatioChange and hasattr(self, "model"):
            self._fit()
        return result

    def _advance(self):
        frame_id = self.model.advance(time.monotonic())
        if frame_id != self._frame_id:
            self._frame_id = frame_id
            self.update()

    def _interact(self, event):
        self.model.interact(event, time.monotonic())
        self._advance()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        painter.drawPixmap(self.rect(), self._pixmaps[self._frame_id])

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
        if self._dragging:
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

            if len(argv) > 1 or (argv and argv[0] not in CHARACTERS):
                raise ValueError("Desktop pet character must be charcoal or ivory")
            character = argv[0] if argv else "charcoal"
            app = QApplication(["redlotus-pets"])
            app.setQuitOnLastWindowClosed(True)
            parent.application = app
            model = asyncio.run(PetFactory.model(character))
            if parent.closed.is_set():
                return 0
            window = PetWindow(model)
            window.show()
            app.processEvents()
            if parent.closed.is_set() or not window.isVisible():
                return 0
            print(json.dumps({"event": "ready", "character": character}), flush=True)
            return app.exec()
        except Exception as exc:
            print(json.dumps({"event": "error", "error": str(exc)}), flush=True)
            return 1
        finally:
            parent.finished.set()


if __name__ == "__main__":
    raise SystemExit(PetApplication.run(sys.argv[1:]))
