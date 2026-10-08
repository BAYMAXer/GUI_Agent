"""One Windows desktop task; browser structure is an optional focused capability."""
from __future__ import annotations

import time

from ..actions import Action
from ..env import Environment, Observation, StepResult
from ..scene import browser_context, scene_from_foreground
from .browser_runtime import BrowserRuntime, BrowserRuntimeConfig, validate_url
from .windows_native import WindowsNative, desktop_point, key_code


def focus_ticket(foreground):
    return (foreground.available, foreground.window_id, foreground.process_id,
            foreground.focus_id, foreground.native_ui, foreground.generation)


class WindowsEnvironment(Environment):
    os_name = "windows"
    fresh_observations = True
    capabilities = {"browser_runtime"}
    supported_actions = {"click", "double_click", "right_click", "type", "scroll", "select",
                         "drag_and_drop", "press", "hotkey", "goto", "open_browser", "wait",
                         "get_page_source", "inspect_page", "request_finish", "fail"}

    def __init__(self, *, browser_config=None, artifact_dir="artifacts/computer", evaluator=None,
                 settle_ms=180, native=None, runtime=None):
        self.native = native or WindowsNative()
        if isinstance(browser_config, dict):
            browser_config = BrowserRuntimeConfig(**browser_config)
        try:
            self.runtime = runtime or BrowserRuntime(self.native, browser_config, artifact_dir)
        except Exception:
            if native is None:
                self.native.close()
            raise
        self.evaluator, self.settle_ms = evaluator, settle_ms
        self.last_observation = None
        self._last_focus = None
        self._task = ""
        self._closed = False

    @property
    def browser_session(self):
        return self.runtime.session

    def _invalidate(self):
        if self.runtime.session:
            self.runtime.session.invalidate()

    def reset(self, task):
        # Continue the desktop as it is; never launch/navigate a browser on task reset.
        self._task = task
        self._invalidate()
        self._last_focus = None
        return self.observe()

    def observe(self):
        started = time.perf_counter()
        for attempt in range(2):
            before = self.native.probe()
            changed = self._last_focus is not None and focus_ticket(before) != self._last_focus
            if changed:
                self._invalidate()
            self._last_focus = focus_ticket(before)
            info = {"coordinate_space": "desktop_physical", "focus_version": before.generation,
                    "focus_ticket": list(focus_ticket(before)), "focus_changed": changed}
            try:
                screenshot, geometry = self.native.capture()
                info["desktop_geometry"] = geometry
                window_geometry = getattr(self.native, "window_geometry", None)
                if window_geometry and before.available:
                    info["foreground_geometry"] = window_geometry(before.window_id)
            except Exception as exc:
                self._invalidate()
                scene = scene_from_foreground(before)
                scene.reason = "desktop_capture_failed"
                info.update(scene=scene.to_dict(), screenshot_error=type(exc).__name__)
                self.last_observation = Observation(info=info)
                return self.last_observation
            captured = time.perf_counter()
            block = None
            scene = scene_from_foreground(before)
            session = self.runtime.focused_session(before)
            if session:
                try:
                    block = session.collect()
                except Exception as exc:
                    session.capture_failed()
                    info["structure_error"] = type(exc).__name__
                scene = session.scene
            elif before.is_browser and not before.native_ui:
                scene.reason = "browser_instance_not_bound" if self.runtime.process_id and (
                    before.process_id != self.runtime.process_id) else "browser_structure_unavailable"
                if self.runtime.structure_error:
                    info["structure_error"] = self.runtime.structure_error
            after = self.native.probe()
            if focus_ticket(before) != focus_ticket(after):
                self._invalidate()
                if attempt == 0:
                    continue
                block = None
                scene = scene_from_foreground(after)
                scene.reason = "focus_changed_during_observation"
                info["observation_unstable"] = True
                info["focus_ticket"] = list(focus_ticket(after))
                info["focus_version"] = after.generation
            info.update(scene=scene.to_dict(), browser_channel=self.runtime.channel,
                        timing_ms={"capture": round((captured-started)*1000, 2),
                                   "structure": round((time.perf_counter()-captured)*1000, 2),
                                   "observe": round((time.perf_counter()-started)*1000, 2)})
            self.last_observation = Observation(screenshot=screenshot, info=info, context=[block] if block else [])
            return self.last_observation

    def preflight(self, action, observation):
        def reject(reason):
            self._invalidate()
            return {"channel": "reobserve", "reason": reason}
        if observation is None or observation.screenshot is None or observation.info.get("observation_unstable"):
            return reject("Desktop observation unavailable or unstable")
        current = self.native.probe()
        if not current.available or list(focus_ticket(current)) != observation.info.get("focus_ticket"):
            return reject("Focus changed after observation; old action rejected")
        try:
            if self.native.geometry() != observation.info.get("desktop_geometry"):
                return reject("Desktop geometry changed after observation")
            window_geometry = getattr(self.native, "window_geometry", None)
            if window_geometry and window_geometry(current.window_id) != observation.info.get("foreground_geometry"):
                return reject("Foreground window moved, resized or changed DPI after observation")
        except Exception:
            return reject("Desktop geometry unavailable")
        return None

    def route_action(self, action, observation):
        session = self.browser_session
        return session.route(action, observation) if session and browser_context(observation) else None

    def execute_routed(self, action, route):
        rejected = self.preflight(action, self.last_observation)
        if rejected:
            return StepResult(self.observe(), info={"status": "rejected", "dispatched": False, **rejected})
        info = self.browser_session.execute(action, route)
        time.sleep(self.settle_ms / 1000)
        return StepResult(self.observe(), info=info)

    def step(self, action):
        rejected = self.preflight(action, self.last_observation)
        if rejected:
            return StepResult(self.observe(), info={"status": "rejected", "dispatched": False, **rejected})
        name, a = action.action, action.args
        geometry = self.last_observation.info["desktop_geometry"]
        info = {"channel": "desktop_input", "coordinate_space": "desktop_physical",
                "status": "executed", "dispatched": False}
        try:
            if a.get("grounding_failed"):
                raise ValueError("grounding_failed")
            if name in {"click", "double_click", "right_click", "type"}:
                point = desktop_point(a["x"], a["y"], geometry)
                info.update(dispatched=True, desktop_point=list(point))
                self.native.click(point, geometry, right=name == "right_click", count=2 if name == "double_click" else 1)
                if name == "type":
                    time.sleep(0.06)
                    target_focus = self.native.probe()
                    if not target_focus.available or focus_ticket(target_focus) != focus_ticket(self.native.probe()):
                        raise RuntimeError("Text input focus unavailable")
                    def guard():
                        if focus_ticket(self.native.probe()) != focus_ticket(target_focus):
                            raise RuntimeError("Focus changed during text input")
                    if a.get("overwrite"):
                        guard()
                        self.native.hotkey(["ctrl", "a"])
                        self.native.hotkey(["backspace"])
                    self.native.type_text(str(a["text"]), guard=guard)
            elif name in {"press", "hotkey"}:
                keys = a["keys"] if name == "hotkey" else [a["key"]]
                for key in keys:
                    key_code(key)
                info["dispatched"] = True
                self.native.hotkey(keys)
            elif name == "scroll":
                if a["direction"] not in {"up", "down"} or int(a["amount"]) <= 0:
                    raise ValueError("Invalid scroll")
                if "x" in a and "y" in a:
                    self.native.move(desktop_point(a["x"], a["y"], geometry), geometry)
                info["dispatched"] = True
                point = self.native.scroll(int(a["amount"]), a["direction"],
                                           window_id=self.native.probe().window_id, geometry=geometry)
                if point:
                    info["desktop_point"] = list(point)
            elif name == "drag_and_drop":
                start = desktop_point(a["x1"], a["y1"], geometry)
                end = desktop_point(a["x2"], a["y2"], geometry)
                info["dispatched"] = True
                self.native.drag(start, end, geometry)
            elif name == "open_browser":
                validate_url(a.get("url"))
                info["dispatched"] = True
                opened = self.runtime.open(a.get("url"))
                info.update(opened)
                if opened["navigation_needed"]:
                    self._navigate_visual(a["url"])
            elif name == "goto":
                validate_url(a["url"])
                foreground = self.native.probe()
                if not foreground.is_browser or foreground.native_ui:
                    raise ValueError("goto requires a foreground browser; use open_browser first")
                info["dispatched"] = True
                # Address-bar input also works when no structural adapter is available.
                self._navigate_visual(a["url"])
            elif name == "wait":
                seconds = float(a["seconds"])
                if not 0 < seconds <= 60:
                    raise ValueError("Wait must be between zero and 60 seconds")
                time.sleep(seconds)
            elif name == "select":
                raise ValueError("Visual dropdown selection requires click then option click")
            else:
                raise ValueError(f"Unsupported desktop action: {name}")
        except Exception as exc:
            info.update(status="uncertain" if info["dispatched"] else "rejected", error=str(exc)[:300])
        time.sleep(self.settle_ms / 1000)
        return StepResult(self.observe(), info=info)

    def _navigate_visual(self, url):
        foreground = self.native.probe()
        if not foreground.available or not foreground.is_browser or foreground.native_ui:
            raise RuntimeError("Browser lost focus before navigation")
        self.native.hotkey(["ctrl", "l"])
        time.sleep(0.06)
        address_focus = self.native.probe()
        def guard():
            current = self.native.probe()
            if (current.window_id != foreground.window_id or current.process_id != foreground.process_id
                    or focus_ticket(current) != focus_ticket(address_focus)):
                raise RuntimeError("Browser lost focus during navigation")
        self.native.type_text(validate_url(url), guard=guard)
        guard()
        self.native.hotkey(["enter"])

    def run_command(self, command):
        return "Shell actions are not exposed by the Windows desktop environment"

    def run_python(self, code):
        return "Python actions are not exposed by the Windows desktop environment"

    def get_page_source(self):
        return self.get_source_for_action(Action("get_page_source"))

    def get_source_for_action(self, action):
        if self.preflight(action, self.last_observation) or not browser_context(self.last_observation):
            return "Focused browser structure unavailable"
        try:
            return self.browser_session.source(action.args.get("target_ref"))
        except Exception:
            return "Focused browser source unavailable"

    def evaluate(self):
        if self.evaluator is None:
            raise NotImplementedError("No independent Windows task evaluator configured")
        return float(self.evaluator(self))

    def close(self):
        if self._closed:
            return
        try:
            self.runtime.close()
        finally:
            self.native.close()
            self._closed = True
