"""Run environment, scripted and real-model fixture acceptance in that order."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from ..config.model_registry import environment_setting


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", default=os.getenv("OSWORLD_BROWSER_CHANNEL", "auto"))
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = root / "artifacts/acceptance"
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / "report.json"
    report = {"status": "running", "success": False, "real_model_verified": False, "stages": []}

    def save():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    def run_stage(name, module, parameters, evidence):
        stage = {"name": name, "status": "running", "evidence": str(evidence)}
        report["stages"].append(stage)
        evidence.unlink(missing_ok=True)
        save()
        completed = subprocess.run([sys.executable, "-m", module, *parameters], cwd=root)
        stage["exit_code"] = completed.returncode
        stage["status"] = "passed" if completed.returncode == 0 else "failed"
        save()
        if completed.returncode:
            raise RuntimeError(f"{name} failed (exit {completed.returncode}); inspect {evidence}")
        return json.loads(evidence.read_text(encoding="utf-8"))

    save()
    try:
        doctor = run_stage("environment", "osworld_agent.script.doctor",
                           ["--output", str(output / "doctor.json")], output / "doctor.json")
        if not doctor["success"]:
            raise RuntimeError("Environment check failed")
        smoke_dir = output / "smoke"
        smoke = run_stage("scripted_fixture", "osworld_agent.script.smoke_browser_windows",
                          ["--channel", args.channel, "--output", str(smoke_dir)], smoke_dir / "report.json")
        if not smoke["success"] or smoke["score"] != 1:
            raise RuntimeError("Scripted fixture did not pass its evaluator")
        missing = [name for name in ("PLAN_MODEL", "PLAN_API_URL", "PLAN_API_KEY") if not environment_setting(name)]
        if environment_setting("GROUNDING_API_URL") and not environment_setting("GROUNDING_API_KEY"):
            missing.append("GROUNDING_API_KEY")
        if missing:
            report["status"] = "blocked"
            report["reason"] = "Configure " + ", ".join(missing) + " in .env; model parameters are never guessed."
            save()
            print(report["reason"])
            return 1
        real_dir = output / "browser-agent"
        parameters = ["--channel", args.channel, "--max-steps", str(args.max_steps), "--output", str(real_dir)]
        if not args.headless:
            parameters.append("--headed")
        real = run_stage("real_model_fixture", "osworld_agent.run_browser_windows", parameters, real_dir / "report.json")
        if not (real["success"] and real["score"] == 1 and real["evaluation_available"]
                and real["reward_source"] == "environment_evaluator"):
            raise RuntimeError("Real-model task did not pass the independent fixture evaluator")
        report.update(status="passed", success=True, real_model_verified=True)
        save()
        print(f"Acceptance passed. Independent evaluator score=1. Report: {report_path}")
        return 0
    except (RuntimeError, OSError, ValueError, KeyError) as exc:
        if report["stages"]:
            report["stages"][-1]["status"] = "failed"
        report.update(status="failed", reason=str(exc))
        save()
        print(f"Acceptance failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
