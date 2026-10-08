"""Check portable browser/VMware prerequisites without running agent tasks."""
from __future__ import annotations

import argparse
import importlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import urlparse


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--desktop", action="store_true")
    parser.add_argument("--check-api", action="store_true", help="GET /models only; no inference request")
    args = parser.parse_args()
    errors = []

    def check(label, operation):
        try:
            detail = operation()
            print(f"[OK] {label}" + (f": {detail}" if detail else ""))
            return detail
        except Exception as exc:
            errors.append(label)
            # Do not print arbitrary HTTP bodies or credential-bearing URLs.
            print(f"[FAIL] {label}: {exc}")
            return None

    print(f"Python {sys.version.split()[0]}: {sys.executable}")
    for module in ("osworld_agent.agent", "playwright.sync_api", "openai", "yaml", "PIL", "fastapi", "uvicorn"):
        check(module, lambda module=module: importlib.import_module(module) and "imported")

    def browser_check():
        from playwright.sync_api import sync_playwright
        from ..adapters.browser_env import launch_browser
        with sync_playwright() as pw:
            browser, channel = launch_browser(pw, os.getenv("OSWORLD_BROWSER_CHANNEL", "auto"), True)
            try:
                page = browser.new_page()
                page.set_content("<title>agent-doctor</title><button>Ready</button>")
                if page.title() != "agent-doctor":
                    raise RuntimeError("Browser page check failed")
                return f"{channel} {browser.version}"
            finally:
                browser.close()
    check("browser launch", browser_check)

    if not args.desktop and not args.check_api:
        configured = all(os.getenv(name) for name in ("QWEN_MODEL", "QWEN_API_URL", "QWEN_API_KEY"))
        print("[INFO] model settings: " + ("present (connectivity not checked)" if configured else "fill QWEN_MODEL/QWEN_API_URL/QWEN_API_KEY in .env for real agent tasks"))

    if args.desktop:
        def desktop_check():
            value = os.getenv("OSWORLD_DESKTOP_ENV_PATH", "")
            path = Path(value)
            if not value or not (path / "desktop_env").is_dir():
                raise RuntimeError("Set OSWORLD_DESKTOP_ENV_PATH to a checkout containing desktop_env/")
            sys.path.insert(0, str(path.resolve()))
            from desktop_env.desktop_env import DesktopEnv
            return str(path.resolve()) if DesktopEnv else None
        check("OSWorld DesktopEnv and dependencies", desktop_check)

        def examples_check():
            value = os.getenv("OSWORLD_EXAMPLES_DIR", "")
            path = Path(value)
            if not value or not path.is_dir() or next(path.rglob("*.json"), None) is None:
                raise RuntimeError("Set OSWORLD_EXAMPLES_DIR to evaluation_examples/examples containing task JSON files")
            return str(path.resolve())
        check("task examples", examples_check)

        def vmrun_check():
            found = shutil.which("vmrun")
            if not found:
                for name in ("ProgramFiles(x86)", "ProgramFiles"):
                    candidate = Path(os.getenv(name, "C:/Program Files")) / "VMware/VMware Workstation/vmrun.exe"
                    if candidate.is_file():
                        found = str(candidate)
                        # Needed by OSWorld when it launches vmrun by name.
                        os.environ["PATH"] = str(candidate.parent) + os.pathsep + os.environ.get("PATH", "")
                        break
            if not found:
                raise RuntimeError("Install VMware Workstation and add its folder to PATH")
            result = subprocess.run([found, "-T", "ws", "list"], capture_output=True, text=True, timeout=20)
            if result.returncode:
                raise RuntimeError("vmrun list failed; verify the VMware installation")
            return found
        vmrun = check("VMware vmrun", vmrun_check)

        def vm_check():
            value = os.getenv("OSWORLD_VM_PATH", "")
            path = Path(value)
            if not value or path.suffix.lower() != ".vmx" or not path.is_file():
                raise RuntimeError("Set OSWORLD_VM_PATH to the copied .vmx file; copy the entire VM folder including snapshots")
            if not vmrun:
                raise RuntimeError("vmrun is unavailable; cannot inspect snapshots")
            result = subprocess.run([vmrun, "-T", "ws", "listSnapshots", str(path)], capture_output=True,
                                    text=True, timeout=30)
            snapshot = os.getenv("OSWORLD_SNAPSHOT_NAME", "init_state")
            if result.returncode or snapshot not in [line.strip() for line in result.stdout.splitlines()]:
                raise RuntimeError(f"The VM must include the configured snapshot '{snapshot}' (vmrun listSnapshots)")
            return str(path.resolve()) + f" ({snapshot} present; VM not started)"
        check("VM and configured snapshot", vm_check)

        def preset_check():
            import yaml
            default = Path(__file__).resolve().parents[1] / "config/model_presets.yaml"
            path = Path(os.getenv("OSWORLD_MODEL_PRESETS") or default)
            presets = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for group in ("decision_models", "grounding_models"):
                rows = presets.get(group) or []
                if not rows or any(not r.get("name") or not r.get("url") for r in rows):
                    raise RuntimeError("Model presets must have a name and URL for decision and grounding models")
            return str(path)
        check("model presets", preset_check)

    if args.check_api:
        def api_check():
            import httpx
            base = os.getenv("QWEN_API_URL", "").rstrip("/")
            key = os.getenv("QWEN_API_KEY", "")
            model = os.getenv("QWEN_MODEL", "")
            parsed = urlparse(base)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
                raise RuntimeError("Set a valid QWEN_API_URL (without credentials in the URL)")
            if not key or not model:
                raise RuntimeError("Set QWEN_API_KEY and QWEN_MODEL")
            try:
                with httpx.Client(trust_env=False, timeout=15) as client:
                    response = client.get(base + "/models", headers={"Authorization": f"Bearer {key}"})
            except httpx.HTTPError:
                raise RuntimeError("Model endpoint is unreachable; check network/VPN and URL") from None
            if response.status_code != 200:
                raise RuntimeError(f"GET /models returned HTTP {response.status_code}; some providers do not implement this endpoint")
            try:
                names = {row.get("id") for row in response.json().get("data", [])}
            except (ValueError, AttributeError, TypeError):
                raise RuntimeError("GET /models returned an invalid model list") from None
            if model not in names:
                raise RuntimeError("QWEN_MODEL was not found in GET /models")
            return "configured model listed; inference and vision support not checked"
        check("model API", api_check)

    if errors:
        print(f"{len(errors)} check(s) failed. See README.md for setup steps.")
        return 1
    print("Prerequisite checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
