import copy
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from openai import OpenAI
from PIL import Image

from osworld_agent.actions import Action
from osworld_agent.adapters.browser_env import BrowserEnvironment
from osworld_agent.adapters.grounding import GroundingClient
from osworld_agent.agent import Agent
from osworld_agent.browser_index import BrowserIndex
from osworld_agent.config import GroundingConfig, GroundingContextConfig, ModelConfig
from osworld_agent.context import PromptCompiler
from osworld_agent.metrics import summarize_trajectory
from osworld_agent.model.decision_model import ChatModel
from osworld_agent.model.grounding import GroundingRequest, GroundingResult, NullGrounding, resolve_coords
from osworld_agent.pipeline import Pipeline, StepState
from osworld_agent.trajectory import build_trajectory, dump_trajectory, restore_messages, iter_sft_records, iter_on_policy_records


class ProtocolClient:
    """A protocol double, not a model benchmark."""
    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.inputs = []
        self.last_call = {}

    def chat(self, messages, **kwargs):
        self.inputs.append(copy.deepcopy(messages))
        text = messages[-1]["content"][-1]["text"]
        if self.replies:
            response = self.replies.pop(0)
        else:
            nodes = [json.loads(line) for line in text.splitlines() if line.startswith('{"ref":')]
            node = next(n for n in nodes if "Bob" in n.get("scope", ""))
            response = json.dumps({"status": "ok", "target_ref": node["ref"], "point": None})
        self.last_call = {"model": "protocol-test-double", "response": response, "finish_reason": "stop",
                          "generation": {"temperature": .1, "max_tokens": 256}}
        return response

    def close(self):
        pass


@pytest.fixture
def browser():
    e = BrowserEnvironment(Path(__file__).parent.joinpath("fixtures/browser_task.html").resolve().as_uri())
    yield e
    e.close()


def prepare(browser, html):
    browser.page.set_content(html)
    return browser.observe()


def pipeline(browser, ground):
    return Pipeline(model=ChatModel(ModelConfig()), grounding=ground, env=browser, system_prompt="Return JSON")


ROWS = """<html><body><table>
<tr><td>Alice</td><td><button onclick="window.saved='Alice'">保存</button></td></tr>
<tr><td>Bob</td><td><button onclick="window.saved='Bob'">保存</button></td></tr>
</table><p role="status" id="status">Ready</p></body></html>"""


def test_late_target_after_4000_is_indexed_and_executed_without_model(browser):
    html = "<body>" + "".join(f"<button>Noise {i}</button>" for i in range(4500))
    obs = prepare(browser, html + '<button onclick="window.saved=true">保存客户</button></body>')
    assert len(obs.context[0]["nodes"]) > 4000
    target = next(n for n in obs.context[0]["nodes"] if n["role"] == "button" and n["name"] == "保存客户")
    assert target["direct"]
    assert browser.session.index.retrieve("请保存客户并返回成功提示")[0]["ref"] == target["ref"]
    ground = GroundingClient(client=ProtocolClient())
    p, state = pipeline(browser, ground), StepState(obs=obs)
    resolved = p.ground(Action("click", {"target": "保存客户"}), state)
    assert state.route["provenance"] == "deterministic" and not state.grounding_calls
    out = p.execute(resolved, state)
    assert out.info["status"] == "executed" and browser.page.evaluate("window.saved") is True
    assert ground.model.inputs == [] and state.offline_grounding_labels


def test_chinese_query_and_relevant_facts_are_not_crowded_out():
    nodes = [{"ref": f"s:{i}", "name": f"Noise {i}", "role": "button", "direct": True} for i in range(100)]
    nodes += [{"ref": "s:save", "name": "保存客户", "role": "button", "direct": True},
              {"ref": "s:total", "name": "Invoice total: 21,300 USD", "role": "StaticText"}]
    c = PromptCompiler()
    text, refs, _ = c.structure([{"kind": "browser_ax", "nodes": nodes}], "请保存客户并核对invoice total", 2000)
    assert "s:save" in refs and "s:total" in refs
    assert "21,300" in text and '"ref":"n' in text


def test_scoped_exact_target_skips_model_but_unscoped_is_ambiguous(browser):
    obs = prepare(browser, ROWS)
    index = browser.session.index
    assert index.exact("保存", hint={"name": "保存", "role": "button"}) is None
    exact = index.exact("保存", hint={"name": "保存", "role": "button", "scope": "Bob"})
    assert "Bob" in exact["scope"]
    ground = GroundingClient(client=ProtocolClient())
    p, state = pipeline(browser, ground), StepState(obs=obs)
    result = p.ground(Action("click", {"target": "Save Bob's customer", "target_hint": {
        "name": "保存", "role": "button", "scope": "Bob"}}), state)
    assert state.route["provenance"] == "deterministic"
    assert p.execute(result, state).info["status"] == "executed"
    assert browser.page.evaluate("window.saved") == "Bob"


def test_grounding_selects_unexposed_to_decider_ref_using_its_own_allowlist(browser):
    obs = prepare(browser, ROWS)
    obs.info["exposed_refs"] = []  # Local retrieval is a separate grounded invocation.
    ground = GroundingClient(client=ProtocolClient())
    p, state = pipeline(browser, ground), StepState(obs=obs)
    resolved = p.ground(Action("click", {"target": "Save Bob's customer", "target_hint": {
        "name": "保存", "role": "button"}}), state)
    assert state.route["provenance"] == "grounding" and len(state.grounding_calls) == 1
    assert state.route["target_ref"] in state.grounding_calls[0]["prompt_metadata"]["candidate_refs"]
    assert p.execute(resolved, state).info["status"] == "executed"
    assert browser.page.evaluate("window.saved") == "Bob"


def test_hallucinated_ref_never_clicks(browser):
    obs = prepare(browser, ROWS)
    client = ProtocolClient(['{"status":"ok","target_ref":"n999999","point":null}'])
    p, state = pipeline(browser, GroundingClient(client=client)), StepState(obs=obs)
    result = p.ground(Action("click", {"target": "Save Bob", "target_hint": {"name": "保存"}}), state)
    execution = p.execute(result, state)
    assert not execution.info["dispatched"] and execution.info["status"] == "rejected"
    assert browser.page.evaluate("window.saved || null") is None


def test_need_more_expands_once_and_stops_after_two_calls(browser):
    obs = prepare(browser, ROWS)
    client = ProtocolClient(['{"status":"need_more","target_ref":null,"point":null}'])
    ground = GroundingClient(client=client)
    p, state = pipeline(browser, ground), StepState(obs=obs)
    result = p.ground(Action("click", {"target": "Save Bob", "target_hint": {"name": "保存"}}), state)
    assert len(state.grounding_calls) == 2
    assert state.grounding_calls[-1]["prompt_metadata"]["expanded"]
    assert p.execute(result, state).info["status"] == "executed"
    obs = browser.observe()
    client = ProtocolClient(['{"status":"need_more"}'] * 3)
    p, state = pipeline(browser, GroundingClient(client=client)), StepState(obs=obs)
    result = p.ground(Action("click", {"target": "Save", "target_hint": {"name": "保存"}}), state)
    assert len(client.inputs) == 2 and result.args["grounding_failed"]


@pytest.mark.parametrize("point", [[-1, -1], [1000, 500], [True, 12], [float("inf"), 4], [10], "10,20"])
def test_bad_normalized_points_are_rejected(point):
    c = ProtocolClient([json.dumps({"status": "ok", "target_ref": None, "point": point})])
    r = GroundingClient(client=c).resolve(GroundingRequest(Image.new("RGB", (1280, 800)), "Button"))
    assert r.status != "ok" and resolve_coords(r) is None


def test_crop_and_resize_mapping_uses_actual_screenshot_size():
    c = ProtocolClient(['{"status":"ok","target_ref":null,"point":[500,500]}'])
    g = GroundingClient(GroundingConfig(width=1920, height=1080), client=c)
    r = g.resolve(GroundingRequest(Image.new("RGB", (800, 600)), "Button", roi=(100, 50, 300, 250)))
    assert resolve_coords(r) == (200, 150)
    assert r.metadata["image_transform"]["original_size"] == [800, 600]
    assert resolve_coords(GroundingResult(800, 20), Image.new("RGB", (800, 600))) is None
    c = ProtocolClient(['{"status":"ok","target_ref":null,"point":[500,500]}'])
    r = GroundingClient(client=c).resolve(GroundingRequest(Image.new("RGB", (1280, 800)), "Button"))
    assert resolve_coords(r) == (640, 400)


def test_view_budget_only_exposes_complete_candidates():
    candidates = [{"ref": f"s:{i}", "name": "Save", "role": "button", "scope": "Bob"} for i in range(200)]
    g = GroundingClient(client=ProtocolClient())
    messages, meta = g.compile_request(GroundingRequest(Image.new("RGB", (1280, 800)), "Save Bob", mode="node",
        candidates=candidates, snapshot_id="s", ref_aliases={f"n{i}": f"s:{i}" for i in range(200)}))
    assert 0 < meta["candidate_count"] <= 20 and meta["input_estimate"] <= 8192
    assert meta["text_tokens"] <= meta["text_budget"]
    actual = [json.loads(line)["ref"] for line in messages[-1]["content"][-1]["text"].splitlines()
              if line.startswith('{"ref":')]
    assert set(actual) == set(meta["ref_aliases"])


def test_cache_and_semantic_changes_invalidate_refs(browser):
    obs = prepare(browser, ROWS)
    again = browser.observe()
    assert again.context[0]["cache_hit"]
    assert again.context[0]["snapshot_id"] == obs.context[0]["snapshot_id"]
    node = next(n for n in browser.session.index.retrieve("保存") if "Bob" in n["scope"])
    action = Action("click", {"target": "保存", "target_ref": node["ref"]})
    route = browser.route_action(action, again)
    browser.page.locator("td", has_text="Bob").evaluate("(e)=>e.textContent='Mallory'")
    assert browser.session.execute(action, route)["status"] == "rejected"
    changed = browser.observe()
    assert not changed.context[0]["cache_hit"]
    assert changed.context[0]["snapshot_id"] != obs.context[0]["snapshot_id"]
    assert browser.route_action(action, changed)["channel"] == "reobserve"


def test_inspect_page_cursor_and_short_refs(browser):
    obs = prepare(browser, "<body>" + "".join(f"<p>Invoice {i} amount {100+i}</p>" for i in range(90)) + "</body>")
    p, state = pipeline(browser, NullGrounding()), StepState(obs=obs, task="Read invoice amount")
    first = p.execute(Action("inspect_page", {"query": "Invoice"}), state)
    assert first.info["status"] == "executed" and first.info["next_cursor"]
    messages = p.build_input(state)
    assert "browser_region" in str(messages) and "next_cursor" in str(messages)
    second = p.execute(Action("inspect_page", {"query": "Invoice", "cursor": first.info["next_cursor"]}), state)
    assert second.info["status"] == "executed"
    browser.page.locator("body").evaluate("(e)=>e.append(document.createElement('button'))")
    state.obs = browser.observe()
    stale = p.execute(Action("inspect_page", {"query": "Invoice", "cursor": first.info["next_cursor"]}), state)
    assert stale.info["status"] == "rejected"


def test_model_calls_replay_and_sft_roles_and_offline_rl_gate(browser, tmp_path):
    class DecisionPolicy(ChatModel):
        index = 0
        def chat(self, messages, **kwargs):
            if len(messages) == 1:
                return "CONFIRM"
            self.index += 1
            action = {"type": "click", "target": "Save Bob", "target_hint": {"name": "保存"}} if self.index == 1 else {
                "type": "request_finish", "answer": "Saved Bob"}
            return json.dumps({"next_action": action, "state_update": {}})
    browser.start_url = Path(__file__).parent.joinpath("fixtures/browser_task.html").resolve().as_uri()
    original_reset = browser.reset
    def reset(task):
        original_reset(task)
        return prepare(browser, ROWS)
    browser.reset = reset
    browser.evaluator = lambda page: page.evaluate("window.saved === 'Bob'")
    ground = GroundingClient(client=ProtocolClient())
    result = Agent(DecisionPolicy(ModelConfig()), ground, browser, max_steps=2).run("Save Bob")
    assert result.success and result.score == 1
    trajectory = build_trajectory("test", "browser", result.task, result)
    dump_trajectory(str(tmp_path / "trajectory.json"), trajectory)
    packed = json.loads((tmp_path / "trajectory.json").read_text(encoding="utf-8"))
    calls = packed["steps"][0]["model_calls"]
    assert [c["actor_role"] for c in calls] == ["decision", "grounding"]
    assert restore_messages(calls[-1]["messages"], tmp_path / "trajectory.json") == ground.model.inputs[0]
    assert len(list(iter_sft_records(packed))) == 2
    assert len(list(iter_sft_records(packed, actor_role="grounding"))) == 1
    with pytest.raises(ValueError, match="policy_revision"):
        list(iter_on_policy_records(packed, "student", actor_role="grounding"))
    calls[-1].update(policy_revision="student", token_ids=[10, 20], token_logprobs=[-.1, -.2])
    assert len(list(iter_on_policy_records(packed, "student", actor_role="grounding"))) == 1
    calls[-1]["provenance"] = "offline_deterministic"
    with pytest.raises(ValueError, match="Offline"):
        list(iter_on_policy_records(packed, "student", actor_role="grounding"))
    metrics = summarize_trajectory(packed)
    assert metrics["candidate_recall"] is None and metrics["node_selection_accuracy"] is None


def test_real_http_client_protocol_and_server_token_statistics():
    recorded = []
    def handler(request):
        recorded.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 0,
            "model": "grounding-test-model", "choices": [{"index": 0,
                "message": {"role": "assistant", "content": '{"status":"ok","target_ref":null,"point":[500,500]}'},
                "finish_reason": "stop", "token_ids": [11, 22], "logprobs": {"content": [
                    {"token": "token_id:11", "logprob": -.1, "bytes": None, "top_logprobs": []},
                    {"token": "token_id:22", "logprob": -.2, "bytes": None, "top_logprobs": []}]}}],
            "prompt_token_ids": [1, 2], "usage": {"prompt_tokens": 2048, "completion_tokens": 2, "total_tokens": 2050}})
    cfg = GroundingConfig(name="grounding-test-model", collect_logprobs=True, return_token_ids=True,
                          policy_revision="fixed-student")
    model = ChatModel(ModelConfig(name=cfg.name, collect_logprobs=True, return_token_ids=True,
                                  policy_revision=cfg.policy_revision))
    model._client = OpenAI(api_key="test", base_url="http://protocol.test/v1",
                          http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    ground = GroundingClient(cfg, client=model)
    try:
        result = ground.resolve(GroundingRequest(Image.new("RGB", (400, 200)), "Button"))
        assert resolve_coords(result) == (200, 100)
        assert result.calls[0]["token_ids"] == [11, 22] and result.calls[0]["token_logprobs"] == [-.1, -.2]
        assert recorded[0]["chat_template_kwargs"]["enable_thinking"] is False
        assert recorded[0]["return_token_ids"] is True and recorded[0]["max_tokens"] == 256
    finally:
        model.close()


def test_strict_scope_does_not_click_similar_customer_id(browser):
    prepare(browser, '<table><tr><td>客户99</td><td><button>保存</button></td></tr></table>')
    assert browser.session.index.exact("保存", hint={"name": "保存", "scope": "客户9"}) is None


def test_unrelated_live_text_does_not_block_valid_target(browser):
    obs = prepare(browser, ROWS + '<p id="clock">12:00</p>')
    target = browser.session.index.exact("保存", hint={"name": "保存", "scope": "Bob"})
    action = Action("click", {"target": "保存", "target_ref": target["ref"]})
    route = browser.route_action(action, obs)
    browser.page.locator("#clock").evaluate("(e)=>e.textContent='12:01'")
    assert browser.session.execute(action, route)["status"] == "executed"


@pytest.mark.parametrize("container", [
    '<form aria-label="客户表单"><div><label>姓名<input value="Bob"></label></div>{button}</form>',
    '<section aria-label="客户卡片"><div><p>Bob</p><div>{button}</div></div></section>',
    '<div role="dialog" aria-label="客户弹窗"><div><p>Bob</p>{button}</div></div>',
])
def test_named_region_with_nested_wrappers_executes(browser, container):
    obs = prepare(browser, container.format(button='<button type="button" onclick="window.saved=true">保存</button>'))
    target = browser.session.index.exact("保存")
    action = Action("click", {"target": "保存", "target_ref": target["ref"]})
    info = browser.session.execute(action, browser.route_action(action, obs))
    assert info["status"] == "executed", info
    assert browser.page.evaluate("window.saved") is True


def test_legacy_grounder_uses_unified_request_and_real_screenshot_size():
    from osworld_agent.adapters.normalized_grounding import NormalizedGroundingClient, _extract_coords
    from osworld_agent.adapters.pixel_grounding import PixelGroundingClient
    def response(text):
        return SimpleNamespace(id="legacy", model="legacy", usage=None,
            choices=[SimpleNamespace(message=SimpleNamespace(content=text), finish_reason="stop")])
    normalized = NormalizedGroundingClient("", "test", "legacy", width=1920, height=1080)
    normalized._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **k: response("[500,500]"))))
    result = normalized.resolve(GroundingRequest(Image.new("RGB", (800, 600)), "button"))
    assert resolve_coords(result) == (400, 300) and len(result.calls) == 1
    assert result.calls[0]["actor_role"] == "grounding" and result.calls[0]["messages"]
    assert _extract_coords("[-2,8]") is None and _extract_coords("[1000,20]") is None
    pixel = PixelGroundingClient("", "test", "legacy", grounding_width=1000, grounding_height=1000)
    pixel._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **k: response("[-1,-1]"))))
    result = pixel.resolve(GroundingRequest(Image.new("RGB", (800, 600)), "button"))
    assert resolve_coords(result) is None and len(result.calls) == 3
    assert all(not c["valid_action"] for c in result.calls)
    assert NullGrounding().resolve(GroundingRequest(Image.new("RGB", (800, 600)), "button")).status == "not_found"


def test_unlabeled_div_cards_keep_customer_context(browser):
    prepare(browser, '<body>' + ''.join(f'<div><div><p>Customer {i}</p><button>Save</button></div></div>'
                                      for i in range(60)) + '</body>')
    index = browser.session.index
    pool = index.retrieve("Save Customer 59", hint={"name": "Save", "role": "button"})
    assert "Customer 59" in pool[0]["scope"]
    assert index.exact("Save", hint={"name": "Save", "scope": "Customer 59"})["ref"] == pool[0]["ref"]
    obs = browser.observe()
    action = Action("click", {"target": "Save", "target_ref": pool[0]["ref"]})
    route = browser.route_action(action, obs)
    assert browser.session.execute(action, route)["status"] == "executed"
    browser.page.get_by_text("Customer 59", exact=True).evaluate("e=>e.textContent='Mallory'")
    assert browser.session.execute(action, route)["status"] == "rejected"


def test_scroll_reuses_ax_but_invalidates_geometry_and_refs(browser):
    prepare(browser, '<div style="height:1800px">Top</div><button>Save</button>')
    browser.session.options.cache_refresh_seconds = 30
    obs = browser.observe()
    browser.page.evaluate("scrollTo(0,500)")
    updated = browser.observe()
    assert updated.context[0]["cache_hit"] and updated.context[0]["geometry_cache_invalidated"]
    assert updated.context[0]["snapshot_id"] != obs.context[0]["snapshot_id"]
    assert all(n["bounds"] is None for n in updated.context[0]["nodes"])


def test_processor_budget_resizes_image_without_losing_candidates():
    class ImageAreaCounter:
        processor = True
        method = "test_image_area_counter"
        profile = {}
        @staticmethod
        def text(text):
            return len(text)
        @staticmethod
        def messages(messages, images):
            return 1000 + images[0].width * images[0].height // 30
    g = GroundingClient(client=ProtocolClient(['{"status":"ok","target_ref":null,"point":[500,500]}']))
    g.counter = ImageAreaCounter()
    request = GroundingRequest(Image.new("RGB", (1280, 800)), "Save", mode="node",
        candidates=[{"ref": "s:0", "role": "button", "name": "Save"}], ref_aliases={"n0": "s:0"})
    _, metadata = g.compile_request(request)
    assert metadata["candidate_refs"] == ["s:0"] and metadata["input_estimate"] <= 8192
    assert metadata["image_transform"]["input_size"] != [1280, 800]
    result = g.resolve(GroundingRequest(Image.new("RGB", (1280, 800)), "Save"))
    assert resolve_coords(result) == (640, 400)


def test_metrics_do_not_count_unexposed_target_as_model_mistake():
    trajectory = {"steps": [{"grounding_labels": {"target_ref": "s:correct"},
        "grounding_metadata": {"candidate_view": [{"ref": "s:correct"}]},
        "model_calls": [{"actor_role": "grounding", "prompt_metadata": {
            "mode": "node", "candidate_refs": ["s:other"]}, "resolved_target": {"target_ref": "s:other"}}]}]}
    metrics = summarize_trajectory(trajectory)
    assert metrics["candidate_recall"] == 1 and metrics["candidate_view_recall"] == 0
    assert metrics["node_selection_accuracy"] is None and metrics["node_label_count"] == 0
