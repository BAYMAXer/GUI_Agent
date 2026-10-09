"""Monitor and input invariants using fake Win32 APIs; never inject desktop input."""
import ctypes
from ctypes import wintypes as wt
from types import SimpleNamespace
import sys

import pytest

from osworld_agent.adapters import windows_native as module
from osworld_agent.adapters.windows_native import WindowsNative, desktop_point


DISPLAY1, DISPLAY2 = r"\\.\DISPLAY1", r"\\.\DISPLAY2"


class Function:
    """A mock callable with ctypes signature attributes."""
    def __init__(self, callback=lambda *args: 1):
        self.callback = callback
    def __call__(self, *args):
        return self.callback(*args)


class User:
    def __init__(self):
        self.displays = [
            {"handle": 1, "device": DISPLAY1, "primary": False,
             "rect": (-400, -100, 0, 200), "work": (-400, -100, 0, 180)},
            {"handle": 2, "device": DISPLAY2, "primary": True,
             "rect": (0, 0, 600, 400), "work": (0, 0, 600, 380)},
        ]
        self.inputs = []
        self.accepted = []
        self.foreground = "10"
        self.cursor = (10, 10)
        self.windows = {10: (20, 20, 300, 250), 11: (30, 30, 200, 200), 20: (-300, -50, -50, 150)}
        self.owners = {11: 10}
        self.maximized = False
        self.minimized = False
        self.activated = []
        self.activation_success = True
        self.positions = []
        implementations = {
            "GetSystemMetrics": self.metrics, "EnumDisplayMonitors": self.enumerate,
            "GetMonitorInfoW": self.monitor_info, "SendInput": self.send,
            "GetWindowThreadProcessId": self.process, "IsWindow": lambda hwnd: hwnd in self.windows,
            "GetWindowRect": self.window_rect, "GetDpiForWindow": lambda hwnd: 144,
            "IsZoomed": lambda hwnd: self.maximized, "IsIconic": lambda hwnd: self.minimized,
            "MonitorFromWindow": lambda hwnd, flags: 2, "GetWindow": lambda hwnd, kind: self.owners.get(hwnd, 0),
            "SetForegroundWindow": self.activate, "GetCursorPos": self.cursor_pos,
            "WindowFromPoint": lambda point: 10, "GetAncestor": lambda hwnd, kind: hwnd,
            "GetClientRect": self.client_rect, "ClientToScreen": lambda *args: True,
            "SetWindowPos": self.position, "GetClassNameW": self.window_class,
        }
        for name in ("SetProcessDpiAwarenessContext", "SetThreadDpiAwarenessContext", "GetSystemMetrics",
                     "SendInput", "SetForegroundWindow", "ShowWindow", "IsWindow", "IsZoomed", "IsIconic",
                     "IsWindowVisible", "GetWindowThreadProcessId", "GetWindow", "GetWindowTextLengthW",
                     "GetWindowRect", "GetDpiForWindow", "GetCursorPos", "WindowFromPoint", "GetAncestor",
                     "GetClassNameW",
                     "GetClientRect", "ClientToScreen", "SetWindowPos", "MonitorFromWindow",
                     "EnumDisplayMonitors", "GetMonitorInfoW"):
            setattr(self, name, Function(implementations.get(name, lambda *args: 1)))

    def metrics(self, index):
        rectangles = [d["rect"] for d in self.displays]
        left, top = min(r[0] for r in rectangles), min(r[1] for r in rectangles)
        right, bottom = max(r[2] for r in rectangles), max(r[3] for r in rectangles)
        return {76: left, 77: top, 78: right - left, 79: bottom - top}[index]

    def enumerate(self, hdc, clip, callback, data):
        return all(callback(d["handle"], None, None, data) for d in self.displays)

    def monitor_info(self, handle, pointer):
        display = next(d for d in self.displays if d["handle"] == handle)
        info = pointer._obj
        info.rcMonitor = wt.RECT(*display["rect"])
        info.rcWork = wt.RECT(*display["work"])
        info.dwFlags = int(display["primary"])
        info.szDevice = display["device"]
        return True

    def send(self, count, events, size):
        accepted = self.accepted.pop(0) if self.accepted else count
        self.inputs.append([{"type": e.type, "flags": e.mi.dwFlags if e.type == 0 else e.ki.dwFlags,
                             "x": e.mi.dx if e.type == 0 else e.ki.wVk,
                             "y": e.mi.dy if e.type == 0 else e.ki.wScan}
                            for e in events[:count]])
        return accepted

    def process(self, hwnd, pointer):
        pointer._obj.value = 100
        return 1

    def window_rect(self, hwnd, pointer):
        pointer._obj.left, pointer._obj.top, pointer._obj.right, pointer._obj.bottom = self.windows[hwnd]
        return True

    def client_rect(self, hwnd, pointer):
        pointer._obj.right, pointer._obj.bottom = 100, 100
        return True

    def cursor_pos(self, pointer):
        pointer._obj.x, pointer._obj.y = self.cursor
        return True

    def activate(self, hwnd):
        self.activated.append(hwnd)
        if self.activation_success:
            self.foreground = str(hwnd)
        return self.activation_success

    def position(self, hwnd, after, x, y, width, height, flags):
        self.positions.append((hwnd, x, y, width, height, flags))
        self.windows[hwnd] = (x, y, x + width, y + height)
        return True

    def window_class(self, hwnd, buffer, size):
        buffer.value = "Shell_TrayWnd"
        return len(buffer.value)


@pytest.fixture
def native_factory(monkeypatch):
    user = User()
    monkeypatch.setattr(module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(module.ctypes, "WINFUNCTYPE", getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE), raising=False)
    dwm = SimpleNamespace(DwmGetWindowAttribute=Function(lambda *args: -1))
    monkeypatch.setattr(module.ctypes, "WinDLL", lambda name, **kwargs: user if name == "user32" else dwm, raising=False)
    monkeypatch.setattr(module, "FocusTracker", lambda: SimpleNamespace(
        probe=lambda: SimpleNamespace(window_id=user.foreground), close=lambda: None))
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    return lambda monitor="primary": (WindowsNative(monitor), user)


def test_primary_uses_win32_flag_and_retains_virtual_geometry(native_factory):
    native, user = native_factory()
    geometry = native.geometry()
    assert geometry["target_monitor"] == DISPLAY2
    assert geometry["capture_geometry"] == {"left": 0, "top": 0, "width": 600, "height": 400}
    assert [geometry[k] for k in ("left", "top", "width", "height")] == [-400, -100, 1000, 500]
    assert geometry["monitors"][1]["primary"]
    assert geometry["monitors"][1]["work_area"]["height"] == 380
    assert user.WindowFromPoint.restype == wt.HWND
    assert user.EnumDisplayMonitors.restype == wt.BOOL


def test_device_pinned_across_reorder_and_primary_change(native_factory):
    native, user = native_factory()
    before = native.geometry()
    user.displays.reverse()
    assert native.geometry() == before
    for display in user.displays:
        display["primary"] = not display["primary"]
    assert native.geometry()["target_monitor"] == DISPLAY2
    user.displays = [d for d in user.displays if d["device"] != DISPLAY2]
    with pytest.raises(RuntimeError, match="Selected monitor unavailable"):
        native.geometry()


def test_explicit_device_case_insensitive_and_invalid_selection(native_factory):
    native, user = native_factory(DISPLAY1.lower())
    assert native.geometry()["target_monitor"] == DISPLAY1
    with pytest.raises(RuntimeError, match="Selected monitor unavailable"):
        native_factory(r"\\.\DISPLAY404")


@pytest.mark.parametrize("rectangle", [(-400, -100, 0, 200), (0, -300, 400, 0), (600, 0, 1000, 300)])
def test_capture_grabs_only_target_and_maps_its_origin(native_factory, monkeypatch, rectangle):
    native, user = native_factory(DISPLAY1)
    user.displays[0]["rect"] = rectangle
    user.displays[0]["work"] = rectangle
    grabbed = []
    class Capture:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def grab(self, region):
            grabbed.append(dict(region))
            size = region["width"], region["height"]
            return SimpleNamespace(size=size, rgb=b"\0" * (size[0] * size[1] * 3))
    monkeypatch.setitem(sys.modules, "mss", SimpleNamespace(MSS=Capture))
    image, geometry = native.capture()
    assert grabbed == [geometry["capture_geometry"]]
    assert image.size == (rectangle[2] - rectangle[0], rectangle[3] - rectangle[1])
    assert desktop_point(5, 6, geometry) == (rectangle[0] + 5, rectangle[1] + 6)
    with pytest.raises(ValueError):
        desktop_point(image.width, 0, geometry)


def test_move_normalizes_to_full_virtual_desktop(native_factory):
    native, user = native_factory(DISPLAY1)
    geometry = native.geometry()
    native.move((-200, 0), geometry)
    event = user.inputs[0][0]
    assert event["x"] == round(200 * 65535 / 999)
    assert event["y"] == round(100 * 65535 / 499)
    assert event["flags"] == 0xC001


@pytest.mark.parametrize("operation", ["move", "drag", "scroll"])
def test_outside_selected_monitor_never_dispatches(native_factory, operation):
    native, user = native_factory(DISPLAY1)
    geometry = native.geometry()
    with pytest.raises(ValueError, match="selected monitor"):
        if operation == "move":
            native.move((10, 10), geometry)
        elif operation == "drag":
            native.drag((-200, 0), (10, 10), geometry)
        else:
            user.cursor = (10, 10)
            native.scroll(1, "down", window_id="10", geometry=geometry)
    assert user.inputs == []


def reject_after(native, limit):
    calls = []
    def guard():
        calls.append(True)
        if len(calls) > limit:
            raise RuntimeError("focus stolen")
    native.input_guard = guard
    return calls


def test_double_click_rechecks_before_second_click(native_factory):
    native, user = native_factory()
    calls = reject_after(native, 2)
    with pytest.raises(RuntimeError, match="focus stolen"):
        native.click((30, 30), native.geometry(), count=2)
    assert len(calls) == 3 and len(user.inputs) == 2
    assert [e["flags"] for e in user.inputs[-1]] == [2, 4]


def test_after_click_snapshot_accepts_intentional_focus_and_rejects_later_away_back(native_factory, monkeypatch):
    native, user = native_factory()
    epochs = {"actual": 0, "expected": 0}
    snapshots = []
    def guard():
        if epochs["actual"] != epochs["expected"]:
            raise RuntimeError("focus changed since accepted click")
    def send(count, events, size):
        accepted = user.send(count, events, size)
        if any(event.type == 0 and event.mi.dwFlags == 2 for event in events[:count]):
            epochs["actual"] += 1  # The requested click changed child focus.
        return accepted
    def after_click():
        epochs["expected"] = epochs["actual"]
        snapshots.append(epochs["expected"])
    def interclick_sleep(seconds):
        assert snapshots == [1]
        epochs["actual"] += 2  # Switched away and back after the post-click snapshot.
    native.input_guard = guard
    user.SendInput.callback = send
    monkeypatch.setattr(module.time, "sleep", interclick_sleep)
    with pytest.raises(RuntimeError, match="accepted click"):
        native.click((30, 30), native.geometry(), count=2, after_click=after_click)
    assert snapshots == [1] and len(user.inputs) == 2


def test_foreground_epoch_ignores_control_focus_changes(monkeypatch):
    from osworld_agent.scene import Foreground
    tracker = module.FocusTracker.__new__(module.FocusTracker)
    tracker._lock = module.threading.Lock()
    tracker._epoch = tracker._foreground_epoch = 0
    monkeypatch.setattr(module, "probe_foreground", lambda os_name: Foreground(available=True, window_id="10"))
    tracker._record_event(3)
    tracker._record_event(0x8005)
    tracker._record_event(6)
    focus = tracker.probe()
    assert focus.generation == 3 and focus.foreground_generation == 1
    tracker._record_event(3)
    tracker._record_event(3)
    assert tracker.probe().foreground_generation == 3


def test_drag_interruption_always_releases_without_guard(native_factory):
    native, user = native_factory()
    calls = reject_after(native, 2)
    with pytest.raises(RuntimeError, match="focus stolen"):
        native.drag((30, 30), (100, 100), native.geometry())
    assert len(calls) == 3 and len(user.inputs) == 3
    assert user.inputs[-1][0]["flags"] == 4


def test_drag_callback_snapshots_every_accepted_down_and_movement(native_factory):
    native, user = native_factory()
    callbacks = []
    native.drag((30, 30), (100, 100), native.geometry(),
                after_input=lambda: callbacks.append(len(user.inputs)))
    assert callbacks == list(range(2, 15))  # down, then twelve path points; no callback for cleanup up
    assert len(user.inputs) == 15 and user.inputs[-1][0]["flags"] == 4


@pytest.mark.parametrize("interrupt_batch", [2, 3, 7])
def test_drag_callback_interruption_releases_and_stops_remaining_path(native_factory, interrupt_batch):
    native, user = native_factory()
    def after_input():
        if len(user.inputs) == interrupt_batch:
            raise RuntimeError("original window identity or foreground epoch changed")
    with pytest.raises(RuntimeError, match="identity or foreground epoch"):
        native.drag((30, 30), (100, 100), native.geometry(), after_input=after_input)
    assert len(user.inputs) == interrupt_batch + 1
    assert user.inputs[-1][0]["flags"] == 4 and native._held_inputs == {}


def test_guard_rejection_before_drag_down_does_not_release(native_factory):
    native, user = native_factory()
    reject_after(native, 1)
    with pytest.raises(RuntimeError, match="focus stolen"):
        native.drag((30, 30), (100, 100), native.geometry())
    assert len(user.inputs) == 1 and user.inputs[0][0]["flags"] == 0xC001


def test_text_batches_recheck_native_guard(native_factory):
    native, user = native_factory()
    reject_after(native, 1)
    with pytest.raises(RuntimeError, match="focus stolen"):
        native.type_text("中" * 100)
    assert len(user.inputs) == 1 and len(user.inputs[0]) == 128


@pytest.mark.parametrize("accepted", [0, 1, 2])
def test_partial_hotkey_releases_only_actually_pressed_keys(native_factory, accepted):
    native, user = native_factory()
    calls = reject_after(native, 1)
    user.accepted = [accepted]
    with pytest.raises(RuntimeError, match="partially dispatched"):
        native.hotkey(["ctrl", "a"])
    assert len(calls) == 1
    assert len(user.inputs) == (2 if accepted else 1)
    if accepted:
        assert [e["x"] for e in user.inputs[1]] == ([0x11] if accepted == 1 else [0x41, 0x11])
        assert all(e["flags"] & 2 for e in user.inputs[1])


def test_guard_rejection_does_not_increment_dispatch_count_or_release(native_factory):
    native, user = native_factory()
    reject_after(native, 0)
    with pytest.raises(RuntimeError, match="focus stolen"):
        native.hotkey(["ctrl", "a"])
    assert native.input_dispatch_count == 0 and user.inputs == []


def test_close_releases_only_held_inputs_and_bypasses_guard(native_factory):
    native, user = native_factory()
    native._send([native._mouse(2), native._key(0x11)])
    reject_after(native, 0)
    native.close()
    assert [(e["type"], e["flags"]) for e in user.inputs[-1]] == [(1, 2), (0, 4)]
    assert native._held_inputs == {}
    count = len(user.inputs)
    native.close()
    assert len(user.inputs) == count


def test_zero_dispatch_failure_has_no_close_cleanup(native_factory):
    native, user = native_factory()
    user.accepted = [0]
    with pytest.raises(RuntimeError, match="partially dispatched"):
        native.hotkey(["ctrl", "a"])
    native.close()
    assert len(user.inputs) == 1


@pytest.mark.parametrize("succeeds", [False, True])
def test_recovery_activation_never_injects_alt(native_factory, monkeypatch, succeeds):
    native, user = native_factory()
    user.foreground = "20"
    user.activation_success = succeeds
    ticks = iter([0, 1])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks, 1))
    if succeeds:
        native.activate("10", allow_input_fallback=False)
    else:
        with pytest.raises(RuntimeError, match="refused"):
            native.activate("10", allow_input_fallback=False)
    assert user.activated == [10] and user.inputs == []


def test_activation_waits_for_foreground_hook_epoch_after_hwnd_switch(native_factory):
    native, user = native_factory()
    samples = [SimpleNamespace(window_id="20", process_id=100, foreground_generation=3),
               SimpleNamespace(window_id="10", process_id=100, foreground_generation=3),
               SimpleNamespace(window_id="10", process_id=100, foreground_generation=4)]
    probed = []
    def probe():
        probed.append(samples.pop(0))
        return probed[-1]
    native.tracker.probe = probe
    native.activate("10", allow_input_fallback=False)
    assert len(probed) == 3
    assert probed[-1].foreground_generation == 4 and user.inputs == []


def test_activation_does_not_wait_for_new_event_when_target_already_foreground(native_factory):
    native, user = native_factory()
    probed = []
    def probe():
        probed.append(True)
        return SimpleNamespace(window_id="10", process_id=100, foreground_generation=3)
    native.tracker.probe = probe
    native.activate("10", allow_input_fallback=False)
    assert len(probed) == 2 and user.inputs == []


def test_activation_missing_hook_event_fails_without_recording_stale_epoch(native_factory, monkeypatch):
    native, user = native_factory()
    snapshots = iter([SimpleNamespace(window_id="20", process_id=100, foreground_generation=3),
                      SimpleNamespace(window_id="10", process_id=100, foreground_generation=3)])
    last = []
    def probe():
        last.append(next(snapshots, last[-1] if last else None))
        return last[-1]
    native.tracker.probe = probe
    ticks = iter([0, 1])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks, 1))
    with pytest.raises(RuntimeError, match="event did not synchronize"):
        native.activate("10", allow_input_fallback=False)
    assert user.inputs == []


def test_activation_validates_original_hwnd_pid_after_hook_event(native_factory, monkeypatch):
    native, user = native_factory()
    snapshots = iter([SimpleNamespace(window_id="20", process_id=100, foreground_generation=3),
                      SimpleNamespace(window_id="10", process_id=777, foreground_generation=4)])
    last = []
    def probe():
        last.append(next(snapshots, last[-1] if last else None))
        return last[-1]
    native.tracker.probe = probe
    ticks = iter([0, 1])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks, 1))
    with pytest.raises(RuntimeError, match="refused"):
        native.activate("10", allow_input_fallback=False)
    assert user.inputs == []


def test_hwnd_pid_root_and_owned_dialog_identity(native_factory):
    native, user = native_factory()
    assert native.window_process("10") == 100
    assert native.window_at_point((30, 30)) == "10"
    assert native.window_class("10") == "Shell_TrayWnd"
    assert native.related_window("11", "10")
    assert not native.related_window("20", "10")
    del user.windows[10]
    assert not native.related_window("11", "10")
    with pytest.raises(RuntimeError, match="no longer exists"):
        native.window_process("10")


def test_visible_dwm_bounds_and_maximized_margins(native_factory):
    native, user = native_factory()
    def frame(hwnd, attribute, pointer, size):
        pointer._obj.left, pointer._obj.top, pointer._obj.right, pointer._obj.bottom = (0, 0, 600, 380)
        return 0
    native.dwm.DwmGetWindowAttribute.callback = frame
    assert native.window_bounds("10") == {"left": 0, "top": 0, "right": 600, "bottom": 380}
    native.dwm.DwmGetWindowAttribute.callback = lambda *args: -1
    user.windows[10] = (-8, -8, 608, 388)
    user.maximized = True
    assert native.window_bounds("10") == {"left": 0, "top": 0, "right": 600, "bottom": 380}


def test_new_window_placed_and_capped_to_selected_work_area(native_factory):
    native, user = native_factory(DISPLAY1)
    user.windows[10] = (20, 20, 900, 900)
    native.place_window("10")
    assert user.positions == [(10, -400, -100, 400, 280, 0x14)]
    assert user.inputs == []
