"""Scene transitions use injected focus signals, real Chrome AX capture, no OSWorld."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from osworld_agent.actions import Action
from osworld_agent.adapters.browser_env import BrowserEnvironment
from osworld_agent.agent import Agent
from osworld_agent.config import ModelConfig
from osworld_agent.env import Environment, Observation, StepResult
from osworld_agent.model.decision_model import ChatModel
from osworld_agent.model.grounding import GroundingResult, NullGrounding
from osworld_agent.pipeline import Pipeline, StepState
from osworld_agent.scene import Foreground, Scene, browser_context, remote_linux_probe
from osworld_agent.trajectory import build_trajectory

FIXTURE = Path(__file__).parent / "fixtures" / "browser_task.html"
BROWSER = Foreground.from_raw({"available": True, "process_name": "chrome.exe", "process_id": 1, "window_id": "10"})
DESKTOP = Foreground.from_raw({"available": True, "process_name": "notepad.exe", "process_id": 2, "window_id": "20"})
NATIVE = Foreground.from_raw({"available": True, "process_name": "chrome.exe", "process_id": 1, "window_id": "10", "menu_active": True})


@pytest.fixture
def browser():
    env = BrowserEnvironment(FIXTURE.resolve().as_uri())
    env.reset("")
    yield env
    env.close()


@pytest.mark.parametrize("raw, expected, native", [
    ({"available": True, "process_name": "notepad.exe", "title": "Chrome user guide"}, False, False),
    ({"available": True, "process_name": "C:\\Browser\\chrome.exe", "title": "No browser name"}, True, False),
    ({"available": True, "process_name": "chrome-helper.exe"}, False, False),
    ({"available": True, "process_name": "msedge.exe", "window_class": "#32770"}, True, True),
    ({"available": True, "process_name": "chrome.exe", "menu_active": True}, True, True),
    ({"available": False}, False, False),
])
def test_process_identity_not_title_or_cursor(raw, expected, native):
    foreground = Foreground.from_raw(raw)
    assert foreground.is_browser is expected and foreground.native_ui is native


def test_gate_switches_and_drops_previous_snapshot(browser):
    phase = [BROWSER]
    browser.session.foreground_probe = lambda: phase[0]
    browser.session._managed_page = False
    observed = browser.observe()
    assert observed.browser_use and observed.structure_available
    ref = next(n["ref"] for n in observed.context[0]["nodes"] if n["name"] == "Save customer" and n["direct"])
    action = Action("click", {"target": "Save customer", "target_ref": ref})
    route = browser.route_action(action, observed)
    phase[0] = DESKTOP
    # Focus changed while the model was reasoning: no dispatch into the old page.
    execution = browser.session.execute(action, route)
    assert execution["status"] == "rejected" and not execution["dispatched"]
    desktop = browser.observe()
    assert not desktop.browser_use and not desktop.context
    assert browser.session.snapshot is None and browser.session.nodes == {}
    with pytest.raises(RuntimeError, match="outside browser content"):
        browser.session.source()
    phase[0] = BROWSER
    resumed = browser.observe()
    assert resumed.browser_use and resumed.structure_available
    assert browser.route_action(action, resumed)["channel"] == "reobserve"
    phase[0] = NATIVE
    dialog = browser.observe()
    assert dialog.info["scene"]["mode"] == "browser_native" and not dialog.browser_use


def test_ax_failure_keeps_scene_but_disables_structure(browser, monkeypatch):
    original = browser.session._cdp.send
    def send(method, *args, **kwargs):
        if method == "Accessibility.getFullAXTree":
            raise RuntimeError("AX unavailable")
        return original(method, *args, **kwargs)
    monkeypatch.setattr(browser.session._cdp, "send", send)
    observation = browser.observe()
    assert observation.browser_use and not observation.structure_available
    assert observation.info["scene"]["reason"] == "ax_capture_failed"
    assert browser_context(observation) == [] and browser.session.nodes == {}


def test_focus_change_during_capture_discards_structure(browser, monkeypatch):
    calls = [0]
    def foreground():
        calls[0] += 1
        return BROWSER if calls[0] == 1 else DESKTOP
    browser.session.foreground_probe = foreground
    observed = browser.observe()
    assert not observed.browser_use and not observed.context
    assert observed.info["scene"]["reason"] == "focus_changed_during_capture"


def test_source_action_cannot_fetch_in_visual_scene():
    env = SimpleNamespace(get_source_for_action=lambda *_: pytest.fail("must not fetch browser data"))
    pipeline = Pipeline(model=ChatModel(ModelConfig()), grounding=NullGrounding(), env=env, system_prompt="JSON")
    state = StepState(obs=Observation(info={"scene": Scene(mode="desktop").to_dict()}))
    result = pipeline.execute(Action("get_page_source"), state)
    assert result.info["status"] == "rejected" and not state.page_source


def test_nonbrowser_does_not_probe_cdp(browser, monkeypatch):
    browser.session.foreground_probe = lambda: DESKTOP
    def forbidden():
        raise AssertionError("CDP must not be attached in desktop scene")
    monkeypatch.setattr(browser.session, "_attach", forbidden)
    observed = browser.observe()
    assert not observed.browser_use and "structure_error" not in observed.info


def test_address_bar_focus_disables_page_context(browser, monkeypatch):
    browser.session.foreground_probe = lambda: BROWSER
    browser.session._managed_page = False
    evaluate = browser.page.evaluate
    def unfocused(expression, *args, **kwargs):
        if expression == "document.hasFocus() && document.visibilityState === 'visible'":
            return False
        return evaluate(expression, *args, **kwargs)
    monkeypatch.setattr(browser.page, "evaluate", unfocused)
    observed = browser.observe()
    assert not observed.browser_use and not observed.context
    assert observed.info["scene"]["reason"] == "page_content_not_focused"


def text_of(messages):
    content = messages[-1]["content"]
    return content if isinstance(content, str) else "\n".join(p.get("text", "") for p in content)


def test_input_gate_blocks_stale_ax_desktop_text_and_source():
    model = ChatModel(ModelConfig())
    env = SimpleNamespace(route_action=lambda *_: pytest.fail("visual scene cannot route through DOM"))
    pipeline = Pipeline(model=model, grounding=NullGrounding(), env=env, system_prompt="Output JSON")
    scene = Scene(mode="desktop", reason="desktop_foreground", page_id="old", url="old-url").to_dict()
    observation = Observation(Image.new("RGB", (20, 20)), text="DESKTOP_A11Y_SENTINEL",
        context=[{"kind": "browser_ax", "snapshot_id": "stale", "nodes": [{"ref": "stale:1", "name": "AX_SENTINEL"}]}],
        info={"scene": scene})
    state = StepState(task="Use the desktop", obs=observation, screenshot_history=[observation.screenshot],
                      page_source="HTML_SENTINEL", page_source_scope=("old", "old-url"))
    text = text_of(pipeline.build_input(state))
    assert all(s not in text for s in ("AX_SENTINEL", "DESKTOP_A11Y_SENTINEL", "HTML_SENTINEL"))
    assert state.prompt_metadata["exposed_refs"] == [] and state.page_source == ""
    pipeline.ground(Action("wait", {"seconds": 1}), state)
    pipeline.ground(Action("click", {"target": "Save", "target_ref": "stale:1"}), state)
    assert state.route["channel"] == "reobserve"
    recorded = pipeline._observation_record(observation, "o_1")
    assert not recorded["context"] and recorded["info"]["scene"]["browser_use"] == 0


def test_source_is_bound_to_page_and_delivered_once(browser):
    observation = browser.observe()
    pipeline = Pipeline(model=ChatModel(ModelConfig()), grounding=NullGrounding(), env=browser, system_prompt="JSON")
    scene = observation.info["scene"]
    state = StepState(task="Inspect", obs=observation, screenshot_history=[observation.screenshot],
        page_source="SOURCE_SENTINEL", page_source_scope=(scene["page_id"], scene["url"]))
    assert "SOURCE_SENTINEL" in text_of(pipeline.build_input(state))
    assert "SOURCE_SENTINEL" not in text_of(pipeline.build_input(state))
    state.page_source, state.page_source_scope = "OTHER_PAGE_SOURCE", ("other-page", scene["url"])
    assert "OTHER_PAGE_SOURCE" not in text_of(pipeline.build_input(state))


def test_remote_probe_never_falls_back_to_agent_host():
    assert not remote_linux_probe(None).available
    seen = []
    controller = SimpleNamespace(run_python_script=lambda code: seen.append(code) or {"output": json.dumps({
        "available": True, "process_name": "google-chrome", "window_id": "1", "process_id": 123})})
    foreground = remote_linux_probe(controller)
    assert foreground.is_browser and foreground.process_id == 123
    assert "getactivewindow" in seen[0] and "getwindowname" not in seen[0]


def test_single_task_desktop_browser_native_desktop_trajectory(browser):
    from osworld_agent.tests.test_browser_pipeline import DesktopFixture
    phases = [DESKTOP, BROWSER, NATIVE, DESKTOP]
    class MixedEnvironment(DesktopFixture):
        index = 0
        def reset(self, task):
            self.index = 0
            browser.session.foreground_probe = lambda: phases[self.index]
            browser.session._managed_page = False
            return self.observe()
        def observe(self):
            observation = browser.observe()
            if not observation.browser_use:
                observation.screenshot = Image.new("RGB", (100, 100), "gray")
                observation.text = "DESKTOP_A11Y_MUST_NOT_REACH_POLICY"
            return observation
        def step(self, action):
            assert "x" in action.args  # desktop and native UI used visual grounding
            self.index += 1
            return StepResult(self.observe())
        def route_action(self, action, observation):
            return browser.route_action(action, observation)
        def execute_routed(self, action, route):
            result = browser.session.execute(action, route)
            assert result["status"] == "executed"
            self.index += 1
            return StepResult(self.observe(), info=result)
    class Policy(ChatModel):
        index = 0
        def chat(self, messages, **kwargs):
            text = text_of(messages)
            assert "DESKTOP_A11Y_MUST_NOT_REACH_POLICY" not in text
            action = {"type": "click", "target": "Desktop or native button"}
            if self.index == 1:
                nodes = [json.loads(l) for l in text.splitlines() if l.startswith('{"ref":')]
                node = next(n for n in nodes if n.get("name") == "Save customer" and n.get("direct"))
                action = {"type": "click", "target": "Save customer", "target_ref": node["ref"]}
            elif self.index == 3:
                action = {"type": "fail"}
            self.index += 1
            return json.dumps({"next_action": action, "state_update": {}})
    class Grounder(NullGrounding):
        calls = 0
        def locate(self, screenshot, target):
            self.calls += 1
            return GroundingResult(20, 20)
    grounder = Grounder()
    result = Agent(Policy(ModelConfig()), grounder, MixedEnvironment(), max_steps=4).run("Use desktop, then browser, then native dialog, then desktop")
    trajectory = build_trajectory("mixed", "desktop", result.task, result)
    assert [s["observation"]["info"]["scene"]["browser_use"] for s in trajectory["steps"]] == [0, 1, 0, 0]
    assert [s["routing"]["channel"] for s in trajectory["steps"][:3]] == ["visual", "browser_dom", "visual"]
    assert grounder.calls == 2
    assert all(s["policy_input"] for s in trajectory["steps"])
