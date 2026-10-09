"""CDP guard tests do not launch a browser or inject native desktop input."""
from types import SimpleNamespace

import pytest

from osworld_agent.actions import Action
from osworld_agent.browser import BrowserSession


class BrowserInputFixture:
    def __init__(self, *, guarded=True, block_after=None, lose_dom_focus_after=None, fail_down=False):
        self.events = []
        self.blocked = False
        self.dom_focused = True
        self.block_after = block_after
        self.lose_dom_focus_after = lose_dom_focus_after
        self.fail_down = fail_down
        mouse = SimpleNamespace(
            click=lambda x, y, **kwargs: self.record("click", kwargs),
            move=lambda x, y: self.record("move", [x, y]),
            wheel=lambda x, y: self.record("wheel", [x, y]),
            down=self.down,
            up=lambda **kwargs: self.record("up", kwargs),
        )
        keyboard = SimpleNamespace(press=lambda key: self.record(key), insert_text=lambda text: self.record("text", text))
        page = SimpleNamespace(mouse=mouse, keyboard=keyboard, wait_for_timeout=lambda ms: self.record("wait"))
        self.session = BrowserSession(page=page, input_guard=self.guard if guarded else None)
        self.session._cdp = SimpleNamespace(send=self.send)
        self.session._check = lambda ref, snapshot: ({"backend_id": 1}, "target-object")
        self.session._call = self.call

    def record(self, name, value=None):
        self.events.append((name, value))
        if name == self.block_after:
            self.blocked = True
        if name == self.lose_dom_focus_after:
            self.dom_focused = False

    def down(self, **kwargs):
        self.record("down", kwargs)
        if self.fail_down:
            raise RuntimeError("Partial mouse dispatch")

    def guard(self):
        self.record("guard")
        if self.blocked:
            raise RuntimeError("Task window moved to the second monitor")

    def send(self, method, args):
        self.record("scroll_into_view" if method == "DOM.scrollIntoViewIfNeeded" else "release_object")

    def call(self, obj, function, args=None):
        if "document.activeElement" in function:
            return self.dom_focused
        if "this.options" in function:
            return [1]
        if "this.selectedIndex=" in function:
            self.record("select", args)
            return None
        return {"x": 10, "y": 12, "w": 80, "h": 20, "tag": "select", "editable": True, "readOnly": False}

    def execute(self, name, **args):
        return self.session.execute(Action(name, args), {"target_ref": "target", "snapshot_id": "snapshot"})

    def names(self):
        return [name for name, _ in self.events]


def test_guard_rejects_before_any_mutating_cdp_operation():
    fixture = BrowserInputFixture()
    fixture.blocked = True
    result = fixture.execute("click")
    assert result["status"] == "rejected" and not result["dispatched"]
    assert fixture.names() == ["guard", "release_object"]


def test_guard_rechecks_after_actionability_wait_before_click():
    fixture = BrowserInputFixture(block_after="wait")
    result = fixture.execute("click")
    assert result["status"] == "uncertain" and result["dispatched"]
    assert "scroll_into_view" in fixture.names() and "click" not in fixture.names()
    assert "second monitor" in result["error"]


@pytest.mark.parametrize("after,blocked", [("click", "ControlOrMeta+A"),
                                         ("ControlOrMeta+A", "Backspace"), ("Backspace", "text")])
def test_overwrite_interrupts_before_each_remaining_keyboard_operation(after, blocked):
    fixture = BrowserInputFixture(block_after=after)
    result = fixture.execute("type", text="Task text", overwrite=True)
    assert result["status"] == "uncertain" and result["dispatched"]
    assert after in fixture.names() and blocked not in fixture.names()


def test_dom_focus_change_after_select_all_prevents_delete_and_text():
    fixture = BrowserInputFixture(lose_dom_focus_after="ControlOrMeta+A")
    result = fixture.execute("type", text="Task text", overwrite=True)
    assert result["status"] == "uncertain" and result["dispatched"]
    assert "ControlOrMeta+A" in fixture.names() and "Backspace" not in fixture.names() and "text" not in fixture.names()


def test_second_click_interrupted_with_first_mouse_button_released():
    fixture = BrowserInputFixture(block_after="up")
    result = fixture.execute("double_click")
    assert result["status"] == "uncertain" and result["dispatched"]
    presses = [(name, value["click_count"]) for name, value in fixture.events if name in {"down", "up"}]
    assert presses == [("down", 1), ("up", 1)]


def test_partial_mouse_down_always_attempts_release():
    fixture = BrowserInputFixture(fail_down=True)
    result = fixture.execute("double_click")
    assert result["status"] == "uncertain" and result["dispatched"]
    assert fixture.names()[-3:] == ["down", "up", "release_object"]


def test_scroll_rechecks_after_pointer_move_before_wheel():
    fixture = BrowserInputFixture(block_after="move")
    result = fixture.execute("scroll", direction="down", amount=2)
    assert result["status"] == "uncertain" and result["dispatched"]
    assert "move" in fixture.names() and "wheel" not in fixture.names()


def test_select_guard_prevents_dom_value_mutation_after_layout_change():
    fixture = BrowserInputFixture(block_after="wait")
    result = fixture.execute("select", option="Enterprise")
    assert result["status"] == "uncertain" and result["dispatched"]
    assert "select" not in fixture.names()


def test_default_browser_profile_keeps_existing_double_click_behavior():
    fixture = BrowserInputFixture(guarded=False)
    result = fixture.execute("double_click")
    assert result["status"] == "executed" and result["dispatched"]
    assert "guard" not in fixture.names() and "down" not in fixture.names()
    assert next(value for name, value in fixture.events if name == "click")["click_count"] == 2


def test_guarded_type_sends_all_operations_when_window_remains_valid():
    fixture = BrowserInputFixture()
    result = fixture.execute("type", text="中文😀", overwrite=True)
    assert result["status"] == "executed" and result["dispatched"]
    assert fixture.names().count("guard") == 5
    assert [name for name in fixture.names() if name in {"ControlOrMeta+A", "Backspace", "text"}] == ["ControlOrMeta+A", "Backspace", "text"]


def test_real_chromium_guarded_double_click_preserves_dom_event_sequence(tmp_path):
    from osworld_agent.adapters.browser_env import BrowserEnvironment
    fixture = tmp_path / "guarded-double-click.html"
    fixture.write_text("<button onclick='window.clicks=(window.clicks||0)+1' "
        "ondblclick='window.doubles=(window.doubles||0)+1'>Double target</button>", encoding="utf-8")
    env = BrowserEnvironment(fixture.as_uri())
    try:
        observed = env.reset("Double click the task button")
        node = next(n for n in observed.context[0]["nodes"] if n.get("name") == "Double target" and n.get("direct"))
        action = Action("double_click", {"target": "Double target", "target_ref": node["ref"]})
        guards = []
        env.session.input_guard = lambda: guards.append(True)
        result = env.session.execute(action, env.route_action(action, observed))
        assert result["status"] == "executed"
        assert env.page.evaluate("window.clicks") == 2
        assert env.page.evaluate("window.doubles") == 1
        assert len(guards) == 4
    finally:
        env.close()
