"""Desktop coordinate/focus guards without injecting input into the test machine."""
from dataclasses import replace
import json
from types import SimpleNamespace

from PIL import Image
import pytest

from osworld_agent.actions import Action
from osworld_agent.adapters.browser_runtime import BrowserRuntime, BrowserRuntimeConfig, validate_url
from osworld_agent.adapters.windows_env import WindowsEnvironment
from osworld_agent.adapters.windows_native import desktop_point, key_code, unicode_units
from osworld_agent.adapters.windows_native import WindowsNative
from osworld_agent.config import Config, ModelConfig
from osworld_agent.model.decision_model import ChatModel
from osworld_agent.model.grounding import NullGrounding
from osworld_agent.pipeline import Pipeline, StepState
from osworld_agent.scene import Foreground


DESKTOP = Foreground(available=True, window_id="10", focus_id="11", process_id=20, process_name="notepad.exe")
BROWSER = Foreground(available=True, window_id="30", process_id=40, process_name="chrome.exe", is_browser=True)
GEOMETRY = {"left": -100, "top": -20, "width": 300, "height": 200, "scale": 1,
            "coordinate_space": "desktop_physical", "target_monitor": "TEST_DISPLAY",
            "capture_geometry": {"left": -100, "top": -20, "width": 300, "height": 200},
            "monitors": [{"device": "TEST_DISPLAY", "primary": True, "left": -100, "top": -20, "width": 300, "height": 200}]}


class Native:
    foreground = DESKTOP
    closed = False
    def __init__(self):
        self.events = []
        self.safety_events = []
        self.layout = dict(GEOMETRY)
    def probe(self):
        return self.foreground
    def geometry(self):
        return self.layout
    def capture(self):
        capture = self.layout.get("capture_geometry", self.layout)
        return Image.new("RGB", (capture["width"], capture["height"])), dict(self.layout)
    def window_bounds(self, window_id):
        capture = self.layout.get("capture_geometry", self.layout)
        return {"left": capture["left"], "top": capture["top"],
                "right": capture["left"] + capture["width"], "bottom": capture["top"] + capture["height"]}
    def window_process(self, window_id):
        return {"10": 20, "30": 40}.get(str(window_id), 0)
    def window_at_point(self, point):
        return self.foreground.window_id
    def related_window(self, candidate, expected):
        return candidate == expected
    def place_window(self, window_id):
        self.safety_events.append(("place", str(window_id)))
    def activate(self, window_id, **kwargs):
        self.safety_events.append(("activate", str(window_id), kwargs))
        target = {"10": DESKTOP, "30": BROWSER}[str(window_id)]
        self.foreground = replace(target, generation=self.foreground.generation + 1)
    def click(self, point, geometry, **kwargs):
        after_click = kwargs.pop("after_click", None)
        self.events.append(("click", point, kwargs))
        if after_click:
            after_click()
    def hotkey(self, keys):
        self.events.append(("keys", keys))
    def type_text(self, text, guard=None):
        if guard:
            guard()
        self.events.append(("text", text))
    def close(self):
        self.closed = True


@pytest.fixture
def desktop():
    native = Native()
    env = WindowsEnvironment(native=native, settle_ms=0)
    env.reset("Read the desktop then open a browser")
    yield env, native
    env.close()


def test_default_does_not_launch_browser_or_add_desktop_text(desktop):
    env, native = desktop
    observed = env.observe()
    assert not observed.browser_use and observed.context == [] and observed.text == ""
    assert env.runtime.process is None and env.browser_session is None
    assert observed.info["coordinate_space"] == "desktop_physical"


def test_negative_monitor_origin_and_unicode_input(desktop):
    env, native = desktop
    result = env.step(Action("type", {"x": 5, "y": 6, "text": "中文😀\n论文", "overwrite": True}))
    assert result.info["status"] == "executed"
    assert native.events == [("click", (-95, -14), {"right": False, "count": 1}),
                             ("keys", ["ctrl", "a"]), ("keys", ["backspace"]), ("text", "中文😀\n论文")]
    assert unicode_units("中😀") == [0x4E2D, 0xD83D, 0xDE00]
    assert desktop_point(299.9, 199.9, GEOMETRY) == (199, 179)


@pytest.mark.parametrize("point", [(300, 10), (-1, 10), (10, 200), (float("nan"), 0), (0, float("inf"))])
def test_outside_coordinates_never_dispatch(desktop, point):
    env, native = desktop
    result = env.step(Action("click", {"x": point[0], "y": point[1]}))
    assert result.info["status"] == "rejected" and not result.info["dispatched"]
    assert native.events == []


def test_monitor_gap_cannot_be_clicked():
    geometry = {**GEOMETRY, "monitors": [{"left": -100, "top": -20, "width": 40, "height": 200}]}
    with pytest.raises(ValueError, match="between monitors"):
        desktop_point(50, 20, geometry)


@pytest.mark.parametrize("new_focus", [BROWSER, replace(DESKTOP, generation=2),
                                     replace(DESKTOP, focus_id="12"), Foreground()])
def test_focus_change_including_away_and_back_rejects_old_action(desktop, new_focus):
    env, native = desktop
    observation = env.last_observation
    native.foreground = new_focus
    assert env.preflight(Action("press", {"key": "enter"}), observation)["channel"] == "reobserve"
    result = env.step(Action("press", {"key": "enter"}))
    assert not result.info["dispatched"] and not native.events


def test_screen_layout_change_rejects_input(desktop):
    env, native = desktop
    native.layout = {**GEOMETRY, "left": -200}
    result = env.step(Action("click", {"x": 20, "y": 20}))
    assert "geometry changed" in result.info["reason"] and not native.events


def test_window_resize_and_dpi_change_reject_stale_visual_coordinates(desktop):
    env, native = desktop
    rectangle = {"left": 0, "top": 0, "right": 200, "bottom": 100, "dpi": 120}
    native.window_geometry = lambda window: dict(rectangle)
    observation = env.observe()
    rectangle["dpi"] = 144
    rectangle["right"] = 250
    rejected = env.preflight(Action("click", {"x": 50, "y": 50}), observation)
    assert rejected["channel"] == "reobserve" and "DPI" in rejected["reason"]


def test_other_browser_and_native_ui_are_visual(desktop):
    env, native = desktop
    env.runtime.process_id = 50
    native.foreground = BROWSER
    assert env.adopt_window()
    observed = env.observe()
    assert not observed.context and not observed.structure_available
    assert observed.info["scene"]["reason"] == "browser_instance_not_bound"
    native.foreground = replace(BROWSER, native_ui=True)
    observed = env.observe()
    assert not observed.browser_use and observed.info["scene"]["mode"] == "browser_native"


def test_ordinary_apps_do_not_attempt_cdp(desktop, monkeypatch):
    env, _ = desktop
    env.runtime.endpoint = "http://127.0.0.1:9999"
    monkeypatch.setattr(env.runtime, "_connect", lambda: pytest.fail("CDP must not run in ordinary apps"))
    assert env.observe().context == []


def test_unavailable_structure_does_not_stop_desktop_task(desktop, monkeypatch):
    env, native = desktop
    native.foreground = BROWSER
    assert env.adopt_window()
    env.runtime.endpoint = "http://127.0.0.1:9999"
    monkeypatch.setattr(env.runtime, "_connect", lambda: (_ for _ in ()).throw(RuntimeError("No CDP")))
    observation = env.observe()
    assert observation.screenshot and observation.context == []
    assert observation.info["structure_error"] == "RuntimeError"
    result = env.step(Action("press", {"key": "tab"}))
    assert result.info["status"] == "executed" and native.events == [("keys", ["tab"])]


def test_attached_browser_is_not_closed_on_environment_close():
    native = Native()
    runtime = BrowserRuntime(native, BrowserRuntimeConfig(endpoint="http://localhost:9222"))
    events = []
    runtime._browser = SimpleNamespace(close=lambda: pytest.fail("Existing browser must stay open"))
    runtime._pw = SimpleNamespace(stop=lambda: events.append("disconnect"))
    runtime.close()
    assert events == ["disconnect"]


def test_pipeline_refresh_and_preflight_preserve_task_memory(desktop):
    env, native = desktop
    pipe = Pipeline(model=ChatModel(ModelConfig()), grounding=NullGrounding(), env=env, system_prompt="JSON")
    state = pipe._init_state("Read Notepad, download paper, return")
    pipe.preprocess(state)
    state.memory.known_facts["paper"] = "retained"
    previous = state.obs
    native.foreground = replace(DESKTOP, generation=2)
    execution = pipe.execute(Action("press", {"key": "enter"}), state)
    assert execution.executed == "rejected" and not native.events
    pipe.preprocess(state)
    assert state.obs is not previous and state.obs.info["focus_version"] == 2
    assert state.memory.known_facts["paper"] == "retained"


def test_config_and_actions_remain_environment_independent():
    from osworld_agent.actions import validate_action
    cfg = Config.from_dict({"env": {"provider": "windows", "browser": {"channel": "msedge"}}})
    assert cfg.env.browser["channel"] == "msedge"
    assert validate_action(Action("open_browser", {"url": "https://example.com"})) is None
    assert key_code("Ctrl") == 0x11 and key_code("ArrowDown") == 0x28
    with pytest.raises(ValueError):
        validate_url("javascript:alert(1)")
    with pytest.raises(ValueError, match="local endpoint"):
        BrowserRuntime(Native(), BrowserRuntimeConfig(endpoint="http://example.com:9222"))


def test_refreshed_decision_observation_matches_rl_successor(desktop):
    from osworld_agent.agent import Agent
    from osworld_agent.trajectory import build_trajectory
    env, native = desktop
    frames = [0]
    def capture():
        frames[0] += 1
        return Image.new("RGB", (300, 200), (frames[0], 0, 0)), dict(GEOMETRY)
    native.capture = capture
    class Policy(ChatModel):
        index = 0
        def chat(self, messages, **kwargs):
            self.last_call = {"provenance": "scripted_test_double"}
            self.index += 1
            return json.dumps({"next_action": {"type": "wait", "seconds": 0.001} if self.index == 1 else {"type": "fail"}})
    result = Agent(Policy(ModelConfig()), NullGrounding(), env, max_steps=2).run("Keep one task across refreshed frames")
    trajectory = build_trajectory("frames", "computer", result.task, result)
    first, second = trajectory["steps"]
    assert trajectory["outcome"]["score"] is None and not trajectory["outcome"]["evaluation_available"]
    assert first["next_observation"] == second["observation"]
    assert first["post_action_observation"]["id"] == "after_a_1"
    assert first["post_action_observation"]["screenshot"] != second["observation"]["screenshot"]
    assert all(c["provenance"] == "scripted_test_double" for c in first["model_calls"])


def test_optional_browser_launch_action_is_not_offered_to_legacy_environments(desktop):
    from osworld_agent.agent import Agent
    env, _ = desktop
    legacy = SimpleNamespace(os_name="linux")
    model = ChatModel(ModelConfig())
    grounder = NullGrounding()
    legacy_agent = Agent(model, grounder, legacy)
    desktop_agent = Agent(model, grounder, env)
    assert "open_browser" not in legacy_agent.system_prompt
    assert "open_browser" in desktop_agent.system_prompt
    pipe = Pipeline(model=model, grounding=grounder, env=legacy, system_prompt="JSON")
    assert pipe._validate_action(Action("open_browser")) is not None


@pytest.mark.parametrize("occluded", [False, True])
def test_scroll_never_goes_to_a_background_window(occluded):
    events = []
    class User:
        def GetCursorPos(self, pointer):
            pointer._obj.x, pointer._obj.y = 1000, 1000
            return True
        def WindowFromPoint(self, point):
            return 10 if point.x == 50 and not occluded else 20
        def GetAncestor(self, window, kind): return window
        def GetClientRect(self, window, pointer):
            pointer._obj.right, pointer._obj.bottom = 100, 100
            return True
        def ClientToScreen(self, window, pointer): return True
    native = WindowsNative.__new__(WindowsNative)
    native.user = User()
    native.move = lambda point, geometry: events.append(("move", point))
    native._mouse = lambda flags, data=0: (flags, data)
    native._send = lambda inputs: events.append(("wheel", inputs))
    if occluded:
        with pytest.raises(RuntimeError, match="occluded"):
            native.scroll(2, "down", window_id="10", geometry=GEOMETRY)
        assert not events
    else:
        assert native.scroll(2, "down", window_id="10", geometry=GEOMETRY) == (50, 50)
        assert events == [("move", (50, 50)), ("wheel", [(0x0800, -240)])]


@pytest.mark.parametrize("alias", ["win", "Windows", "super", "META"])
def test_windows_key_aliases_dispatch_balanced_hotkey_without_desktop_input(alias):
    events = []
    native = WindowsNative.__new__(WindowsNative)
    native._key = lambda code, up=False: (code, up)
    native._send = lambda batch: events.extend(batch)
    native.hotkey([alias, "r"])
    assert key_code(alias) == 0x5B
    assert events == [(0x5B, False), (0x52, False), (0x52, True), (0x5B, True)]


def test_invalid_hotkey_cannot_partially_dispatch_windows_key():
    native = WindowsNative.__new__(WindowsNative)
    native._send = lambda batch: pytest.fail("Validation must precede input dispatch")
    with pytest.raises(ValueError, match="Unsupported key"):
        native.hotkey(["win", "unsupported-key"])


@pytest.fixture
def computer_doctor(tmp_path, monkeypatch):
    from osworld_agent.script import doctor
    import osworld_agent.adapters.browser_runtime as runtime_module
    import osworld_agent.adapters.browser_env as browser_module
    import osworld_agent.adapters.windows_env as windows_module
    report_path = tmp_path / "doctor.json"
    received_configs = []
    closed = []
    monkeypatch.setattr(doctor.sys, "argv", ["doctor", "--computer", "--output", str(report_path)])
    monkeypatch.setattr(doctor, "win32_api_check", lambda: "mocked Win32 APIs")
    # These tests exercise doctor routing; real module availability is covered by installation checks.
    monkeypatch.setattr(doctor, "importlib", SimpleNamespace(import_module=lambda name: SimpleNamespace()))
    monkeypatch.setattr(browser_module, "launch_browser", lambda *a, **kw: pytest.fail("Computer doctor must not launch a browser"))
    for name in ("COMPUTER_BROWSER_CHANNEL", "COMPUTER_BROWSER_EXECUTABLE", "COMPUTER_CDP_ENDPOINT",
                 "COMPUTER_BROWSER_PROFILE_DIR", "COMPUTER_DOWNLOAD_DIR", "COMPUTER_MONITOR"):
        monkeypatch.delenv(name, raising=False)

    class FakeEnvironment:
        def __init__(self, *, browser_config, monitor="primary"):
            received_configs.append(browser_config)
            self.native = Native()
        def observe(self):
            pytest.fail("Doctor must not connect CDP or collect optional page structure")
        def close(self):
            closed.append(True)
    monkeypatch.setattr(windows_module, "WindowsEnvironment", FakeEnvironment)
    return doctor, runtime_module, report_path, received_configs, closed


def test_computer_doctor_allows_pure_visual_desktop_without_browser(computer_doctor, monkeypatch):
    doctor, runtime_module, report_path, received_configs, closed = computer_doctor
    monkeypatch.setattr(runtime_module, "discover_browser", lambda config: (_ for _ in ()).throw(RuntimeError("No configured browser found")))
    assert doctor.main() == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    capability = next(c for c in report["checks"] if c["name"] == "optional computer browser capability")
    assert report["success"] and capability["success"] and capability["optional"]
    assert not capability["available"] and capability["detail"]["reason"] == "No configured browser found"
    assert received_configs[0].channel == "auto" and closed == [True]
    assert "browser launch" not in {c["name"] for c in report["checks"]}


def test_computer_doctor_uses_custom_browser_and_local_cdp_without_connecting(computer_doctor, tmp_path, monkeypatch):
    doctor, runtime_module, report_path, received_configs, closed = computer_doctor
    executable = tmp_path / "中文浏览器.exe"
    executable.write_bytes(b"fixture; never launched")
    monkeypatch.setenv("COMPUTER_BROWSER_CHANNEL", "msedge")
    monkeypatch.setenv("COMPUTER_BROWSER_EXECUTABLE", str(executable))
    monkeypatch.setenv("COMPUTER_CDP_ENDPOINT", "http://127.0.0.1:9222")
    monkeypatch.setattr(runtime_module, "discover_browser", lambda config: (executable, config.channel))
    assert doctor.main() == 0
    config = received_configs[0]
    assert config.channel == "msedge" and config.executable == str(executable)
    assert config.endpoint == "http://127.0.0.1:9222" and closed == [True]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    capability = next(c for c in report["checks"] if c["name"] == "optional computer browser capability")
    assert capability["available"] and not capability["detail"]["cdp_connectivity_checked"]


@pytest.mark.parametrize("setting,value", [
    ("COMPUTER_BROWSER_CHANNEL", "unsupported-channel"),
    ("COMPUTER_BROWSER_EXECUTABLE", "missing-browser.exe"),
    ("COMPUTER_CDP_ENDPOINT", "http://user:credential-secret@localhost:9222/?key=credential-secret"),
])
def test_computer_doctor_rejects_invalid_configuration_without_leaking_credentials(computer_doctor, monkeypatch, setting, value, capsys):
    doctor, _, report_path, received_configs, _ = computer_doctor
    monkeypatch.setenv(setting, value)
    assert doctor.main() == 1
    report_text = report_path.read_text(encoding="utf-8")
    report = json.loads(report_text)
    assert not report["success"] and not received_configs
    assert "credential-secret" not in report_text + capsys.readouterr().out
    assert any(c["name"] == "computer browser configuration" and not c["success"] for c in report["checks"])


def test_computer_doctor_reports_explicit_missing_browser(computer_doctor, monkeypatch):
    doctor, runtime_module, report_path, _, _ = computer_doctor
    monkeypatch.setenv("COMPUTER_BROWSER_CHANNEL", "chrome")
    monkeypatch.setattr(runtime_module, "discover_browser", lambda config: (_ for _ in ()).throw(RuntimeError("No configured browser found")))
    assert doctor.main() == 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert any(c["name"] == "optional computer browser capability" and not c["success"] for c in report["checks"])


def test_computer_doctor_requires_cross_application_fixture(computer_doctor, monkeypatch):
    from pathlib import Path
    doctor, runtime_module, report_path, _, _ = computer_doctor
    original_is_file = Path.is_file
    monkeypatch.setattr(Path, "is_file", lambda path: False if path.name == "computer_papers.html" else original_is_file(path))
    monkeypatch.setattr(runtime_module, "discover_browser", lambda config: (_ for _ in ()).throw(RuntimeError("No configured browser found")))
    assert doctor.main() == 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    resources = next(c for c in report["checks"] if c["name"] == "portable package resources")
    assert not resources["success"] and "computer_papers.html" in resources["detail"]
