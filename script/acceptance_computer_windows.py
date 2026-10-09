"""Verify Windows desktop, optional browser fallback and a configured-model cross-app task."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from ..adapters.browser_runtime import BrowserRuntimeConfig
from ..config.model_registry import environment_setting
from ..run_computer_windows import build_parser, execute_task
from .computer_fixture import ComputerFixture, crossed_applications


def run_real_fixture(args):
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / "report.json"
    report = {"success": False, "score": None, "evaluation_available": False,
              "cross_application_verified": False, "test_kind": "configured_model_real_windows_desktop"}
    try:
        with ComputerFixture(output, BrowserRuntimeConfig(channel=args.channel,
                             executable=args.browser_executable), monitor=args.monitor) as fixture:
            args.task, args.url = fixture.task, ""
            report = execute_task(args, environment=fixture.environment)
            trajectory = json.loads(Path(report["trajectory"]).read_text(encoding="utf-8"))
            report.update(test_kind="configured_model_real_windows_desktop", fixture_directory=str(fixture.directory),
                          cross_application_verified=crossed_applications(trajectory))
            verified = (report["success"] and report["score"] == 1 and report["evaluation_available"]
                        and report["reward_source"] == "environment_evaluator" and report["cross_application_verified"])
            if not verified:
                report["success"] = False
                report["reason"] = "Independent PDF download, saved Notepad content and cross-application evidence must all pass."
    except Exception as exc:
        report.update(success=False, error_type=type(exc).__name__,
                      reason="Real desktop fixture failed; inspect this stage's stdout and trajectory.")
        print(f"Computer fixture failed ({type(exc).__name__}).")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["success"] and not report.get("safety_stop") else 1


def model_arguments(args):
    parameters = []
    for name in ("model", "api_url", "api_key_env", "thinking_style", "channel", "browser_executable", "monitor",
                 "max_steps", "context_window", "input_budget", "tokenizer_path", "processor_path",
                 "ground_url", "ground_model", "ground_type", "ground_key_env", "ground_thinking_style",
                 "ground_tokenizer_path", "ground_processor_path", "ground_policy_revision", "policy_revision"):
        value = getattr(args, name)
        if value != "":
            parameters.extend(["--" + name.replace("_", "-"), str(value)])
    for name in ("trust_env", "logprobs", "token_ids", "ground_logprobs", "ground_token_ids"):
        if getattr(args, name):
            parameters.append("--" + name.replace("_", "-"))
    return parameters


def main():
    parser = build_parser()
    parser.description = __doc__
    parser.set_defaults(output="artifacts/computer-acceptance")
    parser.add_argument("--real-fixture", action="store_true", help="Run only the configured model stage")
    args = parser.parse_args()
    if args.task or args.url:
        parser.error("Computer acceptance uses an isolated local fixture; run the computer agent for custom tasks.")
    session_options = {"--browser-profile-dir", "--cdp-endpoint", "--download-dir"}
    if any(argument.split("=", 1)[0] in session_options for argument in sys.argv[1:]):
        parser.error("Computer acceptance owns its profile and downloads; omit profile, CDP and download overrides.")
    root = Path(__file__).resolve().parents[1]
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if args.real_fixture:
        return run_real_fixture(args)
    report_path = output / "report.json"
    report = {"status": "running", "success": False, "real_model_verified": False,
              "cross_application_verified": False, "stages": []}

    def save():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    def run_stage(name, module, parameters, evidence):
        report["stages"].append({"name": name, "status": "running", "evidence": str(evidence)})
        evidence.unlink(missing_ok=True)
        save()
        completed = subprocess.run([sys.executable, "-m", module, *parameters], cwd=root)
        stage = report["stages"][-1]
        stage.update(exit_code=completed.returncode, status="passed" if completed.returncode == 0 else "failed")
        save()
        if completed.returncode:
            raise RuntimeError(f"{name} failed (exit {completed.returncode}); inspect {evidence}")
        return json.loads(evidence.read_text(encoding="utf-8"))

    save()
    try:
        doctor = run_stage("environment", "osworld_agent.script.doctor",
                           ["--computer", "--monitor", args.monitor, "--output", str(output / "doctor.json")], output / "doctor.json")
        if not doctor["success"]:
            raise RuntimeError("Interactive Windows environment check failed")
        smoke_dir = output / "smoke"
        smoke_args = ["--channel", args.channel, "--monitor", args.monitor, "--output", str(smoke_dir)]
        if args.browser_executable:
            smoke_args.extend(["--browser-executable", args.browser_executable])
        smoke = run_stage("scripted_cross_application", "osworld_agent.script.smoke_computer_windows",
                          smoke_args, smoke_dir / "report.json")
        if not smoke["success"] or smoke["score"] != 1:
            raise RuntimeError("Scripted cross-application task or visual fallback checks failed")
        missing = []
        if not args.model:
            missing.append("PLAN_MODEL")
        if not args.api_url:
            missing.append("PLAN_API_URL")
        if not environment_setting(args.api_key_env):
            missing.append(args.api_key_env)
        if args.ground_url and not environment_setting(args.ground_key_env):
            missing.append(args.ground_key_env)
        if missing:
            report.update(status="blocked", reason="Configure " + ", ".join(missing) + " in .env; no model parameters are guessed.")
            save()
            print(report["reason"])
            return 1
        real_dir = output / "computer-agent"
        real = run_stage("real_model_cross_application", "osworld_agent.script.acceptance_computer_windows",
                         ["--real-fixture", "--output", str(real_dir), *model_arguments(args)], real_dir / "report.json")
        if not (real["success"] and real["score"] == 1 and real["evaluation_available"]
                and real["reward_source"] == "environment_evaluator" and real["cross_application_verified"]):
            raise RuntimeError("Configured model did not pass the independent cross-application evaluator")
        report.update(status="passed", success=True, real_model_verified=True, cross_application_verified=True)
        save()
        print(f"Computer acceptance passed. Independent evaluator score=1. Report: {report_path}")
        return 0
    except (RuntimeError, OSError, ValueError, KeyError) as exc:
        if report["stages"]:
            report["stages"][-1]["status"] = "failed"
        report.update(status="failed", reason=str(exc))
        save()
        print(f"Computer acceptance failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
