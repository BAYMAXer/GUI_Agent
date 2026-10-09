"""Run one task across the actual Windows desktop and optional browser structure."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time
import uuid

from .adapters.browser_runtime import BrowserRuntimeConfig
from .adapters.windows_env import WindowsEnvironment
from .agent import Agent
from .config import ContextConfig, GroundingConfig, GroundingContextConfig, ModelConfig
from .config.model_registry import environment_setting
from .metrics import summarize_trajectory
from .model.decision_model import ChatModel
from .model.grounding import build_grounding_model
from .run_browser_windows import build_parser as browser_parser
from .trajectory import build_trajectory, dump_trajectory, iter_sft_records


def build_parser():
    parser = browser_parser()
    parser.description = __doc__
    parser.set_defaults(task="", max_steps=30,
        output="", channel=os.environ.get("COMPUTER_BROWSER_CHANNEL", "auto"))
    parser.add_argument("--browser-executable", default=os.environ.get("COMPUTER_BROWSER_EXECUTABLE", ""))
    parser.add_argument("--browser-profile-dir", default=os.environ.get("COMPUTER_BROWSER_PROFILE_DIR", ""))
    parser.add_argument("--download-dir", default=os.environ.get("COMPUTER_DOWNLOAD_DIR", ""))
    parser.add_argument("--monitor", default=os.environ.get("COMPUTER_MONITOR") or "primary",
                        help="Work screen: primary or a display device name from computer doctor")
    parser.set_defaults(cdp_endpoint=os.environ.get("COMPUTER_CDP_ENDPOINT", ""))
    return parser


def execute_task(args, *, environment=None):
    """Export one task; callers may supply an isolated evaluated desktop fixture."""
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    out = Path(args.output or Path("artifacts/computer-agent") / run_id).resolve()
    out.mkdir(parents=True, exist_ok=True)
    env = environment if environment is not None else WindowsEnvironment(browser_config=BrowserRuntimeConfig(channel=args.channel,
        executable=args.browser_executable, endpoint=args.cdp_endpoint, profile_dir=args.browser_profile_dir,
        download_dir=args.download_dir), artifact_dir=out, monitor=args.monitor)
    model = grounding = None
    started = time.perf_counter()
    try:
        model = ChatModel(ModelConfig(name=args.model, url=args.api_url, api_key_env=args.api_key_env,
            thinking_style=args.thinking_style, trust_env=args.trust_env, collect_logprobs=args.logprobs,
            policy_revision=args.policy_revision, return_token_ids=args.token_ids))
        separate_grounding = bool(args.ground_url)
        # One compatible API can exercise both roles; checkpoint provenance stays separate.
        grounding = build_grounding_model(GroundingConfig(
            name=args.ground_model if separate_grounding else args.model,
            url=args.ground_url if separate_grounding else args.api_url,
            api_key_env=args.ground_key_env if separate_grounding else args.api_key_env,
            protocol=args.ground_type,
            thinking_style=args.ground_thinking_style if separate_grounding else args.thinking_style,
            trust_env=args.trust_env, policy_revision=args.ground_policy_revision if separate_grounding else args.policy_revision,
            collect_logprobs=args.ground_logprobs, return_token_ids=args.ground_token_ids,
            context=GroundingContextConfig(tokenizer_path=args.ground_tokenizer_path,
                                          processor_path=args.ground_processor_path)))
        config = ContextConfig(context_window=args.context_window, max_input_tokens=args.input_budget,
                               tokenizer_path=args.tokenizer_path, processor_path=args.processor_path)
        task = args.task + ("\n相关网页 URL: " + args.url if args.url else "")
        result = Agent(model, grounding, env, max_steps=args.max_steps, context_config=config).run(task)
        trajectory = build_trajectory(run_id, "computer", task, result)
        dump_trajectory(str(out / "trajectory.json"), trajectory)
        packed = json.loads((out / "trajectory.json").read_text(encoding="utf-8"))
        for role, name in (("decision", "sft.jsonl"), ("grounding", "grounding-sft.jsonl")):
            with (out / name).open("w", encoding="utf-8") as stream:
                for row in iter_sft_records(packed, actor_role=role, include_offline_labels=role == "grounding"):
                    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        report = {**packed["outcome"], "elapsed_s": round(time.perf_counter()-started, 2),
            "safety_stop": bool(getattr(result, "safety_stop", False)),
            "safety_stop_detail": packed["outcome"].get("safety_stop"),
            "termination_reason": getattr(result, "termination_reason", ""),
            "decision_model": args.model, "grounding_model": args.ground_model if separate_grounding else args.model,
            "grounding_uses_plan_endpoint": not separate_grounding, "downloads": env.runtime.downloads,
            "trajectory": str(out / "trajectory.json"), "metrics": summarize_trajectory(packed)}
        (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return report
    finally:
        if environment is None:
            env.close()
        if model is not None:
            model.close()
        if hasattr(grounding, "close"):
            grounding.close()


def main():
    parser = build_parser()
    args = parser.parse_args()
    if not args.task.strip():
        parser.error("Windows computer use requires --task; it starts from the current desktop")
    if not args.model or not args.api_url or not environment_setting(args.api_key_env):
        parser.error("Configure PLAN_MODEL, PLAN_API_URL and PLAN_API_KEY; credentials must not be CLI arguments")
    if args.ground_url and not environment_setting(args.ground_key_env):
        parser.error("A separate grounding API requires its API key environment variable")
    report = execute_task(args)
    return 0 if report["success"] and not report.get("safety_stop") else 1


if __name__ == "__main__":
    raise SystemExit(main())
