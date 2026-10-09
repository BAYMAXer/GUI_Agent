"""Safety interruption tests use local fixtures and never send desktop input."""
import copy
import json

from PIL import Image

from osworld_agent.config import ModelConfig
from osworld_agent.env import Environment, Observation, StepResult
from osworld_agent.model.decision_model import ChatModel
from osworld_agent.model.grounding import GroundingResult, NullGrounding
from osworld_agent.pipeline import Pipeline
from osworld_agent.trajectory import build_trajectory, dump_trajectory


STOP = {"reason": "工作屏布局改变，请重新启动任务。", "code": "monitor_layout_changed"}


def observation(blocked=False):
    return Observation(Image.new("RGB", (80, 60), "white"), info={"safety_stop": copy.deepcopy(STOP)} if blocked else {})


class SafetyFixture(Environment):
    os_name = "windows"
    fresh_observations = True

    def __init__(self, *, initial_stop=False, observation_stop=False, preflight_stop=0, partial_stop=False):
        self.initial_stop = initial_stop
        self.observation_stop = observation_stop
        self.preflight_stop = preflight_stop
        self.partial_stop = partial_stop
        self.preflight_calls = 0
        self.executed = []
        self.evaluation_calls = 0

    def reset(self, task):
        return observation(self.initial_stop)

    def observe(self):
        return observation(self.observation_stop)

    def preflight(self, action, obs):
        self.preflight_calls += 1
        if self.preflight_calls == self.preflight_stop:
            return {"channel": "reobserve", "reason": STOP["reason"], "safety_stop": copy.deepcopy(STOP)}
        return None

    def step(self, action):
        self.executed.append(action)
        if self.partial_stop:
            return StepResult(observation(), done=True, info={"status": "uncertain", "dispatched": True,
                "input_batches": 2, "safety_stop": copy.deepcopy(STOP)})
        return StepResult(observation(), info={"status": "executed", "dispatched": True})

    def evaluate(self):
        self.evaluation_calls += 1
        return 1.0

    def run_command(self, command):
        raise AssertionError("No shell execution expected")

    def run_python(self, code):
        raise AssertionError("No Python execution expected")

    def get_page_source(self):
        raise AssertionError("No browser inspection expected")

    def close(self):
        pass


class Policy(ChatModel):
    def __init__(self, actions=None):
        super().__init__(ModelConfig())
        self.actions = actions or [{"type": "click", "target": "task button"}]
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append(copy.deepcopy(messages))
        if len(messages) == 1:
            return "CONFIRM"
        action = self.actions[min(len(self.calls) - 1, len(self.actions) - 1)]
        return json.dumps({"next_action": action, "state_update": {}, "expected_effect": "Task window changed"})


class Grounder(NullGrounding):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def locate(self, screenshot, target):
        self.calls += 1
        return GroundingResult(x=10, y=10, confidence=1)


def pipeline(env, policy=None, grounding=None):
    return Pipeline(model=policy or Policy(), grounding=grounding or Grounder(), env=env,
                    system_prompt="Operate the task window.", max_steps=3, max_retries=1)


def assert_stopped(result, env):
    assert result.termination_reason == "safety_stop"
    assert not result.success and result.score == 0
    assert not result.evaluation_available and env.evaluation_calls == 0
    assert result.final_answer == STOP["reason"] and result.safety_stop == STOP


def test_initial_observation_stops_before_any_model_or_context_inspection(monkeypatch):
    env, policy = SafetyFixture(initial_stop=True), Policy()
    run = pipeline(env, policy)
    monkeypatch.setattr(run, "build_input", lambda state: (_ for _ in ()).throw(AssertionError("Unsafe context compiled")))
    result = run.run("Update task document")
    assert_stopped(result, env)
    assert not policy.calls and not env.executed and result.steps == 0
    assert result.trajectory[0]["event"] == "safety_stop"
    assert result.trajectory[0]["observation"]["info"]["safety_stop"] == STOP


def test_observation_stop_keeps_prior_action_and_discards_next_decision():
    env, policy = SafetyFixture(observation_stop=True), Policy()
    result = pipeline(env, policy).run("Update task document")
    assert_stopped(result, env)
    assert len(policy.calls) == len(env.executed) == result.steps == 1
    previous, event = result.trajectory
    assert previous["execution_info"]["status"] == "executed"
    assert previous["truncated"] and not previous["terminated"]
    assert previous["next_observation"]["info"]["safety_stop"] == STOP
    assert previous["assessment"]["action_status"] == "uncertain"
    assert event["event"] == "safety_stop" and event["safety_stop"] == STOP


def test_decision_time_preflight_stop_skips_grounding_and_input():
    env = SafetyFixture(preflight_stop=1)
    policy = Policy([{"type": "click", "target": "task button"}])
    grounding = Grounder()
    result = pipeline(env, policy, grounding).run("Update task document")
    assert_stopped(result, env)
    assert len(policy.calls) == 1 and grounding.calls == 0 and not env.executed
    entry = result.trajectory[0]
    assert entry["execution_info"]["safety_stop"] == STOP
    assert entry["execution_info"]["dispatched"] is False
    assert entry["safety_stop"] == STOP and entry["truncated"] and not entry["terminated"]


def test_second_preflight_catches_interruption_during_grounding():
    env = SafetyFixture(preflight_stop=2)
    policy = Policy([{"type": "click", "target": "task button"}])
    grounding = Grounder()
    result = pipeline(env, policy, grounding).run("Update task document")
    assert_stopped(result, env)
    assert len(policy.calls) == grounding.calls == 1 and not env.executed
    assert result.trajectory[0]["execution_info"]["dispatched"] is False


def test_partial_input_stop_preserves_dispatch_evidence_without_success():
    env, policy = SafetyFixture(partial_stop=True), Policy()
    result = pipeline(env, policy).run("Update task document")
    assert_stopped(result, env)
    assert len(policy.calls) == len(env.executed) == 1
    entry = result.trajectory[0]
    assert entry["execution_info"]["status"] == "uncertain"
    assert entry["execution_info"]["dispatched"] and entry["execution_info"]["input_batches"] == 2
    assert entry["done"] and entry["truncated"] and not entry["terminated"]
    assert entry["assessment"]["source"] == "safety_stop"


def test_post_action_stop_prevents_finish_verification_model_call():
    env = SafetyFixture(observation_stop=True)
    policy = Policy([{"type": "request_finish", "answer": "Done"}])
    result = pipeline(env, policy).run("Update task document")
    assert_stopped(result, env)
    assert len(policy.calls) == 1 and not env.executed
    assert result.trajectory[0]["verification"] is None


def test_export_preserves_zero_action_stop_reason_and_observation(tmp_path):
    env = SafetyFixture(initial_stop=True)
    result = pipeline(env).run("Update task document")
    exported = build_trajectory("safety-fixture", "windows", result.task, result)
    destination = tmp_path / "trajectory.json"
    dump_trajectory(str(destination), exported)
    saved = json.loads(destination.read_text(encoding="utf-8"))
    assert saved["outcome"]["safety_stop"] == STOP and saved["outcome"]["termination_reason"] == "safety_stop"
    assert saved["steps"] == [] and saved["outcome"]["num_steps"] == 0
    event = saved["events"][0]
    assert event["observation"]["info"]["safety_stop"] == STOP
    assert (tmp_path / event["observation"]["screenshot"]).is_file()


def test_partial_input_stop_exports_execution_and_stop_evidence():
    env = SafetyFixture(partial_stop=True)
    result = pipeline(env).run("Update task document")
    exported = build_trajectory("safety-fixture", "windows", result.task, result)
    assert exported["outcome"]["score"] is None
    step = exported["steps"][0]
    assert step["safety_stop"] == STOP
    assert step["execution_info"]["dispatched"] and step["execution_info"]["input_batches"] == 2
    assert step["truncated"] and not step["terminated"]


def test_recoverable_rejection_reobserves_and_can_complete():
    class RecoveringFixture(SafetyFixture):
        def preflight(self, action, obs):
            self.preflight_calls += 1
            if self.preflight_calls == 1:
                return {"channel": "reobserve", "reason": "Task window restored; stale action discarded"}
            return None
    env = RecoveringFixture()
    policy = Policy([{"type": "click", "target": "task button"}, {"type": "request_finish", "answer": "Done"}])
    grounding = Grounder()
    result = pipeline(env, policy, grounding).run("Update task document")
    assert result.success and result.termination_reason == "success" and not result.safety_stop
    assert result.score == 1 and result.evaluation_available and env.evaluation_calls == 1
    assert len(policy.calls) == 3 and grounding.calls == 0 and not env.executed
    assert result.trajectory[0]["execution_info"]["dispatched"] is False
