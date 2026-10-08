"""Check portable browser/VMware prerequisites without running agent tasks."""
from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import urlparse
from ..config.model_registry import environment_setting, resolve_model_name


def computer_browser_config():
    from ..adapters.browser_runtime import BrowserRuntimeConfig
    channel = os.getenv("COMPUTER_BROWSER_CHANNEL") or "auto"
    if channel not in {"auto", "chrome", "msedge", "chromium"}:
        raise ValueError("COMPUTER_BROWSER_CHANNEL must be auto, chrome, msedge or chromium")
    executable = os.getenv("COMPUTER_BROWSER_EXECUTABLE", "")
    if executable and not Path(executable).expanduser().is_file():
        raise FileNotFoundError("COMPUTER_BROWSER_EXECUTABLE does not name an existing file")
    endpoint = os.getenv("COMPUTER_CDP_ENDPOINT", "")
    if endpoint:
        try:
            parsed = urlparse(endpoint)
            valid = (parsed.scheme in {"http", "https", "ws", "wss"}
                     and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                     and not parsed.username and not parsed.password and not parsed.query
                     and not parsed.fragment and (parsed.port is None or 0 < parsed.port <= 65535))
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("COMPUTER_CDP_ENDPOINT requires a local HTTP/WebSocket URL without credentials or query parameters")
    return BrowserRuntimeConfig(channel=channel, executable=executable, endpoint=endpoint,
        profile_dir=os.getenv("COMPUTER_BROWSER_PROFILE_DIR", ""),
        download_dir=os.getenv("COMPUTER_DOWNLOAD_DIR", ""))


def computer_browser_capability(config):
    """Discover an optional executable without starting or attaching a browser."""
    from ..adapters.browser_runtime import discover_browser
    detail = {"configured_channel": config.channel, "executable_configured": bool(config.executable),
              "cdp_endpoint_configured": bool(config.endpoint), "launch_checked": False,
              "cdp_connectivity_checked": False}
    try:
        executable, channel = discover_browser(config)
    except RuntimeError as exc:
        if config.executable or config.channel != "auto":
            raise RuntimeError("Configured computer browser is unavailable; install it or adjust COMPUTER_BROWSER_* settings") from None
        detail.update(available=False, reason=str(exc))
    else:
        detail.update(available=True, resolved_channel=channel, executable=str(executable),
                      availability_basis="executable_file")
    return detail


def win32_api_check():
    import ctypes
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    for name in ("SetThreadDpiAwarenessContext", "GetForegroundWindow", "GetGUIThreadInfo",
                 "SetWinEventHook", "SendInput", "GetWindowRect", "GetDpiForWindow"):
        getattr(user, name)
    getattr(kernel, "QueryFullProcessImageNameW")
    return "Win32 DPI, foreground, focus monitoring and input APIs present; no input dispatched"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--desktop", action="store_true")
    parser.add_argument("--computer", action="store_true", help="Check real Windows capture/focus without injecting input")
    parser.add_argument("--check-api", action="store_true", help="GET /models only; no inference request")
    parser.add_argument("--output", default="artifacts/doctor/report.json")
    args = parser.parse_args()
    errors = []
    checks = []

    def check(label, operation):
        try:
            detail = operation()
            print(f"[OK] {label}" + (f": {detail}" if detail else ""))
            checks.append({"name": label, "success": True, "detail": detail})
            return detail
        except Exception as exc:
            errors.append(label)
            # Do not print arbitrary HTTP bodies or credential-bearing URLs.
            print(f"[FAIL] {label}: {exc}")
            checks.append({"name": label, "success": False, "detail": str(exc)})
            return None

    print(f"Python {sys.version.split()[0]}: {sys.executable}")
    def runtime_check():
        if sys.platform != "win32" or sys.version_info[:2] != (3, 12) or sys.maxsize <= 2**32:
            raise RuntimeError("This deployment expects Windows x64 with Python 3.12")
        return "Windows x64 / Python 3.12"
    check("Windows runtime", runtime_check)

    def resources_check():
        root = Path(__file__).resolve().parents[1]
        resources = ["config/action.yaml", "config/skills.yaml", "config/environment_aliases.json",
                     "viz/static/index.html", "tests/fixtures/browser_task.html"]
        if args.computer:
            resources.append("tests/fixtures/computer_papers.html")
        for relative in resources:
            if not (root / relative).is_file():
                raise RuntimeError(f"Missing packaged resource: {relative}")
        return "action/skill registries, environment aliases, dashboard and fixture present"
    check("portable package resources", resources_check)
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
    if not args.computer or args.desktop:
        check("browser launch", browser_check)

    if args.computer:
        for module in ("mss", "psutil", "osworld_agent.adapters.windows_native"):
            check(module, lambda module=module: importlib.import_module(module) and "imported")
        check("Win32 desktop APIs", win32_api_check)
        configured_browsers = []
        def configuration_check():
            config = computer_browser_config()
            configured_browsers.append(config)
            return {"channel": config.channel, "executable_configured": bool(config.executable),
                    "cdp_endpoint_configured": bool(config.endpoint),
                    "profile_configured": bool(config.profile_dir), "download_directory_configured": bool(config.download_dir)}
        check("computer browser configuration", configuration_check)
        config = configured_browsers[0] if configured_browsers else None
        if config is not None:
            detail = check("optional computer browser capability", lambda: computer_browser_capability(config))
            if detail is not None:
                checks[-1].update(optional=True, available=detail["available"])
                if not detail["available"]:
                    print("[INFO] optional computer browser is unavailable; pure visual desktop tasks remain supported")

        def computer_check():
            from ..adapters.windows_env import WindowsEnvironment
            environment = WindowsEnvironment(browser_config=config)
            try:
                # Only capture/probe: an optional configured CDP endpoint is not connected here.
                foreground = environment.native.probe()
                screenshot, geometry = environment.native.capture()
                if screenshot is None or not foreground.available:
                    raise RuntimeError("Interactive desktop capture/focus is unavailable")
                return {"screenshot_size": list(screenshot.size), "desktop_geometry": geometry,
                        "foreground_available": True, "browser_started": False,
                        "structure_connectivity_checked": False}
            finally:
                environment.close()
        if config is not None:
            check("interactive Windows desktop", computer_check)

    if not args.desktop and not args.check_api:
        configured = all(environment_setting(name) for name in ("PLAN_MODEL", "PLAN_API_URL", "PLAN_API_KEY"))
        print("[INFO] model settings: " + ("present (connectivity not checked)" if configured else "fill PLAN_MODEL/PLAN_API_URL/PLAN_API_KEY in .env for real agent tasks"))

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
            base = environment_setting("PLAN_API_URL").rstrip("/")
            key = environment_setting("PLAN_API_KEY")
            model = resolve_model_name(environment_setting("PLAN_MODEL"))
            parsed = urlparse(base)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
                raise RuntimeError("Set a valid PLAN_API_URL (without credentials in the URL)")
            if not key or not model:
                raise RuntimeError("Set PLAN_API_KEY and PLAN_MODEL")
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
                raise RuntimeError("PLAN_MODEL was not found in GET /models")
            return "configured model listed; inference and vision support not checked"
        check("model API", api_check)

    report = {"success": not errors, "python": sys.version.split()[0], "executable": sys.executable,
              "platform": sys.platform, "desktop_checked": args.desktop, "computer_checked": args.computer,
              "api_checked": args.check_api, "checks": checks}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if errors:
        print(f"{len(errors)} check(s) failed. See README.md for setup steps.")
        return 1
    print("Prerequisite checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
