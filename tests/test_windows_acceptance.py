"""Acceptance must use fresh independent evidence and never guess credentials."""
import json
from types import SimpleNamespace

import pytest

from osworld_agent.script import acceptance_windows


@pytest.fixture
def acceptance(tmp_path, monkeypatch):
    root = tmp_path / "Windows clone with spaces"
    root.mkdir()
    monkeypatch.setattr(acceptance_windows, "__file__", str(root / "script" / "acceptance_windows.py"))
    monkeypatch.setattr(acceptance_windows.sys, "argv", ["acceptance", "--headless"])
    settings = {}
    monkeypatch.setattr(acceptance_windows, "environment_setting", lambda name: settings.get(name, ""))
    calls = []
    payloads = [{"success": True}, {"success": True, "score": 1},
                {"success": True, "score": 1, "evaluation_available": True,
                 "reward_source": "environment_evaluator"}]

    def run(command, cwd):
        index = len(calls)
        calls.append(command)
        report = root / ["artifacts/acceptance/doctor.json", "artifacts/acceptance/smoke/report.json",
                         "artifacts/acceptance/browser-agent/report.json"][index]
        report.parent.mkdir(parents=True, exist_ok=True)
        if payloads[index] is not None:
            report.write_text(json.dumps(payloads[index]), encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(acceptance_windows.subprocess, "run", run)
    return root, settings, calls, payloads


def report(root):
    return json.loads((root / "artifacts/acceptance/report.json").read_text(encoding="utf-8"))


def configure(settings):
    settings.update(PLAN_MODEL="deployment-name", PLAN_API_URL="https://example.invalid/v1", PLAN_API_KEY="test-key")


def test_missing_credentials_runs_independent_checks_then_blocks(acceptance):
    root, _, calls, _ = acceptance
    assert acceptance_windows.main() == 1
    saved = report(root)
    assert len(calls) == 2
    assert saved["status"] == "blocked" and not saved["real_model_verified"]
    assert all(stage["status"] == "passed" for stage in saved["stages"])
    assert "PLAN_API_KEY" in saved["reason"]


@pytest.mark.parametrize("override", [{"score": 0}, {"evaluation_available": False},
                                      {"reward_source": "model_claim"}])
def test_model_claim_requires_independent_success(acceptance, override):
    root, settings, _, payloads = acceptance
    configure(settings)
    payloads[2].update(override)
    assert acceptance_windows.main() == 1
    saved = report(root)
    assert saved["status"] == "failed" and not saved["success"] and not saved["real_model_verified"]
    assert saved["stages"][-1]["status"] == "failed"


def test_stale_success_report_cannot_pass_a_new_run(acceptance):
    root, settings, _, payloads = acceptance
    configure(settings)
    old = root / "artifacts/acceptance/browser-agent/report.json"
    old.parent.mkdir(parents=True)
    old.write_text(json.dumps(payloads[2]), encoding="utf-8")
    payloads[2] = None
    assert acceptance_windows.main() == 1
    assert report(root)["status"] == "failed" and not old.exists()


def test_verified_fixture_passes_without_putting_key_in_arguments(acceptance):
    root, settings, calls, _ = acceptance
    configure(settings)
    assert acceptance_windows.main() == 0
    saved = report(root)
    assert saved["success"] and saved["real_model_verified"] and saved["status"] == "passed"
    assert not any("test-key" in part for command in calls for part in command)
