"""训练轨迹导出：把一次任务运行整理成「可直接喂 SFT / RL」的标准轨迹。

轨迹结构（一个任务一个 trajectory.json）：
{
  "version": "1.0",
  "task": {"id", "domain", "instruction"},
  "outcome": {"success", "score", "num_steps", "final_answer"},
  "steps": [
    {
      "step", "screenshot",           # 决策前的观察 s_t（截图文件名）
      "reasoning": {screen_analysis, observed_effect, action_status, expected_effect, state_update},
      "action": {"type", "args"},     # 决策动作（含 grounding 坐标）
      "grounding": {"target", "coord"} # 可选：定位详情（本地 runner 才有）
      "execution": {"command", "outcome"},
      "reward": null,                  # 留空，训练时由 reward 模型/规则回填
      "done": false
    }
  ]
}

说明：
- reward 字段故意留 null：步级 reward 的算法还没定，训练时统一回填；
- 截图不内嵌 base64，只记文件名（截图文件与 trajectory.json 同目录），避免文件爆炸；
- 本地 runner（viz/runner.py）有 logging 采集（decisions/groundings），能导出最全的
  reasoning + grounding + 截图文件名；服务器 run_docker.py 只有 result.trajectory，
  导出时会缺 grounding 详情和截图文件名，但仍保留 reasoning/action/execution。
"""
from __future__ import annotations

import json
import base64
import hashlib
import copy
import math
from pathlib import Path
from typing import Any, Dict, List, Optional

TRAJECTORY_VERSION = "3.0"


def _parse_action(action_repr) -> Dict[str, Any]:
    """把 result.trajectory 里的 action 字段（可能是 JSON 字符串）解析成 dict。"""
    if isinstance(action_repr, dict):
        return action_repr
    if isinstance(action_repr, str):
        try:
            return json.loads(action_repr)
        except (json.JSONDecodeError, TypeError):
            return {"raw": action_repr}
    return {"raw": str(action_repr)}


def build_trajectory(
    task_id: str,
    domain: str,
    instruction: str,
    result,
    decisions: Optional[List[Dict[str, Any]]] = None,
    groundings: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """把一次 AgentResult 组装成标准训练轨迹 dict。

    decisions/groundings 来自 logging 采集（本地 runner 才有）；传 None 则从
    result.trajectory 尽力取（服务器 run_docker 场景）。
    """
    # 预索引 logging 采集的数据（按 step 号）
    decision_map = {}
    if decisions:
        for d in decisions:
            decision_map[d.get("step")] = d
    grounding_map: Dict[int, List[Dict[str, Any]]] = {}
    if groundings:
        for g in groundings:
            grounding_map.setdefault(g.get("step"), []).append(g)

    steps: List[Dict[str, Any]] = []
    for s in (result.trajectory or []):
        step_no = s.get("step")
        d = decision_map.get(step_no, {})

        # 解析失败的 entry（无 action）跳过，不进入训练轨迹
        if not s.get("action") and "policy_input" not in s:
            continue

        action_repr = s.get("action")
        action = _parse_action(action_repr)

        # assessment 是「下一步决策」回填的上一步动作评价（时序对齐后不再与 action 同帧）
        assessment = s.get("assessment") or {}

        # grounding 详情（本地 runner 才有）
        grounding = s.get("grounding")
        gs = grounding_map.get(step_no, [])
        if gs and not grounding:
            g = next((g for g in reversed(gs) if g.get("coord") is not None), gs[-1])
            grounding = {
                "target": g.get("query", ""),
                "coord": g.get("coord"),
                "elapsed": g.get("elapsed"),
            }

        steps.append({
            "step": step_no,
            "action_id": s.get("action_id") or f"a_{step_no}",
            "observation_id": s.get("observation_id") or f"o_{step_no}",
            "screenshot": d.get("screenshot_file") or f"step_{step_no}.png",
            "reasoning": {
                "screen_analysis": s.get("screen_analysis", ""),
                "expected_effect": s.get("expected_effect", ""),
                "state_update": s.get("state_update") or {},
            },
            "action": action,
            "grounding": grounding,
            "execution": {
                "command": s.get("executed", ""),
                "outcome": s.get("outcome", ""),
            },
            # 时序对齐：assessment 评价的是【本步 action】，来自下一步决策回填
            "assessment": assessment,
            "reward": s.get("reward"),   # 留空，训练时回填
            "done": bool(s.get("done", False)),
            "terminated": bool(s.get("terminated", False)),
            "truncated": bool(s.get("truncated", False)),
            "valid_action": s.get("valid_action", bool(s.get("action"))),
            "observation": s.get("observation") or {"id": f"o_{step_no}", "context": [], "screenshot": None},
            "next_observation": s.get("next_observation"),
            "policy_input": s.get("policy_input"),
            "policy_output": s.get("policy_output"),
            "decision_calls": s.get("decision_calls", []),
            "model_calls": s.get("model_calls", []),
            "grounding_metadata": s.get("grounding_metadata", {}),
            "grounding_labels": s.get("grounding_labels", {}),
            "offline_grounding_labels": s.get("offline_grounding_labels", []),
            "prompt_metadata": s.get("prompt_metadata", {}),
            "routing": s.get("routing", {}),
            "resolved_action": s.get("resolved_action"),
            "execution_info": s.get("execution_info", {}),
            "verification": s.get("verification"),
            "timing_ms": s.get("timing_ms", {}),
            "trainable": bool(s.get("policy_input") and s.get("policy_output") and s.get("valid_action", False)),
        })

    return {
        "version": TRAJECTORY_VERSION,
        "task": {
            "id": task_id,
            "domain": domain,
            "instruction": instruction,
        },
        "outcome": {
            "success": bool(getattr(result, "success", False)),
            "score": float(getattr(result, "score", 0.0)),
            "num_steps": int(getattr(result, "steps", 0)),
            "final_answer": getattr(result, "final_answer", ""),
            "evaluation_available": bool(getattr(result, "evaluation_available", False)),
            "reward_source": "environment_evaluator" if getattr(result, "evaluation_available", False) else "unavailable",
            "termination_reason": getattr(result, "termination_reason", "unknown"),
        },
        "steps": steps,
    }


def dump_trajectory(path: str, trajectory: Dict[str, Any]) -> None:
    """Content-address images, including every history/annotated/final image actually sent."""
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    assets = output.parent / "assets"

    def externalize(obj):
        if isinstance(obj, str) and obj.startswith("data:image/") and ";base64," in obj:
            header, encoded = obj.split(",", 1)
            data = base64.b64decode(encoded, validate=True)
            digest = hashlib.sha256(data).hexdigest()
            suffix = ".jpg" if "jpeg" in header else ".png"
            assets.mkdir(exist_ok=True)
            target = assets / (digest + suffix)
            if not target.exists():
                target.write_bytes(data)
            return f"assets/{target.name}"
        if isinstance(obj, dict):
            return {k: externalize(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [externalize(v) for v in obj]
        return obj

    packed = externalize(trajectory)
    for step in packed["steps"]:
        if step.get("observation", {}).get("screenshot"):
            step["screenshot"] = step["observation"]["screenshot"]
    output.write_text(json.dumps(packed, ensure_ascii=False, indent=2), encoding="utf-8")


def iter_sft_records(trajectory, require_success=True, actor_role="decision", include_offline_labels=False):
    """Same exporter for every domain. No reconstruction, no hindsight in the prompt.

    Success means an independent evaluator passed; model self-reports aren't labels.
    Loss applies only to completion, never to input/retry assistant messages.
    """
    outcome = trajectory.get("outcome", {})
    if trajectory.get("provenance", {}).get("use_for_model_training") is False:
        return
    if require_success and not (outcome.get("evaluation_available") and outcome.get("score", 0) >= 1):
        return
    for step in trajectory.get("steps", []):
        if actor_role != "decision":
            if actor_role not in {"grounding", "all"}:
                raise ValueError("actor_role must be decision, grounding or all")
            calls = [c for c in step.get("model_calls", []) if c.get("actor_role") == "grounding"]
            if include_offline_labels and step.get("execution_info", {}).get("status") == "executed":
                calls += step.get("offline_grounding_labels", [])
            for call in calls:
                if (not call.get("messages") or not call.get("response") or call.get("finish_reason") == "length"
                        or not (call.get("valid_action") or call.get("result_status") == "need_more")):
                    continue
                yield {"prompt": copy.deepcopy(call["messages"]),
                       "completion": [{"role": "assistant", "content": call["response"]}],
                       "task_id": trajectory["task"]["id"], "step": step["step"],
                       "actor_role": "grounding", "provenance": call.get("provenance", "sampled"),
                       "loss_scope": "completion_only", "schema_version": TRAJECTORY_VERSION}
            if actor_role != "all":
                continue
        if not step.get("trainable"):
            continue
        calls = step.get("decision_calls") or []
        if calls and calls[-1].get("finish_reason") == "length":
            continue
        yield {"prompt": copy.deepcopy(step["policy_input"]),
               "completion": [{"role": "assistant", "content": step["policy_output"]}],
               "task_id": trajectory["task"]["id"], "step": step["step"],
               "actor_role": "decision", "provenance": "sampled",
               "loss_scope": "completion_only", "schema_version": TRAJECTORY_VERSION}


def restore_messages(messages, trajectory_path):
    """Load exported messages for replay using exactly the saved image bytes."""
    root = Path(trajectory_path).resolve().parent
    restored = copy.deepcopy(messages)
    for message in restored:
        if not isinstance(message.get("content"), list):
            continue
        for part in message["content"]:
            if part.get("type") != "image_url":
                continue
            url = part["image_url"]["url"]
            if url.startswith("assets/"):
                path = (root / url).resolve()
                if not path.is_relative_to(root):
                    raise ValueError("Image path escapes trajectory directory")
                mime = "image/jpeg" if path.suffix == ".jpg" else "image/png"
                part["image_url"]["url"] = f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()
    return restored


def import_external_episode(task_id, domain, instruction, records, *, source, score=None):
    """Import pre-mapped external records without inventing missing policy inputs.

    records use messages/response/observation/next_observation/action; dataset-specific
    loaders map their fields here once. Browser context is optional for every domain.
    score must come from the dataset's outcome/evaluator, not be inferred from actions.
    """
    from .actions import parse_decision, action_from_decision, validate_action
    from .pipeline import AgentResult
    result = AgentResult(task=instruction, score=float(score or 0), steps=len(records),
                         evaluation_available=score is not None,
                         success=score is not None and score >= 1,
                         termination_reason="external_dataset")
    for i, record in enumerate(records, 1):
        response = record.get("response")
        action = action_from_decision(parse_decision(response) or {}) if isinstance(response, str) else None
        valid = bool(action and validate_action(action) is None)
        observation = copy.deepcopy(record.get("observation") or {})
        observation.setdefault("id", f"o_{i}")
        observation.setdefault("context", [])
        observation.setdefault("screenshot", record.get("screenshot"))
        result.trajectory.append({"step": i, "observation": observation,
            "next_observation": record.get("next_observation"),
            "action": {"action": action.action, "args": action.args} if action else record.get("action"),
            "policy_input": copy.deepcopy(record.get("messages")), "policy_output": response,
            "valid_action": valid, "reward": record.get("reward"),
            "terminated": bool(record.get("terminated", False)),
            "truncated": bool(record.get("truncated", False)),
            "done": bool(record.get("terminated") or record.get("truncated")),
            "prompt_metadata": {"origin": "external", "source": source,
                                "input_fidelity": "provided" if record.get("messages") else "missing"},
            "decision_calls": copy.deepcopy(record.get("decision_calls", [])),
            "model_calls": copy.deepcopy(record.get("model_calls", [])),
            "grounding_metadata": copy.deepcopy(record.get("grounding_metadata", {})),
            "grounding_labels": copy.deepcopy(record.get("grounding_labels", {})),
            "offline_grounding_labels": copy.deepcopy(record.get("offline_grounding_labels", [])),
            "execution_info": copy.deepcopy(record.get("execution_info", {})),
            "routing": copy.deepcopy(record.get("routing", {})),
            "resolved_action": copy.deepcopy(record.get("resolved_action"))})
    trajectory = build_trajectory(task_id, domain, instruction, result)
    trajectory["outcome"]["reward_source"] = "external_dataset_label" if score is not None else "unavailable"
    trajectory["provenance"] = {"source": source, "policy": "external", "use_for_model_training": True}
    return trajectory


def iter_on_policy_records(trajectory, policy_revision, actor_role="decision"):
    """Strict RL gate: missing behavior-policy statistics cannot be fabricated.

    Includes rejected format attempts. An API teacher's output is SFT data, not
    student on-policy data. The trainer supplies advantages and token loss masks.
    """
    outcome = trajectory.get("outcome", {})
    if not outcome.get("evaluation_available"):
        raise ValueError("Independent episode reward is required")
    if not policy_revision:
        raise ValueError("A fixed behavior policy revision is required")
    if trajectory.get("provenance", {}).get("use_for_model_training") is False:
        raise ValueError("Test fixtures are not policy rollouts")
    samples = []
    if actor_role not in {"decision", "grounding"}:
        raise ValueError("RL export requires a single actor_role")
    for step in trajectory.get("steps", []):
        calls = [c for c in step.get("model_calls", []) if c.get("actor_role") == actor_role]
        if not calls and actor_role == "decision":
            calls = step.get("decision_calls", [])  # v2/external compatibility
        if not calls:
            if actor_role == "grounding":
                continue  # A deterministic route did not sample this policy.
            raise ValueError("Missing decision call records")
        for attempt, call in enumerate(calls):
            if not call.get("response") and call.get("error_type") and not call.get("token_ids"):
                continue  # A transport failure did not sample the behavior policy.
            if call.get("provenance", "sampled") != "sampled":
                raise ValueError("Offline labels are not on-policy samples")
            ids, lp = call.get("token_ids"), call.get("token_logprobs")
            if (call.get("policy_revision") != policy_revision or not ids or not lp or len(ids) != len(lp)
                    or any(isinstance(i, bool) or not isinstance(i, int) or i < 0 for i in ids)
                    or any(isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p) for p in lp)):
                raise ValueError("Need matching policy_revision, completion token_ids and token_logprobs for every call")
            samples.append({"prompt": call["messages"], "completion": call["response"],
                "token_ids": ids, "behavior_logprobs": lp, "step": step["step"], "attempt": attempt,
                "actor_role": actor_role,
                "valid_action": bool(call.get("valid_action")), "episode_reward": outcome["score"],
                "step_reward": step.get("reward") if attempt == len(calls)-1 else None,
                "terminated": step.get("terminated", False), "truncated": step.get("truncated", False),
                "next_observation": step.get("next_observation")})
    if actor_role == "grounding" and not samples:
        raise ValueError("No sampled grounding calls; deterministic labels are SFT-only")
    yield from samples
