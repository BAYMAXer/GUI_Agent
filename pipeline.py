"""主循环流水线：观察 → 决策（证据先行）→ 定位 → 执行 → 控制状态更新。

闭环设计（核心）：
- 执行前，模型在 next_action 的同时说明 expected_effect（预期观察到什么）；
- 执行后，模型先客观描述 observed_effect，再给 action_status（成功/失败）；
- 程序对比 expected vs observed + 确定性卡死检测，得出 control_state；
- Planner 只能 request_finish，程序验证后才真正结束。

阶段（run() 里按序调用）：
  ① preprocess   预处理：确保观察 + 记录截图 + 构建记忆文本
  ② build_input  构造输入：结构化记忆（含控制状态）+ 3 张截图
  ③ decide       决策：observed_effect + action_status + state_update + next_action + expected_effect
  ④ ground       grounding：描述→坐标，记录坐标供下一步标记
  ⑤ execute      执行：skill / shell / subagent / terminal(request_finish|fail) / env
  ⑥ postprocess  后处理：记录 expected/observed + 控制状态 + 卡死检测 + Finish 验证
"""
from __future__ import annotations

import json
import copy
import time
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Tuple

from .actions import (
    Action, validate_action, action_signature, get_action_spec, ACTION_REGISTRY,
    parse_decision, action_from_decision,
)
from .env import Environment, Observation
from .model import ChatModel, Grounding, resolve_coords, mark_coordinate
from .memory import StructuredMemory
from .skills import is_skill, validate_skill, run_skill, enabled_skills, SkillContext
from .prompts import build_user_prompt, build_format_feedback
from .context import PromptCompiler
from .config import ContextConfig
from .model.decision_model import encode_image
from .model.grounding import GroundingRequest, screenshot_image
from .scene import observation_scene, browser_context

# 确定性卡死检测参数
_NO_CHANGE_DIFF_THRESHOLD = 2.0       # 64x64 灰度 mean-abs-diff 阈值（< 此值视为无变化）
_NO_CHANGE_CONSECUTIVE_THRESHOLD = 2  # 连续 N 步无变化判定卡死
_REPEAT_ACTION_THRESHOLD = 3          # 连续 N 次相同 action+target 判定卡死
_LOOP_HISTORY_LEN = 6                 # 周期循环检测的历史长度
_LOOP_PERIODS = (2,)                  # 周期循环（A,B,A,B），周期1由 _REPEAT_ACTION_THRESHOLD 覆盖


# --------------------------------------------------------------------------- #
# 数据容器
# --------------------------------------------------------------------------- #
@dataclass
class AgentResult:
    """单任务运行结果。"""
    task: str
    success: bool = False
    score: float = 0.0
    steps: int = 0
    final_answer: str = ""
    trajectory: List[dict] = field(default_factory=list)
    screenshots: Dict[int, Any] = field(default_factory=dict)   # step -> 决策前截图（PIL 或 bytes）
    evaluation_available: bool = False
    termination_reason: str = "max_steps"


@dataclass
class StepState:
    """跨步骤流动的循环状态。"""
    task: str = ""
    obs: Optional[Observation] = None
    memory: Optional[StructuredMemory] = None
    skill_ctx: Optional[SkillContext] = None
    skill_results: Dict[str, str] = field(default_factory=dict)
    terminal_result: str = ""
    code_agent_result: str = ""
    page_source: str = ""
    page_source_scope: tuple = ("", "", "")
    memory_text: str = ""
    # 视觉记忆
    screenshot_history: List[Any] = field(default_factory=list)
    last_grounding_coord: Optional[Tuple[int, int]] = None
    last_grounding_target: str = ""
    # 程序维护的信息
    step_id: int = 0
    last_action: str = ""
    last_target: str = ""
    execution_status: str = ""
    failure_count: int = 0
    # 卡死检测
    no_change_count: int = 0
    action_keys: List[str] = field(default_factory=list)
    shell_outputs: List[str] = field(default_factory=list)   # 最近 shell 命令输出（用于 shell 卡死检测）
    prompt_metadata: Dict[str, Any] = field(default_factory=dict)
    route: Optional[dict] = None
    grounding_calls: List[dict] = field(default_factory=list)
    grounding_metadata: dict = field(default_factory=dict)
    inspected_page: Optional[dict] = None
    offline_grounding_labels: list = field(default_factory=list)


@dataclass
class Decision:
    """③ 决策阶段的产出（先分析当前屏幕 → 证据先行 → 预期效果）。"""
    raw: str = ""
    action: Optional[Action] = None
    screen_analysis: str = ""            # 当前屏幕状态描述（含打开的应用）
    observed_effect: str = ""            # 上一步动作的实际观察（先客观描述）
    action_status: str = ""              # success / failure / uncertain
    state_update: Dict[str, Any] = field(default_factory=dict)
    expected_effect: str = ""            # 下一步动作的预期效果
    calls: List[dict] = field(default_factory=list)


@dataclass
class Execution:
    """⑤ 执行阶段的产出。"""
    kind: str = ""                        # skill / shell / subagent / terminal / finish_request / env
    outcome: str = ""
    executed: str = ""
    obs: Optional[Observation] = None
    done: bool = False
    success: bool = False
    answer: str = ""
    info: Dict[str, Any] = field(default_factory=dict)
    reward: Optional[float] = None


# --------------------------------------------------------------------------- #
# 主循环流水线
# --------------------------------------------------------------------------- #
class Pipeline:
    """主循环流水线。run() 只列 6 个阶段。"""

    def __init__(self, *, model: ChatModel, grounding: Grounding, env: Environment,
                 system_prompt: str, code_agent=None, max_steps: int = 20,
                 max_retries: int = 3, enable_reflection: bool = True,
                 early_stop_repeat: int = 3, context_config=None):
        self.model = model
        self.grounding = grounding
        self.env = env
        self.code_agent = code_agent
        self.system_prompt = system_prompt
        self.max_steps = max_steps
        self.max_retries = max_retries
        self.enable_reflection = enable_reflection
        self.early_stop_repeat = early_stop_repeat
        config = context_config or ContextConfig()
        output_tokens = getattr(getattr(model, "cfg", None), "max_tokens", config.output_reserve)
        self.compiler = PromptCompiler(replace(config, output_reserve=max(config.output_reserve, output_tokens)))
        self._verification = None

    # ================= 主循环 ================= #
    def run(self, task: str) -> AgentResult:
        result = AgentResult(task=task)
        state = self._init_state(task)

        for step_no in range(1, self.max_steps + 1):
            started = time.perf_counter()
            state.step_id = step_no
            state = self.preprocess(state)
            if state.obs is not None:
                result.screenshots[step_no] = state.obs.screenshot
            messages = self.build_input(state)
            before = self._observation_record(state.obs, f"o_{step_no}")
            if getattr(self.env, "fresh_observations", False):
                previous = next((t for t in reversed(result.trajectory) if t.get("next_observation")), None)
                if previous:
                    previous["post_action_observation"] = previous["next_observation"]
                    previous["next_observation"] = copy.deepcopy(before)
            decision = self.decide(messages)
            for call in decision.calls:
                call["actor_role"] = "decision"
                call.setdefault("provenance", "sampled")
                call["prompt_metadata"] = copy.deepcopy(state.prompt_metadata)
            result.steps = step_no
            if decision.action is None:
                # 动作解析/校验失败：空转一步继续（对齐 Agent S3 的 wait 语义），
                # 不提前终止任务——9B 模型偶尔漏 next_action，下一轮往往能恢复。
                result.trajectory.append({
                    "step": step_no, "error": "动作解析失败",
                    "raw": (decision.raw or "")[-500:],
                    "observation": before, "policy_input": copy.deepcopy(decision.calls[-1]["messages"] if decision.calls else messages),
                    "policy_output": decision.raw, "decision_calls": decision.calls, "model_calls": decision.calls,
                    "prompt_metadata": state.prompt_metadata, "valid_action": False,
                    "terminated": False, "truncated": False,
                })
                state.memory.add_event("⚠️ 上一步动作解析失败，请重新输出合法的 next_action JSON")
                fresh = self._fresh_observation()
                if fresh is not None:
                    state.obs = fresh
                next_id = f"after_a_{step_no}" if getattr(self.env, "fresh_observations", False) else f"o_{step_no+1}"
                result.trajectory[-1]["next_observation"] = self._observation_record(state.obs, next_id)
                continue
            ground_started = time.perf_counter()
            action = self.ground(decision.action, state)
            ground_ms = (time.perf_counter() - ground_started) * 1000
            execute_started = time.perf_counter()
            execution = self.execute(action, state)
            if execution.obs is None:
                execution.obs = self._fresh_observation()
            execute_ms = (time.perf_counter() - execute_started) * 1000
            state, should_break = self.postprocess(step_no, decision, execution, state, result)
            entry = next(t for t in reversed(result.trajectory) if t.get("action_id") == f"a_{step_no}")
            next_id = f"after_a_{step_no}" if getattr(self.env, "fresh_observations", False) else f"o_{step_no+1}"
            entry.update({"observation": before,
                "next_observation": self._observation_record(state.obs, next_id),
                "policy_input": copy.deepcopy(decision.calls[-1]["messages"] if decision.calls else messages),
                "policy_output": decision.raw, "decision_calls": decision.calls,
                "model_calls": decision.calls + copy.deepcopy(state.grounding_calls),
                "grounding_metadata": copy.deepcopy(state.grounding_metadata),
                "offline_grounding_labels": copy.deepcopy(state.offline_grounding_labels),
                "prompt_metadata": state.prompt_metadata, "valid_action": True,
                "resolved_action": {"action": action.action, "args": action.args},
                "routing": copy.deepcopy(state.route) or {"channel": "visual" if self._coordinate_of(action) else "environment"},
                "execution_info": execution.info, "reward": execution.reward,
                "verification": self._verification if execution.kind == "finish_request" else None,
                "terminated": bool(should_break and (result.success or execution.done)),
                "truncated": bool(should_break and not (result.success or execution.done)),
                "timing_ms": {"ground": round(ground_ms, 2), "execute_observe": round(execute_ms, 2),
                              "total": round((time.perf_counter()-started)*1000, 2)}})
            if should_break:
                result.termination_reason = "success" if result.success else ("failure" if execution.done else "stall")
                break

        result.score = self._evaluate(result)
        # 补齐最后一步的 assessment（任务结束时没有下一步决策来评估 a_N）
        self._fill_final_assessment(result)
        transitions = [t for t in result.trajectory if "policy_input" in t]
        if transitions:
            transitions[-1]["truncated"] = not transitions[-1].get("terminated", False)
            transitions[-1]["done"] = True
        return result

    @staticmethod
    def _observation_record(obs, observation_id):
        if obs is None:
            return {"id": observation_id, "context": [], "screenshot": None}
        info = copy.deepcopy(obs.info)
        info["scene"] = copy.deepcopy(observation_scene(obs))
        return {"id": observation_id, "context": copy.deepcopy(browser_context(obs)),
                "text": "", "info": info, "screenshot": encode_image(obs.screenshot)}

    def _fresh_observation(self):
        try:
            return self.env.observe()
        except Exception as exc:
            return Observation(info={"observation_error": type(exc).__name__})

    # ================= 六个阶段 ================= #
    def preprocess(self, state: StepState) -> StepState:
        if state.obs is None:
            state.obs = self.env.reset(state.task)
        elif getattr(self.env, "fresh_observations", False):
            state.obs = self._fresh_observation() or state.obs
        state.screenshot_history.append(state.obs.screenshot)
        if len(state.screenshot_history) > 3:
            state.screenshot_history = state.screenshot_history[-3:]
        state.memory_text = self._build_memory_text(state)
        return state

    def build_input(self, state: StepState) -> List[dict]:
        scene = observation_scene(state.obs)
        scope = (scene.get("page_id", ""), scene.get("url", ""), scene.get("snapshot_id", ""))
        page_source = state.page_source if (scene.get("browser_use") == 1 and scene.get("structure_available") and scope[0]
                                           and scope == state.page_source_scope) else ""
        state.page_source = ""  # 一次性返回后清空，避免每步重复塞大段源码
        state.page_source_scope = ("", "", "")
        blocks = list(browser_context(state.obs))
        subgoal = state.memory.task_state.get("current_subgoal", "") if state.memory else ""
        session = getattr(self.env, "browser_session", None)
        if blocks and session and getattr(session, "index", None):
            try:
                selected = session.index.overview(state.task + " " + subgoal, limit=20)
                session.enrich(n["ref"] for n in selected)
            except Exception as exc:
                state.obs.info["candidate_inspection_error"] = type(exc).__name__
        if state.inspected_page is not None:
            if state.inspected_page.get("snapshot_id") == scene.get("snapshot_id") and blocks:
                # A requested local view is a first-class observation, not a truncated HTML string.
                blocks = [state.inspected_page]
            state.inspected_page = None
        scene_hint = {k: scene.get(k) for k in ("browser_use", "structure_available", "mode", "reason")}
        blocks.insert(0, {"kind": "scene", "text": "当前场景: " + json.dumps(scene_hint, ensure_ascii=False)})
        if state.obs:
            errors = {k: v for k, v in state.obs.info.items() if "error" in k}
            if errors:
                blocks.insert(0, {"kind": "observation_status", "text": json.dumps(errors, ensure_ascii=False)})
        auxiliary = json.dumps({"skills": state.skill_results, "terminal": state.terminal_result,
                               "code_agent": state.code_agent_result, "page_source": page_source}, ensure_ascii=False)
        user_text, images, metadata = self.compiler.compile(system=self.system_prompt, task=state.task,
            memory=self._memory_prompt_data(state), blocks=blocks, auxiliary=auxiliary,
            images=self._build_images(state) if getattr(self.model, "vision", True) else [], subgoal=subgoal)
        state.prompt_metadata = metadata
        state.prompt_metadata["scene"] = copy.deepcopy(scene)
        if state.obs:
            state.obs.info["exposed_refs"] = metadata["exposed_refs"]
            state.obs.info["ref_aliases"] = metadata["ref_aliases"]
        return [
            {"role": "system", "content": self.system_prompt},
            self.model.build_vision_message(user_text, images),
        ]

    def decide(self, messages: List[dict]) -> Decision:
        raw, action = "", None
        screen_analysis = observed_effect = action_status = expected_effect = ""
        state_update: Dict[str, Any] = {}
        calls = []
        original = copy.deepcopy(messages)
        for _ in range(max(1, self.max_retries)):
            if len(messages) > 2:
                messages = original + [
                    {"role": "assistant", "content": self.compiler.clip(raw, 512)},
                    {"role": "user", "content": self.compiler.clip(messages[-1]["content"], 512)}]
            try:
                raw = self.model.chat(messages, json_mode=True)
            except Exception as exc:
                call = copy.deepcopy(getattr(self.model, "last_call", {}))
                call.update({"messages": copy.deepcopy(messages), "error_type": type(exc).__name__, "valid_action": False})
                calls.append(call)
                return Decision(raw="", action=None, calls=calls)
            call = copy.deepcopy(getattr(self.model, "last_call", {}))
            call.update({"messages": copy.deepcopy(messages), "response": raw, "valid_action": False})
            calls.append(call)
            obj = parse_decision(raw)
            action = action_from_decision(obj or {})
            if action is not None:
                err = self._validate_action(action)
                if err is None and not isinstance((obj or {}).get("state_update", {}), dict):
                    err = "state_update 必须是对象"
                patch = (obj or {}).get("state_update") or {}
                if err is None and any(k in patch and not isinstance(patch[k], list) for k in ("completed_add", "pending_add")):
                    err = "completed_add 和 pending_add 必须是列表"
                if err is None and "known_fact_add" in patch and not isinstance(patch["known_fact_add"], dict):
                    err = "known_fact_add 必须是对象"
                if err is None:
                    call["valid_action"] = True
                    screen_analysis = str((obj or {}).get("screen_analysis") or "")
                    observed_effect = str((obj or {}).get("observed_effect") or "")
                    action_status = str((obj or {}).get("action_status") or "")
                    state_update = (obj or {}).get("state_update") or {}
                    expected_effect = str((obj or {}).get("expected_effect") or "")
                    return Decision(raw=raw, action=action, screen_analysis=screen_analysis,
                                    observed_effect=observed_effect, action_status=action_status,
                                    state_update=state_update, expected_effect=expected_effect, calls=calls)
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content": build_format_feedback(err)})
            else:
                messages.append({"role": "assistant", "content": raw})
                if obj and "next_action" not in obj:
                    feedback = "你的 JSON 里漏了 next_action 字段（screen_analysis/observed_effect/state_update 都写了，就缺 next_action）。请补上 next_action，里面有 type 和对应参数。"
                elif obj and "next_action" in obj and not isinstance(obj["next_action"], dict):
                    feedback = "next_action 必须是一个对象（含 type 字段），现在不是合法对象。请改成 {\"type\": \"动作名\", ...}。"
                else:
                    feedback = "没有找到合法的 JSON 动作（需含 next_action）。"
                messages.append({"role": "user", "content": build_format_feedback(feedback)})
        return Decision(raw=raw, action=None, calls=calls)

    def ground(self, action: Action, state: StepState) -> Action:
        state.last_grounding_target = str(action.args.get("target", "")
                                          or action.args.get("from_target", ""))
        state.route = None
        state.grounding_calls = []
        state.grounding_metadata = {}
        state.offline_grounding_labels = []
        if browser_context(state.obs):
            try:
                state.route = self.env.route_action(action, state.obs)
            except Exception as exc:
                state.route = {"channel": "reobserve", "reason": f"结构通道不可用: {type(exc).__name__}"}
        if state.route is not None:
            state.last_grounding_coord = None
            return action
        if action.args.get("target_ref") and not browser_context(state.obs):
            state.route = {"channel": "reobserve", "reason": "No structured observation for this reference"}
            state.last_grounding_coord = None
            return action
        if browser_context(state.obs) and action.action in {"click", "double_click", "right_click", "type", "select"}:
            resolved = self._ground_browser(action, state)
            if resolved is not None:
                state.last_grounding_coord = self._coordinate_of(resolved)
                return resolved
        try:
            resolved = self._resolve(action, state.obs, state)
        except Exception as exc:
            state.route = {"channel": "grounding_failed", "error_type": type(exc).__name__}
            resolved = Action(action.action, {**action.args, "grounding_failed": True})
        state.last_grounding_coord = self._coordinate_of(resolved)
        return resolved

    def _ground_browser(self, action, state):
        session = getattr(self.env, "browser_session", None)
        if not session or not getattr(session, "index", None):
            return None
        scene = observation_scene(state.obs)
        if not session.snapshot or session.snapshot["snapshot_id"] != scene.get("snapshot_id"):
            state.route = {"channel": "reobserve", "reason": "Browser snapshot changed before retrieval"}
            return action
        target = action.args.get("target", "")
        hint = dict(action.args.get("target_hint", {}))
        if hint.get("scope_ref"):
            ref = state.obs.info.get("ref_aliases", {}).get(hint["scope_ref"], hint["scope_ref"])
            if ref not in state.obs.info.get("exposed_refs", []):
                state.route = {"channel": "reobserve", "reason": "Scope reference was not in the decision input"}
                return action
            hint["scope_ref"] = ref
        subgoal = state.memory.task_state.get("current_subgoal", "") if state.memory else ""
        try:
            exact = session.index.exact(target, action=action.action, hint=hint)
            if exact:
                resolved = Action(action.action, {**action.args, "target_ref": exact["ref"]})
                state.route = session.route(resolved, state.obs, allowed_refs=[exact["ref"]], provenance="deterministic")
                state.grounding_metadata = {"kind": "deterministic", "snapshot_id": session.snapshot["snapshot_id"],
                    "target_ref": exact["ref"], "label_provenance": "offline_deterministic",
                    "candidate_view": [exact], "ref_aliases": {session.index.short[exact["ref"]]: exact["ref"]}}
                compiler = getattr(self.grounding, "compile_request", None)
                if compiler:
                    try:
                        pool = session.index.retrieve(target, action=action.action, hint=hint, subgoal=subgoal, limit=20)
                        request = GroundingRequest(state.obs.screenshot, target, action=action.action, mode="node",
                            target_hint=hint, candidates=pool, snapshot_id=session.snapshot["snapshot_id"],
                            ref_aliases={session.index.short[n["ref"]]: n["ref"] for n in pool}, subgoal=subgoal)
                        messages, metadata = compiler(request)
                        alias = next(a for a, r in metadata["ref_aliases"].items() if r == exact["ref"])
                        state.offline_grounding_labels.append({"actor_role": "grounding", "messages": messages,
                            "response": json.dumps({"status": "ok", "target_ref": alias, "point": None}),
                            "prompt_metadata": metadata, "valid_action": True, "provenance": "offline_deterministic",
                            "policy_revision": None})
                    except (ValueError, StopIteration):
                        pass  # Direct execution does not depend on constructing an offline training example.
                return resolved
            if not getattr(self.grounding, "supports_nodes", False):
                return None
            cfg = getattr(self.grounding, "context_config", None)
            limit = getattr(cfg, "candidate_limit", 20)
            max_calls = min(2, max(1, getattr(cfg, "max_calls", 2)))
            for attempt in range(max_calls):
                pool = session.index.retrieve(target, action=action.action, hint=hint, subgoal=subgoal, limit=limit)
                if not pool:
                    return None if attempt == 0 else self._reject_grounding(action, state, "No additional candidates")
                session.enrich(n["ref"] for n in pool)
                pool = [session.index.record(session.nodes[n["ref"]], n.get("retrieval_score")) for n in pool]
                request = GroundingRequest(state.obs.screenshot, target, action=action.action, mode="node",
                    target_hint=hint, candidates=pool, snapshot_id=session.snapshot["snapshot_id"],
                    ref_aliases={session.index.short[n["ref"]]: n["ref"] for n in pool},
                    subgoal=subgoal, expanded=attempt > 0,
                    roi=self._browser_roi(state.obs, [n.get("scope_ref") for n in pool]))
                result = self.grounding.resolve(request)
                state.grounding_calls.extend(copy.deepcopy(result.calls))
                state.grounding_metadata = {"kind": "sampled", "retrieved_count": len(pool),
                    "candidate_view": pool, "snapshot_id": request.snapshot_id, "result_status": result.status,
                    **result.metadata}
                if result.status == "ok" and result.target_ref:
                    # A model may only select refs actually serialized into THIS invocation.
                    allowed = result.metadata.get("candidate_refs", [])
                    if result.target_ref not in allowed or result.target_ref not in {n["ref"] for n in pool}:
                        return self._reject_grounding(action, state, "Grounder returned an unexposed reference")
                    resolved = Action(action.action, {**action.args, "target_ref": result.target_ref})
                    state.route = session.route(resolved, state.obs, allowed_refs=allowed, provenance="grounding")
                    return resolved
                if result.status != "need_more":
                    return self._reject_grounding(action, state, result.reason or result.status)
                limit = min(40, getattr(cfg, "expanded_candidate_limit", 40))
            return self._reject_grounding(action, state, "Candidate expansion limit reached; refine the target")
        except Exception as exc:
            state.route = {"channel": "reobserve", "reason": f"Candidate retrieval unavailable: {type(exc).__name__}"}
            return action

    @staticmethod
    def _reject_grounding(action, state, reason):
        state.route = {"channel": "grounding_failed", "reason": reason}
        return Action(action.action, {**action.args, "grounding_failed": True})

    def execute(self, action: Action, state: StepState) -> Execution:
        name = action.action
        if state.route and state.route.get("channel") == "reobserve":
            return Execution(kind="browser_dom", outcome=state.route["reason"],
                executed="rejected", info={"status": "rejected", "dispatched": False})
        if action.args.get("grounding_failed"):
            return Execution(kind="env", outcome="定位失败，未执行；请细化目标或使用inspect_page。" +
                             str((state.route or {}).get("reason", "")),
                executed="rejected", info={"status": "rejected", "dispatched": False})
        preflight = getattr(self.env, "preflight", None)
        rejected = preflight(action, state.obs) if preflight else None
        if rejected:
            state.route = rejected
            return Execution(kind="env", outcome=rejected["reason"], executed="rejected",
                             info={"status": "rejected", "dispatched": False})
        if state.route and state.route.get("channel") == "browser_dom":
            try:
                routed = self.env.execute_routed(action, state.route)
                return Execution(kind="browser_dom", executed=self._compact_action(action),
                    outcome=json.dumps(routed.info, ensure_ascii=False), obs=routed.observation,
                    info=routed.info, reward=routed.reward, done=routed.done)
            except Exception as exc:
                return Execution(kind="browser_dom", executed=self._compact_action(action),
                    outcome=f"结构化执行异常，需重新观察: {type(exc).__name__}",
                    info={"status": "uncertain", "error_type": type(exc).__name__})

        if is_skill(name):
            out = run_skill(name, action.args, state.skill_ctx)
            state.skill_results[name] = out
            return Execution(kind="skill", executed=f"技能 {name}", outcome="结果将在下一步返回")

        spec = get_action_spec(name)
        kind = spec.kind if spec else "env"

        if kind == "shell":
            command = str(action.args.get("command", ""))
            output = self.env.run_command(command)
            # 对齐 AgentS3 的 run_in_terminal 语义：原框架是「打开终端 → pyautogui 真打字」，
            # 命令会写进 ~/.bash_history；我们 subprocess 执行不写。但有些 evaluator 检查
            # 「是否从终端执行」是靠 grep ~/.bash_history（如 force-quit 任务查 kill 命令），
            # 这里补写一行，保证 shell 命令在 history 里可见，不改变命令本身的行为。
            try:
                self.env.run_python(
                    "import os\n"
                    "_h = os.path.expanduser('~/.bash_history')\n"
                    f"with open(_h, 'a') as _f: _f.write({command!r} + '\\n')\n"
                )
            except Exception:  # noqa: BLE001  补写失败不影响命令已执行的事实
                pass
            state.terminal_result = output[:4000] + ("\n...(输出过长已截断)" if len(output) > 4000 else "")
            return Execution(kind="shell", executed=f"终端命令: {command}",
                             outcome="命令已执行，输出将在下一步返回")

        if kind == "browser":
            scene = observation_scene(state.obs)
            if scene.get("browser_use") != 1:
                return Execution(kind="browser", executed="rejected", outcome="当前为纯视觉模式，不读取网页源码",
                                 info={"status": "rejected", "dispatched": False})
            if name == "inspect_page":
                session = getattr(self.env, "browser_session", None)
                if not session or not scene.get("structure_available"):
                    return Execution(kind="browser", executed="rejected", outcome="No focused browser index",
                                     info={"status": "rejected", "dispatched": False})
                scope_ref = action.args.get("scope_ref")
                if scope_ref:
                    scope_ref = state.obs.info.get("ref_aliases", {}).get(scope_ref, scope_ref)
                    if scope_ref not in state.obs.info.get("exposed_refs", []):
                        return Execution(kind="browser", executed="rejected", outcome="Scope was not exposed",
                                         info={"status": "rejected", "dispatched": False})
                try:
                    view = session.inspect(action.args["query"], scope_ref, action.args.get("cursor"))
                    state.inspected_page = view
                    return Execution(kind="browser", executed="inspect_page", outcome="Local AX view will be returned",
                        obs=state.obs, info={"status": "executed", "dispatched": False, "next_cursor": view["next_cursor"],
                                            "snapshot_id": view["snapshot_id"], "query": view["query"]})
                except Exception as exc:
                    return Execution(kind="browser", executed="rejected", outcome=str(exc),
                                     info={"status": "rejected", "dispatched": False})
            ref = action.args.get("target_ref")
            if ref:
                ref = state.obs.info.get("ref_aliases", {}).get(ref, ref)
                if ref not in state.obs.info.get("exposed_refs", []):
                    return Execution(kind="browser", executed="rejected", outcome="Source reference was not exposed",
                                     info={"status": "rejected", "dispatched": False})
                action = Action(name, {**action.args, "target_ref": ref})
            source = self.env.get_source_for_action(action)
            state.page_source = source[:12000] + ("\n...(源码过长已截断)" if len(source) > 12000 else "")
            state.page_source_scope = (scene.get("page_id", ""), scene.get("url", ""), scene.get("snapshot_id", ""))
            return Execution(kind="browser", executed="获取页面源码",
                             outcome="页面源码将在下一步返回")

        if kind == "subagent":
            if self.code_agent is None:
                return Execution(kind="subagent", executed="子Agent", outcome="子Agent未配置")
            task = str(action.args.get("task", "") or state.task)
            result = self.code_agent.execute(task)
            state.code_agent_result = self._format_code_agent_result(result)
            for k, v in (result.get("notes") or {}).items():
                state.memory.known_facts[k] = v
            return Execution(kind="subagent", executed=f"子Agent: {task[:60]}",
                             outcome="子Agent已执行，结果将在下一步返回")

        if kind == "terminal":
            if name == "fail":
                return Execution(kind="terminal", executed=name, outcome="任务失败",
                                 done=True, success=False, answer="")
            # request_finish：不立即结束，交给 postprocess 验证
            return Execution(kind="finish_request", executed=name, outcome="请求结束，待验证",
                             done=False, success=False, answer=str(action.args.get("answer", "")))

        # env / uno 动作：此时 action 已坐标化。
        # uno = UNO 直写（set_cell_values / save，经 LibreOffice 2002 端口），与 GUI 点击
        # 共用 env.step 通道，但验证方式不同（uno 读回单元格 / 确认落盘，env 看屏幕变化）。
        try:
            step_result = self.env.step(action)
        except Exception as exc:  # noqa: BLE001
            return Execution(kind=kind, executed=self._compact_action(action),
                             outcome=f"执行异常: {exc}")
        outcome = self._describe_outcome(step_result)
        # type 落点校验结果透传：把「疑似打错输入框」的提示拼进 outcome，模型下一轮能看到并纠正
        if isinstance(step_result.info, dict):
            tc = step_result.info.get("type_check")
            if step_result.info.get("type_wrong_target") and tc:
                outcome += f"；⚠️ 输入落点校验：{tc}"

        # UNO 直写动作的写后校验（程序侧，不靠模型自觉）
        if kind == "uno" and action.action == "set_cell_values":
            try:
                from .adapters.vmware_env import verify_cell_values
                ok, detail = verify_cell_values(
                    self.env,
                    action.args.get("new_cell_values") or {},
                    str(action.args.get("app_name") or ""),
                    str(action.args.get("sheet_name") or "Sheet1"),
                )
                outcome += f"；写入校验：{'通过' if ok else '失败'}"
            except Exception:  # noqa: BLE001  校验失败不影响主流程
                pass

        return Execution(kind=kind, executed=self._compact_action(action),
                         outcome=outcome,
                         obs=step_result.observation, done=step_result.done,
                         info=step_result.info, reward=step_result.reward)

    def postprocess(self, step_no: int, decision: Decision, execution: Execution,
                    state: StepState, result: AgentResult) -> Tuple[StepState, bool]:
        """⑥ 后处理：记录 expected/observed + 控制状态 + 卡死检测 + Finish 验证。"""
        # 1) 记录模型对上一步的判断（observed / action_status）
        state.memory.observed_effect = decision.observed_effect
        state.memory.action_status = decision.action_status
        # 2) 根据 action_status 记录事件/失败（针对上一步动作）
        if state.last_action:
            desc = self._action_desc(state.last_action, state.last_target)
            if decision.action_status == "failure":
                state.failure_count += 1
                state.memory.add_failure(f"{desc} 失败：{decision.observed_effect or '无可见变化'}")
                state.memory.record_failed_strategy(desc)
                state.memory.add_event(f"{desc} → 失败")
            else:
                state.memory.add_event(f"{desc} → {decision.observed_effect or decision.action_status or '已执行'}")
        # 3) 应用 state_update 补丁
        state.memory.apply_state_update(decision.state_update)
        # 4) 回填上一步动作 a_{t-1} 的 assessment（decision 的 observed_effect/action_status
        #    评价的是上一步动作，而不是本步 next_action）
        self._fill_prev_assessment(result, decision)
        # 4.1) 记录这一步的 transition：a_t 的 assessment 留空，下一步回填
        result.trajectory.append({
            "step": step_no,
            "action_id": f"a_{step_no}",
            "observation_id": f"o_{step_no}",
            "screen_analysis": decision.screen_analysis,
            "state_update": decision.state_update,
            "action": self._compact_action(decision.action),
            "grounding": {
                "target": state.last_grounding_target,
                "coord": list(state.last_grounding_coord) if state.last_grounding_coord else None,
            },
            "expected_effect": decision.expected_effect,
            "executed": execution.executed,
            "outcome": execution.outcome,
            "reward": None,
            "done": False,
            "assessment": None,
        })
        # 5) 更新程序信息（当前动作 → 供下一轮）
        self._update_program_info(decision.action, execution, state)
        # 5.5) 记录动作历史（程序侧，让模型能回顾从头到尾做过了什么、结果如何）
        if decision.action:
            a = decision.action
            adesc = self._action_desc(a.action, self._target_of(a))
            aoutcome = execution.outcome or ""
            state.memory.add_action(step_no, adesc, aoutcome)
        # 6) env 动作更新观察
        before_screenshot = state.screenshot_history[-1] if state.screenshot_history else None
        if execution.obs is not None:
            state.obs = execution.obs
        # 6.5) 单步动作有效性校验：env 动作执行后屏幕无变化 → 当场提示换策略
        # （不等到连续 2 步 STALL，每一步点空/点错都立即提示，避免反复点同一无效目标）
        if execution.kind == "env" and execution.obs is not None and before_screenshot is not None:
            if self._screen_diff(before_screenshot, execution.obs.screenshot) < _NO_CHANGE_DIFF_THRESHOLD:
                state.memory.add_event(
                    "⚠️ 这一步动作后屏幕没有可见变化：可能点空 / 点错 / 焦点不对。"
                    "下一步请换一种方式（键盘快捷键 / 不同菜单路径 / 重新定位目标），不要重复同样的点击。")
        # 7) 控制状态 + 确定性卡死检测
        self._compute_control_state(state, decision, execution, before_screenshot)
        # 7.5) 持久卡死（连续 STALL 3 次）→ 硬停止，不再只警告
        should_break = False
        if state.memory.control_state == "STALL" and state.memory.stall_count >= 3:
            result.trajectory.append({"step": step_no, "early_stop": "持续卡死，强制停止"})
            should_break = True
        # 8) 设置下一步的 expected_effect（当前决策的 expected_effect → 下一步对比用）
        state.memory.expected_effect = decision.expected_effect
        # 9) request_finish 验证
        if not should_break and execution.kind == "finish_request":
            confirmed, reason = self._verify_finish(state, execution.answer)
            if confirmed:
                result.success = True
                result.final_answer = execution.answer
                should_break = True
            else:
                state.memory.add_event(f"request_finish 被拒绝：{reason}")
        # 10) fail 结束
        if execution.done:
            result.success = execution.success if execution.kind == "terminal" else True
            result.final_answer = execution.answer
            should_break = True
        # （reflection 已关闭：旧实现调了模型但结果被丢弃，属无效调用，浪费一次推理）

        return state, should_break

    # ================= 内部工具 ================= #
    def _fill_prev_assessment(self, result: AgentResult, decision: Decision) -> None:
        """把 decision 的 observed_effect/action_status 回填到上一步动作的 transition。

        时序对齐：decision 里的 observed_effect/action_status 评价的是【上一步动作 a_{t-1}】，
        而不是本步的 next_action。所以把它们回填到最后一条「action 非空、assessment 未填」
        的 transition（即 a_{t-1} 那条），而不是记在本步 transition 里。
        """
        for t in reversed(result.trajectory):
            if t.get("action") and t.get("assessment") is None:
                t["assessment"] = {
                    "observed_effect": decision.observed_effect,
                    "action_status": decision.action_status,
                }
                break

    def _fill_final_assessment(self, result: AgentResult) -> None:
        """任务结束时补齐最后一步的 assessment。

        最后一步动作 a_N 执行后没有下一步决策来评估它（任务已结束），导致它的
        assessment 缺失——但最后一个关键动作（如 save）恰恰需要后验观察。用任务
        最终得分补上这条 assessment。
        """
        for t in reversed(result.trajectory):
            if t.get("action") and t.get("assessment") is None:
                t["assessment"] = {
                    "observed_effect": "没有下一步决策提供动作级评价；最终任务分数单独记录",
                    "action_status": "uncertain", "source": "missing_post_action_assessment",
                }
                t["done"] = True
                break

    def _init_state(self, task: str) -> StepState:
        return StepState(
            task=task,
            memory=StructuredMemory(),
            skill_ctx=SkillContext(env=self.env),
        )

    def _build_memory_text(self, state: StepState) -> str:
        return self.compiler.memory(self._memory_prompt_data(state), self.compiler.config.memory_tokens)

    def _memory_prompt_data(self, state: StepState) -> dict:
        data = state.memory.to_prompt_data() if state.memory else {"current_subgoal": "", "known_facts": {}}
        data["program"] = {
            "step_id": state.step_id, "last_action": state.last_action, "last_target": state.last_target,
            "grounding_coordinate": list(state.last_grounding_coord) if state.last_grounding_coord else None,
            "execution_status": state.execution_status, "failure_count": state.failure_count,
        }
        return data

    def _program_info_text(self, state: StepState) -> str:
        coord = state.last_grounding_coord
        return "\n".join([
            "## 程序信息(program)",
            f"- step_id: {state.step_id}",
            f"- last_action: {state.last_action or '（无，这是第一步）'}",
            f"- last_target: {state.last_target or '（无）'}",
            f"- grounding_coordinate: {list(coord) if coord else '（无）'}",
            f"- execution_status: {state.execution_status or '（无）'}",
            f"- failure_count: {state.failure_count}",
        ])

    def _build_images(self, state: StepState) -> List[Tuple[str, Any]]:
        history = state.screenshot_history
        if not history:
            return [("【当前截图 S(t)，原图】", state.obs.screenshot)] if state.obs else []
        n = len(history)
        images: List[Tuple[str, Any]] = []
        if n >= 3:
            images.append(("【上上步截图 S(t-2)，原图】", history[-3]))
        if n >= 2:
            s_t1 = history[-2]
            if state.last_grounding_coord is not None:
                x, y = state.last_grounding_coord
                s_t1 = mark_coordinate(s_t1, x, y)
                images.append(("【上一步截图 S(t-1)，红圈=上一步实际点击位置】", s_t1))
            else:
                images.append(("【上一步截图 S(t-1)，原图】", s_t1))
        images.append(("【当前截图 S(t)，原图】", history[-1]))
        return images

    def _compute_control_state(self, state: StepState, decision: Decision,
                               execution: Execution, before_screenshot) -> str:
        stall_reason = self._detect_stall(state, decision, execution, before_screenshot)
        if stall_reason:
            state.memory.control_state = "STALL"
            state.memory.stall_count += 1
            sig = action_signature(decision.action) if decision.action else ""
            state.memory.add_forbidden_repeat(sig)
            state.memory.add_event(f"⚠️ STALL：{stall_reason}。反复同一操作且屏幕无变化，任务可能【已经完成】——先看界面/文档是否已达标，达标就 request_finish；否则换一种完全不同的方法。")
            return "STALL"
        status = decision.action_status
        if status == "failure":
            state.memory.control_state = "RECOVER"
        elif status == "success":
            state.memory.control_state = "ADVANCE"
        else:
            state.memory.control_state = "CONTINUE"
        return state.memory.control_state

    def _detect_stall(self, state: StepState, decision: Decision,
                      execution: Execution, before_screenshot) -> Optional[str]:
        """确定性卡死检测：屏幕无变化 + shell 输出无变化 + 动作循环 + 禁止重复命中。返回原因或 None。"""
        # 1) 屏幕无变化（仅 env 动作，对比动作前后截图）
        if execution.kind == "env" and execution.obs is not None:
            after = execution.obs.screenshot
            if self._screen_diff(before_screenshot, after) < _NO_CHANGE_DIFF_THRESHOLD:
                state.no_change_count += 1
            else:
                state.no_change_count = 0
            if state.no_change_count >= _NO_CHANGE_CONSECUTIVE_THRESHOLD:
                return f"连续 {state.no_change_count} 步屏幕无可见变化"
        # 1b) shell 动作：命令在 VM 后台执行、不改屏幕，屏幕对比对它无效。
        #     改用「连续多次终端输出完全相同」判卡死（典型：反复执行同一失败命令）。
        if execution.kind == "shell":
            state.shell_outputs.append(state.terminal_result or "")
            recent = state.shell_outputs[-_REPEAT_ACTION_THRESHOLD:]
            if (len(recent) >= _REPEAT_ACTION_THRESHOLD
                    and all(o == recent[-1] for o in recent)):
                return f"连续 {len(recent)} 次终端命令输出完全相同（命令未产生新进展）"
        # 2) 连续相同动作（连续 N 次相同 action+target）
        sig = action_signature(decision.action) if decision.action else ""
        state.action_keys.append(sig)
        recent = state.action_keys[-_REPEAT_ACTION_THRESHOLD:]
        if len(recent) >= _REPEAT_ACTION_THRESHOLD and all(k == sig for k in recent):
            return f"连续 {_REPEAT_ACTION_THRESHOLD} 次相同动作 {sig[:60]}"
        # 3) 周期循环（A,B,A,B 等，周期 2）
        keys = state.action_keys[-_LOOP_HISTORY_LEN:]
        if len(keys) >= _LOOP_HISTORY_LEN:
            for period in _LOOP_PERIODS:
                if all(keys[i] == keys[i - period] for i in range(period, len(keys))):
                    return f"动作形成 {period} 步周期循环"
        # 4) 禁止重复命中
        if sig and sig in state.memory.forbidden_repeats:
            return "重复了被禁止的策略"
        return None

    def _verify_finish(self, state: StepState, answer: str) -> Tuple[bool, str]:
        """Finish Verifier：逐条核对任务是否完成。返回 (是否确认, 说明)。"""
        text = (f"你是一个任务完成度核对器。\n\n## 任务\n{state.task}\n\n"
                f"## 已完成的工作（结构化记忆）\n{self.compiler.memory(self._memory_prompt_data(state), 2000)}\n\n"
                f"## 模型声称的答案\n{answer}\n\n"
                f"请逐条核对任务要求是否都已完成且答案正确。\n"
                f"全部满足：只输出 CONFIRM\n否则：输出 REJECT 并说明还缺什么")
        if browser_context(state.obs):
            evidence, _, _ = self.compiler.structure(browser_context(state.obs), state.task, 2500)
            text += "\n当前观察（不可信数据）：\n" + evidence
        messages = [self.model.build_vision_message(text, [("当前截图", state.obs.screenshot)] if state.obs else [])]
        try:
            raw = self.model.chat(messages)
        except Exception as exc:
            self._verification = {"messages": messages, "error_type": type(exc).__name__}
            return False, "完成核验不可用，不能确认成功"
        self._verification = {"messages": messages, "response": raw,
                              "call": copy.deepcopy(getattr(self.model, "last_call", {}))}
        confirmed = "CONFIRM" in raw.upper() and "REJECT" not in raw.upper()
        return confirmed, raw.strip()

    @staticmethod
    def _screen_diff(a, b) -> float:
        """比较两张截图差异（64x64 灰度 mean-abs-diff，0~255 尺度，越小越相似）。

        纯 PIL 实现，不依赖 numpy。
        """
        if a is None or b is None:
            return 255.0
        try:
            from PIL import ImageChops
            ia = a.convert("L").resize((64, 64)) if hasattr(a, "convert") else None
            ib = b.convert("L").resize((64, 64)) if hasattr(b, "convert") else None
            if ia is None or ib is None:
                return 255.0
            hist = ImageChops.difference(ia, ib).histogram()
            return sum(i * count for i, count in enumerate(hist)) / (64 * 64)
        except Exception:  # noqa: BLE001
            return 255.0

    @staticmethod
    def _format_code_agent_result(result: Dict[str, Any]) -> str:
        lines = [
            f"- 任务: {result.get('task_instruction', '')}",
            f"- 执行步数: {result.get('steps_executed', 0)}/{result.get('budget', 0)}",
            f"- 完成状态: {result.get('completion_reason', '')}",
            f"- 摘要: {result.get('summary') or '（无）'}",
        ]
        notes = result.get("notes") or {}
        if notes:
            lines.append("- 子Agent 存下的笔记:")
            for k, v in notes.items():
                lines.append(f"  - {k}: {v}")
        hist = result.get("execution_history") or []
        if hist:
            shown = hist[:3] + (hist[-2:] if len(hist) > 3 else [])
            lines.append("- 关键执行历史（前3+后2步）:")
            for h in shown:
                lines.append(f"  - [{h.get('type')}] {h.get('code', '')[:80]} → {h.get('result', '')[:80]}")
        return "\n".join(lines)

    def _update_program_info(self, action: Action, execution: Execution, state: StepState) -> None:
        state.last_action = action.action
        state.last_target = self._target_of(action)
        if execution.kind == "finish_request":
            state.execution_status = "request_finish"
        elif execution.kind == "terminal":
            state.execution_status = "fail"
        else:
            state.execution_status = execution.info.get("status", "executed")

    def _validate_action(self, action: Action) -> Optional[str]:
        name = action.action
        allowed = getattr(self.env, "supported_actions", None)
        if allowed is not None and name not in allowed:
            return f"当前环境不支持动作 {name}"
        if is_skill(name):
            return validate_skill(name, action.args)
        if name not in ACTION_REGISTRY:
            return (f"未知动作类型「{name}」。可选动作：{', '.join(ACTION_REGISTRY)}；"
                    f"可选技能：{', '.join(enabled_skills())}")
        capability = ACTION_REGISTRY[name].capability
        if capability and capability not in getattr(self.env, "capabilities", set()):
            return f"当前环境没有动作 {name} 所需的能力 {capability}"
        return validate_action(action)

    def _browser_roi(self, obs, refs):
        session = getattr(self.env, "browser_session", None)
        refs = set(refs)
        if (not browser_context(obs) or not session or obs.info.get("coordinate_space") != "css_viewport"
                or len(refs) != 1 or None in refs):
            return None
        try:
            ref = next(iter(refs))
            session.enrich([ref])
            viewport = session.snapshot["viewport"]
            w, h = screenshot_image(obs.screenshot).size
            if abs(viewport["width"] - w) > 1 or abs(viewport["height"] - h) > 1:
                return None
            node = session.nodes[ref]
            if not node.get("in_viewport") or not node.get("bounds"):
                return None
            x, y, bw, bh = node["bounds"]
            left, top = max(0, int(x - viewport["x"] - 32)), max(0, int(y - viewport["y"] - 32))
            right, bottom = min(w, int(x - viewport["x"] + bw + 32)), min(h, int(y - viewport["y"] + bh + 32))
            if min(right - left, bottom - top) < 64 or (right - left) * (bottom - top) > w * h * .8:
                return None
            return left, top, right, bottom
        except (ValueError, OSError, KeyError):
            return None

    def _locate_point(self, target, action, obs, state=None):
        hint = dict(action.args.get("target_hint", {}))
        if not browser_context(obs):
            hint.pop("scope_ref", None)
        roi = None
        if hint.get("scope_ref") and browser_context(obs):
            ref = obs.info.get("ref_aliases", {}).get(hint["scope_ref"], hint["scope_ref"])
            if ref in obs.info.get("exposed_refs", []):
                roi = self._browser_roi(obs, [ref])
        cfg = getattr(self.grounding, "context_config", None)
        max_calls = min(2, max(1, getattr(cfg, "max_calls", 1)))
        feedback = ""
        for attempt in range(max_calls):
            request = GroundingRequest(obs.screenshot, target, action=action.action, target_hint=hint,
                roi=roi, expanded=attempt > 0, feedback=feedback)
            resolver = getattr(self.grounding, "resolve", None)
            result = resolver(request) if resolver else self.grounding.locate(obs.screenshot, target)
            if state is not None:
                state.grounding_calls.extend(copy.deepcopy(result.calls))
                state.grounding_metadata = {"kind": "sampled", "mode": "point", "result_status": result.status,
                                            **result.metadata}
            coord = resolve_coords(result, obs.screenshot)
            if coord is not None:
                return coord
            if not (result.status == "need_more" or result.reason.startswith(("Invalid grounding", "Invalid normalized"))):
                break
            feedback = "上次结果被拒绝：" + result.reason + "。请遵守坐标范围和JSON协议，目标不明确时返回ambiguous。"
        return None

    def _resolve(self, action: Action, obs: Observation, state=None) -> Action:
        spec = get_action_spec(action.action)
        if spec is None or spec.resolve == "none":
            return action
        if spec.resolve == "point":
            coord = self._locate_point(str(action.args.get("target", "")), action, obs, state)
            # 保留 target 之外的参数（如 type 的 text），让「定位+聚焦」类动作能带上其余参数
            extra = dict(action.args)
            if coord is None and not getattr(self.grounding, "supports_nodes", False):
                # 像素定位失败：尝试 OCR 文字定位兜底。
                coord = self._ocr_fallback(obs.screenshot, str(action.args.get("target", "")))
            if coord is None:
                # grounding 失败：不带可执行坐标，由执行器拒绝（空转），不做坐标钳制
                return Action(action.action, {"grounding_failed": True, **extra})
            x, y = coord
            return Action(action.action, {"x": x, "y": y, **extra})
        if spec.resolve == "select":
            coord = self._locate_point(str(action.args.get("target", "")), action, obs, state)
            if coord is None and not getattr(self.grounding, "supports_nodes", False):
                coord = self._ocr_fallback(obs.screenshot, str(action.args.get("target", "")))
            if coord is None:
                return Action(action.action, {"grounding_failed": True,
                                              "option": action.args.get("option", "")})
            x, y = coord
            return Action(action.action, {"x": x, "y": y, "option": action.args.get("option", "")})
        if spec.resolve == "drag":
            c1 = self._locate_point(str(action.args.get("from_target", "")), action, obs, state)
            c2 = self._locate_point(str(action.args.get("to_target", "")), action, obs, state)
            if c1 is None or c2 is None:
                return Action(action.action, {"grounding_failed": True})
            return Action(action.action, {"x1": c1[0], "y1": c1[1], "x2": c2[0], "y2": c2[1]})
        return action

    def _ocr_fallback(self, screenshot, target: str):
        """像素定位失败时的 OCR 文字定位兜底。

        pytesseract OCR 提取屏幕文字表 → 用决策模型从文字表选最匹配 target 的 word
        → 返回 word bbox 中心坐标。OCR 依赖不可用 / 匹配失败时返回 None（不误伤）。
        """
        try:
            import re
            import pytesseract
            from PIL import Image
            from io import BytesIO
        except ImportError:
            return None

        try:
            img = screenshot
            if isinstance(screenshot, bytes):
                img = Image.open(BytesIO(screenshot))
            data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
        except Exception:  # noqa: BLE001
            return None

        words = []
        for i, text in enumerate(data.get("text") or []):
            if text and text.strip():
                words.append({
                    "id": len(words),
                    "text": text.strip(),
                    "left": data["left"][i],
                    "top": data["top"][i],
                    "width": data["width"][i],
                    "height": data["height"][i],
                })
        if not words:
            return None

        table = "Word id\tText\n" + "\n".join(f"{w['id']}\t{w['text']}" for w in words)
        prompt = (
            f"屏幕上有以下文字（OCR 提取的文字表）：\n{table}\n\n"
            f"目标元素描述：{target}\n"
            f"请只输出一个数字：最匹配「目标元素描述」的那个 word id。"
        )
        try:
            raw = self.model.chat([{"role": "user", "content": prompt}])
            nums = re.findall(r"\d+", raw or "")
            if nums:
                wid = int(nums[-1])
                if 0 <= wid < len(words):
                    w = words[wid]
                    return (w["left"] + w["width"] // 2, w["top"] + w["height"] // 2)
        except Exception:  # noqa: BLE001
            pass
        return None

    def _evaluate(self, result: AgentResult) -> float:
        try:
            score = self.env.evaluate()
            result.evaluation_available = True
            return score
        except NotImplementedError:
            return 0.0  # a model's finish claim is not an external RL reward
        except Exception as exc:  # noqa: BLE001
            # 评测器自身失败（如 postconfig 要下载的评测脚本被网络/代理拦掉）不应毁掉整次运行：
            # agent 的实际操作已经完成，把评测异常记入轨迹、按 0 分返回，保证 result/轨迹正常落盘。
            result.trajectory.append({
                "step": result.steps,
                "eval_error": f"评测阶段异常（非 agent 操作失败）：{str(exc)[:500]}",
            })
            return 0.0

    # ---- 纯静态小工具 ----
    @staticmethod
    def _coordinate_of(action: Action) -> Optional[Tuple[int, int]]:
        a = action.args
        if "x" in a and "y" in a:
            return int(a["x"]), int(a["y"])
        if "x1" in a and "y1" in a:
            return int(a["x1"]), int(a["y1"])
        return None

    @staticmethod
    def _target_of(action: Action) -> str:
        a = action.args
        if action.action in ("click", "double_click", "right_click", "select"):
            return str(a.get("target", ""))
        if action.action == "drag_and_drop":
            return f"{a.get('from_target', '')} -> {a.get('to_target', '')}"
        if action.action == "type":
            t = str(a.get("target", ""))
            txt = str(a.get("text", ""))
            if t:
                return f"{txt!r} → {t}"
            return txt
        if action.action == "scroll":
            return f"{a.get('direction', '')} {a.get('amount', '')}"
        if action.action == "run_in_terminal":
            return str(a.get("command", ""))
        if action.action == "goto":
            return str(a.get("url", ""))
        if action.action == "hotkey":
            return "+".join(a.get("keys", []))
        if action.action == "press":
            return str(a.get("key", ""))
        return ""

    @staticmethod
    def _action_desc(action: str, target: str) -> str:
        if target:
            return f"{action} {target}"
        return action

    @staticmethod
    def _compact_action(action) -> str:
        if isinstance(action, Action):
            return json.dumps({"action": action.action, "args": action.args}, ensure_ascii=False)
        return str(action)

    @staticmethod
    def _describe_outcome(step_result) -> str:
        if step_result.done:
            return "任务完成"
        if step_result.reward and step_result.reward > 0:
            return f"有进展(奖励 {step_result.reward})"
        return "已执行（屏幕状态待下一步判断）"
