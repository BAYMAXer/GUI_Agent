"""Real Windows Chrome retrieval/execution QA; optional real-model accuracy probes.

No scripted response is reported as a model measurement. API probes require an
explicit endpoint; a missing endpoint leaves model accuracy/latency unknown.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
from pathlib import Path
import sys
import time

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root.parent))
from osworld_agent.actions import Action
from osworld_agent.adapters.browser_env import BrowserEnvironment
from osworld_agent.adapters.grounding import GroundingClient
from osworld_agent.config import GroundingConfig, GroundingContextConfig, ModelConfig
from osworld_agent.context import PromptCompiler
from osworld_agent.metrics import latency_summary
from osworld_agent.model.decision_model import ChatModel
from osworld_agent.model.grounding import GroundingRequest, resolve_coords
from osworld_agent.pipeline import Pipeline, StepState


def timed(fn):
    started = time.perf_counter()
    value = fn()
    return value, round((time.perf_counter() - started) * 1000, 2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", choices=["chrome", "msedge", "chromium", "auto"], default="chrome")
    parser.add_argument("--sizes", nargs="+", type=int, default=[1000, 4501, 10001])
    parser.add_argument("--samples", type=int, default=8)
    parser.add_argument("--output", default=str(root / "artifacts" / "grounding-qa"))
    parser.add_argument("--api-url", default="")
    parser.add_argument("--model", default="grounding")
    parser.add_argument("--api-key-env", default="GROUNDING_API_KEY")
    parser.add_argument("--thinking-style", choices=["vllm", "dashscope", "none"], default="vllm")
    parser.add_argument("--processor-path", default="")
    args = parser.parse_args()
    if args.samples < 1 or any(n < 2 for n in args.sizes):
        parser.error("Use positive samples and at least two buttons per size")
    env = BrowserEnvironment(channel=args.channel)
    ground = GroundingClient(GroundingConfig(name=args.model, url=args.api_url,
        api_key_env=args.api_key_env if os.environ.get(args.api_key_env) else "",
        thinking_style=args.thinking_style, context=GroundingContextConfig(processor_path=args.processor_path)))
    p = Pipeline(model=ChatModel(ModelConfig()), grounding=ground, env=env, system_prompt="Return JSON")
    compiler = PromptCompiler()
    cases, cache_ms, retrieval_ms, execute_ms, api_calls = [], [], [], [], []
    try:
        for count in args.sizes:
            env.page.set_content('<body><h1>客户账单</h1><p>账单合计 21,300 USD</p>' +
                ''.join(f'<button>Noise {i}</button>' for i in range(count - 1)) +
                '<button onclick="window.saved=true">保存客户</button></body>')
            obs, first_ms = timed(env.observe)
            assert obs.context and obs.info["scene"]["browser_use"] == 1
            target = next(n for n in env.session.index.nodes if n["role"] == "button" and n["name"] == "保存客户")
            cold_capture = obs.context[0]["capture_ms"]
            hits = 0
            for _ in range(args.samples):
                pool, elapsed = timed(lambda: env.session.index.retrieve("请保存客户并返回成功提示"))
                retrieval_ms.append(elapsed)
                hits += target["ref"] in [n["ref"] for n in pool]
                obs = env.observe()
                if obs.context[0]["cache_hit"]:
                    cache_ms.append(obs.context[0]["capture_ms"])
            user, _, meta = compiler.compile(system="Return JSON", task="保存客户，并核对账单合计",
                memory="", blocks=obs.context, auxiliary="", images=[("current", obs.screenshot)])
            state = StepState(obs=obs)
            action = p.ground(Action("click", {"target": "保存客户"}), state)
            execution, elapsed = timed(lambda: p.execute(action, state))
            execute_ms.append(elapsed)
            assert execution.info["status"] == "executed" and env.page.evaluate("window.saved") is True
            assert not state.grounding_calls
            cases.append({"kind": "late_chinese_target", "buttons": count, "indexed_nodes": len(obs.context[0]["nodes"]),
                "candidate_recall": hits / args.samples, "cold_capture_ms": cold_capture,
                "first_observe_ms": first_ms, "executed": True, "grounding_calls": 0,
                "important_fact_in_decision_view": "21,300" in user,
                "decision_input": {k: meta[k] for k in ("input_estimate", "counter", "budget_is_estimated")}})

        # A labeled duplicate-control test separates shortlist recall from model selection.
        env.page.set_content('<table>' + ''.join(
            f'<tr><td>客户{i}</td><td><button onclick="window.saved={i}">保存</button></td></tr>'
            for i in range(100)) + '</table>')
        obs = env.observe()
        target = env.session.index.exact("保存", hint={"name": "保存", "scope": "客户99"})
        hint = {"name": "保存", "role": "button"}
        pool, elapsed = timed(lambda: env.session.index.retrieve("保存客户99的记录", hint=hint))
        retrieval_ms.append(elapsed)
        env.session.enrich(n["ref"] for n in pool)
        pool = [env.session.index.record(env.session.nodes[n["ref"]]) for n in pool]
        request = GroundingRequest(obs.screenshot, "保存客户99的记录", mode="node", target_hint=hint,
            candidates=pool, snapshot_id=env.session.snapshot["snapshot_id"],
            ref_aliases={env.session.index.short[n["ref"]]: n["ref"] for n in pool})
        _, meta = ground.compile_request(request)
        duplicate = {"kind": "duplicate_buttons", "target_in_local_shortlist": target["ref"] in [n["ref"] for n in pool],
            "target_in_model_view": target["ref"] in meta["candidate_refs"],
            "candidate_count": meta["candidate_count"], "input_estimate": meta["input_estimate"],
            "counter": meta["counter"], "budget_is_estimated": meta["budget_is_estimated"],
            "node_selection_correct": None, "executed_correctly": None}
        if args.api_url:
            state = StepState(obs=obs)
            action = p.ground(Action("click", {"target": request.target, "target_hint": hint}), state)
            duplicate["node_selection_correct"] = action.args.get("target_ref") == target["ref"]
            execution = p.execute(action, state)
            duplicate["executed_correctly"] = execution.info["status"] == "executed" and env.page.evaluate("window.saved") == 99
            api_calls.extend(state.grounding_calls)
        cases.append(duplicate)

        # A canvas has no actionable AX child; labels are measured in screenshot pixels.
        env.page.set_content('<canvas id="c" width="600" height="300"></canvas><script>'
            'const x=c.getContext("2d");x.fillStyle="#2864d8";x.fillRect(200,100,120,60);'
            'x.fillStyle="white";x.font="20px sans-serif";x.fillText("Save",232,138);</script>')
        obs = env.observe()
        canvas = {"kind": "canvas_visual", "visual_hit": None}
        if args.api_url:
            r = ground.resolve(GroundingRequest(obs.screenshot, "Click the blue Save button in the canvas"))
            point = resolve_coords(r, obs.screenshot)
            box = env.page.locator("canvas").bounding_box()
            canvas["visual_hit"] = bool(point and box["x"] + 200 <= point[0] < box["x"] + 320
                                        and box["y"] + 100 <= point[1] < box["y"] + 160)
            api_calls.extend(r.calls)
        cases.append(canvas)
        report = {"test_kind": "real_browser_local_retrieval_and_execution", "platform": platform.platform(),
            "browser_channel": env.browser_channel, "browser_version": env.page.context.browser.version,
            "model_api_tested": bool(args.api_url), "api_model": args.model if args.api_url else None,
            "cases": cases, "cached_capture_latency": latency_summary(cache_ms),
            "retrieval_latency": latency_summary(retrieval_ms), "execute_and_observe_latency": latency_summary(execute_ms),
            "model_latency": latency_summary([c.get("latency_ms") for c in api_calls]),
            "model_prompt_tokens_actual": [c["usage"]["prompt_tokens"] for c in api_calls if c.get("usage")],
            "use_for_model_training": False}
        output = Path(args.output)
        output.mkdir(parents=True, exist_ok=True)
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=True, indent=2))
    finally:
        ground.close()
        env.close()


if __name__ == "__main__":
    main()
