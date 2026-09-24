"""HLDD 005 Sustained-Hold Actuation (2026-09-23) — hold a roll/pitch key
until the tracked condition changes instead of computing a bounded tap.

sustained_hold=False (the default on both orient_nose_to_target and
orient_pitch_to_target) must reproduce the exact original tap/cooldown
behavior — that is covered by the existing tests in test_target_tracking.py
and TestOrientNoseToTarget there, which never pass sustained_hold and must
keep passing unchanged. This file exercises the sustained_hold=True path
directly, plus the release paths every caller now needs (miss ticks, loop
exit, cancel_mission/manual takeover) that a self-expiring tap never did.
"""

import threading

import wingman.controller as controller_module
from wingman.controller_config import ControllerConfig
from wingman.controller import (
    Controller, NOSE_DOWN_KEY, NOSE_UP_KEY, ROLL_LEFT_KEY, ROLL_RIGHT_KEY,
)


def _keys(ctrl):
    with ctrl._action_intents_lock:
        return [(i["action_type"], i["key"]) for i in ctrl._action_intents]


def _make_ctrl(monkeypatch, sustained_hold_enabled=True):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    return Controller(
        (0, 0, 1920, 1200),
        exit_event=threading.Event(),
        config=ControllerConfig(
            simulate_os_input=True,
            disable_hotkeys=True,
            tracking={"sustained_hold_enabled": sustained_hold_enabled},
        ),
    )


# ---------------------------------------------------------------------------
# Roll: press once, hold, release/switch on condition change
# ---------------------------------------------------------------------------

class TestSustainedRollHold:
    def test_constant_error_presses_once_not_per_call(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch)
        for _ in range(5):
            result = ctrl.orient_nose_to_target(-0.3, sustained_hold=True)
            assert result == "left"
        presses = [k for k in _keys(ctrl) if k == ("key_press", ROLL_LEFT_KEY)]
        assert len(presses) == 1, "must press once, not once per call, while error stays the same"
        assert ("key_release", ROLL_LEFT_KEY) not in _keys(ctrl)

    def test_error_entering_deadband_releases_the_held_key(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch)
        ctrl.orient_nose_to_target(-0.3, sustained_hold=True)
        result = ctrl.orient_nose_to_target(0.02, sustained_hold=True, deadband=0.05)
        assert result is None
        assert ("key_release", ROLL_LEFT_KEY) in _keys(ctrl)
        assert ctrl._roll_held is None
        assert ctrl._roll_hold_reason is None

    def test_error_flipping_sign_releases_old_and_presses_new(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch)
        ctrl.orient_nose_to_target(-0.3, sustained_hold=True)
        result = ctrl.orient_nose_to_target(0.3, sustained_hold=True)
        assert result == "right"
        keys = _keys(ctrl)
        assert keys.index(("key_release", ROLL_LEFT_KEY)) < keys.index(("key_press", ROLL_RIGHT_KEY))
        assert ctrl._roll_held == "right"
        # Never both held at once.
        assert not (("key_press", ROLL_LEFT_KEY) in keys[keys.index(("key_release", ROLL_LEFT_KEY)):]
                    and ("key_press", ROLL_RIGHT_KEY) in keys)

    def test_disabled_by_default(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch, sustained_hold_enabled=False)
        assert ctrl._sustained_hold_enabled is False


# ---------------------------------------------------------------------------
# Pitch: same mechanism, no search default
# ---------------------------------------------------------------------------

class TestSustainedPitchHold:
    def test_constant_error_presses_once_not_per_call(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch)
        for _ in range(5):
            result = ctrl.orient_pitch_to_target(0.3, sustained_hold=True)
            assert result == "down"
        presses = [k for k in _keys(ctrl) if k == ("key_press", NOSE_DOWN_KEY)]
        assert len(presses) == 1

    def test_error_entering_deadband_releases_the_held_key(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch)
        ctrl.orient_pitch_to_target(-0.3, sustained_hold=True)
        result = ctrl.orient_pitch_to_target(0.0, sustained_hold=True)
        assert result is None
        assert ("key_release", NOSE_UP_KEY) in _keys(ctrl)
        assert ctrl._pitch_held is None

    def test_error_flipping_sign_releases_old_and_presses_new(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch)
        ctrl.orient_pitch_to_target(-0.3, sustained_hold=True)  # up
        result = ctrl.orient_pitch_to_target(0.3, sustained_hold=True)  # down
        assert result == "down"
        keys = _keys(ctrl)
        assert keys.index(("key_release", NOSE_UP_KEY)) < keys.index(("key_press", NOSE_DOWN_KEY))


# ---------------------------------------------------------------------------
# Search hold (roll-only, operator directive 2026-09-23): no target holds
# ROLL_LEFT_KEY, and reacquisition is always a fresh decision
# ---------------------------------------------------------------------------

class TestRollSearchHold:
    def test_engage_search_holds_roll_left(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch)
        ctrl.engage_roll_search()
        assert ("key_press", ROLL_LEFT_KEY) in _keys(ctrl)
        assert ctrl._roll_held == "left"
        assert ctrl._roll_hold_reason == "search"

    def test_repeated_search_ticks_do_not_repress(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch)
        for _ in range(5):
            ctrl.engage_roll_search()
        presses = [k for k in _keys(ctrl) if k == ("key_press", ROLL_LEFT_KEY)]
        assert len(presses) == 1

    def test_search_from_a_right_target_hold_releases_right_first(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch)
        ctrl.orient_nose_to_target(0.3, sustained_hold=True)  # right, reason="target"
        ctrl.engage_roll_search()
        keys = _keys(ctrl)
        assert ("key_release", ROLL_RIGHT_KEY) in keys
        assert ("key_press", ROLL_LEFT_KEY) in keys
        assert ctrl._roll_held == "left"
        assert ctrl._roll_hold_reason == "search"

    def test_reacquisition_with_same_direction_is_still_a_fresh_decision(self, monkeypatch):
        """The one case a naive "already holding the right key" check gets
        wrong: search holds left, and the newly-acquired target is also to
        the left. Must still release-then-press, and the reason must flip to
        "target" — never a silent no-op that leaves the reason at "search"."""
        ctrl = _make_ctrl(monkeypatch)
        ctrl.engage_roll_search()
        assert ctrl._roll_hold_reason == "search"
        result = ctrl.orient_nose_to_target(-0.3, sustained_hold=True)  # left again
        assert result == "left"
        keys = _keys(ctrl)
        press_count = len([k for k in keys if k == ("key_press", ROLL_LEFT_KEY)])
        release_count = len([k for k in keys if k == ("key_release", ROLL_LEFT_KEY)])
        assert press_count == 2, "must press again, not skip as a no-op"
        assert release_count == 1, "must release the search hold before the fresh press"
        assert ctrl._roll_hold_reason == "target"

    def test_reacquisition_with_opposite_direction_switches_normally(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch)
        ctrl.engage_roll_search()  # left
        result = ctrl.orient_nose_to_target(0.3, sustained_hold=True)  # right
        assert result == "right"
        keys = _keys(ctrl)
        assert ("key_release", ROLL_LEFT_KEY) in keys
        assert ("key_press", ROLL_RIGHT_KEY) in keys
        assert ctrl._roll_hold_reason == "target"


# ---------------------------------------------------------------------------
# Release paths: the hazard a held key introduces that a self-expiring tap
# never had
# ---------------------------------------------------------------------------

class TestReleasePaths:
    def test_release_roll_hold_is_a_noop_when_nothing_held(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch)
        ctrl.release_roll_hold()  # must not raise
        assert ("key_press", ROLL_LEFT_KEY) not in _keys(ctrl)

    def test_release_pitch_hold_is_a_noop_when_nothing_held(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch)
        ctrl.release_pitch_hold()
        assert ("key_press", NOSE_UP_KEY) not in _keys(ctrl)

    def test_release_tracking_holds_releases_both_axes(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch)
        ctrl.orient_nose_to_target(-0.3, sustained_hold=True)
        ctrl.orient_pitch_to_target(0.3, sustained_hold=True)
        ctrl.release_tracking_holds()
        keys = _keys(ctrl)
        assert ("key_release", ROLL_LEFT_KEY) in keys
        assert ("key_release", NOSE_DOWN_KEY) in keys
        assert ctrl._roll_held is None
        assert ctrl._pitch_held is None
        assert ctrl._roll_hold_reason is None

    def test_cancel_mission_releases_a_held_key(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch)
        ctrl.orient_nose_to_target(-0.3, sustained_hold=True)
        ctrl.cancel_mission()
        assert ("key_release", ROLL_LEFT_KEY) in _keys(ctrl)
        assert ctrl._roll_held is None

    def test_manual_takeover_releases_a_held_key_and_clears_state(self, monkeypatch):
        """The blanket INJECTABLE_KEYS release in release_for_manual_takeover
        physically lets go of every key at the OS level, but (simulate mode
        aside) does not know about _roll_held/_roll_hold_reason/_pitch_held —
        without an explicit release, that Python-level state would claim a
        key is still held after the X server has already released it."""
        ctrl = _make_ctrl(monkeypatch)
        ctrl.orient_nose_to_target(0.3, sustained_hold=True)
        ctrl.orient_pitch_to_target(-0.3, sustained_hold=True)
        ctrl.release_for_manual_takeover()
        assert ctrl._roll_held is None
        assert ctrl._roll_hold_reason is None
        assert ctrl._pitch_held is None
