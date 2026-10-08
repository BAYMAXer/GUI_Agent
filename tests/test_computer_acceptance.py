"""Independent files and cross-app evidence gate model acceptance; no native input."""
import json
from types import SimpleNamespace

import pytest

from osworld_agent.script import acceptance_computer_windows as acceptance
from osworld_agent.script import computer_fixture


@pytest.fixture
def stages(tmp_path, monkeypatch):
    monkeypatch.setattr(acceptance, "__file__", str(tmp_path / "script" / "acceptance_computer_windows.py"))
    output = tmp_path / "迁移 with spaces"
    monkeypatch.setattr(acceptance.sys, "argv", ["acceptance", "--output", str(output)])
    monkeypatch.setattr(acceptance, "environment_setting", lambda name: "")
    payloads = [{"success": True}, {"success": True, "score": 1},
                {"success": True, "score": 1, "evaluation_available": True,
                 "reward_source": "environment_evaluator", "cross_application_verified": True}]
    calls = []
    def run(command, cwd):
        index = len(calls)
        calls.append(command)
        evidence = output / ["doctor.json", "smoke/report.json", "computer-agent/report.json"][index]
        evidence.parent.mkdir(parents=True, exist_ok=True)
        if payloads[index] is not None:
            evidence.write_text(json.dumps(payloads[index]), encoding="utf-8")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(acceptance.subprocess, "run", run)
    return output, calls, payloads


def configured(monkeypatch, output):
    monkeypatch.setattr(acceptance.sys, "argv", ["acceptance", "--output", str(output),
        "--model", "deployment", "--api-url", "https://example.invalid/v1", "--ground-type", "pixel", "--trust-env"])
    monkeypatch.setattr(acceptance, "environment_setting", lambda name: "test-key" if name == "PLAN_API_KEY" else "")


def read_report(output):
    return json.loads((output / "report.json").read_text(encoding="utf-8"))


def test_missing_model_runs_desktop_and_fallback_checks_then_blocks(stages):
    output, calls, _ = stages
    assert acceptance.main() == 1
    assert len(calls) == 2 and "--computer" in calls[0]
    report = read_report(output)
    assert report["status"] == "blocked" and not report["real_model_verified"]


@pytest.mark.parametrize("invalid", [{"score": 0}, {"cross_application_verified": False},
                                    {"reward_source": "model_claim"}, {"evaluation_available": False}])
def test_model_claim_does_not_override_independent_evidence(stages, monkeypatch, invalid):
    output, _, payloads = stages
    configured(monkeypatch, output)
    payloads[2].update(invalid)
    assert acceptance.main() == 1
    report = read_report(output)
    assert report["status"] == "failed" and not report["success"] and not report["real_model_verified"]


def test_fresh_cross_app_evidence_and_cli_configuration_pass(stages, monkeypatch):
    output, calls, _ = stages
    configured(monkeypatch, output)
    assert acceptance.main() == 0
    report = read_report(output)
    assert report["success"] and report["cross_application_verified"] and report["real_model_verified"]
    assert "--ground-type" in calls[2] and "pixel" in calls[2] and "--trust-env" in calls[2]
    assert not any("test-key" in value for command in calls for value in command)


def test_previous_model_report_is_not_reused(stages, monkeypatch):
    output, _, payloads = stages
    configured(monkeypatch, output)
    old = output / "computer-agent/report.json"
    old.parent.mkdir(parents=True)
    old.write_text(json.dumps(payloads[2]), encoding="utf-8")
    payloads[2] = None
    assert acceptance.main() == 1
    assert read_report(output)["status"] == "failed" and not old.exists()


@pytest.mark.parametrize("option", ["--browser-profile-dir", "--cdp-endpoint", "--download-dir"])
def test_explicit_session_override_is_rejected_before_desktop_input(stages, monkeypatch, option):
    output, calls, _ = stages
    monkeypatch.setattr(acceptance.sys, "argv", ["acceptance", "--output", str(output), option + "=personal-session"])
    with pytest.raises(SystemExit) as error:
        acceptance.main()
    assert error.value.code == 2 and not calls


@pytest.mark.parametrize("flags,verified", [([0, 1, 1, 0], True), ([0, 0, 0], False),
                                            ([1, 1, 0], False), ([0, 1, 1], False)])
def test_cross_application_order(flags, verified):
    trajectory = {"steps": [{"observation": {"info": {"scene": {"browser_use": value}}}} for value in flags]}
    assert computer_fixture.crossed_applications(trajectory) is verified


def test_evaluator_requires_pdf_bytes_saved_text_and_original_foreground(tmp_path, monkeypatch):
    fixture = computer_fixture.ComputerFixture(tmp_path, None)
    fixture.downloads = tmp_path / "downloads"
    fixture.downloads.mkdir()
    fixture.pdf = b"fixture PDF bytes"
    download = fixture.downloads / "agent-study.pdf"
    download.write_bytes(fixture.pdf)
    fixture.notes = tmp_path / "own-note.txt"
    fixture.notes.write_text(computer_fixture.EXPECTED_NOTE, encoding="utf-8")
    fixture.notepad = SimpleNamespace(window_id="10")
    foreground = SimpleNamespace(window_id="10", process_name="notepad.exe")
    env = SimpleNamespace(native=SimpleNamespace(probe=lambda: foreground, user=None))
    monkeypatch.setattr(computer_fixture, "window_title", lambda *args: "own-note - Notepad")
    assert fixture.evaluate(env) == 1
    download.write_bytes(b"partial PDF")
    assert fixture.evaluate(env) == 0
    download.write_bytes(fixture.pdf)
    foreground.window_id = "other-app"
    assert fixture.evaluate(env) == 0
    foreground.window_id = "10"
    fixture.notes.write_text("The model said done", encoding="utf-8")
    assert fixture.evaluate(env) == 0
