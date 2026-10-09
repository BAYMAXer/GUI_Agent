"""Work-screen routing and fixture cleanup with no live Windows input."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from osworld_agent.adapters.browser_runtime import BrowserRuntime, BrowserRuntimeConfig
from osworld_agent.run_computer_windows import build_parser
from osworld_agent import run_computer_windows as runner
from osworld_agent.scene import Foreground
from osworld_agent.script import acceptance_computer_windows as acceptance
from osworld_agent.script import doctor, smoke_computer_windows as smoke


DEVICE = r"\\.\DISPLAY2"


def test_computer_monitor_defaults_environment_and_cli(monkeypatch):
    monkeypatch.delenv("COMPUTER_MONITOR", raising=False)
    assert build_parser().parse_args([]).monitor == "primary"
    monkeypatch.setenv("COMPUTER_MONITOR", DEVICE)
    assert build_parser().parse_args([]).monitor == DEVICE
    assert build_parser().parse_args(["--monitor", "primary"]).monitor == "primary"


def test_computer_runner_safety_stop_always_returns_nonzero(monkeypatch):
    monkeypatch.setattr(runner, "environment_setting", lambda name: "fake-key")
    monkeypatch.setattr(runner, "execute_task", lambda args: {"success": True, "safety_stop": True})
    monkeypatch.setattr(sys, "argv", ["computer", "--task", "fixture", "--model", "fixture",
                                     "--api-url", "https://example.invalid/v1"])
    assert runner.main() == 1


def test_computer_runner_exports_safety_reason_and_selected_monitor(tmp_path, monkeypatch):
    received = []
    evidence = {"code": "focus_recovery_failed", "reason": "Windows rejected task focus recovery"}
    result = SimpleNamespace(success=False, steps=0, final_answer=evidence["reason"], trajectory=[],
        safety_stop=evidence, termination_reason="safety_stop", evaluation_available=False)

    class Environment:
        def __init__(self, *, browser_config, artifact_dir, monitor):
            received.append(monitor)
            self.runtime = SimpleNamespace(downloads=[])
        def close(self):
            received.append("closed")

    monkeypatch.setattr(runner, "WindowsEnvironment", Environment)
    monkeypatch.setattr(runner, "ChatModel", lambda config: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(runner, "build_grounding_model", lambda config: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(runner, "Agent", lambda *args, **kwargs: SimpleNamespace(run=lambda task: result))
    args = build_parser().parse_args(["--monitor", DEVICE, "--output", str(tmp_path), "--task", "fixture"])
    report = runner.execute_task(args)
    assert received == [DEVICE, "closed"]
    assert report["safety_stop"] and report["safety_stop_detail"] == evidence
    trajectory = json.loads(Path(report["trajectory"]).read_text(encoding="utf-8"))
    assert trajectory["outcome"]["safety_stop"] == evidence
    assert (tmp_path / "report.json").is_file() and (tmp_path / "sft.jsonl").is_file()


def test_acceptance_passes_selected_device_to_all_stages(tmp_path, monkeypatch):
    monkeypatch.setattr(acceptance.sys, "argv", ["acceptance", "--monitor", DEVICE,
        "--output", str(tmp_path), "--model", "fixture", "--api-url", "https://example.invalid/v1"])
    monkeypatch.setattr(acceptance, "environment_setting", lambda name: "fixture-key")
    commands = []
    payload = {"success": True, "score": 1, "evaluation_available": True,
               "reward_source": "environment_evaluator", "cross_application_verified": True}

    def run(command, cwd):
        commands.append(command)
        output = command[command.index("--output") + 1]
        destination = (tmp_path / "doctor.json") if "--computer" in command else Path(output) / "report.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(payload), encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(acceptance.subprocess, "run", run)
    assert acceptance.main() == 0
    assert len(commands) == 3
    assert all(command[command.index("--monitor") + 1] == DEVICE for command in commands)


@pytest.fixture
def doctor_monitor(tmp_path, monkeypatch):
    import osworld_agent.adapters.windows_env as windows
    received, calls = [], []
    capture = {"left": -1920, "top": 0, "width": 1920, "height": 1080}
    monitors = [{**capture, "device": DEVICE, "primary": False,
                 "work_area": {**capture, "height": 1040}},
                {"left": 0, "top": 0, "width": 2560, "height": 1440,
                 "device": r"\\.\DISPLAY1", "primary": True}]
    geometry = {"left": -1920, "top": 0, "width": 4480, "height": 1440,
                "capture_geometry": capture, "target_monitor": DEVICE, "monitors": monitors}
    report = tmp_path / "doctor.json"
    monkeypatch.setattr(doctor.sys, "argv", ["doctor", "--computer", "--monitor", DEVICE, "--output", str(report)])
    monkeypatch.setattr(doctor, "win32_api_check", lambda: "fake APIs")
    monkeypatch.setattr(doctor, "importlib", SimpleNamespace(import_module=lambda name: None))
    monkeypatch.setattr(doctor, "computer_browser_config", lambda: BrowserRuntimeConfig())
    monkeypatch.setattr(doctor, "computer_browser_capability", lambda config: {"available": False})

    class Environment:
        def __init__(self, *, browser_config, monitor):
            received.append(monitor)
            if monitor == "missing":
                raise ValueError("Configured monitor is unavailable")
            self.native = SimpleNamespace(probe=lambda: Foreground(available=True),
                capture=lambda: (SimpleNamespace(size=(1920, 1080)), geometry))
        def observe(self):
            pytest.fail("doctor must capture without running focus recovery")
        def close(self):
            calls.append("closed")

    monkeypatch.setattr(windows, "WindowsEnvironment", Environment)
    return report, received, calls


def test_doctor_lists_device_rects_primary_and_selected_screen(doctor_monitor):
    report, received, calls = doctor_monitor
    assert doctor.main() == 0
    data = json.loads(report.read_text(encoding="utf-8"))
    detail = next(row["detail"] for row in data["checks"] if row["name"] == "interactive Windows desktop")
    assert received == [DEVICE] and calls == ["closed"]
    assert detail["target_monitor"] == DEVICE
    assert detail["capture_geometry"]["left"] == -1920
    assert detail["screenshot_size"] == [1920, 1080]
    assert detail["monitors"][1]["primary"] is True


def test_doctor_invalid_monitor_fails_without_observing_or_activating(doctor_monitor, monkeypatch):
    report, _, calls = doctor_monitor
    monkeypatch.setattr(doctor.sys, "argv", ["doctor", "--computer", "--monitor", "missing", "--output", str(report)])
    assert doctor.main() == 1
    data = json.loads(report.read_text(encoding="utf-8"))
    assert not data["success"] and not calls
    assert any(row["name"] == "interactive Windows desktop" and not row["success"] for row in data["checks"])


def test_fixture_cleanup_safety_stop_does_not_touch_windows():
    env = SimpleNamespace(safety_stop={"reason": "focus changed"})
    smoke.close_notepad(env, SimpleNamespace(window_id="10"), None)


def test_fixture_cleanup_posts_only_to_verified_owned_hwnd(tmp_path, monkeypatch):
    messages = []
    pid = [20]

    def process_id(hwnd, target):
        target._obj.value = pid[0]

    def post(hwnd, message, wparam, lparam):
        messages.append((hwnd, message))

    user = SimpleNamespace(GetWindowThreadProcessId=process_id, PostMessageW=post)
    env = SimpleNamespace(safety_stop=None, native=SimpleNamespace(user=user))
    note = SimpleNamespace(window_id="10", process_id=20)
    monkeypatch.setattr(smoke, "window_title", lambda *args: "owned-note - Notepad")
    smoke.close_notepad(env, note, tmp_path / "owned-note.txt")
    assert messages == [(10, 0x0010)]
    pid[0] = 999
    smoke.close_notepad(env, note, tmp_path / "owned-note.txt")
    assert len(messages) == 1


class WorkNative:
    def __init__(self):
        self.capture = {"left": -100, "top": -20, "width": 300, "height": 200}
        self.bounds = {"left": -90, "top": -10, "right": 180, "bottom": 160}
        self.events = []
        self.foreground = Foreground(available=True, window_id="30", process_id=40, is_browser=True)
    def geometry(self):
        return {"capture_geometry": self.capture}
    def window_bounds(self, window):
        self.events.append(("bounds", window))
        return self.bounds
    def probe(self):
        return self.foreground
    def windows(self, pid):
        return [30]
    def place_window(self, window):
        self.events.append(("place", window))
    def activate(self, *args, **kwargs):
        self.events.append(("activate", args))


def test_attached_browser_outside_work_screen_never_moves_activates_or_reads_structure(tmp_path, monkeypatch):
    native = WorkNative()
    native.bounds["right"] = 210
    runtime = BrowserRuntime(native, BrowserRuntimeConfig(endpoint="http://localhost:9222"), tmp_path)
    monkeypatch.setattr(runtime, "_connect", lambda: setattr(runtime, "process_id", 40))
    with pytest.raises(RuntimeError, match="work screen boundary"):
        runtime.open("https://example.invalid")
    assert not any(event[0] in {"place", "activate"} for event in native.events)
    assert runtime.focused_session(native.foreground) is None


@pytest.mark.parametrize("blocked", [False, True])
def test_browser_navigation_checks_environment_guard_before_cdp_mutation(tmp_path, monkeypatch, blocked):
    native = WorkNative()
    runtime = BrowserRuntime(native, BrowserRuntimeConfig(endpoint="http://localhost:9222"), tmp_path)
    navigation = []
    page = SimpleNamespace(is_closed=lambda: False,
        goto=lambda *args, **kwargs: navigation.append("goto"))
    runtime._browser = SimpleNamespace(contexts=[SimpleNamespace(pages=[page])])
    runtime.session = SimpleNamespace(_real_page_focus=lambda page: True)
    monkeypatch.setattr(runtime, "_connect", lambda: setattr(runtime, "process_id", 40))

    def guard():
        navigation.append("guard")
        if blocked:
            raise RuntimeError("Pinned display layout changed")

    runtime.before_navigate = guard
    if blocked:
        with pytest.raises(RuntimeError, match="Pinned display layout changed"):
            runtime.open("https://example.invalid")
        assert navigation == ["guard"]
    else:
        assert not runtime.open("https://example.invalid")["navigation_needed"]
        assert navigation == ["guard", "goto"]
