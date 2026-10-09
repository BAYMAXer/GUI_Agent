"""Physical desktop screenshots and Win32 input; no application text perception."""
from __future__ import annotations

from dataclasses import replace
import ctypes
import math
import platform
import threading
import time

from ..scene import probe_foreground


def unicode_units(text):
    data = str(text).encode("utf-16-le")
    return [int.from_bytes(data[i:i + 2], "little") for i in range(0, len(data), 2)]


def desktop_point(x, y, geometry):
    x, y = float(x), float(y)
    capture = geometry.get("capture_geometry", geometry)
    if not (math.isfinite(x) and math.isfinite(y) and
            0 <= x < capture["width"] and 0 <= y < capture["height"]):
        raise ValueError("Coordinate outside desktop screenshot")
    point = (int(x) + capture["left"], int(y) + capture["top"])
    monitors = geometry.get("monitors", [])
    if monitors and not any(m["left"] <= point[0] < m["left"] + m["width"] and
                            m["top"] <= point[1] < m["top"] + m["height"] for m in monitors):
        raise ValueError("Coordinate falls between monitors")
    return point


def _physical_point(point, geometry):
    """Validate a physical input point against the selected screenshot rectangle."""
    capture = geometry.get("capture_geometry", geometry)
    x, y = point
    if not (math.isfinite(x) and math.isfinite(y) and
            capture["left"] <= x < capture["left"] + capture["width"] and
            capture["top"] <= y < capture["top"] + capture["height"]):
        raise ValueError("Coordinate outside selected monitor")
    desktop_point(x - capture["left"], y - capture["top"], geometry)
    return x, y


_KEYS = {"ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12,
         "win": 0x5B, "windows": 0x5B, "super": 0x5B, "meta": 0x5B,
         "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
         "backspace": 0x08, "delete": 0x2E, "space": 0x20, "up": 0x26, "down": 0x28,
         "left": 0x25, "right": 0x27, "home": 0x24, "end": 0x23, "pageup": 0x21,
         "pagedown": 0x22, "pgup": 0x21, "pgdn": 0x22, "insert": 0x2D,
         "capslock": 0x14, "printscreen": 0x2C, "pause": 0x13}
_KEYS.update({"=": 0xBB, "equal": 0xBB, "+": 0xBB, "plus": 0xBB, "-": 0xBD, "minus": 0xBD,
              ",": 0xBC, "comma": 0xBC, ".": 0xBE, "period": 0xBE, "/": 0xBF,
              "slash": 0xBF, "\\": 0xDC, "backslash": 0xDC, ";": 0xBA, "semicolon": 0xBA})


def key_code(key):
    key = str(key).lower().replace("arrow", "")
    if key in _KEYS:
        return _KEYS[key]
    if len(key) == 1 and key.isascii() and key.isalnum():
        return ord(key.upper())
    if key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        return 0x70 + int(key[1:]) - 1
    raise ValueError(f"Unsupported key: {key}")


class FocusTracker:
    """Event version detects switching away and back while a model is thinking."""
    def __init__(self):
        self._lock = threading.Lock()
        self._epoch = 0
        self._foreground_epoch = 0
        self._ready = threading.Event()
        self._error = None
        self._tid = 0
        self._thread = threading.Thread(target=self._listen, name="desktop-focus", daemon=True)
        self._thread.start()
        if not self._ready.wait(3) or self._error:
            self.close()
            raise RuntimeError("Windows focus event monitor unavailable") from self._error

    def _listen(self):
        from ctypes import wintypes as wt
        user = ctypes.WinDLL("user32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentThreadId.restype = wt.DWORD
        user.GetForegroundWindow.restype = wt.HWND
        user.GetAncestor.argtypes = [wt.HWND, wt.UINT]
        user.GetAncestor.restype = wt.HWND
        callback_type = ctypes.WINFUNCTYPE(None, wt.HANDLE, wt.DWORD, wt.HWND,
                                           wt.LONG, wt.LONG, wt.DWORD, wt.DWORD)

        def on_event(hook, event, hwnd, obj, child, tid, timestamp):
            if event != 0x8005 or user.GetAncestor(hwnd, 2) == user.GetForegroundWindow():
                self._record_event(event)

        callback = callback_type(on_event)  # retained until both hooks are released
        user.SetWinEventHook.argtypes = [wt.DWORD, wt.DWORD, wt.HMODULE, callback_type,
                                        wt.DWORD, wt.DWORD, wt.DWORD]
        user.SetWinEventHook.restype = wt.HANDLE
        user.UnhookWinEvent.argtypes = [wt.HANDLE]
        user.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
        user.GetMessageW.restype = ctypes.c_int
        user.PeekMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT]
        user.PeekMessageW.restype = wt.BOOL
        user.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
        user.TranslateMessage.restype = wt.BOOL
        user.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
        user.DispatchMessageW.restype = wt.LPARAM
        hooks = []
        try:
            self._tid = kernel.GetCurrentThreadId()
            # Create the message queue before close() can post WM_QUIT.
            msg = wt.MSG()
            user.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
            for first, last in ((3, 7), (0x8005, 0x8005)):
                hook = user.SetWinEventHook(first, last, None, callback, 0, 0, 0)
                if not hook:
                    raise ctypes.WinError(ctypes.get_last_error())
                hooks.append(hook)
            self._ready.set()
            while user.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user.TranslateMessage(ctypes.byref(msg))
                user.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:
            self._error = exc
            self._ready.set()
        finally:
            for hook in hooks:
                user.UnhookWinEvent(hook)

    def _record_event(self, event):
        with self._lock:
            self._epoch += 1
            if event == 3:  # EVENT_SYSTEM_FOREGROUND; child/control focus has its own epoch.
                self._foreground_epoch += 1

    def probe(self):
        foreground = probe_foreground("windows")
        with self._lock:
            return replace(foreground, generation=self._epoch, foreground_generation=self._foreground_epoch)

    def close(self):
        if self._tid:
            from ctypes import wintypes as wt
            user = ctypes.WinDLL("user32")
            user.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
            user.PostThreadMessageW.restype = wt.BOOL
            user.PostThreadMessageW(self._tid, 0x12, 0, 0)
        if self._thread.is_alive():
            self._thread.join(timeout=3)


class WindowsNative:
    def __init__(self, monitor="primary"):
        if platform.system() != "Windows":
            raise RuntimeError("WindowsEnvironment requires an interactive Windows desktop")
        from ctypes import wintypes as wt
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.user.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        self.user.SetProcessDpiAwarenessContext.restype = wt.BOOL
        self.user.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        self.user.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        self.user.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        self._previous_dpi = self.user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        if not self._previous_dpi:
            raise RuntimeError("Could not enable physical desktop coordinates")
        pointer = ctypes.c_size_t

        class MouseInput(ctypes.Structure):
            _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                        ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", pointer)]

        class KeyboardInput(ctypes.Structure):
            _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                        ("time", wt.DWORD), ("dwExtraInfo", pointer)]

        class HardwareInput(ctypes.Structure):
            _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]

        class Payload(ctypes.Union):
            _fields_ = [("mi", MouseInput), ("ki", KeyboardInput), ("hi", HardwareInput)]

        class Input(ctypes.Structure):
            _anonymous_ = ("payload",)
            _fields_ = [("type", wt.DWORD), ("payload", Payload)]

        self.Input, self.MouseInput, self.KeyboardInput = Input, MouseInput, KeyboardInput
        self.user.SendInput.argtypes = [wt.UINT, ctypes.POINTER(Input), ctypes.c_int]
        self.user.SendInput.restype = wt.UINT
        self.user.GetSystemMetrics.argtypes = [ctypes.c_int]
        self.user.GetSystemMetrics.restype = ctypes.c_int
        self.user.SetForegroundWindow.argtypes = [wt.HWND]
        self.user.SetForegroundWindow.restype = wt.BOOL
        self.user.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
        self.user.ShowWindow.restype = wt.BOOL
        self.user.IsWindow.argtypes = [wt.HWND]
        self.user.IsWindow.restype = wt.BOOL
        self.user.IsZoomed.argtypes = [wt.HWND]
        self.user.IsZoomed.restype = wt.BOOL
        self.user.IsIconic.argtypes = [wt.HWND]
        self.user.IsIconic.restype = wt.BOOL
        self.user.IsWindowVisible.argtypes = [wt.HWND]
        self.user.IsWindowVisible.restype = wt.BOOL
        self.user.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
        self.user.GetWindowThreadProcessId.restype = wt.DWORD
        self.user.GetWindow.argtypes = [wt.HWND, wt.UINT]
        self.user.GetWindow.restype = wt.HWND
        self.user.GetWindowTextLengthW.argtypes = [wt.HWND]
        self.user.GetWindowTextLengthW.restype = ctypes.c_int
        self.user.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
        self.user.GetClassNameW.restype = ctypes.c_int
        self.user.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
        self.user.GetWindowRect.restype = wt.BOOL
        self.user.GetDpiForWindow.argtypes = [wt.HWND]
        self.user.GetDpiForWindow.restype = wt.UINT
        self.user.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
        self.user.GetCursorPos.restype = wt.BOOL
        self.user.WindowFromPoint.argtypes = [wt.POINT]
        self.user.WindowFromPoint.restype = wt.HWND
        self.user.GetAncestor.argtypes = [wt.HWND, wt.UINT]
        self.user.GetAncestor.restype = wt.HWND
        self.user.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
        self.user.GetClientRect.restype = wt.BOOL
        self.user.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
        self.user.ClientToScreen.restype = wt.BOOL
        self.user.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                          ctypes.c_int, ctypes.c_int, wt.UINT]
        self.user.SetWindowPos.restype = wt.BOOL
        self.user.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
        self.user.MonitorFromWindow.restype = wt.HANDLE
        self._monitor_callback = ctypes.WINFUNCTYPE(wt.BOOL, wt.HANDLE, wt.HDC,
                                                    ctypes.POINTER(wt.RECT), wt.LPARAM)

        class MonitorInfo(ctypes.Structure):
            _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT),
                        ("dwFlags", wt.DWORD), ("szDevice", wt.WCHAR * 32)]

        self.MonitorInfo = MonitorInfo
        self.user.EnumDisplayMonitors.argtypes = [wt.HDC, ctypes.POINTER(wt.RECT), self._monitor_callback, wt.LPARAM]
        self.user.EnumDisplayMonitors.restype = wt.BOOL
        self.user.GetMonitorInfoW.argtypes = [wt.HANDLE, ctypes.POINTER(MonitorInfo)]
        self.user.GetMonitorInfoW.restype = wt.BOOL
        self.input_guard = None
        self.input_dispatch_count = 0
        self._held_inputs = {}
        self.monitor = str(monitor).strip()
        self.target_monitor = None
        try:
            self.dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
            self.dwm.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]
            self.dwm.DwmGetWindowAttribute.restype = wt.LONG
        except OSError:
            self.dwm = None
        try:
            # Resolve once. Reordering displays or changing the primary flag cannot switch targets.
            self.geometry()
            self.tracker = FocusTracker()
        except Exception:
            self.user.SetThreadDpiAwarenessContext(self._previous_dpi)
            raise

    def probe(self):
        return self.tracker.probe()

    @staticmethod
    def _monitor_dict(monitor):
        return {k: int(getattr(monitor, k) if hasattr(monitor, k) else monitor[k])
                for k in ("left", "top", "width", "height")}

    @staticmethod
    def _rectangle(rectangle):
        return {"left": int(rectangle.left), "top": int(rectangle.top),
                "width": int(rectangle.right - rectangle.left),
                "height": int(rectangle.bottom - rectangle.top)}

    def _monitor_info(self, handle):
        info = self.MonitorInfo(cbSize=ctypes.sizeof(self.MonitorInfo))
        if not self.user.GetMonitorInfoW(handle, ctypes.byref(info)):
            raise RuntimeError("Windows monitor information unavailable")
        return {**self._rectangle(info.rcMonitor), "device": info.szDevice,
                "primary": bool(info.dwFlags & 1), "work_area": self._rectangle(info.rcWork)}

    def monitors(self):
        result, errors = [], []

        def visit(handle, hdc, rectangle, parameter):
            try:
                result.append(self._monitor_info(handle))
                return True
            except Exception as exc:
                errors.append(exc)
                return False

        callback = self._monitor_callback(visit)
        if not self.user.EnumDisplayMonitors(None, None, callback, 0) or errors or not result:
            raise RuntimeError("Windows display enumeration unavailable") from (errors[0] if errors else None)
        return result

    def geometry(self):
        monitors = sorted(self.monitors(), key=lambda monitor: monitor["device"].casefold())
        target = getattr(self, "target_monitor", None)
        requested = target or self.monitor
        matches = [m for m in monitors if (m["primary"] if requested.lower() == "primary" and not target
                                          else m["device"].casefold() == requested.casefold())]
        if len(matches) != 1:
            raise RuntimeError(f"Selected monitor unavailable: {requested}")
        selected = matches[0]
        self.target_monitor = selected["device"]
        desktop = dict(zip(("left", "top", "width", "height"),
                           (self.user.GetSystemMetrics(index) for index in (76, 77, 78, 79))))
        if desktop["width"] <= 0 or desktop["height"] <= 0:
            raise RuntimeError("Virtual desktop geometry unavailable")
        return {**desktop, "monitors": monitors, "scale": 1, "coordinate_space": "desktop_physical",
                "capture_geometry": self._monitor_dict(selected), "target_monitor": self.target_monitor}

    def capture(self):
        import mss
        from PIL import Image
        geometry = self.geometry()
        with mss.MSS() as capture:
            shot = capture.grab(geometry["capture_geometry"])
            image = Image.frombytes("RGB", shot.size, shot.rgb)
        return image, geometry

    def window_geometry(self, window_id):
        from ctypes import wintypes as wt
        rectangle = wt.RECT()
        if not self.user.GetWindowRect(int(window_id), ctypes.byref(rectangle)):
            raise RuntimeError("Foreground window geometry unavailable")
        return {"left": rectangle.left, "top": rectangle.top, "right": rectangle.right,
                "bottom": rectangle.bottom, "dpi": self.user.GetDpiForWindow(int(window_id))}

    def window_process(self, window_id):
        from ctypes import wintypes as wt
        hwnd = int(window_id)
        if not self.user.IsWindow(hwnd):
            raise RuntimeError("Task window no longer exists")
        pid = wt.DWORD()
        if not self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)) or not pid.value:
            raise RuntimeError("Task window process unavailable")
        return int(pid.value)

    def window_at_point(self, point):
        from ctypes import wintypes as wt
        hwnd = self.user.GetAncestor(self.user.WindowFromPoint(wt.POINT(*map(int, point))), 2)
        if not hwnd or not self.user.IsWindow(hwnd):
            raise RuntimeError("Input point has no valid task window")
        return str(hwnd)

    def window_class(self, window_id):
        hwnd = int(window_id)
        self.window_process(window_id)
        buffer = ctypes.create_unicode_buffer(256)
        if not self.user.GetClassNameW(hwnd, buffer, len(buffer)):
            raise RuntimeError("Task window class unavailable")
        return buffer.value

    def related_window(self, window_id, expected_window_id):
        candidate, expected = int(window_id), int(expected_window_id)
        if not self.user.IsWindow(candidate) or not self.user.IsWindow(expected):
            return False
        seen = set()
        while candidate and candidate not in seen:
            if candidate == expected:
                return True
            seen.add(candidate)
            candidate = self.user.GetWindow(candidate, 4)  # GW_OWNER; no unrelated same-PID windows
        return False

    def window_bounds(self, window_id):
        from ctypes import wintypes as wt
        hwnd = int(window_id)
        self.window_process(window_id)
        rectangle = wt.RECT()
        dwm = getattr(self, "dwm", None)
        extended = bool(dwm is not None and dwm.DwmGetWindowAttribute(
            hwnd, 9, ctypes.byref(rectangle), ctypes.sizeof(rectangle)) == 0)
        if not extended and not self.user.GetWindowRect(hwnd, ctypes.byref(rectangle)):
            raise RuntimeError("Task window bounds unavailable")
        if self.user.IsZoomed(hwnd):
            # Maximized windows retain invisible resize margins outside their monitor.
            handle = self.user.MonitorFromWindow(hwnd, 2)
            if handle:
                work = self._monitor_info(handle)["work_area"]
                rectangle.left = max(rectangle.left, work["left"])
                rectangle.top = max(rectangle.top, work["top"])
                rectangle.right = min(rectangle.right, work["left"] + work["width"])
                rectangle.bottom = min(rectangle.bottom, work["top"] + work["height"])
            elif not extended:
                client = wt.RECT()
                if not self.user.GetClientRect(hwnd, ctypes.byref(client)):
                    raise RuntimeError("Maximized task window bounds unavailable")
                start, end = wt.POINT(client.left, client.top), wt.POINT(client.right, client.bottom)
                if not (self.user.ClientToScreen(hwnd, ctypes.byref(start)) and
                        self.user.ClientToScreen(hwnd, ctypes.byref(end))):
                    raise RuntimeError("Maximized task window bounds mapping unavailable")
                rectangle = wt.RECT(start.x, start.y, end.x, end.y)
        if rectangle.right <= rectangle.left or rectangle.bottom <= rectangle.top:
            raise RuntimeError("Task window has no visible bounds")
        return {"left": int(rectangle.left), "top": int(rectangle.top),
                "right": int(rectangle.right), "bottom": int(rectangle.bottom)}

    def place_window(self, window_id):
        geometry = self.geometry()
        monitor = next(m for m in geometry["monitors"] if m["device"] == geometry["target_monitor"])
        work = monitor["work_area"]
        hwnd = int(window_id)
        self.window_process(window_id)
        # Restore before reading dimensions so a maximized window on another screen fits.
        self.user.ShowWindow(hwnd, 9)
        bounds = self.window_geometry(window_id)
        width = max(1, min(bounds["right"] - bounds["left"], work["width"]))
        height = max(1, min(bounds["bottom"] - bounds["top"], work["height"]))
        left = max(work["left"], min(bounds["left"], work["left"] + work["width"] - width))
        top = max(work["top"], min(bounds["top"], work["top"] + work["height"] - height))
        if not self.user.SetWindowPos(hwnd, None, left, top, width, height, 0x0004 | 0x0010):
            raise RuntimeError("Could not place Agent window on the selected monitor")
        placed = self.window_bounds(window_id)
        if not (work["left"] <= placed["left"] < placed["right"] <= work["left"] + work["width"] and
                work["top"] <= placed["top"] < placed["bottom"] <= work["top"] + work["height"]):
            raise RuntimeError("Agent window does not fit the selected monitor work area")

    def _send(self, events, *, cleanup=False):
        if not events:
            return
        guard = getattr(self, "input_guard", None)
        if guard and not cleanup:
            guard()
        array = (self.Input * len(events))(*events)
        self.input_dispatch_count = getattr(self, "input_dispatch_count", 0) + 1
        sent = self.user.SendInput(len(array), array, ctypes.sizeof(self.Input))
        held = getattr(self, "_held_inputs", None)
        if held is None:
            held = self._held_inputs = {}
        for event in events[:sent]:
            if event.type == 1:
                key = (1, event.ki.wVk, event.ki.wScan, bool(event.ki.dwFlags & 4))
                if event.ki.dwFlags & 2:
                    held.pop(key, None)
                else:
                    release = self.Input.from_buffer_copy(event)
                    release.ki.dwFlags |= 2
                    held[key] = release
            elif event.type == 0:
                for down, up in ((2, 4), (8, 16), (0x20, 0x40)):
                    key = (0, down)
                    if event.mi.dwFlags & down:
                        held[key] = self._mouse(up)
                    if event.mi.dwFlags & up:
                        held.pop(key, None)
        if sent != len(array):
            if held and not cleanup:
                # Release only down events actually accepted by SendInput.
                try:
                    self._send(list(reversed(list(held.values()))), cleanup=True)
                except Exception:
                    pass
            raise RuntimeError("SendInput failed or partially dispatched (desktop locked or integrity boundary)")

    def _mouse(self, flags, x=0, y=0, data=0):
        event = self.Input(type=0)
        event.mi = self.MouseInput(x, y, data & 0xFFFFFFFF, flags, 0, 0)
        return event

    def _key(self, code, up=False, unicode=False):
        event = self.Input(type=1)
        flags = (2 if up else 0) | (4 if unicode else 0)
        if not unicode and code in {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B}:
            flags |= 1
        event.ki = self.KeyboardInput(0 if unicode else code, code if unicode else 0, flags, 0, 0)
        return event

    def move(self, point, geometry):
        x, y = _physical_point(point, geometry)
        dx = round((x - geometry["left"]) * 65535 / max(1, geometry["width"] - 1))
        dy = round((y - geometry["top"]) * 65535 / max(1, geometry["height"] - 1))
        self._send([self._mouse(0x0001 | 0x8000 | 0x4000, dx, dy)])

    def click(self, point, geometry, *, right=False, count=1, after_click=None):
        self.move(point, geometry)
        down, up = (8, 16) if right else (2, 4)
        for i in range(count):
            self._send([self._mouse(down), self._mouse(up)])
            if after_click:
                after_click()
            if i + 1 < count:
                time.sleep(0.05)

    def hotkey(self, keys):
        codes = [key_code(key) for key in keys]
        self._send([self._key(c) for c in codes] + [self._key(c, up=True) for c in reversed(codes)])

    def type_text(self, text, guard=None):
        # Unicode packets preserve Chinese and surrogate pairs without clipboard mutation.
        pending = []
        for char in str(text):
            if char == "\r":
                continue
            codes = [(0x0D if char == "\n" else 0x09, False)] if char in "\n\t" else [
                (unit, True) for unit in unicode_units(char)]
            for unit, unicode in codes:
                pending.extend((self._key(unit, unicode=unicode), self._key(unit, up=True, unicode=unicode)))
            if len(pending) >= 128:
                if guard:
                    guard()
                self._send(pending)
                pending = []
        if pending:
            if guard:
                guard()
            self._send(pending)

    def scroll(self, amount, direction, *, window_id=None, geometry=None):
        from ctypes import wintypes as wt
        geometry = geometry or self.geometry()
        point = wt.POINT()
        if not self.user.GetCursorPos(ctypes.byref(point)):
            raise RuntimeError("Scroll cursor position unavailable")
        if window_id is not None:
            if self.user.GetAncestor(self.user.WindowFromPoint(point), 2) != int(window_id):
                rectangle = wt.RECT()
                if not self.user.GetClientRect(int(window_id), ctypes.byref(rectangle)):
                    raise RuntimeError("Scroll work area unavailable")
                point = wt.POINT((rectangle.left + rectangle.right) // 2, (rectangle.top + rectangle.bottom) // 2)
                if not self.user.ClientToScreen(int(window_id), ctypes.byref(point)):
                    raise RuntimeError("Scroll work area mapping unavailable")
                _physical_point((point.x, point.y), geometry)
                if self.user.GetAncestor(self.user.WindowFromPoint(point), 2) != int(window_id):
                    raise RuntimeError("Foreground scroll area is occluded; choose a visible pane first")
                self.move((point.x, point.y), geometry)
        _physical_point((point.x, point.y), geometry)
        self._send([self._mouse(0x0800, data=int(amount) * 120 * (1 if direction == "up" else -1))])
        return (point.x, point.y) if window_id is not None else None

    def drag(self, start, end, geometry, *, after_input=None):
        path = [start] + [(round(start[0] + (end[0] - start[0]) * i / 12),
                          round(start[1] + (end[1] - start[1]) * i / 12)) for i in range(1, 13)]
        for point in path:
            _physical_point(point, geometry)
        self.move(start, geometry)
        self._send([self._mouse(2)])
        try:
            if after_input:
                after_input()
            for point in path[1:]:
                self.move(point, geometry)
                if after_input:
                    after_input()
                time.sleep(0.02)
        finally:
            if (0, 2) in self._held_inputs:
                self._send([self._mouse(4)], cleanup=True)

    def windows(self, process_id):
        from ctypes import wintypes as wt
        result = []
        callback_type = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

        def visit(hwnd, parameter):
            pid = wt.DWORD()
            self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if (pid.value == process_id and self.user.IsWindowVisible(hwnd) and
                    not self.user.GetWindow(hwnd, 4) and self.user.GetWindowTextLengthW(hwnd) > 0):
                result.append(int(hwnd))
            return True

        callback = callback_type(visit)
        self.user.EnumWindows.argtypes = [callback_type, wt.LPARAM]
        self.user.EnumWindows.restype = wt.BOOL
        self.user.EnumWindows(callback, 0)
        return result

    def activate(self, window_id, *, allow_input_fallback=True):
        target_pid = self.window_process(window_id)
        before = self.probe()
        foreground_epoch = getattr(before, "foreground_generation", None)
        wait_for_epoch = str(before.window_id) != str(window_id) and foreground_epoch is not None
        if self.user.IsIconic(int(window_id)):
            self.user.ShowWindow(int(window_id), 9)
        if not self.user.SetForegroundWindow(int(window_id)) and allow_input_fallback:
            # A deliberate browser-open action can claim foreground after injected user input.
            self.hotkey(["alt"])
            self.user.SetForegroundWindow(int(window_id))
        deadline = time.monotonic() + 0.3
        while True:
            current = self.probe()
            target_matches = (str(current.window_id) == str(window_id) and
                              self.window_process(window_id) == target_pid and
                              getattr(current, "process_id", target_pid) == target_pid)
            epoch_observed = (not wait_for_epoch or
                              getattr(current, "foreground_generation", foreground_epoch) > foreground_epoch)
            if target_matches and epoch_observed:
                return
            if time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        if target_matches:
            raise RuntimeError("Windows foreground activation event did not synchronize; reobserve before retrying")
        raise RuntimeError("Windows refused browser activation; reobserve before retrying")

    def close(self):
        try:
            held = getattr(self, "_held_inputs", {})
            if held:
                try:
                    self._send(list(reversed(list(held.values()))), cleanup=True)
                except Exception:
                    pass
        finally:
            self.tracker.close()
            self.user.SetThreadDpiAwarenessContext(self._previous_dpi)
