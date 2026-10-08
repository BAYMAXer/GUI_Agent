"""Configurable, on-demand Chromium browser capability for desktop tasks."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import time
from urllib.parse import urlparse
import uuid
from urllib.request import urlopen

from ..browser import BrowserOptions, BrowserSession


@dataclass
class BrowserRuntimeConfig:
    channel: str = "auto"
    executable: str = ""
    endpoint: str = ""
    profile_dir: str = ""
    download_dir: str = ""
    connection_timeout_ms: int = 1500
    startup_timeout_s: float = 12.0


def validate_url(url):
    if url and any(ord(char) < 32 for char in url):
        raise ValueError("Browser URL contains a control character")
    if url and url != "about:blank" and urlparse(url).scheme not in {"http", "https", "file"}:
        raise ValueError("Browser URL requires http, https or file scheme")
    return url or "about:blank"


def discover_browser(config):
    if config.executable:
        path = Path(config.executable).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError("Configured browser executable is missing")
        return path, config.channel
    channels = ("chrome", "msedge", "chromium") if config.channel == "auto" else (config.channel,)
    relatives = {"chrome": "Google/Chrome/Application/chrome.exe",
                 "msedge": "Microsoft/Edge/Application/msedge.exe"}
    for channel in channels:
        if channel == "chromium":
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                path = Path(pw.chromium.executable_path)
                if path.is_file():
                    return path, channel
            continue
        if channel not in relatives:
            raise ValueError("Browser channel must be auto, chrome, msedge or chromium")
        for base in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            if os.environ.get(base):
                path = Path(os.environ[base]) / relatives[channel]
                if path.is_file():
                    return path, channel
    raise RuntimeError("No configured browser found; install Chrome/Edge or configure its executable")


class BrowserRuntime:
    """Own only the launched process; attaching never owns an existing browser."""
    def __init__(self, native, config=None, artifact_dir="artifacts/computer"):
        self.native = native
        self.config = config or BrowserRuntimeConfig()
        self.artifact_dir = Path(artifact_dir).resolve()
        self.process = None
        self.process_id = 0
        self._owned_process = None
        self._prelaunch_pids = set()
        self.channel = self.config.channel
        self.endpoint = self.config.endpoint
        self.profile_dir = None
        self.session = None
        self._pw = self._browser = None
        self._control_cdp = None
        self._last_attempt = 0.0
        self.structure_error = ""
        self.downloads = []
        if self.endpoint:
            host = urlparse(self.endpoint).hostname
            if host not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("Desktop CDP binding requires a local endpoint to verify the foreground PID")

    def _connect(self):
        if self._browser is not None:
            if self._browser.is_connected():
                return
            if self.session:
                self.session.close()
            if self._pw:
                self._pw.stop()
            self._pw = self._browser = self.session = None
        if not self.endpoint:
            raise RuntimeError("Browser has no structure connection")
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.connect_over_cdp(
                self.endpoint, timeout=self.config.connection_timeout_ms)
            cdp = self._browser.new_browser_cdp_session()
            try:
                processes = cdp.send("SystemInfo.getProcessInfo")["processInfo"]
                pid = [int(p["id"]) for p in processes if p["type"] == "browser"]
                if len(pid) != 1:
                    raise RuntimeError("CDP browser process identity unavailable")
                if self.process is not None:
                    import psutil
                    if pid[0] in self._prelaunch_pids:
                        raise RuntimeError("CDP browser process predates this launch; attach it explicitly")
                    process = psutil.Process(pid[0])
                    arguments = process.cmdline()
                    profiles = [arg.split("=", 1)[1] for arg in arguments if arg.startswith("--user-data-dir=")]
                    if len(profiles) != 1 or Path(profiles[0]).resolve() != self.profile_dir:
                        raise RuntimeError("CDP process does not belong to the launched browser profile")
                    self._owned_process = process
                self.process_id = pid[0]
                if self.process is not None:
                    download_dir = Path(self.config.download_dir or self.artifact_dir / "downloads").resolve()
                    download_dir.mkdir(parents=True, exist_ok=True)
                    cdp.send("Browser.setDownloadBehavior", {"behavior": "allow", "downloadPath": str(download_dir),
                                                              "eventsEnabled": True})
                    def begin(event):
                        self.downloads.append({"guid": event["guid"], "filename": event["suggestedFilename"],
                                               "state": "inProgress", "directory": str(download_dir)})
                    def progress(event):
                        for download in self.downloads:
                            if download["guid"] == event["guid"]:
                                download.update(state=event["state"], received_bytes=event.get("receivedBytes", 0))
                    cdp.on("Browser.downloadWillBegin", begin)
                    cdp.on("Browser.downloadProgress", progress)
                self._control_cdp = cdp
            except Exception:
                cdp.detach()
                raise
            self.session = BrowserSession(options=BrowserOptions(endpoint=self.endpoint,
                timeout_ms=self.config.connection_timeout_ms), foreground_probe=self.foreground)
            self.session._browser = self._browser
            self.structure_error = ""
        except Exception:
            self._browser = None
            self._pw.stop()
            self._pw = None
            raise

    def foreground(self):
        from dataclasses import replace
        foreground = self.native.probe()
        if (foreground.available and foreground.is_browser and
                (not self.process_id or foreground.process_id != self.process_id)):
            return replace(foreground, is_browser=False, reason="browser_instance_not_bound")
        return foreground

    def _adopt_delegated_process(self):
        """Retain visual browser control when a delegated launcher has no CDP service."""
        if not self.profile_dir or self.config.endpoint:
            return
        import psutil
        matches = []
        for process in psutil.process_iter(["pid", "name"]):
            if process.pid in self._prelaunch_pids or (process.info.get("name") or "").lower() not in {"chrome.exe", "msedge.exe"}:
                continue
            try:
                arguments = process.cmdline()
                profiles = [arg.split("=", 1)[1] for arg in arguments if arg.startswith("--user-data-dir=")]
                if (not any(arg.startswith("--type=") for arg in arguments) and len(profiles) == 1
                        and Path(profiles[0]).resolve() == self.profile_dir):
                    matches.append(process)
            except (psutil.Error, OSError, ValueError):
                continue
        if len(matches) == 1:
            self._owned_process = matches[0]
            self.process_id = matches[0].pid

    def _wait_for_window(self, deadline):
        """Wait for a verified launched process and its own visible window."""
        launched = self.process is not None and not self.config.endpoint
        while True:
            if launched and self._owned_process is None:
                self._adopt_delegated_process()
            # A launcher PID alone is not proof of the delegated browser's identity.
            pid = self.process_id if not launched or self._owned_process is not None else 0
            windows = self.native.windows(pid) if pid else []
            if windows:
                return windows
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("Dedicated browser window unavailable")
            time.sleep(min(0.05, remaining))

    def focused_session(self, foreground):
        # Never attach/read page structure for an ordinary application or a native dialog.
        if not foreground.available or not foreground.is_browser or foreground.native_ui:
            if self.session:
                self.session.invalidate()
            return None
        if self.process_id and foreground.process_id != self.process_id:
            if self.session:
                self.session.invalidate()
            return None
        if self.endpoint and (self._browser is None or not self._browser.is_connected()):
            if time.monotonic() - self._last_attempt < 5:
                return None
            self._last_attempt = time.monotonic()
            try:
                self._connect()
            except Exception as exc:
                self.structure_error = type(exc).__name__
        if foreground.process_id == self.process_id and self.session:
            return self.session
        return None

    def open(self, url=None):
        url = validate_url(url) if url else None
        first_launch = self.process is None and not self.config.endpoint
        deadline = time.monotonic() + self.config.startup_timeout_s
        if first_launch:
            executable, self.channel = discover_browser(self.config)
            self.artifact_dir.mkdir(parents=True, exist_ok=True)
            self.profile_dir = Path(self.config.profile_dir or
                self.artifact_dir / ("browser-profile-" + uuid.uuid4().hex)).resolve()
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            active_port = self.profile_dir / "DevToolsActivePort"
            # An active persistent profile must be attached explicitly, never taken over.
            if active_port.exists():
                live = False
                try:
                    port = int(active_port.read_text(encoding="utf-8").splitlines()[0])
                    if 0 < port <= 65535:
                        with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=0.3) as response:
                            live = response.status == 200
                except (OSError, ValueError, IndexError):
                    pass
                if live:
                    raise RuntimeError("Profile is active; attach its CDP endpoint or use a fresh profile")
                active_port.unlink()  # stale port metadata only; retain cookies and profile files
            import psutil
            self._prelaunch_pids = set(psutil.pids())
            self.process = subprocess.Popen([str(executable), "--remote-debugging-port=0",
                "--remote-debugging-address=127.0.0.1", "--user-data-dir=" + str(self.profile_dir),
                "--no-first-run", "--no-default-browser-check", "--new-window", url or "about:blank"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.process_id = self.process.pid
            deadline = time.monotonic() + self.config.startup_timeout_s
            while time.monotonic() < deadline:
                if active_port.exists():
                    try:
                        port = int(active_port.read_text(encoding="utf-8").splitlines()[0])
                        if 0 < port <= 65535:
                            self.endpoint = f"http://127.0.0.1:{port}"
                            break
                    except (OSError, ValueError, IndexError):
                        pass
                # Some browser launchers exit after delegating to the actual browser process.
                time.sleep(0.05)
        try:
            self._connect()
        except Exception as exc:
            self.structure_error = type(exc).__name__
        windows = self._wait_for_window(deadline)
        # When multiple windows exist, a focused CDP page identifies the active window.
        foreground = self.native.probe()
        if foreground.process_id != self.process_id or foreground.native_ui:
            if len(windows) != 1:
                raise RuntimeError("Multiple browser windows; activate the intended window visually")
            self.native.activate(windows[0])
        navigation_needed = bool(url and not first_launch)
        if self._browser:
            pages = [page for ctx in self._browser.contexts for page in ctx.pages if not page.is_closed()]
            focused = [p for p in pages if self.session._real_page_focus(p)]
            page = focused[0] if len(focused) == 1 else (pages[0] if len(pages) == 1 and len(windows) == 1 else None)
            if page is not None and url and not first_launch:
                page.goto(url, wait_until="domcontentloaded", timeout=15000)
                navigation_needed = False
        return {"browser_channel": self.channel, "structure_error": self.structure_error,
                "navigation_needed": navigation_needed, "process_id": self.process_id}

    def close(self):
        if self.session:
            self.session.close()
        if self._browser is not None and self._owned_process is not None:
            try:
                self._browser.close()
            except Exception:
                pass
        if self._pw:
            self._pw.stop()  # attached browsers are disconnected, never closed
        if self._owned_process is not None:
            import psutil
            try:
                self._owned_process.wait(timeout=2)
            except psutil.TimeoutExpired:
                self._owned_process.terminate()
            except psutil.NoSuchProcess:
                pass
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self._pw = self._browser = self.session = None
        self._control_cdp = None
        self._owned_process = None
        self.process = None
