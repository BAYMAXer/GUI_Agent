"""Deterministic per-observation scene gate; mouse position is not a signal."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import platform


_BROWSERS = {"chrome", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
             "msedge", "microsoft-edge", "microsoft-edge-stable", "brave", "brave-browser",
             "firefox", "firefox-esr", "opera", "vivaldi", "safari"}


@dataclass(frozen=True)
class Foreground:
    available: bool = False
    window_id: str = ""
    process_id: int = 0
    process_name: str = ""
    window_class: str = ""
    is_browser: bool = False
    native_ui: bool = False
    reason: str = "foreground_unavailable"

    @classmethod
    def from_raw(cls, raw):
        if not isinstance(raw, dict):
            return cls(reason="invalid_foreground_probe")
        if not raw.get("available"):
            return cls(reason=str((raw or {}).get("reason", "foreground_unavailable")))
        # Exact process identity, never a keyword in a document/window title.
        process = str(raw.get("process_name", "")).replace("\\", "/").rsplit("/", 1)[-1].lower()
        name = process.removesuffix(".exe")
        klass = str(raw.get("window_class", ""))
        native = bool(raw.get("menu_active") or raw.get("dialog") or klass in {"#32770", "#32768"})
        return cls(available=True, window_id=str(raw.get("window_id", "")),
            process_id=int(raw.get("process_id", 0)), process_name=process, window_class=klass,
            is_browser=name in _BROWSERS, native_ui=native,
            reason="native_ui" if native else ("browser_foreground" if name in _BROWSERS else "desktop_foreground"))


@dataclass
class Scene:
    browser_use: int = 0
    structure_available: bool = False
    mode: str = "unknown"
    reason: str = "not_detected"
    foreground: dict = field(default_factory=dict)
    page_id: str = ""
    url: str = ""
    snapshot_id: str = ""

    def to_dict(self):
        return asdict(self)


def scene_from_foreground(foreground):
    mode = "unknown"
    if foreground.available:
        mode = "browser_native" if foreground.is_browser and foreground.native_ui else (
            "desktop" if not foreground.is_browser else "unknown")
    return Scene(mode=mode, reason=foreground.reason, foreground=asdict(foreground))


def observation_scene(observation):
    """Legacy/unknown environments default to visual. Context presence is not proof."""
    if observation is None:
        return Scene().to_dict()
    value = observation.info.get("scene")
    return value if isinstance(value, dict) else Scene(mode="desktop", reason="no_browser_evidence").to_dict()


def browser_context(observation):
    scene = observation_scene(observation)
    if scene.get("browser_use") != 1 or not scene.get("structure_available"):
        return []
    return [b for b in observation.context if b.get("kind") == "browser_ax"
            and b.get("snapshot_id") == scene.get("snapshot_id")]


def _probe_windows_raw():
    import ctypes
    from ctypes import wintypes as wt
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetForegroundWindow.restype = wt.HWND
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user32.GetWindowThreadProcessId.restype = wt.DWORD
    user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel32.OpenProcess.restype = wt.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
    kernel32.CloseHandle.argtypes = [wt.HANDLE]
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return {"available": False, "reason": "no_foreground_window"}
    pid = wt.DWORD()
    tid = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return {"available": False, "reason": "foreground_process_unreadable"}
    try:
        image = ctypes.create_unicode_buffer(32768)
        size = wt.DWORD(len(image))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)):
            return {"available": False, "reason": "foreground_process_unreadable"}
    finally:
        kernel32.CloseHandle(handle)
    klass = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, klass, len(klass))

    class GUIThreadInfo(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("flags", wt.DWORD), ("hwndActive", wt.HWND),
                    ("hwndFocus", wt.HWND), ("hwndCapture", wt.HWND), ("hwndMenuOwner", wt.HWND),
                    ("hwndMoveSize", wt.HWND), ("hwndCaret", wt.HWND), ("rcCaret", wt.RECT)]
    user32.GetGUIThreadInfo.argtypes = [wt.DWORD, ctypes.POINTER(GUIThreadInfo)]
    gui = GUIThreadInfo(cbSize=ctypes.sizeof(GUIThreadInfo))
    got_gui = user32.GetGUIThreadInfo(tid, ctypes.byref(gui))
    return {"available": True, "window_id": str(hwnd), "process_id": pid.value,
            "process_name": image.value, "window_class": klass.value,
            "menu_active": bool(got_gui and gui.flags & (0x4 | 0x8 | 0x10))}


def _probe_linux_raw():
    # Self-contained so the OSWorld adapter can execute this on the TARGET VM.
    import os
    import subprocess
    def read(args):
        return subprocess.check_output(args, stderr=subprocess.DEVNULL, timeout=2, text=True).strip()
    try:
        window = read(["xdotool", "getactivewindow"])
        pid = int(read(["xdotool", "getwindowpid", window]))
        process = os.path.basename(os.readlink(f"/proc/{pid}/exe"))
        klass = read(["xprop", "-id", window, "WM_CLASS"])
        window_type = read(["xprop", "-id", window, "_NET_WM_WINDOW_TYPE"])
        return {"available": True, "window_id": window, "process_id": pid,
                "process_name": process, "window_class": klass,
                "dialog": "_NET_WM_WINDOW_TYPE_DIALOG" in window_type,
                "menu_active": any(t in window_type for t in ("_NET_WM_WINDOW_TYPE_POPUP_MENU", "_NET_WM_WINDOW_TYPE_DROPDOWN_MENU"))}
    except Exception:
        return {"available": False, "reason": "x11_foreground_unavailable"}


def probe_foreground(os_name=None):
    system = (os_name or platform.system()).lower()
    try:
        if system == "windows":
            return Foreground.from_raw(_probe_windows_raw())
        if system == "linux":
            return Foreground.from_raw(_probe_linux_raw())
        return Foreground(reason="unsupported_foreground_platform")
    except Exception:
        return Foreground(reason="foreground_probe_failed")


def remote_linux_probe(controller):
    """Never fall back to querying the agent host when a target VM probe fails."""
    import inspect
    runner = getattr(controller, "run_python_script", None)
    if runner is None:
        return Foreground(reason="target_controller_unavailable")
    try:
        code = inspect.getsource(_probe_linux_raw) + "\nimport json\nprint(json.dumps(_probe_linux_raw()))"
        result = runner(code)
        output = result if isinstance(result, str) else result.get("output", "")
        for line in reversed(output.strip().splitlines()):
            try:
                return Foreground.from_raw(json.loads(line))
            except (ValueError, TypeError):
                continue
    except Exception:
        pass
    return Foreground(reason="target_foreground_probe_failed")
