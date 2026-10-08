"""Local Chromium environment, including Windows, without OSWorld.

Screenshots and visual fallback coordinates are CSS viewport pixels here.
OSWorld adapters retain desktop screenshots and desktop visual coordinates.
"""
from __future__ import annotations

from io import BytesIO
import os
import platform
from PIL import Image

from ..actions import Action
from ..browser import BrowserSession, BrowserOptions
from ..env import Environment, Observation, StepResult


def launch_browser(playwright, channel, headless):
    """Try packaged Chromium, Edge, then Chrome when auto selection is requested."""
    candidates = ("chromium", "msedge", "chrome") if channel == "auto" else (channel,)
    last_error = None
    for candidate in candidates:
        try:
            browser = playwright.chromium.launch(channel=candidate, headless=headless)
        except Exception as exc:
            last_error = exc
            continue
        if candidate != candidates[0]:
            print(f"[browser] {candidates[0]} could not start; using {candidate}")
        return browser, candidate
    raise RuntimeError("No browser could start. Rerun setup.cmd, or install Edge/Chrome and select its channel.") from last_error


class BrowserEnvironment(Environment):
    supported_actions = {"click", "double_click", "right_click", "type", "scroll", "select",
                         "press", "hotkey", "goto", "wait", "get_page_source", "request_finish", "fail"}

    def __init__(self, start_url="about:blank", *, headless=True, channel=None,
                 width=1280, height=800, endpoint=None, evaluator=None, settle_ms=120):
        self.start_url, self.evaluator, self.settle_ms = start_url, evaluator, settle_ms
        self.os_name = platform.system().lower()
        self._pw = self._browser = None
        self.browser_channel = "cdp" if endpoint else None
        if endpoint:
            self.session = BrowserSession(options=BrowserOptions(endpoint=endpoint))
        else:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            try:
                self._browser, self.browser_channel = launch_browser(
                    self._pw, channel or os.environ.get("OSWORLD_BROWSER_CHANNEL", "chrome"), headless)
                context = self._browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=1)
                page = context.new_page()
                page.set_default_timeout(4000)
                page.set_default_navigation_timeout(15000)
                self.session = BrowserSession(page)
            except Exception:
                self._pw.stop()
                raise

    @property
    def page(self):
        self.session._attach()
        return self.session.page

    def reset(self, task):
        # Each rollout starts with a new browser context, not the previous task's cookies/storage.
        if self._browser is not None:
            old = self.session.page.context
            viewport = self.session.page.viewport_size
            self.session.close()
            old.close()
            context = self._browser.new_context(viewport=viewport, device_scale_factor=1)
            page = context.new_page()
            page.set_default_timeout(4000)
            page.set_default_navigation_timeout(15000)
            self.session = BrowserSession(page)
            page.goto(self.start_url, wait_until="domcontentloaded")
        # For an attached browser, continue the actual focused page; never navigate on reset.
        return self.observe()

    def observe(self):
        info = {"coordinate_space": "css_viewport"}
        try:
            block = self.session.collect()
        except Exception as exc:
            self.session.capture_failed()
            block = None
            info["structure_error"] = str(exc)[:500]
        info["scene"] = self.session.scene.to_dict()
        screenshot = None
        if self.session.page is not None:
            try:
                screenshot = Image.open(BytesIO(self.session.page.screenshot(scale="css"))).convert("RGB")
            except Exception as exc:
                info["screenshot_error"] = str(exc)[:300]
        return Observation(screenshot=screenshot, info=info, context=[block] if block else [])

    def route_action(self, action, observation):
        return self.session.route(action, observation)

    def execute_routed(self, action, route):
        info = self.session.execute(action, route)
        self.page.wait_for_timeout(self.settle_ms)
        return StepResult(self.observe(), info=info)

    def step(self, action):
        page, a, name = self.page, action.args, action.action
        info = {"channel": "browser_input", "status": "executed", "dispatched": False}
        try:
            if a.get("grounding_failed"):
                raise ValueError("grounding_failed")
            if name in {"click", "double_click", "right_click", "type", "select"}:
                if name == "select":
                    raise ValueError("Visual select requires click then option click")
                x, y = float(a["x"]), float(a["y"])
                size = page.evaluate("({width:innerWidth,height:innerHeight})")
                if not (0 <= x < size["width"] and 0 <= y < size["height"]):
                    raise ValueError("Visual coordinate outside viewport")
                info["dispatched"] = True
                page.mouse.click(x, y, button="right" if name == "right_click" else "left",
                                 click_count=2 if name == "double_click" else 1)
                if name == "type":
                    if a.get("overwrite"):
                        page.keyboard.press("ControlOrMeta+A")
                        page.keyboard.press("Backspace")
                    page.keyboard.insert_text(str(a["text"]))
            elif name == "goto":
                from urllib.parse import urlparse
                if urlparse(a["url"]).scheme not in ("http", "https", "file"):
                    raise ValueError("Unsupported URL scheme")
                info["dispatched"] = True
                page.goto(a["url"], wait_until="domcontentloaded")
            elif name in {"press", "hotkey"}:
                keys = a["keys"] if name == "hotkey" else [a["key"]]
                aliases = {"ctrl": "Control", "control": "Control", "alt": "Alt", "shift": "Shift",
                           "enter": "Enter", "return": "Enter", "esc": "Escape", "escape": "Escape",
                           "tab": "Tab", "backspace": "Backspace", "delete": "Delete",
                           "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft", "right": "ArrowRight"}
                info["dispatched"] = True
                page.keyboard.press("+".join(aliases.get(str(k).lower(), str(k)) for k in keys))
            elif name == "scroll":
                info["dispatched"] = True
                page.mouse.wheel(0, int(a["amount"]) * 120 * (-1 if a["direction"] == "up" else 1))
            elif name == "wait":
                page.wait_for_timeout(float(a["seconds"]) * 1000)
            else:
                raise ValueError(f"Unsupported action: {name}")
        except Exception as exc:
            info.update(status="uncertain" if info["dispatched"] else "rejected", error=str(exc)[:500])
        page.wait_for_timeout(self.settle_ms)
        return StepResult(self.observe(), info=info)

    def run_command(self, command):
        return "Browser environment does not support shell actions"

    def run_python(self, code):
        return "Browser environment does not support Python actions"

    def get_page_source(self):
        return self.session.source()

    def get_source_for_action(self, action):
        try:
            return self.session.source(action.args.get("target_ref"))
        except Exception as exc:
            return f"DOM unavailable: {str(exc)[:300]}"

    def evaluate(self):
        if self.evaluator is None:
            raise NotImplementedError("No independent task evaluator configured")
        return float(self.evaluator(self.page))

    def close(self):
        self.session.close()
        if self._browser is not None:
            self._browser.close()
        if self._pw is not None:
            self._pw.stop()
