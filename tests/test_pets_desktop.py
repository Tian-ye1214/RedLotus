"""Qt stays in a small child, with EOF ownership and bounded physical geometry."""
import importlib.util
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from redlotus.runtime.resources import resource_root


ROOT = Path(__file__).resolve().parents[1]
QT = importlib.util.find_spec("PySide6") is not None
requires_qt = pytest.mark.skipif(not QT, reason="Install RedLotus[pets] to exercise Qt")
MODEL = """
from redlotus.pets.model import PetModel
class ProbePet(PetModel):
    def __init__(self):
        self.events = []
        self.current = 'idle'
        self.pixels = {
            'idle': bytes([220, 40, 50, 255]) * 10000,
            'look': bytes([40, 220, 50, 255]) * 10000,
            'happy': bytes([40, 50, 220, 255]) * 10000,
            'drag': bytes([220, 220, 50, 255]) * 10000,
        }
    @property
    def action(self): return self.current
    @property
    def frame_id(self): return self.current
    @property
    def size(self): return (100, 100)
    @property
    def frames(self): return self.pixels
    def advance(self, now): return self.current
    def interact(self, event, now):
        self.events.append(event)
        self.current = {'pointer_enter': 'look', 'pointer_leave': 'idle',
                        'primary_click': 'happy', 'drag_start': 'drag',
                        'drag_end': 'idle'}[event]
"""
WINDOW = MODEL + """
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QPainter
from PySide6.QtWidgets import QApplication
from redlotus.pets.desktop import PetWindow
app = QApplication([])
pet = ProbePet()
window = PetWindow(pet)
window.show()
app.processEvents()
window._timer.stop()
pet.events.clear()
"""
POINTER = """
from types import SimpleNamespace
from redlotus.pets import desktop
pointer = QPoint(-10000, -10000)
desktop.QCursor = SimpleNamespace(pos=lambda: pointer)
"""


def child(code, *, scale="1"):
    env = os.environ | {"PYTHONPATH": str(resource_root().parent), "QT_QPA_PLATFORM": "offscreen",
                        "QT_SCALE_FACTOR": scale, "PYTHONIOENCODING": "utf-8"}
    return subprocess.Popen([sys.executable, "-u", "-c", textwrap.dedent(code)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env, cwd=ROOT)


def probe(code, *, scale="1"):
    process = child(code, scale=scale)
    try:
        out, err = process.communicate(timeout=15)
        assert process.returncode == 0, (out, err)
        return json.loads(out)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def read_line(stream):
    lines = queue.Queue()
    threading.Thread(target=lambda: lines.put(stream.readline()), daemon=True).start()
    return lines.get(timeout=10)


def test_missing_qt_reports_install_guidance_as_json_without_agent_imports():
    report = probe("""
import importlib.abc, json, sys
class NoQt(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'PySide6' or fullname.startswith('PySide6.'):
            raise ModuleNotFoundError('No module named PySide6')
sys.meta_path.insert(0, NoQt())
from redlotus.pets.desktop import PetApplication
code = PetApplication.run(['charcoal'])
assert code != 0
assert not any(name.startswith(('redlotus.core', 'redlotus.TTS',
                                'redlotus.runtime.config', 'redlotus.ui'))
               for name in sys.modules)
""")
    assert report["event"] == "error"
    assert "RedLotus[pets]" in report["error"]


@requires_qt
@pytest.mark.parametrize("scale", ["1", "1.25", "1.5", "2"])
def test_window_is_transparent_focusless_and_bounded_in_physical_pixels(scale):
    result = probe(WINDOW + """
import json, math
screen = window.screen()
area = screen.availableGeometry()
image = window.grab().toImage()
flags = window.windowFlags()
print(json.dumps({
    'size': [window.width(), window.height()],
    'dpr': window.devicePixelRatioF(),
    'pixels': [image.width(), image.height()],
    'in_workarea': area.contains(window.geometry()),
    'bottom_right': window.geometry().bottomRight() == area.bottomRight(),
    'flags': all(bool(flags & flag) for flag in (
        Qt.WindowType.FramelessWindowHint, Qt.WindowType.WindowStaysOnTopHint,
        Qt.WindowType.WindowDoesNotAcceptFocus)),
    'transparent': window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground),
    'no_activate': window.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating),
    'no_focus': window.focusPolicy() == Qt.FocusPolicy.NoFocus,
    'color': image.pixelColor(image.width() // 2, image.height() // 2).getRgb(),
}))
window.close()
""", scale=scale)
    assert result["size"] == [int(100 / result["dpr"])] * 2
    assert max(result["pixels"]) <= 100
    assert all(result[key] for key in (
        "in_workarea", "bottom_right", "flags", "transparent", "no_activate", "no_focus"))
    assert result["color"] == [220, 40, 50, 255]


@requires_qt
def test_drag_threshold_release_outside_and_frame_changes_use_native_events():
    result = probe(WINDOW + """
import json
def send(kind, local, global_pos, button, buttons):
    event = QMouseEvent(kind, QPointF(local), QPointF(global_pos), button, buttons,
                        Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(window, event)
origin = window.pos()
local = QPoint(10, 10)
start = origin + local
left, none = Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton
send(QEvent.Type.MouseButtonPress, local, start, left, left)
assert 'primary_click' not in pet.events
send(QEvent.Type.MouseMove, local, start + QPoint(1, 0), none, left)
assert window.pos() == origin and 'drag_start' not in pet.events
send(QEvent.Type.MouseButtonRelease, local, start, left, none)
click_events = list(pet.events)
pet.events.clear()
send(QEvent.Type.MouseButtonPress, local, start, left, left)
destination = start - QPoint(QApplication.startDragDistance() + 30, 20)
send(QEvent.Type.MouseMove, local, destination, none, left)
assert window.pos() != origin
outside = window.pos() - QPoint(10, 10)
send(QEvent.Type.MouseButtonRelease, QPoint(-10, -10), outside, left, none)
drag_events = list(pet.events)
updates = []
window.update = lambda: updates.append(pet.frame_id)
window._advance()
window._advance()
assert not updates
pet.current = 'happy'
window._advance()
window._advance()
print(json.dumps({'click': click_events, 'drag': drag_events,
                  'updates': updates, 'released': type(window).mouseGrabber() is None,
                  'clamped': window.screen().availableGeometry().contains(window.geometry())}))
window.close()
""")
    assert result["click"].count("primary_click") == 1
    assert result["drag"].count("drag_start") == 1
    assert result["drag"][-2:] == ["pointer_leave", "drag_end"]
    assert "primary_click" not in result["drag"]
    assert result["released"] and result["clamped"]
    assert result["updates"] == ["happy"]


@requires_qt
def test_fractional_scaling_preserves_hard_pixel_edges_and_transparency():
    result = probe(MODEL + """
import json
from PySide6.QtWidgets import QApplication
from redlotus.pets.desktop import PetWindow
app = QApplication([])
pet = ProbePet()
pet.pixels['idle'] = b''.join(bytes([255, 0, 0, 255]) if (x + y) % 2
                            else bytes(4) for y in range(100) for x in range(100))
window = PetWindow(pet)
window.show()
app.processEvents()
image = window.grab().toImage()
colors = {image.pixelColor(x, y).getRgb()
          for y in range(image.height()) for x in range(image.width())}
print(json.dumps(sorted(colors)))
window.close()
""", scale="1.5")
    assert result == [[0, 0, 0, 0], [255, 0, 0, 255]]


@requires_qt
def test_placement_clamps_each_work_area_edge_after_geometry_change():
    result = probe(WINDOW + """
import json
screen = window.screen()
area = screen.availableGeometry()
window._place(QPoint(-100000, -100000), screen)
top_left = window.pos() == area.topLeft()
window._place(QPoint(100000, 100000), screen)
bottom_right = window.geometry().bottomRight() == area.bottomRight()
screen.availableGeometryChanged.emit(area)
print(json.dumps([top_left, bottom_right, area.contains(window.geometry())]))
window.close()
""")
    assert result == [True, True, True]


@requires_qt
def test_cancelling_menu_does_not_wake_an_already_sleeping_pet():
    result = probe(WINDOW + """
import json
from PySide6.QtGui import QCursor
QCursor.setPos(window.screen().availableGeometry().topLeft())
app.processEvents()
pet.pixels['sleep'] = pet.pixels['idle']
pet.current = 'sleep'
pet.events.clear()
window._menu.popup(window.pos())
window._menu.hide()
print(json.dumps({'action': pet.action, 'events': pet.events}))
window.close()
""")
    assert result == {"action": "sleep", "events": []}


@requires_qt
def test_right_click_menu_has_only_exit_and_manual_close_exits_with_open_stdin():
    process = child(MODEL + """
from PySide6.QtCore import QTimer
from redlotus.pets import desktop
from redlotus.pets.factory import PetFactory
async def load(character): return ProbePet()
PetFactory.model = load
original = desktop.PetWindow
class ClosingWindow(original):
    def __init__(self, pet):
        super().__init__(pet)
        assert len(self._menu.actions()) == 1
        assert self._menu.actions()[0].text() == '退出桌宠'
        QTimer.singleShot(100, self._menu.actions()[0].trigger)
desktop.PetWindow = ClosingWindow
code = desktop.PetApplication.run(['ivory'])
import sys, threading
assert not any(thread.name.startswith('pets-parent-') for thread in threading.enumerate())
assert not any(name.startswith(('redlotus.core', 'redlotus.TTS', 'redlotus.ui'))
               for name in sys.modules)
raise SystemExit(code)
""")
    try:
        process.wait(timeout=10)  # Do not close stdin; the user's menu owns this exit.
        assert process.returncode == 0, process.stderr.read()
        assert json.loads(process.stdout.read()) == {"event": "ready", "character": "ivory"}
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


@requires_qt
@pytest.mark.parametrize("stage", ["loading", "event_loop", "queue_failure"])
def test_parent_eof_kills_child_even_if_loader_or_gui_stalls(stage):
    code = MODEL + """
import asyncio, sys, time
from redlotus.pets.desktop import PetApplication
from redlotus.pets import desktop
from redlotus.pets.factory import PetFactory
from PySide6.QtCore import QTimer
class BrokenQueue:
    @staticmethod
    def invokeMethod(*args): raise RuntimeError('Qt object has been deleted')
if STAGE == 'queue_failure': desktop.QMetaObject = BrokenQueue
def stall_gui():
    sys.stderr.write('gui-blocked\\n')
    sys.stderr.flush()
    time.sleep(30)
async def load(character):
    if STAGE == 'loading':
        sys.stderr.write('loader-started\\n')
        sys.stderr.flush()
        await asyncio.to_thread(time.sleep, 30)
    else:
        QTimer.singleShot(0, stall_gui)
    return ProbePet()
PetFactory.model = load
raise SystemExit(PetApplication.run(['charcoal']))
"""
    process = child(code.replace("STAGE", repr(stage)))
    try:
        expected = "loader-started\n" if stage == "loading" else "gui-blocked\n"
        while (line := read_line(process.stderr)) != expected:
            assert line, process.poll()
        started = time.monotonic()
        process.stdin.close()
        process.wait(timeout=5)
        assert 1.5 < time.monotonic() - started < 4
        assert process.returncode == 0
    finally:
        process.stdin = None
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


@requires_qt
def test_ready_child_exits_on_parent_eof():
    process = child(MODEL + """
from redlotus.pets.desktop import PetApplication
from redlotus.pets.factory import PetFactory
async def load(character): return ProbePet()
PetFactory.model = load
raise SystemExit(PetApplication.run(['charcoal']))
""")
    try:
        assert json.loads(read_line(process.stdout)) == {"event": "ready", "character": "charcoal"}
        started = time.monotonic()
        process.stdin.close()
        process.wait(timeout=5)
        assert process.returncode == 0, process.stderr.read()
        assert time.monotonic() - started < 1.5
    finally:
        process.stdin = None
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


@requires_qt
def test_close_before_readiness_does_not_enter_an_empty_event_loop():
    process = child(MODEL + """
from redlotus.pets import desktop
from redlotus.pets.factory import PetFactory
from PySide6.QtCore import QTimer
async def load(character): return ProbePet()
PetFactory.model = load
class ClosingWindow(desktop.PetWindow):
    def __init__(self, model):
        super().__init__(model)
        QTimer.singleShot(0, self.close)
desktop.PetWindow = ClosingWindow
raise SystemExit(desktop.PetApplication.run(['charcoal']))
""")
    try:
        process.wait(timeout=3)
        assert process.returncode == 0, process.stderr.read()
        assert not process.stdout.read(), "closed windows cannot acknowledge readiness"
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


@requires_qt
@pytest.mark.parametrize("character", ["charcoal", "ivory"])
def test_real_bundled_child_starts_without_reading_configuration(character):
    process = child("""
import runpy, sys
from redlotus.runtime import config, resources
def forbidden(*args, **kwargs): raise AssertionError('Desktop child initialized configuration')
config.settings = config._read_config = resources.settings = forbidden
sys.argv = ['redlotus.pets.desktop', CHARACTER]
try:
    runpy.run_module('redlotus.pets.desktop', run_name='__main__')
finally:
    assert not any(name.startswith(('redlotus.core', 'redlotus.TTS', 'redlotus.ui'))
                   for name in sys.modules)
""".replace("CHARACTER", repr(character)))
    try:
        assert json.loads(read_line(process.stdout)) == {"event": "ready", "character": character}
        process.stdin.close()
        process.wait(timeout=5)
        assert process.returncode == 0, process.stderr.read()
    finally:
        process.stdin = None
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


@requires_qt
@pytest.mark.parametrize("failure", ["character", "resources"])
def test_startup_errors_are_one_json_record_and_a_nonzero_exit(failure):
    process = child("""
from redlotus.pets.desktop import PetApplication
from redlotus.pets.factory import PetFactory
async def broken(character): raise ValueError('Broken sprite atlas')
if FAILURE == 'resources': PetFactory.model = broken
raise SystemExit(PetApplication.run(['missing' if FAILURE == 'character' else 'charcoal']))
""".replace("FAILURE", repr(failure)))
    try:
        process.wait(timeout=10)
        assert process.returncode == 1, process.stderr.read()
        report = json.loads(process.stdout.read())
        assert report["event"] == "error"
        assert ("missing" if failure == "character" else "Broken sprite atlas") in report["error"]
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)

@requires_qt
@pytest.mark.parametrize("dpr", ["1", "1.25", "1.5", "2"])
def test_scale_changes_physical_size_and_preserves_workarea(dpr):
    result = probe(WINDOW + """
import json
sizes = []
for scale in (.5, 1, 2, 3):
    window.set_scale(scale)
    image = window.grab().toImage()
    sizes.append([scale, image.width(), image.height(),
                  window.screen().availableGeometry().contains(window.geometry())])
print(json.dumps(sizes))
window.close()
""", scale=dpr)
    for scale, width, height, contained in result:
        assert scale * 100 - 2 <= width <= scale * 100
        assert width == height and contained


@requires_qt
def test_grip_resize_anchors_opposite_corner_and_emits_only_on_release():
    result = probe(WINDOW + POINTER + """
import json
def send(kind, global_pos, button, buttons):
    event = QMouseEvent(kind, QPointF(window.mapFromGlobal(global_pos)), QPointF(global_pos),
                        button, buttons, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(window, event)
area = window.screen().availableGeometry()
window._place(area.center() - QPoint(50, 50), window.screen())
anchor = window.geometry().topLeft()
start = window.mapToGlobal(window._grip_rect().center())
pointer = start
window._advance()
scales = []
window.scale_changed.connect(scales.append)
pet.events.clear()
left, none = Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton
send(QEvent.Type.MouseButtonPress, start, left, left)
send(QEvent.Type.MouseMove, start + QPoint(60, 60), none, left)
assert not scales
assert window.geometry().topLeft() == anchor
assert window.width() == 160
send(QEvent.Type.MouseButtonRelease, start + QPoint(500, 500), left, none)
start = window.mapToGlobal(window._grip_rect().center())
pointer = start
window._advance()
send(QEvent.Type.MouseButtonPress, start, left, left)
send(QEvent.Type.MouseMove, start - QPoint(30, 30), none, left)
assert scales == [1.6] and window.width() == 130
send(QEvent.Type.MouseButtonRelease, start - QPoint(500, 500), left, none)
print(json.dumps({'scales': scales, 'events': pet.events,
                  'released': type(window).mouseGrabber() is None}))
window.close()
""")
    assert result["scales"] == [1.6, 1.3]
    assert not any(event in result["events"] for event in ("primary_click", "drag_start", "drag_end"))
    assert result["released"]


@requires_qt
def test_edge_grip_faces_inward_and_keeps_corner_frozen_during_resize():
    result = probe(WINDOW + POINTER + """
import json
area = window.screen().availableGeometry()
window._place(area.bottomRight(), window.screen())
assert window._grip_rect().center().x() < window.width() / 2
assert window._grip_rect().center().y() < window.height() / 2
anchor = window.geometry().bottomRight()
start = window.mapToGlobal(window._grip_rect().center())
pointer = start
window._advance()
def send(kind, pos, button, buttons):
    QApplication.sendEvent(window, QMouseEvent(kind, QPointF(window.mapFromGlobal(pos)),
        QPointF(pos), button, buttons, Qt.KeyboardModifier.NoModifier))
left, none = Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton
send(QEvent.Type.MouseButtonPress, start, left, left)
send(QEvent.Type.MouseMove, start - QPoint(70, 70), none, left)
assert window.geometry().bottomRight() == anchor
assert window.width() == 170
send(QEvent.Type.MouseMove, start - QPoint(1000, 1000), none, left)
assert window.width() == 300
send(QEvent.Type.MouseButtonRelease, start, left, none)
print(json.dumps([window.scale, area.contains(window.geometry())]))
window.close()
""")
    assert result == [3.0, True]


@requires_qt
@pytest.mark.parametrize("character", ["charcoal", "ivory"])
def test_grip_survives_transparent_gap_without_native_hover(character):
    result = probe("""
import asyncio, json
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication
from redlotus.pets.factory import PetFactory
from redlotus.pets.desktop import PetWindow
app = QApplication([])
pet = asyncio.run(PetFactory.model(CHARACTER))
window = PetWindow(pet)
window.show()
app.processEvents()
window._timer.stop()
window._place(window.screen().availableGeometry().center() - QPoint(50, 50), window.screen())
""".replace("CHARACTER", repr(character)) + POINTER + """
source = window._pixmaps[window._frame_id].toImage()
body = next(QPoint(x, y) for y in range(100) for x in range(100)
            if source.pixelColor(x, y).alpha() == 255)
gap = QPoint(98, 50)
assert source.pixelColor(gap).alpha() == 0
for local in (body, gap, window._grip_rect().center()):
    pointer = window.mapToGlobal(local)
    window._advance()
    window.leaveEvent(QEvent(QEvent.Type.Leave))
    image = window.grab().toImage()
    assert image.pixelColor(window._grip_rect().center()).alpha() == 255, 'grip disappeared across transparent pixels'
assert not window._pointer_inside
origin, start = window.pos(), pointer
scales = []
window.scale_changed.connect(scales.append)
left, none = Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton
for kind, point, button, buttons in (
    (QEvent.Type.MouseButtonPress, start, left, left),
    (QEvent.Type.MouseMove, start + QPoint(40, 40), none, left),
    (QEvent.Type.MouseButtonRelease, start + QPoint(500, 500), left, none)):
    QApplication.sendEvent(window, QMouseEvent(kind, QPointF(window.mapFromGlobal(point)),
        QPointF(point), button, buttons, Qt.KeyboardModifier.NoModifier))
print(json.dumps([window.width(), window.pos() == origin, scales,
                  type(window).mouseGrabber() is None, pet.action]))
window.close()
""")
    assert result[:4] == [140, True, [1.4], True]
    assert result[4] not in ("happy", "drag")


@requires_qt
def test_grip_proximity_delay_reentry_and_capture_do_not_require_frame_changes():
    result = probe(WINDOW + POINTER + """
import json
clock = 100.
desktop.time = SimpleNamespace(monotonic=lambda: clock)
def tick(local, at):
    global pointer, clock
    pointer, clock = window.mapToGlobal(local), at
    window._advance()
def visible():
    pixel = window.grab().toImage().pixelColor(window._grip_rect().topLeft() + QPoint(3, 3))
    return pixel.red() > 230 and pixel.green() > 180 and pixel.blue() > 140
outside = QPoint(-13, 0)
tick(outside, 100.)
assert not visible()
tick(QPoint(-12, 0), 100.01)
assert visible(), 'approaching transparent window must show the grip'
tick(outside, 100.60)
assert visible()
tick(outside, 100.62)
assert not visible()
tick(QPoint(-12, 0), 101.)
tick(outside, 101.5)
tick(QPoint(-12, 0), 101.55)
tick(outside, 101.8)
assert visible(), 'reentry must cancel the pending hide'
tick(outside, 102.16)
assert not visible()
tick(window._grip_rect().center(), 103.)
assert window.cursor().shape() == Qt.CursorShape.SizeFDiagCursor
start = pointer
left, none = Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton
QApplication.sendEvent(window, QMouseEvent(QEvent.Type.MouseButtonPress,
    QPointF(window.mapFromGlobal(start)), QPointF(start), left, left, Qt.KeyboardModifier.NoModifier))
tick(outside, 110.)
assert visible(), 'capture must pin the grip after the hide deadline'
assert window.cursor().shape() == Qt.CursorShape.SizeFDiagCursor
QApplication.sendEvent(window, QMouseEvent(QEvent.Type.MouseButtonRelease,
    QPointF(outside), QPointF(pointer), left, none, Qt.KeyboardModifier.NoModifier))
tick(outside, 110.59)
assert visible()
tick(outside, 110.61)
assert not visible() and window.cursor().shape() == Qt.CursorShape.ArrowCursor
window.close()
print(json.dumps(not window._timer.isActive() and type(window).mouseGrabber() is None))
""")
    assert result


@requires_qt
@pytest.mark.parametrize("corner", [(0, 0), (1, 0), (0, 1), (1, 1)])
def test_grip_cursor_and_anchor_at_each_screen_corner_preserve_body_click(corner):
    result = probe(WINDOW + POINTER + """
import json
area = window.screen().availableGeometry()
right, bottom = CORNER
window._place(QPoint(area.right() if right else area.left(),
                     area.bottom() if bottom else area.top()), window.screen())
sx, sy = window._grip_corner()
assert (sx, sy) == (-1 if right else 1, -1 if bottom else 1)
pointer = start = window.mapToGlobal(window._grip_rect().center())
window._advance()
expected = Qt.CursorShape.SizeFDiagCursor if right == bottom else Qt.CursorShape.SizeBDiagCursor
assert window.cursor().shape() == expected
anchor = window.pos() + QPoint(window.width() - 1 if sx < 0 else 0,
                              window.height() - 1 if sy < 0 else 0)
def send(kind, point, button, buttons):
    QApplication.sendEvent(window, QMouseEvent(kind, QPointF(window.mapFromGlobal(point)),
        QPointF(point), button, buttons, Qt.KeyboardModifier.NoModifier))
left, none = Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton
send(QEvent.Type.MouseButtonPress, start, left, left)
send(QEvent.Type.MouseMove, start + QPoint(sx * 30, sy * 30), none, left)
assert window._grip_corner() == (sx, sy) and window.width() == 130
assert window.pos() + QPoint(window.width() - 1 if sx < 0 else 0,
                            window.height() - 1 if sy < 0 else 0) == anchor
send(QEvent.Type.MouseButtonRelease, start, left, none)
assert not any(event in pet.events for event in ('primary_click', 'drag_start', 'drag_end'))
pointer = window.mapToGlobal(window.rect().center())
window._advance()
assert window.cursor().shape() == Qt.CursorShape.ArrowCursor
send(QEvent.Type.MouseButtonPress, pointer, left, left)
send(QEvent.Type.MouseButtonRelease, pointer, left, none)
assert pet.events.count('primary_click') == 1
send(QEvent.Type.MouseButtonPress, pointer, left, left)
send(QEvent.Type.MouseMove, pointer + QPoint(sx * 40, sy * 40), none, left)
send(QEvent.Type.MouseButtonRelease, pointer, left, none)
assert pet.events.count('drag_start') == pet.events.count('drag_end') == 1
assert window.width() == 130 and pet.events.count('primary_click') == 1
print(json.dumps(area.contains(window.geometry())))
window.close()
""".replace("CORNER", repr(corner)))
    assert result


@requires_qt
@pytest.mark.parametrize("dpr", ["1", "1.25", "1.5", "2"])
@pytest.mark.parametrize("character", ["charcoal", "ivory"])
def test_grip_background_arrow_and_hover_across_sizes_and_dpi(character, dpr):
    result = probe("""
import asyncio, json, math
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication
from redlotus.pets.factory import PetFactory
from redlotus.pets.desktop import PetWindow
app = QApplication([])
window = PetWindow(asyncio.run(PetFactory.model(CHARACTER)))
window.show()
app.processEvents()
window._timer.stop()
""".replace("CHARACTER", repr(character)) + POINTER + """
sizes = []
for scale in (.5, 1., 1.72, 2., 3.):
    window.set_scale(scale)
    pointer = window.mapToGlobal(QPoint(-5, -5))
    window._advance()
    grip = window._grip_rect()
    assert grip.width() == grip.height() == min(22, window.width() // 2, window.height() // 2)
    assert window.rect().contains(grip)
    image = window.grab().toImage()
    ratio = window.devicePixelRatioF()
    x, y = (math.floor(value * ratio) for value in (grip.x(), grip.y()))
    side = math.floor(grip.width() * ratio)
    inset = math.ceil(3 * ratio)
    colors = [image.pixelColor(i, j) for j in range(y + inset, y + side - inset)
              for i in range(x + inset, x + side - inset)]
    assert all(color.alpha() == 255 for color in colors), 'button interior must accept native hits'
    assert any(color.red() > 230 and color.green() > 210 for color in colors), 'missing light background'
    assert any(color.red() < 100 and color.green() < 100 for color in colors), 'missing dark arrow'
    pointer = window.mapToGlobal(grip.center())
    window._advance()
    assert window.grab().toImage() != image, 'hover must highlight the button'
    assert window.cursor().shape() in (Qt.CursorShape.SizeFDiagCursor, Qt.CursorShape.SizeBDiagCursor)
    sx, sy = window._grip_corner()
    far_corner = QPoint(0 if sx > 0 else window.width() - 1, 0 if sy > 0 else window.height() - 1)
    sample = QPoint(math.floor(far_corner.x() * ratio), math.floor(far_corner.y() * ratio))
    assert image.pixelColor(sample).alpha() == 0, 'transparent pet area must stay transparent'
    sizes.append([image.width(), image.height()])
print(json.dumps(sizes))
window.close()
""", scale=dpr)
    for target, (width, height) in zip((50, 100, 172, 200, 300), result):
        assert target - 2 <= width <= target and width == height


@requires_qt
@pytest.mark.parametrize("dpr", ["1", "1.25", "1.5", "2"])
def test_sprite_enlargement_keeps_pixel_edges_and_reduction_stays_smooth(dpr):
    result = probe(MODEL + """
import json
from PySide6.QtWidgets import QApplication
from redlotus.pets.desktop import PetWindow
app = QApplication([])
pet = ProbePet()
pet.pixels['idle'] = b''.join(bytes([255, 0, 0, 255]) if x % 2
                            else bytes(4) for y in range(100) for x in range(100))
window = PetWindow(pet)
window.show()
app.processEvents()
window._timer.stop()
results = []
for scale in (.5, .75, 1., 1.72, 2., 3.):
    window.set_scale(scale)
    image = window.grab().toImage()
    colors = {image.pixelColor(x, image.height() // 2).getRgb() for x in range(image.width())}
    results.append([scale, any(0 < color[3] < 255 for color in colors),
                    colors.issubset({(255, 0, 0, 255), (0, 0, 0, 0)})])
print(json.dumps(results))
window.close()
""", scale=dpr)
    for scale, mixed, source_colors in result:
        assert mixed == (scale < 1)
        if scale >= 1:
            assert source_colors


@requires_qt
@pytest.mark.parametrize("phase", ["streaming", "done", "cancelled", "failed"])
def test_bubble_shows_body_beside_close_without_status_row(phase):
    result = probe(WINDOW + """
import json
from PySide6.QtWidgets import QLabel, QPushButton
bubble = window.bubble
body = 'Reply only'
bubble.apply_snapshot(dict(event='reply', reply_id='1', seq=1, phase=PHASE,
                           text=body, truncated=False))
app.processEvents()
assert bubble.text.toPlainText() == body
assert not [label.text() for label in bubble.findChildren(QLabel) if label.isVisible()], 'status row is still visible'
close, = bubble.findChildren(QPushButton)
assert close.text() == '×' and close.focusPolicy() == Qt.FocusPolicy.NoFocus
assert bubble.text.focusPolicy() == Qt.FocusPolicy.NoFocus
assert bubble.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
assert abs(bubble.text.y() - close.y()) <= 1, 'body must start beside close, not below an empty header'
assert bubble.text.geometry().right() < close.x()
assert bubble.height() <= 64 and bubble.width() == 280
assert bubble._timer.isActive() == (PHASE != 'streaming')
if PHASE != 'streaming':
    assert bubble._timer.interval() == 15000
    # Unix CoarseTimer deadlines may be coalesced by up to 5%.
    remaining = bubble._timer.remainingTime()
    assert 0 < remaining <= bubble._timer.interval() * 1.05, (
        f'remaining={remaining}, interval={bubble._timer.interval()}, type={bubble._timer.timerType()}')
    bubble.enterEvent(QEvent(QEvent.Type.Enter))
    assert not bubble._timer.isActive()
    bubble.leaveEvent(QEvent(QEvent.Type.Leave))
    assert bubble._timer.isActive()
close.click()
bubble.apply_snapshot(dict(event='reply', reply_id='1', seq=2, phase=PHASE,
                           text=body + ' later', truncated=False))
assert not bubble.isVisible()
bubble.apply_snapshot(dict(event='reply', reply_id='2', seq=3, phase='streaming',
                           text='First line\\nSecond line', truncated=False))
app.processEvents()
assert bubble.isVisible() and bubble.text.verticalScrollBar().maximum() == 0
print(json.dumps(True))
window.close()
""".replace("PHASE", repr(phase)))
    assert result


@requires_qt
def test_reply_is_plain_focusless_clamped_and_hidden_until_new_reply():
    result = probe(WINDOW + """
import json
bubble = window.bubble
def snap(reply, text, seq, phase='streaming', truncated=False):
    bubble.apply_snapshot(dict(event='reply', reply_id=reply, text=text,
                               seq=seq, phase=phase, truncated=truncated))
snap('a', '<b>literal</b> 中文', 1)
app.processEvents()
assert bubble.isVisible() and bubble.text.toPlainText() == '<b>literal</b> 中文'
assert bubble.width() == 280 and bubble.height() <= 180
assert bubble.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
assert bubble.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
assert bubble.screen().availableGeometry().contains(bubble.geometry())
bubble.close()
snap('a', 'still hidden', 2)
assert not bubble.isVisible()
snap('b', '', 3)
assert not bubble.isVisible()
snap('b', 'new reply', 4)
assert bubble.isVisible()
snap('old', 'stale', 2)
assert bubble.text.toPlainText() == 'new reply'
snap('', '', 5, 'clear')
print(json.dumps(not bubble.isVisible()))
window.close()
""")
    assert result


@requires_qt
def test_reply_autoscrolls_when_qt_reports_scroll_range_after_show():
    result = probe(WINDOW + """
import json
from PySide6.QtWidgets import QAbstractSlider
bubble = window.bubble
bubble.apply_snapshot(dict(event='reply', reply_id='a', seq=1, phase='streaming',
                           text='\\n\\n'.join(str(i) for i in range(100)), truncated=False))
app.processEvents()
bar = bubble.text.verticalScrollBar()
end = bar.maximum()
assert end > 0
bar.setRange(0, 0)
bar.setRange(0, end)
followed = [bar.value(), bar.maximum()]
bar.triggerAction(QAbstractSlider.SliderAction.SliderToMinimum)
bar.setRange(0, end + 20)
scrolled = [bar.value(), bar.maximum()]
bubble.apply_snapshot(dict(event='reply', reply_id='', seq=2, phase='clear',
                           text='', truncated=False))
bubble.apply_snapshot(dict(event='reply', reply_id='b', seq=3, phase='streaming',
                           text='\\n\\n'.join(str(i) for i in range(100)), truncated=False))
app.processEvents()
new_end = bar.maximum()
bar.setRange(0, 0)
bar.setRange(0, new_end)
print(json.dumps([followed, scrolled, [bar.value(), bar.maximum()]]))
window.close()
""")
    assert result[0][0] == result[0][1], f"late scroll range left the fresh reply at {result[0]}"
    assert result[1][0] == 0, f"later scroll range overrode the user's scroll: {result[1]}"
    assert result[2][0] == result[2][1] > 0, f"new reply did not follow after clear: {result[2]}"


@requires_qt
def test_reply_scroll_and_truncation_are_bounded_and_terminal_hover_pauses():
    result = probe(WINDOW + """
import json, time
bubble = window.bubble
def snap(text, seq, phase='streaming', truncated=False):
    bubble.apply_snapshot(dict(event='reply', reply_id='a', text=text,
                               seq=seq, phase=phase, truncated=truncated))
text = '\\n\\n'.join(str(i) for i in range(100))
snap(text, 1)
app.processEvents()
bar = bubble.text.verticalScrollBar()
assert bar.value() == bar.maximum() and bar.maximum() > 0, (
    f'scroll={bar.value()}/{bar.maximum()}, bubble={bubble.width()}x{bubble.height()}, '
    f'text={bubble.text.width()}x{bubble.text.height()}, document={bubble.text.document().size()}')
assert bubble.height() == 180 and bubble.text.height() == 152
bar.setValue(12)
snap(text + '\\n\\nlast', 2)
assert bar.value() == 12
snap('old prefix' + 'x' * 32767 + '尾', 3, 'done')
assert bubble.text.toPlainText() == 'x' * 32767 + '尾'
assert bubble.banner.isVisible()
assert bubble.banner.text() == '较早内容请在终端查看'
from PySide6.QtWidgets import QPushButton
close = bubble.findChild(QPushButton)
assert bubble.height() <= 180 and bubble.text.geometry().right() < close.x()
assert bubble.text.y() > bubble.banner.y() and close.y() == 12
assert bubble._timer.isActive() and bubble._timer.interval() == 15000
started = bubble._timer.remainingTime()
assert 0 < started <= bubble._timer.interval() * 1.05, (
    f'remaining={started}, interval={bubble._timer.interval()}, type={bubble._timer.timerType()}')
bubble.enterEvent(QEvent(QEvent.Type.Enter))
remaining = bubble._remaining
assert not bubble._timer.isActive() and 0 < remaining <= started
time.sleep(.03)
assert bubble._remaining == remaining and not bubble._timer.isActive()
bubble.leaveEvent(QEvent(QEvent.Type.Leave))
assert bubble._timer.isActive() and bubble._timer.interval() == remaining
resumed = bubble._timer.remainingTime()
assert 0 < resumed <= remaining * 1.05, (
    f'remaining={resumed}, interval={bubble._timer.interval()}, type={bubble._timer.timerType()}')
bubble._timer.setInterval(1)
from PySide6.QtTest import QTest
QTest.qWait(20)
print(json.dumps(not bubble.isVisible()))
window.close()
""")
    assert result


@requires_qt
def test_bubble_follows_pet_and_uses_available_side():
    result = probe(WINDOW + """
import json
bubble = window.bubble
bubble.apply_snapshot(dict(event='reply', reply_id='a', text='hello',
                           seq=1, phase='streaming', truncated=False))
area = window.screen().availableGeometry()
window._place(area.topLeft(), window.screen())
assert bubble.geometry().top() >= window.geometry().bottom()
first = bubble.pos()
window._place(area.bottomRight(), window.screen())
assert bubble.geometry().bottom() <= window.geometry().top()
assert bubble.pos() != first
assert area.contains(bubble.geometry())
window.close()
print(json.dumps(not bubble.isVisible()))
""")
    assert result


@requires_qt
@pytest.mark.parametrize("side", ["left", "right"])
def test_side_bubble_tail_points_toward_pet(side):
    result = probe(WINDOW + """
import json
from redlotus.pets.desktop import OverlayWindow
bubble = window.bubble
assert isinstance(window, OverlayWindow) and isinstance(bubble, OverlayWindow)
bubble.apply_snapshot(dict(event='reply', reply_id='a', text='hello',
                           seq=1, phase='streaming', truncated=False))
bubble._tail = SIDE
image = bubble.grab().toImage()
y = max(16, min(bubble.height() - 16, window.geometry().center().y() - bubble.y()))
x = 1 if SIDE == 'left' else bubble.width() - 2
print(json.dumps(image.pixelColor(x, y).alpha() > 0))
window.close()
""".replace("SIDE", repr(side)))
    assert result


@requires_qt
def test_application_restores_scale_and_writes_only_released_grip_changes():
    process = child(MODEL + """
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication
from redlotus.pets import desktop
from redlotus.pets.factory import PetFactory
async def load(character): return ProbePet()
PetFactory.model = load
class ResizingWindow(desktop.PetWindow):
    def __init__(self, pet):
        super().__init__(pet)
        QTimer.singleShot(100, self.resize_probe)
    def resize_probe(self):
        assert self.scale == 1.5 and self.width() == 150
        start = self.mapToGlobal(self._grip_rect().center())
        from types import SimpleNamespace
        desktop.QCursor = SimpleNamespace(pos=lambda: start)
        self._advance()
        sx, sy = self._grip_corner()
        def send(kind, pos, button, buttons):
            QApplication.sendEvent(self, QMouseEvent(kind, QPointF(self.mapFromGlobal(pos)),
                QPointF(pos), button, buttons, Qt.KeyboardModifier.NoModifier))
        left, none = Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton
        send(QEvent.Type.MouseButtonPress, start, left, left)
        send(QEvent.Type.MouseMove, start + QPoint(sx * 20, sy * 20), none, left)
        send(QEvent.Type.MouseButtonRelease, start, left, none)
        self.close()
desktop.PetWindow = ResizingWindow
raise SystemExit(desktop.PetApplication.run(['ivory', '--scale', '1.5']))
""")
    try:
        process.wait(timeout=10)
        assert process.returncode == 0, process.stderr.read()
        assert [json.loads(line) for line in process.stdout.readlines()] == [
            {"event": "ready", "character": "ivory"}, {"event": "scale", "scale": 1.7}]
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


@requires_qt
@pytest.mark.parametrize("stage", ["loading", "live"])
def test_parent_pipe_coalesces_utf8_snapshots_and_ignores_bad_frames(stage):
    process = child(MODEL + """
import asyncio, json, sys, threading
from PySide6.QtCore import QTimer
from redlotus.pets import desktop
from redlotus.pets.factory import PetFactory
parse = json.loads
def guarded_parse(value):
    if value == 'recursion-error': raise RecursionError('injected parser limit')
    return parse(value)
desktop.json.loads = guarded_parse
async def load(character):
    print('loading', file=sys.stderr, flush=True)
    await asyncio.sleep(.5)
    return ProbePet()
PetFactory.model = load
class ReportingWindow(desktop.PetWindow):
    def __init__(self, pet):
        super().__init__(pet)
        self.deliveries = []
        original = self.bubble.apply_snapshot
        def receive(snapshot):
            self.deliveries.append(threading.current_thread() is threading.main_thread())
            original(snapshot)
        self.bubble.apply_snapshot = receive
        QTimer.singleShot(300, self.report)
    def report(self):
        print('probe ' + json.dumps({'text': self.bubble.text.toPlainText(),
              'deliveries': self.deliveries, 'seq': self.bubble._seq}), file=sys.stderr, flush=True)
        self.close()
desktop.PetWindow = ReportingWindow
raise SystemExit(desktop.PetApplication.run(['charcoal']))
""")
    try:
        assert read_line(process.stderr) == "loading\n"
        ready = json.loads(read_line(process.stdout)) if stage == "live" else None
        wire = b'not-json\n' + b'x' * 262145 + b'\n' + b'\xff\n' + b'recursion-error\n'
        for seq in range(100):
            wire += (json.dumps(dict(event="reply", reply_id="a", phase="streaming",
                                     text=f"中文快照 {seq}", truncated=False, seq=seq),
                               ensure_ascii=False) + "\n").encode("utf-8")
        wire += b'{"event":"reply","reply_id":"old","phase":"done","text":"stale","truncated":false,"seq":2}\n'
        wire += b'{"event":"reply","reply_id":"bad","phase":"bogus","text":"bad","truncated":false,"seq":999}\n'
        process.stdin.buffer.write(wire)
        process.stdin.buffer.flush()
        process.wait(timeout=10)
        assert process.returncode == 0
        records = [line[6:] for line in process.stderr.readlines() if line.startswith("probe ")]
        report = json.loads(records[0])
        assert report["text"] == "中文快照 99" and report["seq"] == 99
        assert report["deliveries"] and all(report["deliveries"])
        if stage == "loading":
            assert report["deliveries"] == [True]
        assert (ready or json.loads(process.stdout.read())) == {"event": "ready", "character": "charcoal"}
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


PIPE = """
import json, os, threading, time
from redlotus.pets.desktop import ParentPipe
source_read, source_write = os.pipe()
sink_read, sink_write = os.pipe()
source = os.fdopen(source_read, 'rb', buffering=0)
sink = os.fdopen(sink_write, 'wb', buffering=0)
parent = ParentPipe(source, sink)
os.set_blocking(sink_read, False)
os.set_blocking(sink_write, False)
filled = 0
while True:
    try:
        count = os.write(sink_write, b'x' * 4096)
    except BlockingIOError:
        break
    if not count: break
    filled += count
parent.start()
assert not parent._reader.daemon and not parent._writer.daemon
"""


@requires_qt
def test_writer_keeps_latest_pending_scale_and_finish_joins_both_threads():
    result = probe(PIPE + """
parent.send(dict(event='ready', character='charcoal'))
deadline = time.monotonic() + 1
while 'ready' in parent._output and time.monotonic() < deadline: time.sleep(.001)
began = time.monotonic()
with parent._lock:
    for index in range(10000): parent.send(dict(event='scale', scale=index / 10000))
elapsed = time.monotonic() - began
while filled:
    filled -= len(os.read(sink_read, min(filled, 4096)))
parent.finish()
assert not parent._reader.is_alive() and not parent._writer.is_alive()
assert parent.finished.is_set()
data = b''
while True:
    try: chunk = os.read(sink_read, 4096)
    except BlockingIOError: break
    if not chunk: break
    data += chunk
source.close()
sink.close()
os.close(source_write)
os.close(sink_read)
print(json.dumps({'elapsed': elapsed, 'lines': [json.loads(line) for line in data.splitlines()]}))
""")
    assert result["elapsed"] < .5
    assert result["lines"] == [{"event": "ready", "character": "charcoal"},
                               {"event": "scale", "scale": .9999}]


@requires_qt
def test_finish_stops_idle_reader_and_writer_with_a_full_output_pipe():
    result = probe(PIPE + """
parent.send(dict(event='ready', character='charcoal'))
began = time.monotonic()
parent.finish()
elapsed = time.monotonic() - began
assert not parent._reader.is_alive() and not parent._writer.is_alive()
assert parent.finished.is_set()
source.close()
sink.close()
os.close(source_write)
os.close(sink_read)
print(json.dumps(elapsed))
""")
    assert result < 1.5


@requires_qt
def test_pipe_retains_recent_reply_text_and_marks_truncation():
    result = probe(PIPE + """
parent._receive(json.dumps(dict(event='reply', reply_id='a', phase='streaming',
    seq=1, text='older prefix' + 'x' * 32767 + '尾', truncated=False)).encode('utf-8'))
snapshot = parent._pending
parent.finish()
assert not parent._reader.is_alive() and not parent._writer.is_alive()
source.close()
sink.close()
os.close(source_write)
os.close(sink_read)
print(json.dumps([snapshot['text'] == 'x' * 32767 + '尾', snapshot['truncated']]))
""")
    assert result == [True, True]


def test_standalone_file_descriptors_skip_parent_monitoring_and_flush_output():
    result = probe("""
import json, tempfile
from redlotus.pets.desktop import ParentPipe
with tempfile.TemporaryFile() as source, tempfile.TemporaryFile() as output:
    parent = ParentPipe(source, output)
    parent.start()
    parent.send(dict(event='ready', character='charcoal'))
    parent.finish()
    assert not parent.closed.is_set()
    assert not parent._reader.is_alive() and not parent._writer.is_alive()
    output.seek(0)
    print(output.read().decode('utf-8'), end='')
""")
    assert result == {"event": "ready", "character": "charcoal"}


@requires_qt
def test_terminal_reply_after_empty_start_gets_its_own_hide_timer():
    result = probe(WINDOW + """
import json
bubble = window.bubble
def snap(reply, text, phase, seq):
    bubble.apply_snapshot(dict(event='reply', reply_id=reply, text=text,
                               seq=seq, phase=phase, truncated=False))
snap('first', 'old', 'done', 1)
bubble.close()
snap('second', '', 'streaming', 2)
snap('second', 'new', 'done', 3)
print(json.dumps(bubble.isVisible() and bubble._timer.isActive()))
window.close()
""")
    assert result


@requires_qt
@pytest.mark.parametrize("body", ["first line\nsecond line\nthird line",
    "A short reply wraps inside the bubble and stays visible."])
def test_first_bubble_grows_to_fit_styled_text_before_needing_scroll(body):
    result = probe(MODEL + """
import json, math
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication
from redlotus.pets.desktop import PetWindow
app = QApplication([])
font = QFont()
font.setPixelSize(8)
app.setFont(font)
window = PetWindow(ProbePet())
bubble = window.bubble
bubble.apply_snapshot(dict(event='reply', reply_id='1', seq=1, phase='done',
    text=BODY, truncated=False))
app.processEvents()
print(json.dumps([bubble.text.height(), math.ceil(bubble.text.document().size().height()),
                  bubble.text.verticalScrollBar().maximum(), bubble.height()]))
window.close()
""".replace("BODY", repr(body)))
    assert result[0] >= result[1]
    assert result[2] == 0 and result[3] <= 180
