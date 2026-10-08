r"""Windows/Chromium runner with a real OpenAI-compatible planning API.

Example (PowerShell):
  .\.venv\Scripts\python.exe run_browser_windows.py --model <model-id> --api-url <base-url>

Credentials come from PLAN_API_KEY (or --api-key-env), never CLI flags/artifacts.
Default task uses the local fixture and independent evaluator; no OSWorld needed.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from osworld_agent.adapters.browser_env import BrowserEnvironment
from osworld_agent.agent import Agent
from osworld_agent.config import ModelConfig, ContextConfig, GroundingConfig, GroundingContextConfig
from osworld_agent.config.model_registry import GROUNDING_PROTOCOLS, normalize_grounding_protocol, environment_setting
from osworld_agent.model.grounding import build_grounding_model
from osworld_agent.metrics import summarize_trajectory
from osworld_agent.model.decision_model import ChatModel
from osworld_agent.model.grounding import NullGrounding
from osworld_agent.trajectory import build_trajectory, dump_trajectory, iter_sft_records

DEFAULT_TASK = "Set Customer name to Acme Ltd, Plan to Enterprise, enable notifications, and Save customer. Verify that it was saved."


def fixture_evaluator(page):
    return float(page.evaluate("""() => {
      const r=JSON.parse(localStorage.getItem('customer-record')||'null');
      return !!r && r.customer==='Acme Ltd' && r.plan==='Enterprise' && r.notify===true && window.saveCount===1;
    }"""))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=environment_setting("PLAN_MODEL"))
    parser.add_argument("--api-url", default=environment_setting("PLAN_API_URL"))
    parser.add_argument("--api-key-env", default="PLAN_API_KEY")
    parser.add_argument("--thinking-style", choices=["none", "dashscope", "vllm"], default=environment_setting("PLAN_THINKING_STYLE", "none"))
    parser.add_argument("--trust-env", action="store_true", help="Use configured HTTP proxy")
    parser.add_argument("--url", default="")
    parser.add_argument("--task", default=DEFAULT_TASK)
    parser.add_argument("--cdp-endpoint", default="")
    parser.add_argument("--channel", default=os.environ.get("OSWORLD_BROWSER_CHANNEL", "auto"),
                        choices=["auto", "chromium", "chrome", "msedge"])
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--context-window", type=int, default=32768)
    parser.add_argument("--input-budget", type=int, default=12288)
    parser.add_argument("--tokenizer-path", default=os.environ.get("PLAN_TOKENIZER_PATH", ""))
    parser.add_argument("--processor-path", default=os.environ.get("PLAN_PROCESSOR_PATH", ""))
    parser.add_argument("--output", default="artifacts/browser-agent")
    parser.add_argument("--ground-url", default=os.environ.get("GROUNDING_API_URL", ""))
    parser.add_argument("--ground-model", default=environment_setting("GROUNDING_MODEL", "grounding"))
    parser.add_argument("--ground-type", type=normalize_grounding_protocol, choices=GROUNDING_PROTOCOLS,
                        default=os.environ.get("GROUNDING_PROTOCOL", "auto"))
    parser.add_argument("--ground-key-env", default="GROUNDING_API_KEY")
    parser.add_argument("--ground-thinking-style", choices=["none", "dashscope", "vllm"], default=os.environ.get("GROUNDING_THINKING_STYLE", "vllm"))
    parser.add_argument("--ground-tokenizer-path", default=os.environ.get("GROUNDING_TOKENIZER_PATH", ""))
    parser.add_argument("--ground-processor-path", default=os.environ.get("GROUNDING_PROCESSOR_PATH", ""))
    parser.add_argument("--ground-logprobs", action="store_true")
    parser.add_argument("--ground-token-ids", action="store_true", help="Requires vLLM token-ID extensions")
    parser.add_argument("--ground-policy-revision", default="")
    parser.add_argument("--logprobs", action="store_true")
    parser.add_argument("--token-ids", action="store_true", help="Requires vLLM token-ID extensions")
    parser.add_argument("--policy-revision", default="")
    args = parser.parse_args()
    if not args.model or not args.api_url or not environment_setting(args.api_key_env):
        parser.error("Provide --model, --api-url and set the API key environment variable; no model endpoint is guessed.")
    if args.ground_url and not environment_setting(args.ground_key_env):
        parser.error("A grounding URL requires its API key environment variable; explicitly use EMPTY for an unauthenticated endpoint.")
    fixture = Path(__file__).parent / "tests" / "fixtures" / "browser_task.html"
    is_fixture = not args.url and not args.cdp_endpoint and args.task == DEFAULT_TASK
    env = BrowserEnvironment(args.url or fixture.resolve().as_uri(), channel=args.channel,
        headless=not args.headed, endpoint=args.cdp_endpoint or None,
        evaluator=fixture_evaluator if is_fixture else None)
    model = ChatModel(ModelConfig(name=args.model, url=args.api_url, api_key_env=args.api_key_env,
        thinking_style=args.thinking_style, trust_env=args.trust_env, collect_logprobs=args.logprobs,
        policy_revision=args.policy_revision, return_token_ids=args.token_ids))
    grounding = NullGrounding()
    if args.ground_url:
        grounding = build_grounding_model(GroundingConfig(name=args.ground_model, url=args.ground_url,
            api_key_env=args.ground_key_env if environment_setting(args.ground_key_env) else "",
            protocol=args.ground_type, thinking_style=args.ground_thinking_style, trust_env=args.trust_env,
            policy_revision=args.ground_policy_revision, collect_logprobs=args.ground_logprobs,
            return_token_ids=args.ground_token_ids,
            context=GroundingContextConfig(tokenizer_path=args.ground_tokenizer_path,
                                           processor_path=args.ground_processor_path)))
    config = ContextConfig(context_window=args.context_window, max_input_tokens=args.input_budget,
                           tokenizer_path=args.tokenizer_path, processor_path=args.processor_path)
    start = time.perf_counter()
    try:
        result = Agent(model, grounding, env, max_steps=args.max_steps, context_config=config).run(args.task)
        out = Path(args.output)
        out.mkdir(parents=True, exist_ok=True)
        trajectory = build_trajectory("windows-browser", "browser", args.task, result)
        dump_trajectory(str(out / "trajectory.json"), trajectory)
        packed = json.loads((out / "trajectory.json").read_text(encoding="utf-8"))
        with (out / "sft.jsonl").open("w", encoding="utf-8") as f:
            for row in iter_sft_records(packed):
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        with (out / "grounding-sft.jsonl").open("w", encoding="utf-8") as f:
            for row in iter_sft_records(packed, actor_role="grounding", include_offline_labels=True):
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        report = {**packed["outcome"], "elapsed_s": round(time.perf_counter() - start, 2),
            "model": args.model,
            "direct_actions": sum(s.get("routing", {}).get("channel") == "browser_dom" for s in packed["steps"]),
            "visual_actions": sum(s.get("routing", {}).get("channel") == "visual" for s in packed["steps"]),
            "trajectory": str((out / "trajectory.json").resolve()), "metrics": summarize_trajectory(packed)}
        (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if result.success else 1
    finally:
        env.close()
        model.close()
        if hasattr(grounding, "close"):
            grounding.close()


if __name__ == "__main__":
    raise SystemExit(main())
