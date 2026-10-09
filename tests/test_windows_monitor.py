"""Multi-monitor interference and authorized app switches without live input."""
import copy
from dataclasses import replace

import pytest

from osworld_agent.actions import Action
from osworld_agent.adapters.windows_env import WindowsEnvironment
from osworld_agent.scene import Foreground
from test_windows_env import Native, DESKTOP, BROWSER


SECONDARY = Foreground(available=True, window_id="99", focus_id="98", process_id=90)
OTHER = Foreground(available=True, window_id="50", focus_id="51", process_id=60)
DIALOG = replace(DESKTOP, window_id="40", focus_id="41", native_ui=True)
LAYOUT = {
    "left": -240, "top": -60, "width": 440, "height": 220,
    "scale": 1, "coordinate_space": "desktop_physical", "target_monitor": "WORK",
    "capture_geometry": {"left": 0, "top": 0, "width": 200, "height": 160},
    "monitors": [
        {"device": "SIDE", "primary": False, "left": -240, "top": -60, "width": 240, "height": 220},
        {"device": "WORK", "primary": True, "left": 0, "top": 0, "width": 200, "height": 160},
    ],
}


class MultiNative(Native):
    def __init__(self):
        super().__init__()
        self.foreground = DESKTOP
        self.layout = copy.deepcopy(LAYOUT)
        self.identities = {"10": 20, "30": 40, "40": 20, "50": 60, "99": 90, "70": 100}
        self.bounds = {key: {"left": 20, "top": 20, "right": 180, "bottom": 130}
                       for key in self.identities}
        self.bounds["99"] = {"left": -220, "top": -30, "right": -30, "bottom": 130}
        self.hit_window = "10"
        self.on_click = self.on_keys = None
        self.input_dispatch_count = 0
    def window_process(self, window_id):
        return self.identities.get(str(window_id), 0)
    def window_bounds(self, window_id):
        return dict(self.bounds[str(window_id)])
    def window_at_point(self, point):
        return self.hit_window
    def window_class(self, window_id):
        return "Shell_TrayWnd" if str(window_id) == "70" else "OrdinaryWindow"
    def related_window(self, candidate, expected):
        return candidate == expected or (candidate, expected) == ("40", "10")
    def dispatch(self, event):
        self.input_guard()
        self.input_dispatch_count += 1
        self.events.append(event)
    def click(self, point, geometry, **kwargs):
        after_click = kwargs.pop("after_click", None)
        self.dispatch(("click", point, kwargs))
        if self.on_click:
            self.on_click()
        if after_click:
            after_click()
    def activate(self, window_id, **kwargs):
        self.safety_events.append(("activate", str(window_id), kwargs))
        target = {"10": DESKTOP, "30": BROWSER, "40": DIALOG, "50": OTHER, "99": SECONDARY}[str(window_id)]
        self.foreground = replace(target, generation=self.foreground.generation + 1)
    def hotkey(self, keys):
        self.dispatch(("keys", keys))
        if self.on_keys:
            self.on_keys(keys)
    def type_text(self, text, guard=None):
        if guard:
            guard()
        self.dispatch(("text", text))


@pytest.fixture
def work_screen():
    native = MultiNative()
    environment = WindowsEnvironment(native=native, settle_ms=0)
    environment.reset("Use the selected screen")
    yield environment, native
    environment.close()


def test_capture_dimensions_and_local_coordinates_use_only_work_screen(work_screen):
    env, native = work_screen
    assert env.last_observation.screenshot.size == (200, 160)
    assert env.last_observation.info["desktop_geometry"]["left"] == -240
    assert env.last_observation.info["capture_geometry"]["left"] == 0
    result = env.step(Action("click", {"x": 100, "y": 80}))
    assert result.info["desktop_point"] == [100, 80]
    assert native.events[0][1] == (100, 80)


@pytest.mark.parametrize("action", [Action("click", {"x": 201, "y": 80}),
    Action("scroll", {"x": 201, "y": 80, "direction": "down", "amount": 1}),
    Action("drag_and_drop", {"x1": 10, "y1": 10, "x2": 200, "y2": 80})])
def test_offscreen_targets_never_dispatch(work_screen, action):
    env, native = work_screen
    result = env.step(action)
    assert result.info["status"] == "rejected" and not result.info["dispatched"]
    assert native.events == []


def test_secondary_foreground_is_restored_once_and_stale_action_discarded(work_screen):
    env, native = work_screen
    native.foreground = SECONDARY
    result = env.step(Action("press", {"key": "enter"}))
    assert result.info["status"] == "rejected" and not result.info["dispatched"]
    assert not native.events
    assert native.safety_events == [("activate", "10", {"allow_input_fallback": False})]
    assert result.observation.info["focus_recovery"]["status"] == "restored"
    assert env._expected_focus.window_id == "10"
    assert env.step(Action("press", {"key": "enter"})).info["status"] == "executed"
    native.foreground = SECONDARY
    stopped = env.step(Action("press", {"key": "enter"}))
    assert stopped.info["safety_stop"]["code"] == "focus_interference_repeated"
    assert native.events == [("keys", ["enter"])]
    assert len(native.safety_events) == 1
    native.foreground = DESKTOP
    assert env.observe().info["safety_stop"]  # Manually returning focus cannot clear stop.
    assert not env.adopt_window()


def test_failed_activation_stops_without_input(work_screen):
    env, native = work_screen
    native.foreground = SECONDARY
    native.activate = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("activation denied"))
    result = env.step(Action("press", {"key": "enter"}))
    assert result.info["safety_stop"]["code"] == "focus_recovery_failed"
    assert not native.events


def test_closed_task_window_is_not_reactivated(work_screen):
    env, native = work_screen
    del native.identities["10"]
    native.foreground = SECONDARY
    stopped = env.observe()
    assert stopped.info["safety_stop"]["code"] == "task_window_unavailable"
    assert native.safety_events == [] and not stopped.context


@pytest.mark.parametrize("rectangle", [
    {"left": -220, "top": 0, "right": -20, "bottom": 100},
    {"left": -10, "top": 20, "right": 180, "bottom": 130},
])
def test_initial_secondary_or_spanning_task_window_requires_manual_placement(rectangle):
    native = MultiNative()
    native.bounds["10"] = rectangle
    env = WindowsEnvironment(native=native, settle_ms=0)
    try:
        stopped = env.reset("Task starts here")
        assert stopped.info["safety_stop"]["code"] == "window_outside_work_screen"
        assert native.events == [] and native.safety_events == [] and not stopped.context
    finally:
        env.close()


def test_window_moved_to_secondary_stops_instead_of_following(work_screen):
    env, native = work_screen
    native.bounds["10"] = dict(native.bounds["99"])
    result = env.step(Action("press", {"key": "enter"}))
    assert result.info["safety_stop"]["code"] == "window_outside_work_screen"
    assert not native.events and not native.safety_events


def test_display_layout_change_is_latched_before_input(work_screen):
    env, native = work_screen
    native.layout["monitors"][0]["width"] += 1
    result = env.step(Action("press", {"key": "enter"}))
    assert result.info["safety_stop"]["code"] == "display_layout_changed"
    assert not native.events


def test_focus_stolen_after_click_sends_no_text_or_overwrite(work_screen):
    env, native = work_screen
    native.on_click = lambda: setattr(native, "foreground", SECONDARY)
    result = env.step(Action("type", {"x": 100, "y": 80, "text": "private", "overwrite": True}))
    assert result.info["status"] == "uncertain" and result.info["dispatched"]
    assert [event[0] for event in native.events] == ["click"]
    assert result.info["focus_recovery"]["status"] == "restored"
    assert native.foreground.window_id == "10"


def test_focus_stolen_after_select_all_sends_no_backspace_or_text(work_screen):
    env, native = work_screen
    native.on_keys = lambda keys: setattr(native, "foreground", SECONDARY)
    result = env.step(Action("type", {"x": 100, "y": 80, "text": "private", "overwrite": True}))
    assert result.info["status"] == "uncertain"
    assert [event[0] for event in native.events] == ["click", "keys"]
    assert native.events[-1] == ("keys", ["ctrl", "a"])


def test_long_text_stops_remaining_chunks_after_focus_interference(work_screen):
    env, native = work_screen
    def chunked(text, guard):
        guard()
        native.dispatch(("text", text[:5]))
        native.foreground = SECONDARY
        guard()
        pytest.fail("Remaining text must never be sent")
    native.type_text = chunked
    result = env.step(Action("type", {"x": 100, "y": 80, "text": "abcdefghijk"}))
    assert result.info["status"] == "uncertain"
    assert [event for event in native.events if event[0] == "text"] == [("text", "abcde")]


def test_clicked_app_is_authorized_but_unrequested_foreground_is_not(work_screen):
    env, native = work_screen
    native.hit_window = "50"
    native.on_click = lambda: setattr(native, "foreground", OTHER)
    result = env.step(Action("click", {"x": 100, "y": 80}))
    assert result.info["status"] == "executed" and env._expected_focus.window_id == "50"
    assert native.safety_events == []
    assert env.step(Action("press", {"key": "enter"})).info["status"] == "executed"


def test_taskbar_click_can_activate_an_app_on_work_screen(work_screen):
    env, native = work_screen
    native.hit_window = "70"
    native.on_click = lambda: setattr(native, "foreground", OTHER)
    result = env.step(Action("click", {"x": 100, "y": 80}))
    assert result.info["status"] == "executed" and env._expected_focus.window_id == "50"
    assert not native.safety_events


def test_owned_dialog_and_return_to_parent_remain_valid(work_screen):
    env, native = work_screen
    native.foreground = DIALOG
    assert not env.observe().info.get("safety_stop")
    assert env._expected_focus.window_id == "40"
    del native.identities["40"]
    native.foreground = DESKTOP
    assert not env.observe().info.get("safety_stop")
    assert env._expected_focus.window_id == "10"
    assert native.safety_events == []


def test_same_hwnd_new_pid_is_never_treated_as_task_window(work_screen):
    env, native = work_screen
    native.identities["10"] = 123
    native.foreground = replace(DESKTOP, process_id=123)
    assert env.observe().info["safety_stop"]["code"] == "task_window_unavailable"
    assert native.safety_events == []


def test_previous_task_app_cannot_steal_focus_without_an_authorized_switch(work_screen):
    env, native = work_screen
    native.hit_window = "50"
    native.on_click = lambda: setattr(native, "foreground", OTHER)
    assert env.step(Action("click", {"x": 100, "y": 80})).info["status"] == "executed"
    native.foreground = DESKTOP  # Previously used app takes focus without a click.
    result = env.step(Action("press", {"key": "enter"}))
    assert not result.info["dispatched"]
    assert native.foreground.window_id == "50"
    assert native.safety_events[-1] == ("activate", "50", {"allow_input_fallback": False})


def test_delayed_taskbar_activation_is_authorized_only_once(work_screen):
    env, native = work_screen
    native.hit_window = "70"
    env.step(Action("click", {"x": 100, "y": 80}))
    native.foreground = OTHER
    assert not env.observe().info.get("safety_stop")
    assert env._expected_focus.window_id == "50"
    assert not native.safety_events
    native.foreground = DESKTOP
    env.observe()
    assert native.safety_events == [("activate", "50", {"allow_input_fallback": False})]


def test_foreground_change_before_first_shell_click_is_not_authorized(work_screen):
    env, native = work_screen
    native.hit_window = "70"
    original_class = native.window_class
    changed = []
    def switch_before_click(window):
        if str(window) == "70" and not changed:
            changed.append(True)
            native.foreground = OTHER
        return original_class(window)
    native.window_class = switch_before_click
    result = env.step(Action("click", {"x": 100, "y": 80}))
    assert not result.info["dispatched"]
    assert not native.events
    assert native.foreground.window_id == "10"


def test_browser_side_effect_guard_rejects_moved_window_and_latches_stop(work_screen):
    env, native = work_screen
    native.foreground = BROWSER
    assert env.adopt_window()
    env.observe()
    env._bind_input()
    native.bounds["30"] = dict(native.bounds["99"])
    with pytest.raises(RuntimeError, match="work screen"):
        env._browser_input_guard()
    assert env.safety_stop["code"] == "window_outside_work_screen"


def test_browser_side_effect_guard_allows_element_focus_but_rejects_other_window(work_screen):
    env, native = work_screen
    native.foreground = BROWSER
    assert env.adopt_window()
    env.observe()
    env._bind_input()
    native.foreground = replace(BROWSER, focus_id="new-control", generation=2)
    env._browser_input_guard()
    native.foreground = SECONDARY
    with pytest.raises(RuntimeError, match="lost foreground"):
        env._browser_input_guard()
    assert not native.events


def test_browser_guard_rejects_away_and_back_without_rejecting_element_focus(work_screen):
    env, native = work_screen
    native.foreground = BROWSER
    assert env.adopt_window()
    env.observe()
    env._bind_input()
    native.foreground = replace(BROWSER, generation=2, foreground_generation=1)
    with pytest.raises(RuntimeError, match="away and back"):
        env._browser_input_guard()


def test_window_movement_during_capture_retries_before_accepting_coordinates(work_screen):
    env, native = work_screen
    rectangle = {"left": 20, "top": 20, "right": 180, "bottom": 130, "dpi": 96}
    native.window_geometry = lambda window: dict(rectangle)
    original_capture = native.capture
    calls = []
    def moved_capture():
        calls.append(True)
        shot = original_capture()
        rectangle["left"] = 30
        return shot
    native.capture = moved_capture
    observed = env.observe()
    assert len(calls) == 2
    assert observed.info["foreground_geometry"]["left"] == 30
    assert not observed.info.get("observation_unstable")


def test_geometry_change_at_last_preflight_check_is_latched(work_screen):
    env, native = work_screen
    original_geometry = native.geometry
    calls = []
    def flickering_geometry():
        calls.append(True)
        geometry = copy.deepcopy(original_geometry())
        if len(calls) == 2:
            geometry["width"] += 1
        return geometry
    native.geometry = flickering_geometry
    result = env.step(Action("press", {"key": "enter"}))
    assert result.info["safety_stop"]["code"] == "display_layout_changed"
    assert not native.events


def test_capture_failure_stops_task_instead_of_repeated_model_retries(work_screen):
    env, native = work_screen
    native.capture = lambda: (_ for _ in ()).throw(RuntimeError("capture unavailable"))
    assert env.observe().info["safety_stop"]["code"] == "desktop_capture_failed"


def test_same_window_focus_change_after_preflight_cannot_use_stale_click(work_screen):
    env, native = work_screen
    original_class = native.window_class
    changed = []
    def change_during_hit_test(window):
        if not changed:
            changed.append(True)
            native.foreground = replace(DESKTOP, generation=2, foreground_generation=1)
        return original_class(window)
    native.window_class = change_during_hit_test
    result = env.step(Action("click", {"x": 100, "y": 80}))
    assert not result.info["dispatched"] and not native.events


def test_target_hwnd_pid_reuse_after_hit_test_rejects_click(work_screen):
    env, native = work_screen
    native.hit_window = "50"
    original_class = native.window_class
    def recycled_target(window):
        if str(window) == "50":
            native.identities["50"] = 777
        return original_class(window)
    native.window_class = recycled_target
    result = env.step(Action("click", {"x": 100, "y": 80}))
    assert not result.info["dispatched"] and not native.events


def test_replacement_dialog_owned_by_task_parent_is_authorized(work_screen):
    env, native = work_screen
    native.foreground = DIALOG
    env.observe()
    native.identities["42"] = 20
    native.bounds["42"] = dict(native.bounds["40"])
    native.related_window = lambda candidate, expected: candidate == expected or (candidate in {"40", "42"} and expected == "10")
    native.foreground = replace(DIALOG, window_id="42")
    assert not env.observe().info.get("safety_stop")
    assert env._expected_focus.window_id == "42" and not native.safety_events


def test_drag_control_focus_and_window_motion_can_refresh_expected_snapshot(work_screen):
    env, native = work_screen
    env._bind_input()
    env._drag_focus = (DESKTOP.window_id, DESKTOP.process_id, DESKTOP.foreground_generation)
    native.foreground = replace(DESKTOP, focus_id="drag-handle", generation=1)
    native.bounds["10"]["left"] += 10
    env._after_drag_input()
    env._input_guard()
    native.foreground = replace(native.foreground, foreground_generation=1)
    with pytest.raises(RuntimeError, match="lost foreground"):
        env._after_drag_input()


def test_explicit_alt_tab_returns_to_confirmed_task_window_without_global_input(work_screen):
    env, native = work_screen
    native.foreground = BROWSER
    env.adopt_window()
    env.observe()
    result = env.step(Action("hotkey", {"keys": ["alt", "tab"]}))
    assert result.info["status"] == "executed" and result.info["channel"] == "window_activation"
    assert native.foreground.window_id == "10"
    assert not native.events and not env._recovery_used
    assert native.safety_events == [("activate", "10", {"allow_input_fallback": False})]


def test_alt_tab_cannot_switch_to_a_previous_task_window_now_on_secondary(work_screen):
    env, native = work_screen
    native.foreground = BROWSER
    env.adopt_window()
    env.observe()
    native.bounds["10"] = dict(native.bounds["99"])
    result = env.step(Action("hotkey", {"keys": ["alt", "tab"]}))
    assert result.info["status"] == "rejected" and not result.info["dispatched"]
    assert not native.safety_events and not native.events
    assert native.foreground.window_id == "30"


def test_alt_tab_interference_during_candidate_selection_discards_old_action(work_screen):
    env, native = work_screen
    native.foreground = BROWSER
    env.adopt_window()
    env.observe()
    original_identity = env._identity_valid
    def interrupted_selection(foreground):
        if foreground.window_id == "10":
            native.foreground = SECONDARY
        return original_identity(foreground)
    env._identity_valid = interrupted_selection
    result = env.step(Action("hotkey", {"keys": ["alt", "tab"]}))
    assert result.info["status"] == "rejected" and not result.info["dispatched"]
    assert env._recovery_used and native.foreground.window_id == "30"
    assert native.safety_events == [("activate", "30", {"allow_input_fallback": False})]
    assert not native.events


def test_alt_tab_layout_change_during_activation_stops_task(work_screen):
    env, native = work_screen
    native.foreground = BROWSER
    env.adopt_window()
    env.observe()
    activate = native.activate
    def changed_layout(window, **kwargs):
        activate(window, **kwargs)
        native.layout["capture_geometry"]["width"] -= 1
    native.activate = changed_layout
    result = env.step(Action("hotkey", {"keys": ["alt", "tab"]}))
    assert result.info["status"] == "uncertain" and result.info["dispatched"]
    assert result.info["safety_stop"]["code"] == "display_layout_changed"


@pytest.mark.parametrize("stage", ["observe", "preflight", "input"])
def test_away_and_back_after_recovery_stops_even_with_same_foreground(work_screen, stage):
    env, native = work_screen
    native.foreground = SECONDARY
    env.observe()
    assert env._recovery_used
    env._bind_input()
    native.foreground = replace(native.foreground, generation=native.foreground.generation + 2,
                                foreground_generation=native.foreground.foreground_generation + 2)
    if stage == "observe":
        env.observe()
    elif stage == "preflight":
        env.preflight(Action("press", {"key": "enter"}), env.last_observation)
    else:
        with pytest.raises(RuntimeError, match="switched away and back"):
            env._input_guard()
    assert env.safety_stop["code"] == "focus_interference_repeated"
    assert len(native.safety_events) == 1 and not native.events


@pytest.mark.parametrize("target", [DESKTOP, BROWSER])
def test_requested_click_can_update_foreground_epoch_after_recovery(work_screen, target):
    env, native = work_screen
    native.foreground = SECONDARY
    env.observe()
    native.hit_window = target.window_id
    native.on_click = lambda: setattr(native, "foreground", replace(target, generation=3, foreground_generation=3))
    result = env.step(Action("click", {"x": 100, "y": 80}))
    assert result.info["status"] == "executed" and not env.safety_stop
    assert env._expected_focus.window_id == target.window_id
