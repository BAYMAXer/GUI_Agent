import copy
import json
from pathlib import Path

import pytest
from PIL import Image

from osworld_agent.actions import Action
from osworld_agent.adapters.browser_env import BrowserEnvironment
from osworld_agent.agent import Agent
from osworld_agent.config import ModelConfig, ContextConfig
from osworld_agent.context import PromptCompiler, ContextBudgetError
from osworld_agent.env import Environment, Observation, StepResult
from osworld_agent.model.decision_model import ChatModel
from osworld_agent.model.grounding import NullGrounding, resolve_coords
from osworld_agent.trajectory import build_trajectory, dump_trajectory, restore_messages, iter_sft_records

FIXTURE = Path(__file__).parent / "fixtures" / "browser_task.html"


def evaluate_customer(page):
    return page.evaluate("""() => {
        const r=JSON.parse(localStorage.getItem('customer-record')||'null');
        return !!r && r.customer==='Acme Ltd' && r.plan==='Enterprise' && r.notify===true && window.saveCount===1;
    }""")


class ScriptedPolicy(ChatModel):
    """Protocol/integration test only. This is NOT a model benchmark."""
    def __init__(self):
        super().__init__(ModelConfig(vision=True))
        self.index = 0
        self.inputs = []

    def chat(self, messages, **kwargs):
        self.inputs.append(copy.deepcopy(messages))
        content = messages[-1]["content"]
        text = content if isinstance(content, str) else "\n".join(p.get("text", "") for p in content)
        if "任务完成度核对器" in text:
            return "CONFIRM" if "Saved Acme Ltd" in text else "REJECT missing evidence"
        nodes = []
        for line in text.splitlines():
            if line.startswith('{"ref":'):
                nodes.append(json.loads(line))
        spec = [("type", "Customer name", {"text": "Acme Ltd", "overwrite": True}),
                ("select", "Plan", {"option": "Enterprise"}),
                ("click", "Enable notifications", {}),
                ("click", "Save customer", {}),
                ("request_finish", "", {"answer": "Customer saved"})][self.index]
        self.index += 1
        name, target, extra = spec
        action = {"type": name, **extra}
        if target:
            matches = [n for n in nodes if n.get("name") == target and n.get("direct")]
            assert len(matches) == 1, (target, nodes)
            action.update(target=target, target_ref=matches[0]["ref"])
        return json.dumps({"screen_analysis": "Settings form", "observed_effect": "Observed current state",
            "action_status": "success", "state_update": {}, "next_action": action,
            "expected_effect": "Saved state or updated form"})


class CountingGrounding(NullGrounding):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def locate(self, screenshot, target):
        self.calls += 1
        return super().locate(screenshot, target)


@pytest.fixture
def env():
    env = BrowserEnvironment(FIXTURE.resolve().as_uri(), evaluator=evaluate_customer)
    yield env
    env.close()


def node_action(env, obs, name, target, **args):
    node = next(n for n in obs.context[0]["nodes"] if n["name"] == target and n.get("direct"))
    return Action(name, {"target": target, "target_ref": node["ref"], **args})


def test_real_chrome_pipeline_export_replay(env, tmp_path):
    model, grounding = ScriptedPolicy(), CountingGrounding()
    result = Agent(model, grounding, env, max_steps=6).run("Set Customer name to Acme Ltd, Plan to Enterprise, enable notifications, and Save customer.")
    assert result.success and result.score == 1 and result.evaluation_available
    assert result.steps == 5 and grounding.calls == 0
    assert all(s["routing"]["channel"] == "browser_dom" for s in result.trajectory[:4])
    assert result.trajectory[-1]["terminated"] and not result.trajectory[-1]["truncated"]
    trajectory = build_trajectory("fixture", "browser", result.task, result)
    path = tmp_path / "trajectory.json"
    dump_trajectory(str(path), trajectory)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "data:image" not in path.read_text(encoding="utf-8")
    assert saved["steps"][0]["observation"]["context"][0]["kind"] == "browser_ax"
    for i, step in enumerate(saved["steps"]):
        assert restore_messages(step["policy_input"], path) == model.inputs[i]
        assert (tmp_path / step["next_observation"]["screenshot"]).is_file()
        assert step["prompt_metadata"]["input_estimate"] <= step["prompt_metadata"]["limit"]
    sft = list(iter_sft_records(saved))
    assert len(sft) == 5 and all(s["loss_scope"] == "completion_only" for s in sft)
    assert "assessment" not in sft[0]["prompt"][-1]


def test_stale_navigation_and_disabled_rejected(env):
    obs = env.reset("")
    old = node_action(env, obs, "click", "Save customer")
    disabled = node_action(env, obs, "click", "Unavailable action")
    out = env.session.execute(disabled, env.route_action(disabled, obs))
    assert out["status"] == "rejected" and not out["dispatched"]
    route = env.route_action(old, obs)
    env.page.goto("about:blank")
    out = env.session.execute(old, route)
    assert out["status"] == "rejected" and not out["dispatched"]
    obs2 = env.reset("")
    assert env.route_action(old, obs2)["channel"] == "reobserve"


def test_overlay_semantic_change_and_replacement(env):
    obs = env.reset("")
    action = node_action(env, obs, "click", "Save customer")
    route = env.route_action(action, obs)
    env.page.evaluate("document.body.insertAdjacentHTML('beforeend','<div id=overlay style=\"position:fixed;inset:0;z-index:999;background:white\"></div>')")
    out = env.session.execute(action, route)
    assert out["status"] == "rejected" and not out["dispatched"]
    env.page.locator("#overlay").evaluate("el=>el.remove()")
    env.page.locator("#save").evaluate("el=>el.textContent='Delete customer'")
    assert env.session.execute(action, route)["status"] == "rejected"
    env.page.locator("#save").evaluate("el=>el.outerHTML='<button id=save>Save customer</button>'")
    assert env.session.execute(action, route)["status"] == "rejected"


def test_duplicate_labels_shadow_dom_and_offscreen(env):
    env.reset("")
    env.page.set_content("<button onclick='window.hit=1'>Same</button><button onclick='window.hit=2'>Same</button><div id=host></div><div style='height:1800px'></div><button onclick='window.bottom=true'>Bottom</button>")
    env.page.evaluate("document.querySelector('#host').attachShadow({mode:'open'}).innerHTML='<button onclick=\"window.shadowHit=true\">Shadow</button>'")
    obs = env.observe()
    same = [n for n in obs.context[0]["nodes"] if n["role"] == "button" and n["name"] == "Same"]
    action = Action("click", {"target": "Second Same", "target_ref": same[1]["ref"]})
    assert env.session.execute(action, env.route_action(action, obs))["status"] == "executed"
    assert env.page.evaluate("window.hit") == 2
    for label, flag in [("Shadow", "shadowHit"), ("Bottom", "bottom")]:
        obs = env.observe()
        action = node_action(env, obs, "click", label)
        assert env.session.execute(action, env.route_action(action, obs))["status"] == "executed"
        assert env.page.evaluate("window." + flag) is True


def test_context_budget_preserves_task_and_selects_relevant_nodes():
    compiler = PromptCompiler(ContextConfig(max_input_tokens=6000, max_images=1, image_tokens=500))
    nodes = [{"ref": f"s:{i}", "name": "noise "*100, "role": "StaticText", "parent": None} for i in range(300)]
    nodes.append({"ref": "s:300", "name": "Enterprise Save customer", "role": "button", "parent": None, "direct": True})
    text, _, meta = compiler.compile(system="system", task="Enterprise Save customer", memory="memory"*1000,
        blocks=[{"kind": "browser_ax", "nodes": nodes}], auxiliary="x"*10000, images=[("current", None)])
    assert meta["input_estimate"] <= 6000 and "s:300" in meta["exposed_refs"]
    assert meta["selection"][0]["omitted"] > 0
    with pytest.raises(ContextBudgetError):
        compiler.compile(system="s", task="x"*10000, memory="", blocks=[], auxiliary="", images=[])


class DesktopFixture(Environment):
    os_name = "windows"
    def reset(self, task):
        return Observation(Image.new("RGB", (80, 80), "white"), text="Desktop button")
    def step(self, action):
        return StepResult(self.reset(""))
    def run_command(self, command):
        return ""
    def run_python(self, code):
        return ""
    def get_page_source(self):
        return ""
    def evaluate(self):
        raise NotImplementedError
    def close(self):
        pass


def test_desktop_same_schema_and_no_fake_reward():
    class FinishModel(ChatModel):
        def chat(self, messages, **kwargs):
            if len(messages) == 1:
                return "CONFIRM"
            return '{"next_action":{"type":"request_finish","answer":"done"},"state_update":{}}'
    result = Agent(FinishModel(ModelConfig()), CountingGrounding(), DesktopFixture()).run("Inspect desktop")
    traj = build_trajectory("desktop", "desktop", result.task, result)
    assert result.success and not result.evaluation_available and result.score == 0
    assert traj["steps"][0]["observation"]["context"] == []
    assert traj["steps"][0]["policy_input"]
    assert list(iter_sft_records(traj)) == []


def test_no_grounder_means_no_coordinates():
    assert resolve_coords(NullGrounding().locate(None, "target")) is None


def test_changed_checkbox_state_cannot_toggle_unexpectedly(env):
    obs = env.reset("")
    action = node_action(env, obs, "click", "Enable notifications")
    route = env.route_action(action, obs)
    env.page.locator("#notify").check()
    out = env.session.execute(action, route)
    assert out["status"] == "rejected" and not out["dispatched"]
    assert env.page.locator("#notify").is_checked()


def test_focused_tab_selection_and_persistent_cdp_session(env, monkeypatch):
    env.reset("")
    original = env.page
    other = original.context.new_page()
    other.goto(FIXTURE.resolve().as_uri())
    other.evaluate("document.title='Second tab'")
    # Headless Chrome exposes both pages as focused; refuse ambiguity, then inject
    # a deterministic focus oracle to test selection/session reuse (not OS focus).
    env.session._browser = env._browser
    ambiguous = env.observe()
    assert env.session.page is None and not ambiguous.context and ambiguous.screenshot is None
    active = [original]
    for page in (original, other):
        evaluate = page.evaluate
        def controlled(expression, *args, _page=page, _evaluate=evaluate, **kwargs):
            if expression == "document.hasFocus() && document.visibilityState === 'visible'":
                return active[0] is _page
            return _evaluate(expression, *args, **kwargs)
        monkeypatch.setattr(page, "evaluate", controlled)
    env.observe()
    assert env.session.page is original
    session = env.session._cdp
    env.observe()
    assert env.session._cdp is session
    active[0] = other
    observed = env.observe()
    assert env.session.page is other
    assert observed.context[0]["title"] == "Second tab"


def test_api_client_records_exact_messages_usage_and_reuses_connection():
    import httpx
    from openai import OpenAI
    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "test-response", "object": "chat.completion", "created": 1,
            "model": "qwen-api-test", "choices": [{"index": 0, "message": {"role": "assistant", "content": "  {\"next_action\":{\"type\":\"wait\",\"seconds\":1}}\n"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 42, "completion_tokens": 20, "total_tokens": 62}})
    model = ChatModel(ModelConfig(name="qwen-api-test", thinking_style="dashscope"))
    model._client = OpenAI(api_key="test-only", base_url="https://test.invalid/v1", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    messages = [{"role": "user", "content": "JSON please"}]
    try:
        raw = model.chat(messages, json_mode=True)
        assert raw.startswith("  ") and raw.endswith("\n")
        assert model.last_call["messages"] == messages
        assert model.last_call["usage"]["prompt_tokens"] == 42
        assert model.last_call["token_ids"] is None
        assert seen[0]["enable_thinking"] is False
        assert seen[0]["response_format"] == {"type": "json_object"}
        client = model._client
        model.chat(messages)
        assert model._client is client
    finally:
        model.close()


def test_format_retries_saved_and_bounded():
    class RetryModel(ChatModel):
        count = 0
        def chat(self, messages, **kwargs):
            self.count += 1
            if self.count == 1:
                return "invalid" * 10000
            return '{"next_action":{"type":"wait","seconds":0.01},"state_update":{}}'
    model = RetryModel(ModelConfig(vision=False))
    result = Agent(model, NullGrounding(), DesktopFixture(), max_steps=1).run("Observe the desktop")
    step = result.trajectory[0]
    assert len(step["decision_calls"]) == 2
    assert len(step["policy_input"]) == 4
    assert len(step["policy_input"][-2]["content"].encode()) <= 512
    assert not step["decision_calls"][0]["valid_action"]
    assert step["truncated"] and not step["terminated"]


def test_finish_verifier_failure_is_not_success():
    class OfflineVerifier(ChatModel):
        def chat(self, messages, **kwargs):
            if len(messages) == 1:
                raise RuntimeError("offline")
            return '{"next_action":{"type":"request_finish","answer":"done"}}'
    result = Agent(OfflineVerifier(ModelConfig()), NullGrounding(), DesktopFixture(), max_steps=1).run("Inspect desktop")
    assert not result.success and result.trajectory[0]["truncated"]
    assert result.trajectory[0]["verification"]["error_type"] == "RuntimeError"


def test_external_schema_and_strict_rl_gate():
    from osworld_agent.trajectory import import_external_episode, iter_on_policy_records
    response = '{"next_action":{"type":"wait","seconds":1}}'
    records = [{"messages": [{"role": "user", "content": "Inspect"}], "response": response, "terminated": True}]
    trajectory = import_external_episode("external", "desktop", "Inspect", records, source="test-dataset", score=1)
    assert len(list(iter_sft_records(trajectory))) == 1
    assert trajectory["steps"][0]["observation"]["context"] == []
    with pytest.raises(ValueError, match="Missing decision"):
        list(iter_on_policy_records(trajectory, "student-checkpoint"))
    missing = import_external_episode("missing", "desktop", "Inspect", [{"response": response}], source="legacy", score=1)
    assert not missing["steps"][0]["trainable"]


def test_policy_cannot_override_grounding_coordinates():
    from osworld_agent.actions import validate_action
    assert validate_action(Action("click", {"target": "Save", "x": 10, "y": 20})) is not None


def test_credit_requires_real_reward_and_honors_smoothing():
    from osworld_agent.rl.rollout import RolloutResult
    from osworld_agent.rl.credit import assign_step_level_credit
    with pytest.raises(ValueError):
        assign_step_level_credit([RolloutResult("task", ["click"], score=1)])
    rollouts = [RolloutResult("task", ["a"], score=1, evaluation_available=True),
                RolloutResult("task", ["b"], score=0, evaluation_available=True)]
    default = assign_step_level_credit(rollouts, normalize=False)
    smoother = assign_step_level_credit(rollouts, alpha=5, beta=5, normalize=False)
    assert default[0][0] > smoother[0][0] > 0
