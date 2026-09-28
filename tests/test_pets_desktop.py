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


def child(code, *, scale="1"):
    env = os.environ | {"PYTHONPATH": str(resource_root().parent), "QT_QPA_PLATFORM": "offscreen",
                        "QT_SCALE_FACTOR": scale}
    return subprocess.Popen([sys.executable, "-u", "-c", textwrap.dedent(code)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=env, cwd=ROOT)


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
import sys
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
PetFactory.model = broken
raise SystemExit(PetApplication.run(['missing' if FAILURE == 'character' else 'charcoal']))
""".replace("FAILURE", repr(failure)))
    try:
        process.wait(timeout=10)
        assert process.returncode == 1, process.stderr.read()
        report = json.loads(process.stdout.read())
        assert report["event"] == "error"
        assert ("charcoal or ivory" if failure == "character" else "Broken sprite atlas") in report["error"]
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
