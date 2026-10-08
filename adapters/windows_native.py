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
    if not (math.isfinite(x) and math.isfinite(y) and
            0 <= x < geometry["width"] and 0 <= y < geometry["height"]):
        raise ValueError("Coordinate outside desktop screenshot")
    point = (int(x) + geometry["left"], int(y) + geometry["top"])
    monitors = geometry.get("monitors", [])
    if monitors and not any(m["left"] <= point[0] < m["left"] + m["width"] and
                            m["top"] <= point[1] < m["top"] + m["height"] for m in monitors):
        raise ValueError("Coordinate falls between monitors")
    return point


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
        user.GetForegroundWindow.restype = wt.HWND
        user.GetAncestor.argtypes = [wt.HWND, wt.UINT]
        user.GetAncestor.restype = wt.HWND
        callback_type = ctypes.WINFUNCTYPE(None, wt.HANDLE, wt.DWORD, wt.HWND,
                                           wt.LONG, wt.LONG, wt.DWORD, wt.DWORD)

        def on_event(hook, event, hwnd, obj, child, tid, timestamp):
            if event != 0x8005 or user.GetAncestor(hwnd, 2) == user.GetForegroundWindow():
                with self._lock:
                    self._epoch += 1

        callback = callback_type(on_event)  # retained until both hooks are released
        user.SetWinEventHook.argtypes = [wt.DWORD, wt.DWORD, wt.HMODULE, callback_type,
                                        wt.DWORD, wt.DWORD, wt.DWORD]
        user.SetWinEventHook.restype = wt.HANDLE
        user.UnhookWinEvent.argtypes = [wt.HANDLE]
        user.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
        user.GetMessageW.restype = ctypes.c_int
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

    def probe(self):
        foreground = probe_foreground("windows")
        with self._lock:
            return replace(foreground, generation=self._epoch)

    def close(self):
        if self._tid:
            ctypes.WinDLL("user32").PostThreadMessageW(self._tid, 0x12, 0, 0)
        if self._thread.is_alive():
            self._thread.join(timeout=3)


class WindowsNative:
    def __init__(self):
        if platform.system() != "Windows":
            raise RuntimeError("WindowsEnvironment requires an interactive Windows desktop")
        from ctypes import wintypes as wt
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.user.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
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
        self.user.SetForegroundWindow.argtypes = [wt.HWND]
        self.user.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
        self.user.IsWindowVisible.argtypes = [wt.HWND]
        self.user.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
        self.user.GetWindow.argtypes = [wt.HWND, wt.UINT]
        self.user.GetWindow.restype = wt.HWND
        self.user.GetWindowTextLengthW.argtypes = [wt.HWND]
        self.user.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
        self.user.GetDpiForWindow.argtypes = [wt.HWND]
        self.user.GetDpiForWindow.restype = wt.UINT
        self.user.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
        self.user.WindowFromPoint.argtypes = [wt.POINT]
        self.user.WindowFromPoint.restype = wt.HWND
        self.user.GetAncestor.argtypes = [wt.HWND, wt.UINT]
        self.user.GetAncestor.restype = wt.HWND
        self.user.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
        self.user.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
        try:
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

    def geometry(self):
        import mss
        with mss.MSS() as capture:
            monitors = [self._monitor_dict(m) for m in capture.monitors]
        return {**monitors[0], "monitors": monitors[1:], "scale": 1, "coordinate_space": "desktop_physical"}

    def capture(self):
        import mss
        from PIL import Image
        with mss.MSS() as capture:
            monitors = [self._monitor_dict(m) for m in capture.monitors]
            shot = capture.grab(monitors[0])
            image = Image.frombytes("RGB", shot.size, shot.rgb)
        geometry = {**monitors[0], "monitors": monitors[1:], "scale": 1, "coordinate_space": "desktop_physical"}
        return image, geometry

    def window_geometry(self, window_id):
        from ctypes import wintypes as wt
        rectangle = wt.RECT()
        if not self.user.GetWindowRect(int(window_id), ctypes.byref(rectangle)):
            raise RuntimeError("Foreground window geometry unavailable")
        return {"left": rectangle.left, "top": rectangle.top, "right": rectangle.right,
                "bottom": rectangle.bottom, "dpi": self.user.GetDpiForWindow(int(window_id))}

    def _send(self, events):
        array = (self.Input * len(events))(*events)
        if self.user.SendInput(len(array), array, ctypes.sizeof(self.Input)) != len(array):
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
        x, y = point
        dx = round((x - geometry["left"]) * 65535 / max(1, geometry["width"] - 1))
        dy = round((y - geometry["top"]) * 65535 / max(1, geometry["height"] - 1))
        self._send([self._mouse(0x0001 | 0x8000 | 0x4000, dx, dy)])

    def click(self, point, geometry, *, right=False, count=1):
        self.move(point, geometry)
        down, up = (8, 16) if right else (2, 4)
        for i in range(count):
            self._send([self._mouse(down), self._mouse(up)])
            if i + 1 < count:
                time.sleep(0.05)

    def hotkey(self, keys):
        codes = [key_code(key) for key in keys]
        try:
            self._send([self._key(c) for c in codes] + [self._key(c, up=True) for c in reversed(codes)])
        except Exception:
            try:
                self._send([self._key(c, up=True) for c in reversed(codes)])
            except Exception:
                pass
            raise

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
        point = wt.POINT()
        if window_id is not None:
            if not self.user.GetCursorPos(ctypes.byref(point)):
                raise RuntimeError("Scroll cursor position unavailable")
            if self.user.GetAncestor(self.user.WindowFromPoint(point), 2) != int(window_id):
                rectangle = wt.RECT()
                if not self.user.GetClientRect(int(window_id), ctypes.byref(rectangle)):
                    raise RuntimeError("Scroll work area unavailable")
                point = wt.POINT((rectangle.left + rectangle.right) // 2, (rectangle.top + rectangle.bottom) // 2)
                if not self.user.ClientToScreen(int(window_id), ctypes.byref(point)):
                    raise RuntimeError("Scroll work area mapping unavailable")
                desktop_point(point.x - geometry["left"], point.y - geometry["top"], geometry)
                if self.user.GetAncestor(self.user.WindowFromPoint(point), 2) != int(window_id):
                    raise RuntimeError("Foreground scroll area is occluded; choose a visible pane first")
                self.move((point.x, point.y), geometry)
        self._send([self._mouse(0x0800, data=int(amount) * 120 * (1 if direction == "up" else -1))])
        return (point.x, point.y) if window_id is not None else None

    def drag(self, start, end, geometry):
        self.move(start, geometry)
        self._send([self._mouse(2)])
        try:
            for i in range(1, 13):
                self.move((round(start[0] + (end[0] - start[0]) * i / 12),
                           round(start[1] + (end[1] - start[1]) * i / 12)), geometry)
                time.sleep(0.02)
        finally:
            self._send([self._mouse(4)])

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
        self.user.EnumWindows(callback, 0)
        return result

    def activate(self, window_id):
        self.user.ShowWindow(int(window_id), 9)
        if not self.user.SetForegroundWindow(int(window_id)):
            # A deliberate browser-open action can claim foreground after injected user input.
            self.hotkey(["alt"])
            self.user.SetForegroundWindow(int(window_id))
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            if self.probe().window_id == str(window_id):
                return
            time.sleep(0.01)
        if self.probe().window_id != str(window_id):
            raise RuntimeError("Windows refused browser activation; reobserve before retrying")

    def close(self):
        self.tracker.close()
        self.user.SetThreadDpiAwarenessContext(self._previous_dpi)
