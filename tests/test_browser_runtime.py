"""Browser ownership, delegated launchers and download lifecycle without UI input."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from osworld_agent.adapters.browser_runtime import BrowserRuntime, BrowserRuntimeConfig
from osworld_agent.tests.test_windows_env import BROWSER, Native


@pytest.fixture
def connection(tmp_path, monkeypatch):
    import playwright.sync_api
    import psutil
    calls = []
    callbacks = {}
    profile = tmp_path / "agent-profile"
    profile.mkdir()
    command = ["msedge.exe", "--user-data-dir=" + str(profile)]
    class CDP:
        def send(self, method, parameters=None):
            calls.append((method, parameters))
            return {"processInfo": [{"id": 40, "type": "browser"}]} if method == "SystemInfo.getProcessInfo" else {}
        def on(self, name, callback):
            callbacks[name] = callback
        def detach(self):
            calls.append(("detach", None))
    cdp = CDP()
    browser = SimpleNamespace(new_browser_cdp_session=lambda: cdp,
        close=lambda: calls.append(("close_owned_browser", None)), is_connected=lambda: True)
    pw = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=lambda *a, **kw: browser),
                         stop=lambda: calls.append(("disconnect", None)))
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", lambda: SimpleNamespace(start=lambda: pw))
    process = SimpleNamespace(cmdline=lambda: command, wait=lambda timeout: calls.append(("wait_owned", timeout)))
    monkeypatch.setattr(psutil, "Process", lambda pid: process)
    runtime = BrowserRuntime(Native(), BrowserRuntimeConfig(), tmp_path)
    runtime.endpoint = "http://127.0.0.1:12345"
    runtime.profile_dir = profile
    runtime.process = SimpleNamespace(pid=10, poll=lambda: 0)  # exited launcher, browser PID differs
    yield runtime, calls, callbacks, command
    runtime.close()


def test_delegated_browser_pid_requires_own_profile(connection):
    runtime, calls, callbacks, _ = connection
    runtime._connect()
    assert runtime.process_id == 40 and runtime.session is not None
    assert not any(name == "detach" for name, _ in calls)
    assert "Browser.downloadProgress" in callbacks
    runtime.close()
    assert any(name == "close_owned_browser" for name, _ in calls)


def test_pid_from_another_profile_is_rejected_without_closing_it(connection):
    runtime, calls, _, command = connection
    command[:] = ["msedge.exe", "--user-data-dir=another-profile"]
    with pytest.raises(RuntimeError, match="launched browser profile"):
        runtime._connect()
    assert runtime._owned_process is None and runtime._browser is None
    runtime.close()
    assert not any(name == "close_owned_browser" for name, _ in calls)


def test_cdp_cannot_own_a_process_that_predates_launch(connection):
    runtime, calls, _, _ = connection
    runtime._prelaunch_pids = {40}
    with pytest.raises(RuntimeError, match="predates this launch"):
        runtime._connect()
    assert runtime._owned_process is None and runtime._browser is None
    runtime.close()
    assert not any(name == "close_owned_browser" for name, _ in calls)


def test_download_control_session_remains_alive_and_records_real_events(connection):
    runtime, calls, callbacks, _ = connection
    runtime._connect()
    callbacks["Browser.downloadWillBegin"]({"guid": "download-id", "suggestedFilename": "study.pdf"})
    callbacks["Browser.downloadProgress"]({"guid": "download-id", "state": "completed", "receivedBytes": 598})
    assert runtime.downloads[0]["state"] == "completed" and runtime.downloads[0]["received_bytes"] == 598
    assert Path(runtime.downloads[0]["directory"]).is_dir()
    assert not any(name == "detach" for name, _ in calls)


def test_existing_browser_connection_does_not_change_download_settings(connection):
    runtime, calls, callbacks, _ = connection
    runtime.process = None
    runtime._connect()
    assert runtime.process_id == 40 and runtime._owned_process is None
    assert not any(name == "Browser.setDownloadBehavior" for name, _ in calls)
    runtime.close()
    assert not any(name == "close_owned_browser" for name, _ in calls)


def test_active_persistent_profile_is_not_relaunched(tmp_path, monkeypatch):
    import osworld_agent.adapters.browser_runtime as module
    executable = tmp_path / "chrome.exe"
    executable.write_bytes(b"fixture, never executed")
    profile = tmp_path / "profile"
    profile.mkdir()
    active = profile / "DevToolsActivePort"
    active.write_text("12345\n/browser/socket", encoding="utf-8")
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(module, "urlopen", lambda *a, **kw: Response())
    monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **kw: pytest.fail("Never launch into active profile"))
    runtime = BrowserRuntime(Native(), BrowserRuntimeConfig(executable=str(executable), profile_dir=str(profile)), tmp_path)
    with pytest.raises(RuntimeError, match="Profile is active"):
        runtime.open()
    assert active.exists()


def test_missing_cdp_can_adopt_only_a_new_process_with_the_own_profile(tmp_path, monkeypatch):
    import psutil
    profile = tmp_path / "private-profile"
    profile.mkdir()
    def process(pid, path):
        return SimpleNamespace(pid=pid, info={"pid": pid, "name": "msedge.exe"},
                               cmdline=lambda: ["msedge.exe", "--user-data-dir=" + str(path)])
    old = process(10, profile)
    own = process(40, profile)
    unrelated = process(50, tmp_path / "other-profile")
    renderer = process(60, profile)
    renderer.cmdline = lambda: ["msedge.exe", "--type=renderer", "--user-data-dir=" + str(profile)]
    monkeypatch.setattr(psutil, "process_iter", lambda *a: [old, own, unrelated, renderer])
    runtime = BrowserRuntime(Native(), artifact_dir=tmp_path)
    runtime.profile_dir = profile
    runtime._prelaunch_pids = {10}
    runtime._adopt_delegated_process()
    assert runtime.process_id == 40 and runtime._owned_process is own
    runtime._prelaunch_pids.add(40)
    runtime._owned_process = None
    runtime._adopt_delegated_process()
    assert runtime._owned_process is None


@pytest.fixture
def delayed_launch(tmp_path, monkeypatch):
    import psutil
    import osworld_agent.adapters.browser_runtime as module
    from dataclasses import replace
    executable = tmp_path / "chrome.exe"
    executable.write_bytes(b"fixture; never executed")
    profile = tmp_path / "private-profile"
    now = [0.0]
    pid_ready = [0.0]
    window_ready = [0.0]
    window_queries = []
    activated = []

    def process(pid, path, kind=""):
        arguments = ["chrome.exe", "--user-data-dir=" + str(path)] + (["--type=" + kind] if kind else [])
        return SimpleNamespace(pid=pid, info={"name": "chrome.exe"}, cmdline=lambda: arguments,
                               wait=lambda timeout: None)
    old = process(10, profile)
    unrelated = process(60, tmp_path / "another-profile")
    renderer = process(70, profile, "renderer")
    own = process(40, profile)
    unreadable = SimpleNamespace(pid=80, info={"name": None})
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(module.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    monkeypatch.setattr(psutil, "pids", lambda: [10])
    monkeypatch.setattr(psutil, "process_iter", lambda *a: [old, unrelated, renderer, unreadable] + ([own] if now[0] >= pid_ready[0] else []))

    def launch(*args, **kwargs):
        # The debug port is ready before the actual browser GUI window.
        (profile / "DevToolsActivePort").write_text("12345\n/browser/socket", encoding="utf-8")
        return SimpleNamespace(pid=20, poll=lambda: 0)
    monkeypatch.setattr(module.subprocess, "Popen", launch)
    native = Native()
    native.foreground = replace(BROWSER, process_id=1, window_id="100")
    def windows(pid):
        window_queries.append(pid)
        return [400] if pid == 40 and now[0] >= window_ready[0] else []
    native.windows = windows
    def activate(window):
        activated.append(window)
        native.foreground = replace(BROWSER, process_id=40, window_id=str(window))
    native.activate = activate
    runtime = BrowserRuntime(native, BrowserRuntimeConfig(executable=str(executable), profile_dir=str(profile),
                             startup_timeout_s=1), tmp_path)
    monkeypatch.setattr(runtime, "_connect", lambda: (_ for _ in ()).throw(RuntimeError("CDP temporarily unavailable")))
    yield runtime, now, pid_ready, window_ready, window_queries, activated
    runtime.close()


def test_visual_fallback_waits_for_delayed_window(delayed_launch):
    runtime, now, _, window_ready, window_queries, activated = delayed_launch
    window_ready[0] = 0.2
    result = runtime.open("https://example.com")
    assert result["process_id"] == 40 and result["structure_error"] == "RuntimeError"
    assert 0.2 <= now[0] <= runtime.config.startup_timeout_s
    assert len(window_queries) > 1 and set(window_queries) == {40} and activated == [400]
    assert runtime._owned_process.pid == 40 and runtime.session is None


def test_visual_fallback_waits_for_delayed_delegated_pid(delayed_launch):
    runtime, now, pid_ready, window_ready, window_queries, activated = delayed_launch
    pid_ready[0], window_ready[0] = 0.15, 0.25
    result = runtime.open("https://example.com")
    assert result["process_id"] == 40 and 0.25 <= now[0] <= runtime.config.startup_timeout_s
    assert set(window_queries) == {40} and activated == [400]
    assert runtime._owned_process.pid == 40


def test_delayed_launch_without_visible_window_has_bounded_error(delayed_launch):
    runtime, now, _, window_ready, window_queries, activated = delayed_launch
    window_ready[0] = float("inf")
    with pytest.raises(RuntimeError, match="Dedicated browser window unavailable"):
        runtime.open()
    assert now[0] == pytest.approx(runtime.config.startup_timeout_s)
    assert set(window_queries) == {40} and not activated


def test_delayed_launch_never_uses_old_or_unrelated_browser(delayed_launch):
    runtime, now, pid_ready, _, window_queries, activated = delayed_launch
    pid_ready[0] = float("inf")
    with pytest.raises(RuntimeError, match="Dedicated browser window unavailable"):
        runtime.open()
    assert now[0] == pytest.approx(runtime.config.startup_timeout_s)
    assert runtime._owned_process is None and not window_queries and not activated


def test_attached_browser_wait_does_not_adopt_or_own_existing_process(tmp_path, monkeypatch):
    import osworld_agent.adapters.browser_runtime as module
    now = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(module.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    native = Native()
    native.foreground = BROWSER
    native.windows = lambda pid: [30] if pid == 40 and now[0] >= 0.2 else []
    runtime = BrowserRuntime(native, BrowserRuntimeConfig(endpoint="http://localhost:9222", startup_timeout_s=1), tmp_path)
    monkeypatch.setattr(runtime, "_connect", lambda: setattr(runtime, "process_id", 40))
    monkeypatch.setattr(runtime, "_adopt_delegated_process", lambda: pytest.fail("Never adopt an attached browser"))
    assert runtime.open()["process_id"] == 40
    assert now[0] >= 0.2 and runtime.process is None and runtime._owned_process is None
    runtime.close()
