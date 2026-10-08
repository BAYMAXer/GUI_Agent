"""Deterministic integration smoke, explicitly NOT a Qwen performance test."""
import argparse
import json
import platform
from pathlib import Path
import sys
import time

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root.parent))
from osworld_agent.trajectory import build_trajectory, dump_trajectory
from osworld_agent.adapters.browser_env import BrowserEnvironment
from osworld_agent.agent import Agent
from osworld_agent.script.browser_fixture import ScriptedPolicy, CountingGrounding, evaluate_customer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", choices=["auto", "chromium", "chrome", "msedge"], default=None)
    args = parser.parse_args()
    fixture = root / "tests" / "fixtures" / "browser_task.html"
    env = BrowserEnvironment(fixture.resolve().as_uri(), evaluator=evaluate_customer, channel=args.channel)
    ground = CountingGrounding()
    start = time.perf_counter()
    try:
        result = Agent(ScriptedPolicy(), ground, env, max_steps=6).run(
            "Set Customer name to Acme Ltd, Plan to Enterprise, enable notifications, and Save customer.")
        output = root / "artifacts" / "windows-smoke"
        output.mkdir(parents=True, exist_ok=True)
        trajectory = build_trajectory("windows-fixture-scripted", "browser", result.task, result)
        trajectory["provenance"] = {"policy": "scripted_test_double", "use_for_model_training": False}
        dump_trajectory(str(output / "trajectory.json"), trajectory)
        env.page.screenshot(path=str(output / "final.png"), scale="css")
        report = {"test_kind": "scripted_policy_real_browser", "platform": platform.platform(),
            "browser_channel": env.browser_channel,
            "browser_version": env.page.context.browser.version, "score": result.score,
            "success": result.success, "steps": result.steps, "grounding_calls": ground.calls,
            "scene_flags": [s["observation"]["info"]["scene"]["browser_use"] for s in result.trajectory],
            "structure_available": [s["observation"]["info"]["scene"]["structure_available"] for s in result.trajectory],
            "elapsed_s": round(time.perf_counter() - start, 3),
            "dom_action_ms": [s["execution_info"]["latency_ms"] for s in result.trajectory if s.get("execution_info", {}).get("channel") == "browser_dom"],
            "capture_ms": [s["observation"]["context"][0]["capture_ms"] for s in result.trajectory],
            "prompt_estimates": [s["prompt_metadata"]["input_estimate"] for s in result.trajectory]}
        (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        assert result.success and result.score == 1 and ground.calls == 0
    finally:
        env.close()


if __name__ == "__main__":
    main()
