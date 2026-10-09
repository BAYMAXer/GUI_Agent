"""One Windows desktop task; browser structure is an optional focused capability."""
from __future__ import annotations

import copy
import os
import time

from ..actions import Action
from ..env import Environment, Observation, StepResult
from ..scene import browser_context, scene_from_foreground
from .browser_runtime import BrowserRuntime, BrowserRuntimeConfig, validate_url
from .windows_native import WindowsNative, desktop_point, key_code


def focus_ticket(foreground):
    return (foreground.available, foreground.window_id, foreground.process_id,
            foreground.focus_id, foreground.native_ui, foreground.generation, foreground.foreground_generation)


class WindowsEnvironment(Environment):
    os_name = "windows"
    fresh_observations = True
    capabilities = {"browser_runtime"}
    supported_actions = {"click", "double_click", "right_click", "type", "scroll", "select",
                         "drag_and_drop", "press", "hotkey", "goto", "open_browser", "wait",
                         "get_page_source", "inspect_page", "request_finish", "fail"}

    def __init__(self, *, browser_config=None, artifact_dir="artifacts/computer", evaluator=None,
                 settle_ms=180, native=None, runtime=None, monitor=None):
        self.native = native or WindowsNative(monitor=monitor or os.getenv("COMPUTER_MONITOR", "primary"))
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
        self._layout = None
        self._expected_focus = None
        self._dialog_parents = {}
        self._task_history = []
        self._expected_switch = None
        self._pending_click = None
        self._pending_point = None
        self._click_launch = False
        self._click_acknowledged = False
        self._opening_browser = False
        self._dispatch_focus = None
        self._dispatch_geometry = None
        self._drag_focus = None
        self._recovery_used = False
        self._recovery_event = None
        self._safety_stop = None
        self.native.input_guard = self._input_guard
        self.runtime.before_navigate = self._before_browser_navigation

    @property
    def browser_session(self):
        return self.runtime.session

    @property
    def safety_stop(self):
        return copy.deepcopy(self._safety_stop)

    def _invalidate(self):
        if self.runtime.session:
            self.runtime.session.invalidate()

    def _stop(self, code, reason):
        if self._safety_stop is None:
            self._safety_stop = {"code": code, "reason": reason}
        self._invalidate()

    def _geometry(self):
        try:
            geometry = self.native.geometry()
        except Exception as exc:
            self._stop("monitor_unavailable", f"Work-screen geometry unavailable: {exc}. Restore the display and restart.")
            return None
        if self._layout is None:
            self._layout = copy.deepcopy(geometry)
        elif geometry != self._layout:
            self._stop("display_layout_changed", "Desktop geometry changed during the task. Check the work screen and restart.")
            return None
        return geometry

    def _window_allowed(self, window_id, geometry):
        window_class = getattr(self.native, "window_class", None)
        if window_class and window_class(window_id) in {"Progman", "WorkerW"}:
            return True  # The shell desktop spans displays; its input points remain restricted.
        bounds = getattr(self.native, "window_bounds", None)
        if bounds is None:  # Injected legacy test backends have no window introspection.
            return True
        rectangle = bounds(window_id)
        capture = geometry.get("capture_geometry", geometry)
        return (rectangle["right"] > rectangle["left"] and rectangle["bottom"] > rectangle["top"]
                and capture["left"] <= rectangle["left"] and capture["top"] <= rectangle["top"]
                and rectangle["right"] <= capture["left"] + capture["width"]
                and rectangle["bottom"] <= capture["top"] + capture["height"])

    def _identity_valid(self, foreground):
        process = getattr(self.native, "window_process", None)
        try:
            return foreground.available and (process is None or process(foreground.window_id) == foreground.process_id)
        except (RuntimeError, ValueError, OSError):
            return False

    def _shell_click_dispatched(self):
        return self._click_launch and self._click_acknowledged

    def _authorized(self, foreground):
        expected = self._expected_focus
        if expected is None:
            return True
        if (foreground.window_id, foreground.process_id) == (expected.window_id, expected.process_id):
            return True
        ancestor = (expected.window_id, expected.process_id)
        seen = set()
        related = getattr(self.native, "related_window", None)
        process = getattr(self.native, "window_process", None)
        while ancestor in self._dialog_parents and ancestor not in seen:
            seen.add(ancestor)
            ancestor = self._dialog_parents[ancestor]
            if (foreground.window_id, foreground.process_id) == ancestor:
                return True
            if related and process and process(ancestor[0]) == ancestor[1] and related(foreground.window_id, ancestor[0]):
                return True
        if self._pending_click == (foreground.window_id, foreground.process_id):
            return True
        if self._shell_click_dispatched():
            return True  # Explicit click on the work-screen taskbar/desktop may open an app.
        if self._expected_switch and time.monotonic() <= self._expected_switch["deadline"]:
            if (self._expected_switch["shell"] or
                    (foreground.window_id, foreground.process_id) == self._expected_switch["target"]):
                return True
        if self._opening_browser and self.runtime.process_id and foreground.process_id == self.runtime.process_id:
            return True
        related = getattr(self.native, "related_window", None)
        return bool(related and self._identity_valid(expected) and
                    (related(foreground.window_id, expected.window_id)
                     or related(expected.window_id, foreground.window_id)))

    def adopt_window(self, foreground=None):
        """Confirm an explicit fixture/app switch; never clears a latched safety stop."""
        if self._safety_stop:
            return False
        geometry = self._geometry()
        foreground = foreground or self.native.probe()
        try:
            if (geometry is None or focus_ticket(self.native.probe()) != focus_ticket(foreground)
                    or not self._identity_valid(foreground) or not self._window_allowed(foreground.window_id, geometry)):
                self._stop("window_outside_work_screen", "Move the task window fully into the work screen, then restart.")
                return False
        except Exception as exc:
            self._stop("task_window_unavailable", f"Task window unavailable: {exc}. Restore it and restart.")
            return False
        self._remember_focus(foreground)
        self._expected_switch = None
        self._invalidate()
        return True

    def _check_focus(self, foreground, geometry, *, recover, allow_foreground_transition=False):
        """Returns True only when focus was recovered; interference is never adopted."""
        if self._safety_stop:
            return False
        try:
            if not self._identity_valid(foreground):
                self._stop("foreground_unavailable", "Foreground window unavailable. Restore the interactive desktop and restart.")
                return False
            previous = self._expected_focus
            if (self._recovery_used and previous and not allow_foreground_transition and
                    (foreground.window_id, foreground.process_id) == (previous.window_id, previous.process_id) and
                    foreground.foreground_generation != previous.foreground_generation):
                self._stop("focus_interference_repeated", "Task focus switched away and back again. Restore the task window and restart.")
                return False
            if self._authorized(foreground):
                if not self._window_allowed(foreground.window_id, geometry):
                    self._stop("window_outside_work_screen", "Move the task window fully into the work screen, then restart.")
                    return False
                if recover:
                    self._remember_focus(foreground)
                return False
            if not recover:
                raise RuntimeError("Unexpected foreground window; input interrupted")
            previous = self._expected_focus
            if self._recovery_used:
                self._stop("focus_interference_repeated", "Another window took focus again. Restore the task window and restart.")
                return False
            if not self._identity_valid(previous) or not self._window_allowed(previous.window_id, geometry):
                self._stop("task_window_unavailable", "The last task window was closed or left the work screen. Restore it and restart.")
                return False
            self._recovery_used = True
            self._invalidate()
            self.native.activate(previous.window_id, allow_input_fallback=False)
            restored = self.native.probe()
            if (not self._identity_valid(restored) or
                    (restored.window_id, restored.process_id) != (previous.window_id, previous.process_id)
                    or not self._window_allowed(restored.window_id, geometry)):
                raise RuntimeError("Windows did not restore the task window")
            self._expected_focus = restored
            self._recovery_event = {"from_window": foreground.window_id, "to_window": restored.window_id,
                                    "attempt": 1, "status": "restored"}
            return True
        except Exception as exc:
            if not recover:
                raise
            self._stop("focus_recovery_failed", f"Could not restore task focus: {exc}. Restore the task window and restart.")
            return False

    def _remember_focus(self, foreground):
        previous = self._expected_focus
        related = getattr(self.native, "related_window", None)
        if (previous and foreground.window_id != previous.window_id and related and
                self._identity_valid(previous) and related(foreground.window_id, previous.window_id)):
            self._dialog_parents[(foreground.window_id, foreground.process_id)] = (previous.window_id, previous.process_id)
        elif previous and related:
            ancestor = (previous.window_id, previous.process_id)
            seen = set()
            process = getattr(self.native, "window_process", None)
            while ancestor in self._dialog_parents and ancestor not in seen:
                seen.add(ancestor)
                ancestor = self._dialog_parents[ancestor]
                if process and process(ancestor[0]) == ancestor[1] and related(foreground.window_id, ancestor[0]):
                    self._dialog_parents[(foreground.window_id, foreground.process_id)] = ancestor
                    break
        if previous and (foreground.window_id, foreground.process_id) != (previous.window_id, previous.process_id):
            self._expected_switch = None
        self._expected_focus = foreground
        self._task_history = [entry for entry in self._task_history
                              if (entry.window_id, entry.process_id) != (foreground.window_id, foreground.process_id)]
        self._task_history.append(foreground)

    def _switch_task_window(self, dispatch_info):
        """Alt+Tab switches only to a previously confirmed window on the work screen."""
        geometry = self._geometry()
        if geometry is None:
            raise RuntimeError(self._safety_stop["reason"])
        current = self.native.probe()
        for candidate in reversed(self._task_history):
            if candidate.native_ui or candidate.window_id == current.window_id:
                continue
            try:
                eligible = self._identity_valid(candidate) and self._window_allowed(candidate.window_id, geometry)
            except (RuntimeError, ValueError, OSError):
                eligible = False
            if not eligible:
                continue
            self._input_guard()
            dispatch_info["dispatched"] = True
            self.native.activate(candidate.window_id, allow_input_fallback=False)
            geometry = self._geometry()
            if geometry is None:
                raise RuntimeError(self._safety_stop["reason"])
            switched = self.native.probe()
            if ((switched.window_id, switched.process_id) != (candidate.window_id, candidate.process_id)
                    or not self._identity_valid(switched) or not self._window_allowed(switched.window_id, geometry)):
                raise RuntimeError("Task window switch was not confirmed")
            self._remember_focus(switched)
            return switched.window_id
        raise ValueError("No previous task window on the work screen; select an app through the work-screen taskbar")

    def _input_guard(self):
        if self._safety_stop:
            raise RuntimeError(self._safety_stop["reason"])
        geometry = self._geometry()
        if geometry is None:
            raise RuntimeError(self._safety_stop["reason"])
        current = self.native.probe()
        self._check_focus(current, geometry, recover=False)
        if self._safety_stop:
            raise RuntimeError(self._safety_stop["reason"])
        # A requested click may activate its hit-tested window and focus a child.
        if self._pending_point is not None:
            hit_test = getattr(self.native, "window_at_point", None)
            if hit_test and hit_test(self._pending_point) != self._pending_click[0]:
                raise RuntimeError("Click target changed before input")
            window_process = getattr(self.native, "window_process", None)
            if window_process and window_process(self._pending_click[0]) != self._pending_click[1]:
                raise RuntimeError("Click target process changed before input")
        if self._dispatch_focus and focus_ticket(current) != self._dispatch_focus:
            raise RuntimeError("Focus changed during input; remaining input interrupted")
        window_geometry = getattr(self.native, "window_geometry", None)
        if self._dispatch_geometry and window_geometry:
            if window_geometry(current.window_id) != self._dispatch_geometry:
                raise RuntimeError("Task window moved, resized or changed DPI during input")

    def _after_requested_click(self):
        self._click_acknowledged = True
        geometry = self._geometry()
        if geometry is None:
            raise RuntimeError(self._safety_stop["reason"])
        current = self.native.probe()
        # A successful click can change root/child focus; confirm its expected result
        # immediately and use a strict new ticket before another click is sent.
        self._check_focus(current, geometry, recover=False, allow_foreground_transition=True)
        if self._safety_stop:
            raise RuntimeError(self._safety_stop["reason"])
        self._remember_focus(current)
        self._bind_input(current)

    def _after_drag_input(self):
        geometry = self._geometry()
        if geometry is None:
            raise RuntimeError(self._safety_stop["reason"])
        current = self.native.probe()
        if (not self._identity_valid(current) or
                (current.window_id, current.process_id, current.foreground_generation) != self._drag_focus):
            raise RuntimeError("Task window lost foreground during drag")
        if not self._window_allowed(current.window_id, geometry):
            self._stop("window_outside_work_screen", "Drag moved the task window outside the work screen. Restore it and restart.")
            raise RuntimeError(self._safety_stop["reason"])
        self._bind_input(current)

    def _bind_input(self, foreground=None):
        if foreground is None and self.last_observation:
            self._dispatch_focus = tuple(self.last_observation.info.get("focus_ticket", ()))
            self._dispatch_geometry = copy.deepcopy(self.last_observation.info.get("foreground_geometry"))
            return
        foreground = foreground or self.native.probe()
        self._dispatch_focus = focus_ticket(foreground)
        window_geometry = getattr(self.native, "window_geometry", None)
        self._dispatch_geometry = window_geometry(foreground.window_id) if window_geometry else None

    def _browser_input_guard(self):
        """Guard every DOM side effect; element focus changes may be intentional."""
        if self._safety_stop:
            raise RuntimeError(self._safety_stop["reason"])
        geometry = self._geometry()
        if geometry is None:
            raise RuntimeError(self._safety_stop["reason"])
        current = self.native.probe()
        expected = self._dispatch_focus or tuple((self.last_observation.info if self.last_observation else {}).get("focus_ticket", ()))
        if (not self._identity_valid(current) or not current.is_browser or current.native_ui or
                len(expected) < 3 or (current.window_id, current.process_id) != (expected[1], expected[2])):
            raise RuntimeError("Task browser lost foreground during DOM input")
        if len(expected) >= 7 and current.foreground_generation != expected[6]:
            raise RuntimeError("Task browser switched away and back during DOM input")
        if not self._window_allowed(current.window_id, geometry):
            self._stop("window_outside_work_screen", "Move the task browser fully into the work screen, then restart.")
            raise RuntimeError(self._safety_stop["reason"])
        window_geometry = getattr(self.native, "window_geometry", None)
        if self._dispatch_geometry and window_geometry and window_geometry(current.window_id) != self._dispatch_geometry:
            raise RuntimeError("Task browser moved, resized or changed DPI during DOM input")

    def _before_browser_navigation(self):
        geometry = self._geometry()
        if geometry is None or self._safety_stop:
            raise RuntimeError(self._safety_stop["reason"])
        current = self.native.probe()
        if (not self._opening_browser or current.process_id != self.runtime.process_id or
                not self._identity_valid(current) or not self._window_allowed(current.window_id, geometry)):
            raise RuntimeError("Task browser unavailable before navigation")
        self._bind_input(current)
        self._browser_input_guard()

    def _blocked_observation(self, foreground=None):
        foreground = foreground or self.native.probe()
        scene = scene_from_foreground(foreground)
        scene.mode, scene.reason = "unknown", self._safety_stop["code"]
        info = {"scene": scene.to_dict(), "safety_stop": dict(self._safety_stop),
                "coordinate_space": "desktop_physical", "focus_ticket": list(focus_ticket(foreground))}
        if self._layout:
            info.update(desktop_geometry=copy.deepcopy(self._layout),
                        capture_geometry=copy.deepcopy(self._layout.get("capture_geometry", self._layout)),
                        target_monitor=self._layout.get("target_monitor"))
        if self._recovery_event:
            info["focus_recovery"] = dict(self._recovery_event)
        self.last_observation = Observation(info=info)
        return self.last_observation

    def _result(self, info):
        observation = self.observe()
        if self._safety_stop:
            info["safety_stop"] = dict(self._safety_stop)
        if self._recovery_event:
            info["focus_recovery"] = dict(self._recovery_event)
        return StepResult(observation, info=info)

    def reset(self, task):
        # Continue the desktop as it is; never launch/navigate a browser on task reset.
        self._task = task
        self._invalidate()
        self._last_focus = None
        self._layout = None
        self._expected_focus = None
        self._dialog_parents = {}
        self._task_history = []
        self._expected_switch = None
        self._pending_click = self._dispatch_focus = self._dispatch_geometry = None
        self._drag_focus = None
        self._pending_point = None
        self._opening_browser = False
        self._click_launch = False
        self._click_acknowledged = False
        self._recovery_used = False
        self._recovery_event = self._safety_stop = None
        return self.observe()

    def observe(self):
        started = time.perf_counter()
        for attempt in range(2):
            before = self.native.probe()
            if self._safety_stop:
                return self._blocked_observation(before)
            geometry = self._geometry()
            if geometry is None:
                return self._blocked_observation(before)
            recovered = self._check_focus(before, geometry, recover=True)
            if self._safety_stop:
                return self._blocked_observation(before)
            if recovered:
                before = self.native.probe()
            changed = self._last_focus is not None and focus_ticket(before) != self._last_focus
            if changed:
                self._invalidate()
            self._last_focus = focus_ticket(before)
            info = {"coordinate_space": "desktop_physical", "focus_version": before.generation,
                    "focus_ticket": list(focus_ticket(before)), "focus_changed": changed}
            try:
                window_geometry = getattr(self.native, "window_geometry", None)
                before_window = window_geometry(before.window_id) if window_geometry and before.available else None
                screenshot, geometry = self.native.capture()
                if geometry != self._layout:
                    self._stop("display_layout_changed", "Desktop geometry changed during capture. Check the work screen and restart.")
                    return self._blocked_observation(before)
                info["desktop_geometry"] = geometry
                info["capture_geometry"] = copy.deepcopy(geometry.get("capture_geometry", geometry))
                info["target_monitor"] = geometry.get("target_monitor")
                if self._recovery_event:
                    info["focus_recovery"] = dict(self._recovery_event)
                if before_window is not None:
                    info["foreground_geometry"] = before_window
            except Exception as exc:
                self._stop("desktop_capture_failed", f"Work-screen capture failed: {exc}. Restore the desktop and restart.")
                return self._blocked_observation(before)
            captured = time.perf_counter()
            block = None
            scene = scene_from_foreground(before)
            try:
                session = self.runtime.focused_session(before)
            except Exception as exc:
                self._invalidate()
                session = None
                info["structure_error"] = type(exc).__name__
            if session:
                session.input_guard = self._browser_input_guard
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
            if self._geometry() is None:
                return self._blocked_observation(after)
            recovered = self._check_focus(after, geometry, recover=True)
            if self._safety_stop:
                return self._blocked_observation(after)
            if recovered:
                after = self.native.probe()
            try:
                window_changed = bool(window_geometry and before_window is not None and
                    (after.window_id != before.window_id or window_geometry(after.window_id) != before_window))
            except Exception as exc:
                self._stop("task_window_unavailable", f"Task window changed during observation: {exc}. Restore it and restart.")
                return self._blocked_observation(after)
            if recovered or window_changed or focus_ticket(before) != focus_ticket(after):
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
            route = {"channel": "reobserve", "reason": reason}
            if self._safety_stop:
                route["safety_stop"] = dict(self._safety_stop)
            return route
        if self._safety_stop:
            return reject(self._safety_stop["reason"])
        geometry = self._geometry()
        if geometry is None:
            return reject(self._safety_stop["reason"])
        current = self.native.probe()
        recovered = self._check_focus(current, geometry, recover=True)
        if self._safety_stop:
            return reject(self._safety_stop["reason"])
        if recovered:
            return reject("Focus restored; old action rejected, take a new observation")
        if observation is None or observation.screenshot is None or observation.info.get("observation_unstable"):
            return reject("Desktop observation unavailable or unstable")
        current = self.native.probe()
        if not current.available or list(focus_ticket(current)) != observation.info.get("focus_ticket"):
            return reject("Focus changed after observation; old action rejected")
        try:
            if self.native.geometry() != observation.info.get("desktop_geometry"):
                self._stop("display_layout_changed", "Desktop geometry changed after observation. Check the work screen and restart.")
                return reject(self._safety_stop["reason"])
            window_geometry = getattr(self.native, "window_geometry", None)
            if window_geometry and window_geometry(current.window_id) != observation.info.get("foreground_geometry"):
                return reject("Foreground window moved, resized or changed DPI after observation")
        except Exception:
            self._stop("desktop_geometry_unavailable", "Desktop geometry unavailable. Restore the work screen and restart.")
            return reject(self._safety_stop["reason"])
        return None

    def route_action(self, action, observation):
        session = self.browser_session
        return session.route(action, observation) if session and browser_context(observation) else None

    def execute_routed(self, action, route):
        rejected = self.preflight(action, self.last_observation)
        if rejected:
            return self._result({"status": "rejected", "dispatched": False, **rejected})
        self._bind_input()
        try:
            self._input_guard()
            self.browser_session.input_guard = self._browser_input_guard
            info = self.browser_session.execute(action, route)
        finally:
            self._dispatch_focus = self._dispatch_geometry = None
        time.sleep(self.settle_ms / 1000)
        return self._result(info)

    def step(self, action):
        rejected = self.preflight(action, self.last_observation)
        if rejected:
            return self._result({"status": "rejected", "dispatched": False, **rejected})
        name, a = action.action, action.args
        self._expected_switch = None  # A new decision supersedes any pending click activation.
        geometry = self.last_observation.info["desktop_geometry"]
        info = {"channel": "desktop_input", "coordinate_space": "desktop_physical",
                "status": "executed", "dispatched": False}
        dispatch_before = getattr(self.native, "input_dispatch_count", None)
        try:
            self._bind_input()
            if a.get("grounding_failed"):
                raise ValueError("grounding_failed")
            if name in {"click", "double_click", "right_click", "type"}:
                point = desktop_point(a["x"], a["y"], geometry)
                window_at_point = getattr(self.native, "window_at_point", None)
                target_window = window_at_point(point) if window_at_point else self.native.probe().window_id
                window_process = getattr(self.native, "window_process", None)
                target_pid = window_process(target_window) if window_process else self.native.probe().process_id
                if not target_window or not target_pid or not self._window_allowed(target_window, geometry):
                    raise ValueError("Click target is not fully inside the work screen")
                self._pending_click = (target_window, target_pid)
                self._pending_point = point
                window_class = getattr(self.native, "window_class", None)
                self._click_launch = bool(name != "type" and window_class and
                    window_class(target_window) in {"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW"})
                self._click_acknowledged = False
                self._input_guard()
                info.update(dispatched=True, desktop_point=list(point))
                original_identity = (self._expected_focus.window_id, self._expected_focus.process_id)
                self.native.click(point, geometry, right=name == "right_click", count=2 if name == "double_click" else 1,
                                  after_click=self._after_requested_click)
                if name != "type":
                    clicked_focus = self.native.probe()
                    clicked_identity = (clicked_focus.window_id, clicked_focus.process_id)
                    if clicked_identity == original_identity and (self._click_launch or clicked_identity != self._pending_click):
                        self._expected_switch = {"target": self._pending_click, "shell": self._click_launch,
                                                 "deadline": time.monotonic() + 1.0}
                self._pending_click = None
                self._pending_point = None
                self._click_launch = False
                self._click_acknowledged = False
                if name == "type":
                    time.sleep(0.06)
                    target_focus = self.native.probe()
                    if (not target_focus.available or
                            (target_focus.window_id, target_focus.process_id) != (target_window, target_pid)):
                        raise RuntimeError("Text input focus does not match the clicked window")
                    if not self._identity_valid(target_focus) or not self._window_allowed(target_focus.window_id, geometry):
                        raise RuntimeError("Clicked text window left the work screen")
                    self._remember_focus(target_focus)
                    self._bind_input(target_focus)
                    def guard():
                        self._input_guard()
                        if focus_ticket(self.native.probe()) != focus_ticket(target_focus):
                            raise RuntimeError("Focus changed during text input")
                    if a.get("overwrite"):
                        guard()
                        self.native.hotkey(["ctrl", "a"])
                        guard()
                        self.native.hotkey(["backspace"])
                    self.native.type_text(str(a["text"]), guard=guard)
            elif name in {"press", "hotkey"}:
                keys = a["keys"] if name == "hotkey" else [a["key"]]
                for key in keys:
                    key_code(key)
                self._input_guard()
                if name == "hotkey" and [str(key).lower() for key in keys] == ["alt", "tab"]:
                    info["channel"] = "window_activation"
                    info["window_id"] = self._switch_task_window(info)
                else:
                    info["dispatched"] = True
                    self.native.hotkey(keys)
            elif name == "scroll":
                if a["direction"] not in {"up", "down"} or int(a["amount"]) <= 0:
                    raise ValueError("Invalid scroll")
                self._input_guard()
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
                self._input_guard()
                self._drag_focus = (self._dispatch_focus[1], self._dispatch_focus[2], self._dispatch_focus[6])
                info["dispatched"] = True
                self.native.drag(start, end, geometry, after_input=self._after_drag_input)
            elif name == "open_browser":
                validate_url(a.get("url"))
                self._input_guard()
                info["dispatched"] = True
                self._opening_browser = True
                try:
                    opened = self.runtime.open(a.get("url"))
                    foreground = self.native.probe()
                    if not foreground.is_browser or foreground.process_id != self.runtime.process_id:
                        raise RuntimeError("Opened browser did not become the intended foreground window")
                    if not self.adopt_window(foreground):
                        raise RuntimeError(self._safety_stop["reason"])
                    self._bind_input(foreground)
                finally:
                    self._opening_browser = False
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
            if dispatch_before is not None and name != "open_browser" and info["channel"] != "window_activation":
                info["dispatched"] = getattr(self.native, "input_dispatch_count", dispatch_before) > dispatch_before
            info.update(status="uncertain" if info["dispatched"] else "rejected", error=str(exc)[:300])
        finally:
            self._pending_click = None
            self._pending_point = None
            self._click_launch = False
            self._click_acknowledged = False
            self._opening_browser = False
            self._dispatch_focus = self._dispatch_geometry = None
            self._drag_focus = None
        time.sleep(self.settle_ms / 1000)
        return self._result(info)

    def _navigate_visual(self, url):
        foreground = self.native.probe()
        if not foreground.available or not foreground.is_browser or foreground.native_ui:
            raise RuntimeError("Browser lost focus before navigation")
        self._input_guard()
        self.native.hotkey(["ctrl", "l"])
        time.sleep(0.06)
        address_focus = self.native.probe()
        if (address_focus.window_id, address_focus.process_id) != (foreground.window_id, foreground.process_id):
            raise RuntimeError("Browser lost focus before address input")
        self._bind_input(address_focus)
        def guard():
            self._input_guard()
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
            self.native.input_guard = None
            self.native.close()
            self._closed = True
