"""Durable task memory survives bounded prompts and cross-application steps."""
import copy
import json
from types import SimpleNamespace

from PIL import Image

from osworld_agent.config import ContextConfig, ModelConfig
from osworld_agent.context import PromptCompiler
from osworld_agent.env import Observation
from osworld_agent.memory import StructuredMemory
from osworld_agent.model.decision_model import ChatModel
from osworld_agent.model.grounding import NullGrounding
from osworld_agent.pipeline import Pipeline


def prompt_text(message):
    content = message["content"]
    return content if isinstance(content, str) else "\n".join(p.get("text", "") for p in content)


def memory_from_prompt(text):
    return json.loads(text.split("## 结构化记忆\n", 1)[1].split("\n\n## 外部观察", 1)[0])


def test_cross_application_prompt_keeps_subgoal_and_facts_after_large_events():
    config = ContextConfig(max_input_tokens=6000, memory_tokens=700, max_images=0)
    env = SimpleNamespace(os_name="windows", reset=lambda task: Observation(Image.new("RGB", (80, 80))))
    model = ChatModel(ModelConfig(vision=False))
    pipe = Pipeline(model=model, grounding=NullGrounding(), env=env, system_prompt="Output JSON", context_config=config)
    state = pipe._init_state("Read notes, download the paper, then return to notes")
    state.memory.apply_state_update({"current_subgoal": "Return to notes and record the result",
                                    "known_fact_add": {"fixture_task": "retained across apps", "paper": "GUI Agent Browser Study"}})
    state.memory.control_state = "ADVANCE"
    original_facts = copy.deepcopy(state.memory.known_facts)
    for step in range(1, 9):
        state.step_id = step
        state.obs = Observation(Image.new("RGB", (80, 80)), info={"scene": {
            "browser_use": 0, "structure_available": False,
            "mode": "desktop" if step in {1, 6, 7, 8} else "browser_native",
            "reason": "mock application switch"}})
        state.memory.add_event(json.dumps({"step": step, "result": "execution detail " * 2000}))
        state.memory.add_action(step, "very long action " * 1000, "full history is stored")
        state.memory.expected_effect = "expected " * 1000
        state.memory.observed_effect = "observed " * 1000
        pipe.preprocess(state)
        messages = pipe.build_input(state)
        memory = memory_from_prompt(prompt_text(messages[-1]))
        assert memory["current_subgoal"] == "Return to notes and record the result"
        assert memory["known_facts"] == original_facts
        assert memory["control"]["control_state"] == "ADVANCE"
        assert pipe.compiler.count(json.dumps(memory, ensure_ascii=False, separators=(",", ":"))) <= config.memory_tokens
        assert state.prompt_metadata["input_estimate"] <= config.max_input_tokens
        assert state.prompt_metadata["memory_format"] == "compact_json"
        assert len(memory.get("recent_events", [])) <= 3
    assert len(state.memory.action_history) == 8
    assert state.memory.known_facts == original_facts
    model.close()


def test_recent_memory_is_bounded_without_changing_full_stored_history():
    memory = StructuredMemory()
    for step in range(10):
        memory.add_event(f"event-{step}")
        memory.add_failure(f"failure-{step}")
        memory.add_action(step, f"action-{step}", "executed")
    data = memory.to_prompt_data()
    assert data["recent_events"] == ["event-7", "event-8", "event-9"]
    assert data["failures"] == ["failure-7", "failure-8", "failure-9"]
    assert len(data["action_history"]) == 3 and "action-9" in data["action_history"][-1]
    assert len(memory.action_history) == 10
    compiled = json.loads(PromptCompiler().memory(data, 2200))
    assert compiled["recent_events"] == data["recent_events"]


def test_memory_retains_complete_fact_values_when_events_exceed_budget():
    facts = {"title": "中文论文", "download": {"complete": True, "bytes": 128}, "pages": [1, 2, 3]}
    data = {"current_subgoal": "保存结果", "known_facts": facts,
            "recent_events": ["irrelevant diagnostic " * 10000] * 3,
            "action_history": ["long history " * 10000] * 3}
    compiler = PromptCompiler()
    rendered = compiler.memory(data, 500)
    result = json.loads(rendered)
    assert result["known_facts"] == facts and result["current_subgoal"] == "保存结果"
    assert compiler.count(rendered) <= 500
    assert data["known_facts"] == facts and len(data["recent_events"][0]) > 500


def test_oversized_core_is_valid_bounded_summary_without_destroying_facts():
    memory = StructuredMemory()
    memory.task_state["current_subgoal"] = "很长的子目标" * 500
    memory.known_facts = {"paper": "详细论文内容" * 1000, "result": "success"}
    before = copy.deepcopy(memory)
    compiler = PromptCompiler()
    rendered = compiler.memory(memory.to_prompt_data(), 240)
    assert isinstance(json.loads(rendered), dict) and compiler.count(rendered) <= 240
    assert memory == before
    assert "[truncated]" in rendered or "omitted_known_facts" in rendered


def test_finish_verifier_receives_durable_facts_after_long_events():
    class Verifier(ChatModel):
        def chat(self, messages, **kwargs):
            self.received = prompt_text(messages[-1])
            return "CONFIRM"
    model = Verifier(ModelConfig(vision=False))
    env = SimpleNamespace(os_name="windows")
    pipe = Pipeline(model=model, grounding=NullGrounding(), env=env, system_prompt="JSON")
    state = pipe._init_state("Download and record results")
    state.memory.apply_state_update({"current_subgoal": "Verify notes", "known_fact_add": {"result": "retained across apps"}})
    for _ in range(3):
        state.memory.add_event("large action response " * 10000)
    state.obs = Observation(Image.new("RGB", (80, 80)))
    assert pipe._verify_finish(state, "done")[0]
    memory_text = model.received.split("## 已完成的工作（结构化记忆）\n", 1)[1].split("\n\n## 模型声称", 1)[0]
    memory = json.loads(memory_text)
    assert memory["known_facts"]["result"] == "retained across apps"
    assert memory["current_subgoal"] == "Verify notes"
    model.close()
