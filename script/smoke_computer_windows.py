"""Opt-in real desktop smoke with a scripted policy; never a model benchmark."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes as wt
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import platform
import subprocess
import threading
import time
import uuid

from ..actions import Action
from ..adapters.browser_runtime import BrowserRuntime, BrowserRuntimeConfig
from ..adapters.windows_env import WindowsEnvironment
from ..agent import Agent
from ..config import ModelConfig
from ..metrics import summarize_trajectory
from ..model.decision_model import ChatModel
from ..model.grounding import GroundingResult, NullGrounding
from ..trajectory import build_trajectory, dump_trajectory


def test_pdf():
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    content = b"BT /F1 14 Tf 30 150 Td (GUI Agent Browser Study) Tj ET"
    objects.append(b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream")
    data, offsets = b"%PDF-1.4\n", [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(data)
    data += f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode()
    data += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    return data + f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def window_title(user, hwnd):
    user.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    title = ctypes.create_unicode_buffer(1024)
    user.GetWindowTextW(int(hwnd), title, len(title))
    return title.value


def open_notepad(env, path):
    process = subprocess.Popen(["notepad.exe", str(path)])
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        foreground = env.native.probe()
        if (foreground.process_name == "notepad.exe" and
                path.stem.lower() in window_title(env.native.user, foreground.window_id).lower()):
            env.native.place_window(foreground.window_id)
            env.native.activate(foreground.window_id, allow_input_fallback=False)
            foreground = env.native.probe()
            if not env.adopt_window(foreground):
                raise RuntimeError("Fixture Notepad could not be placed and activated on the work screen")
            return process, foreground
        time.sleep(0.1)
    raise RuntimeError("Could not bind the fixture's own Notepad window")


def close_notepad(env, notepad, path):
    """Close the owned fixture window without keyboard input or focus recovery."""
    if notepad is None or getattr(env, "safety_stop", None):
        return
    user = env.native.user
    pid = wt.DWORD()
    user.GetWindowThreadProcessId(int(notepad.window_id), ctypes.byref(pid))
    if (pid.value != notepad.process_id or
            path.stem.lower() not in window_title(user, notepad.window_id).lower()):
        return
    user.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user.PostMessageW(int(notepad.window_id), 0x0010, 0, 0)  # WM_CLOSE, no input injected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", choices=["auto", "chrome", "msedge", "chromium"], default=os.getenv("COMPUTER_BROWSER_CHANNEL", "auto"))
    parser.add_argument("--browser-executable", default=os.getenv("COMPUTER_BROWSER_EXECUTABLE", ""))
    parser.add_argument("--monitor", default=os.getenv("COMPUTER_MONITOR") or "primary")
    parser.add_argument("--output", default="artifacts/computer-smoke")
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    fixture_dir = output / ("fixture-" + uuid.uuid4().hex[:8])
    fixture_dir.mkdir()
    source = Path(__file__).resolve().parents[1] / "tests/fixtures/computer_papers.html"
    (fixture_dir / "index.html").write_bytes(source.read_bytes())
    pdf = test_pdf()
    (fixture_dir / "paper.pdf").write_bytes(pdf)
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(fixture_dir)))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    notes = fixture_dir / ("agent-task-" + uuid.uuid4().hex[:8] + ".txt")
    instructions = "任务：检索 GUI Agent Browser Study，下载论文 PDF，再回到记事本记录结果。\r\n测试网页：" + url
    notes.write_text(instructions, encoding="utf-8")
    expected = "已下载 GUI Agent Browser Study 论文 PDF。框架中文输入与回写验证完成。"
    checks = []
    downloads = fixture_dir / "downloads"
    env = WindowsEnvironment(browser_config=BrowserRuntimeConfig(channel=args.channel,
        executable=args.browser_executable, download_dir=str(downloads)), artifact_dir=output, monitor=args.monitor)
    notepad = None
    report = {"test_kind": "scripted_policy_real_windows_desktop", "model_verified": False,
              "platform": platform.platform(), "success": False, "checks": checks}
    started = time.perf_counter()

    def check(name, valid, detail=None):
        checks.append({"name": name, "passed": bool(valid), "detail": detail})
        if not valid:
            raise RuntimeError(name)

    def action(action):
        env.observe()
        result = env.step(action)
        check("input:" + action.action, result.info["status"] == "executed", result.info)
        return result.observation

    def focus_page(window):
        rect = wt.RECT()
        env.native.user.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
        env.native.user.GetWindowRect(int(window), ctypes.byref(rect))
        geometry = env.observe().info["capture_geometry"]
        # A blank content region of this known fixture, observed in the desktop screenshot.
        return action(Action("click", {"x": rect.left + 250 - geometry["left"],
                                      "y": rect.top + 350 - geometry["top"]}))

    def activate_fixture(window):
        env.native.activate(window, allow_input_fallback=False)
        if not env.adopt_window():
            raise RuntimeError("Fixture window activation failed on the work screen")
        time.sleep(0.2)

    try:
        _, notepad = open_notepad(env, notes)
        observed = env.reset(instructions)
        check("ordinary_notepad_visual", not observed.browser_use and not observed.context)
        observed.screenshot.save(output / "notepad-before.png")
        action(Action("open_browser", {"url": url}))
        observed = env.observe()
        browser_window = env.native.probe().window_id
        # A freshly opened about:blank window may retain omnibox focus after CDP
        # navigation. Give this owned fixture real content focus before testing AX.
        if not observed.browser_use:
            focus_page(browser_window)
            observed = env.observe()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not observed.structure_available:
            time.sleep(0.1)
            observed = env.observe()
        check("focused_page_has_structure", observed.browser_use and observed.structure_available, observed.info["scene"])
        old_snapshot = observed.info["scene"]["snapshot_id"]
        action(Action("hotkey", {"keys": ["ctrl", "l"]}))
        address = env.observe()
        check("address_bar_visual", not address.browser_use and address.context == [], address.info["scene"])
        action(Action("press", {"key": "esc"}))
        if not env.observe().browser_use:
            focus_page(browser_window)
        page_observed = env.observe()
        check("return_from_address_bar", page_observed.browser_use)
        action(Action("hotkey", {"keys": ["ctrl", "o"]}))
        dialog = env.observe()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not dialog.info["scene"]["foreground"].get("native_ui"):
            time.sleep(0.1)
            dialog = env.observe()
        check("native_file_dialog_visual", not dialog.browser_use and not dialog.context, dialog.info["scene"])
        action(Action("press", {"key": "esc"}))
        if not env.observe().browser_use:
            focus_page(browser_window)
        activate_fixture(notepad.window_id)
        desktop = env.observe()
        check("background_browser_not_injected", not desktop.browser_use and desktop.context == [])
        activate_fixture(browser_window)
        resumed = env.observe()
        check("new_snapshot_after_switch", resumed.browser_use and resumed.info["scene"]["snapshot_id"] != old_snapshot)
        # The old observation must fail even after returning to the same foreground HWND.
        check("away_and_back_preflight", env.preflight(Action("press", {"key": "enter"}), observed) is not None)
        resumed = env.observe()
        old_ref = next(n["ref"] for n in resumed.context[0]["nodes"] if n.get("role") == "button")
        activate_fixture(notepad.window_id)
        env.observe()
        activate_fixture(browser_window)
        env.observe()
        stale = env.browser_session.route(Action("click", {"target": "检索论文", "target_ref": old_ref}), resumed)
        check("stale_reference_rejected", stale and stale["channel"] == "reobserve")
        secondary = BrowserRuntime(env.native, BrowserRuntimeConfig(channel=args.channel,
            executable=args.browser_executable), output / "other-instance")
        try:
            secondary.open(url)
            if not env.adopt_window():
                raise RuntimeError("Secondary fixture browser is outside the work screen")
            time.sleep(0.2)
            other = env.observe()
            check("other_browser_instance_visual", not other.browser_use and other.context == [])
        finally:
            secondary.close()
        activate_fixture(browser_window)
        env.observe()
        session = env.browser_session
        session._cache_time = 0
        original_send = session._cdp.send
        def broken_ax(method, *parameters, **keywords):
            if method == "Accessibility.getFullAXTree":
                raise RuntimeError("Injected AX failure")
            return original_send(method, *parameters, **keywords)
        session._cdp.send = broken_ax
        try:
            failure = env.observe()
            check("ax_failure_visual_fallback", failure.browser_use and not failure.structure_available and failure.context == [])
            action(Action("press", {"key": "tab"}))
        finally:
            session._cdp.send = original_send
        before_interference = env.observe()
        env.native.activate(notepad.window_id, allow_input_fallback=False)
        time.sleep(0.1)
        rejected = env.preflight(Action("press", {"key": "enter"}), before_interference)
        recovered = env.observe()
        check("interference_restored_once", rejected and rejected["channel"] == "reobserve"
              and not env.safety_stop and recovered.info.get("focus_recovery", {}).get("status") == "restored"
              and env.native.probe().window_id == browser_window, recovered.info.get("focus_recovery"))
        env.native.activate(notepad.window_id, allow_input_fallback=False)
        time.sleep(0.1)
        env.native.activate(browser_window, allow_input_fallback=False)
        time.sleep(0.1)
        stopped = env.observe()
        check("repeated_away_and_back_stops", stopped.info.get("safety_stop", {}).get("code") ==
              "focus_interference_repeated" and stopped.screenshot is None, env.safety_stop)
        env.reset(instructions)  # Start a new fixture task after the safety-stop check.
        activate_fixture(notepad.window_id)

        class ScriptedGrounding(NullGrounding):
            calls = 0
            def locate(self, screenshot, target):
                self.calls += 1
                # Test fixture coordinate from the observed own Notepad window rectangle.
                rect = wt.RECT()
                env.native.user.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
                env.native.user.GetWindowRect(int(notepad.window_id), ctypes.byref(rect))
                geometry = env.last_observation.info["capture_geometry"]
                return GroundingResult(rect.left + 150 - geometry["left"], rect.top + 200 - geometry["top"])

        class ScriptedPolicy(ChatModel):
            def __init__(self):
                super().__init__(ModelConfig(name="scripted_test_double"))
                self.index = 0
            def chat(self, messages, **kwargs):
                self.last_call = {"actor_role": "decision", "provenance": "scripted_test_double",
                                  "model": "scripted_test_double", "policy_revision": None}
                content = messages[-1]["content"]
                text = content if isinstance(content, str) else "\n".join(p.get("text", "") for p in content)
                if "任务完成度核对器" in text:
                    return "CONFIRM" if evaluator(env) else "REJECT fixture files not ready"
                actions = [
                    {"type": "open_browser", "url": url},
                    {"type": "type", "target": "论文关键词", "text": "GUI Agent Browser Study", "target_hint": {"name": "论文关键词", "role": "textbox"}},
                    {"type": "click", "target": "检索论文", "target_hint": {"name": "检索论文", "role": "button"}},
                    {"type": "click", "target": "下载论文 PDF", "target_hint": {"name": "下载论文 PDF", "role": "link"}},
                    {"type": "hotkey", "keys": ["alt", "tab"]},
                    {"type": "type", "target": "Notepad editable document", "text": expected, "overwrite": True},
                    {"type": "hotkey", "keys": ["ctrl", "s"]},
                    {"type": "request_finish", "answer": "Fixture download and Notepad writeback complete"}]
                current = actions[min(self.index, len(actions)-1)]
                self.index += 1
                return json.dumps({"next_action": current,
                    "state_update": {"known_fact_add": {"fixture_task": "retained across apps"}}}, ensure_ascii=False)

        def evaluator(environment):
            file = downloads / "agent-study.pdf"
            return float(file.is_file() and file.read_bytes() == pdf and notes.read_text(encoding="utf-8-sig").strip() == expected)
        env.evaluator = evaluator
        grounding = ScriptedGrounding()
        result = Agent(ScriptedPolicy(), grounding, env, max_steps=10).run(instructions)
        trajectory = build_trajectory("computer-fixture-scripted", "computer", instructions, result)
        trajectory["provenance"] = {"policy": "scripted_test_double", "use_for_model_training": False,
                                    "perception": "screenshots_only_on_desktop"}
        dump_trajectory(str(output / "trajectory.json"), trajectory)
        flags = [s["observation"]["info"]["scene"]["browser_use"] for s in result.trajectory if "observation" in s]
        check("same_task_cross_application", flags[:6] == [0, 1, 1, 1, 1, 0], flags)
        check("download_and_notepad_content", result.score == 1)
        check("unique_dom_skips_grounding", grounding.calls == 1, grounding.calls)
        check("unified_trajectory_v3", trajectory["version"] == "3.0" and all(s["policy_input"] for s in trajectory["steps"]))
        check("memory_retained", "retained across apps" in str(trajectory["steps"][-1]["policy_input"]))
        env.observe().screenshot.save(output / "notepad-after.png")
        report.update(success=True, score=result.score, scene_flags=flags, grounding_calls=grounding.calls,
                      browser_channel=env.runtime.channel, browser_version=env.runtime._browser.version,
                      downloads=env.runtime.downloads, metrics=summarize_trajectory(trajectory))
    except Exception as exc:
        report["error"] = str(exc)
        report["safety_stop"] = env.safety_stop
        report["last_scene"] = env.last_observation.info.get("scene") if env.last_observation else None
        if env.last_observation and env.last_observation.screenshot:
            env.last_observation.screenshot.save(output / "failure.png")
    finally:
        # Close only the verified fixture document; never close other Notepad windows.
        try:
            close_notepad(env, notepad, notes)
        except Exception:
            pass
        env.close()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
        report["elapsed_s"] = round(time.perf_counter()-started, 2)
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
