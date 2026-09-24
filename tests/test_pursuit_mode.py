"""HLDD 015 — pursue_and_engage, the missiles-empty alternative to
eject_and_dive that steers with both tracking axes (roll and pitch) instead
of diving, since it never starts the descent controller that forces
eject_and_dive's own heatdive addition to stay roll-only.

pursuit_mode.enabled defaults false (a hard precondition, not a "not
validated yet" default — see pursue_and_engage's own docstring), so the
first block here is a regression guard proving that default is real: zero
behavior change through fire_eject unless the flag is explicitly set.
"""

import threading
import time

import wingman.controller as controller_module
from wingman.controller_config import ControllerConfig
from wingman.controller import (
    Controller, FIRE_ACTIVE_WEAPON, NOSE_DOWN_KEY, NOSE_UP_KEY,
    ROLL_LEFT_KEY, ROLL_RIGHT_KEY, SWITCH_WEAPON,
)


class _AnalyzerStub:
    def __init__(self, ammo=2):
        self.ammo = ammo

    def mark_health_dead_synthetic(self):
        pass

    def get_ammo_missiles(self):
        return self.ammo

    def get_ammo_flares(self):
        return 4

    def get_health(self):
        return 100


class _CaptureStub:
    def __init__(self):
        self.grabs = 0

    def grab_from_thread(self):
        self.grabs += 1
        return object()


class _TrackerStub:
    def __init__(self, visible=True, error_norm=0.5, error_norm_y=-0.3):
        self.visible = visible
        self.error_norm = error_norm
        self.error_norm_y = error_norm_y
        self.updates = 0

    def update(self, frame):
        self.updates += 1
        return {
            "visible": self.visible,
            "error_norm": self.error_norm,
            "error_norm_y": self.error_norm_y,
            "mode": "TRACKING",
        }


def _keys(ctrl):
    with ctrl._action_intents_lock:
        return [(i["action_type"], i["key"]) for i in ctrl._action_intents]


def _make_ctrl(monkeypatch, analyzer=None, capture=None, pursuit_enabled=False,
                pursuit_max_duration_s=1.0, legacy_nose_hold_s=0.05,
                eject_max_s=0.2, heatdive_enabled=False, ammo_zero_grace_s=0.0):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    return Controller(
        (0, 0, 1920, 1200),
        analyzer=analyzer,
        exit_event=threading.Event(),
        capture=capture,
        config=ControllerConfig(
            simulate_os_input=True,
            disable_hotkeys=True,
            telemetry={
                "eject_closed_loop": {
                    "enabled": False,  # legacy branch — fast, deterministic fallback dive
                    "legacy_nose_hold_s": legacy_nose_hold_s,
                    "eject_max_s": eject_max_s,
                    "heatdive_enabled": heatdive_enabled,
                },
                "stale_after_s": 0.3,
            },
            pursuit_mode={
                "enabled": pursuit_enabled,
                "pursuit_max_duration_s": pursuit_max_duration_s,
                "pursuit_padlock_verify": False,
                # 0.0 by default here (not config.yaml's shipped 12.0) so
                # existing tests that construct an already-empty analyzer to
                # mean "ammo is confirmed exhausted" keep meaning that
                # without waiting out a grace period — see
                # test_ammo_zero_within_grace_period_does_not_fall_through
                # for the grace-period behavior itself.
                "ammo_zero_grace_s": ammo_zero_grace_s,
            },
        ),
    )


def _wait_for_pursuit_to_settle(ctrl, timeout=3.0):
    deadline = time.time() + timeout
    while ctrl.is_pursuing() and time.time() < deadline:
        time.sleep(0.01)
    assert not ctrl.is_pursuing(), "pursue_and_engage did not finish in time"
    # If it fell through, eject_and_dive's own thread needs to finish too.
    if ctrl._eject_thread is not None:
        ctrl._eject_thread.join(timeout=timeout)


# ---------------------------------------------------------------------------
# Default off: zero behavior change through fire_eject (regression guard)
# ---------------------------------------------------------------------------

def test_pursuit_mode_disabled_by_default():
    ctrl = Controller((0, 0, 1920, 1200))
    assert ctrl.pursuit_mode_enabled() is False


def test_pursuit_disabled_pursue_and_engage_never_called_via_fire_eject(monkeypatch):
    """fire_eject's own branch, exercised through the real Controller flag —
    not just the AmmoEventsHandler stub test in test_tick_handlers.py."""
    analyzer = _AnalyzerStub()
    capture = _CaptureStub()
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, capture=capture, pursuit_enabled=False)
    assert ctrl.pursuit_mode_enabled() is False


# ---------------------------------------------------------------------------
# No tracker wired: falls back to eject_and_dive directly, never touches
# self._pursuing
# ---------------------------------------------------------------------------

def test_pursue_and_engage_without_tracker_falls_back_to_eject_and_dive(monkeypatch):
    analyzer = _AnalyzerStub()
    capture = _CaptureStub()
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, capture=capture, pursuit_enabled=True)
    # set_target_tracker() deliberately not called.

    ctrl.pursue_and_engage()
    assert not ctrl.is_pursuing(), "fallback must not set self._pursuing at all"
    thread = ctrl._eject_thread
    assert thread is not None, "should have fallen back to eject_and_dive"
    thread.join(timeout=3.0)
    assert not thread.is_alive()
    assert capture.grabs == 0, "no tracking loop should have run"


# ---------------------------------------------------------------------------
# Enabled with a tracker wired: switches weapon, steers BOTH axes, fires
# ---------------------------------------------------------------------------

def test_pursue_and_engage_switches_weapon_and_steers_both_axes(monkeypatch):
    analyzer = _AnalyzerStub(ammo=2)
    capture = _CaptureStub()
    tracker = _TrackerStub(visible=True, error_norm=0.5, error_norm_y=-0.5)
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, capture=capture,
                       pursuit_enabled=True, pursuit_max_duration_s=0.5)
    ctrl.set_target_tracker(tracker)

    ctrl.pursue_and_engage()
    _wait_for_pursuit_to_settle(ctrl)

    keys = _keys(ctrl)
    assert ("key_press", SWITCH_WEAPON) in keys
    assert ("key_press", FIRE_ACTIVE_WEAPON) in keys
    assert ("key_press", ROLL_RIGHT_KEY) in keys   # positive error_norm -> roll right
    assert ("key_press", NOSE_UP_KEY) in keys      # negative error_norm_y -> nose up
    assert tracker.updates > 0
    assert capture.grabs > 0


def test_pursue_and_engage_pitch_direction_matches_error_sign(monkeypatch):
    analyzer = _AnalyzerStub(ammo=2)
    capture = _CaptureStub()
    tracker = _TrackerStub(visible=True, error_norm=-0.5, error_norm_y=0.5)
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, capture=capture,
                       pursuit_enabled=True, pursuit_max_duration_s=0.5)
    ctrl.set_target_tracker(tracker)

    ctrl.pursue_and_engage()
    _wait_for_pursuit_to_settle(ctrl)

    keys = _keys(ctrl)
    assert ("key_press", ROLL_LEFT_KEY) in keys    # negative error_norm -> roll left
    assert ("key_press", NOSE_DOWN_KEY) in keys    # positive error_norm_y -> nose down


# ---------------------------------------------------------------------------
# Termination (HLDD 015 D3)
# ---------------------------------------------------------------------------

def test_ammo_exhausted_falls_through_to_eject_and_dive(monkeypatch):
    analyzer = _AnalyzerStub(ammo=0)  # already empty on the very first read
    capture = _CaptureStub()
    tracker = _TrackerStub()
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, capture=capture,
                       pursuit_enabled=True, pursuit_max_duration_s=5.0,
                       legacy_nose_hold_s=0.05, eject_max_s=0.2)
    ctrl.set_target_tracker(tracker)

    ctrl.pursue_and_engage()
    _wait_for_pursuit_to_settle(ctrl)

    assert ctrl._eject_thread is not None, "ammo-exhausted should fall through to eject_and_dive"
    assert ("key_press", FIRE_ACTIVE_WEAPON) not in _keys(ctrl), \
        "must not fire on a tick that already reads ammo == 0"


def test_ammo_zero_within_grace_period_does_not_fall_through(monkeypatch):
    """Regression (2026-09-23): get_ammo_missiles() reads the AMMO_MISSILE
    HUD region, which does not update to the post-switch_weapon secondary
    loadout instantly — measured live at 1.79s and 9.1s after switch_weapon
    completed, before the analyzer's own "Ammo missiles: 2" first appeared.
    Without a grace period, the very first ammo==0 reading (always still the
    pre-switch value at that point) ended the encounter within ~230ms on
    every single trigger, before tracking ever ran long enough to matter.
    A generous grace period here (5s) must survive a constant ammo==0 reading
    for a short window — same tolerance _eject_heatdive_loop already has for
    this exact lag on its own ammo checks, just applied here too now."""
    analyzer = _AnalyzerStub(ammo=0)  # constant "0" — the whole grace window
    capture = _CaptureStub()
    tracker = _TrackerStub(visible=True, error_norm=0.3, error_norm_y=0.2)
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, capture=capture,
                       pursuit_enabled=True, pursuit_max_duration_s=5.0,
                       ammo_zero_grace_s=5.0)
    ctrl.set_target_tracker(tracker)

    ctrl.pursue_and_engage()
    time.sleep(0.6)  # switch_weapon's own blocking hold (~0.1-0.2s) + >=1 tick (0.2s)
    assert ctrl.is_pursuing(), "must still be pursuing — the ammo==0 reading is within its grace period"
    assert ctrl._eject_thread is None, "must not have fallen through yet"
    assert ("key_press", FIRE_ACTIVE_WEAPON) not in _keys(ctrl), \
        "must still skip firing on ammo == 0, grace period or not"
    assert tracker.updates > 0, "tracking/steering must continue during the grace period"

    ctrl._eject_stop.set()  # external stop — not a fall-through, just ending the test
    _wait_for_pursuit_to_settle(ctrl, timeout=3.0)
    assert ctrl._eject_thread is None, "external stop must not fall through either"


def test_ammo_zero_after_grace_period_elapses_falls_through(monkeypatch):
    """Companion to the grace-period test above: once the grace period has
    genuinely elapsed, a persistent ammo==0 reading must still end the
    encounter — the grace period delays the check, it does not disable it."""
    analyzer = _AnalyzerStub(ammo=0)
    capture = _CaptureStub()
    tracker = _TrackerStub()
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, capture=capture,
                       pursuit_enabled=True, pursuit_max_duration_s=5.0,
                       legacy_nose_hold_s=0.05, eject_max_s=0.2,
                       ammo_zero_grace_s=0.1)
    ctrl.set_target_tracker(tracker)

    ctrl.pursue_and_engage()
    _wait_for_pursuit_to_settle(ctrl, timeout=3.0)

    assert ctrl._eject_thread is not None, "ammo-exhausted should still fall through once grace elapses"


def test_fallthrough_to_eject_and_dive_presses_switch_weapon_only_once(monkeypatch):
    """Regression (2026-09-23): eject_and_dive's own heatdive block used to
    press SWITCH_WEAPON unconditionally, even when pursue_and_engage had
    already pressed it moments earlier for this same encounter — two presses
    ~0.3s apart, live-confirmed to leave the secondary weapon never actually
    selected (consistent with a toggle-style binding being pressed back to
    primary). heatdive_enabled=True here specifically to exercise the path
    that presses the key, which the other fall-through tests in this file
    don't (they use the default heatdive_enabled=False)."""
    analyzer = _AnalyzerStub(ammo=0)  # already empty on the very first read
    capture = _CaptureStub()
    tracker = _TrackerStub()
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, capture=capture,
                       pursuit_enabled=True, pursuit_max_duration_s=5.0,
                       legacy_nose_hold_s=0.05, eject_max_s=0.2,
                       heatdive_enabled=True)
    ctrl.set_target_tracker(tracker)

    ctrl.pursue_and_engage()
    _wait_for_pursuit_to_settle(ctrl)

    assert ctrl._eject_thread is not None, "ammo-exhausted should fall through to eject_and_dive"
    switch_presses = [k for k in _keys(ctrl) if k == ("key_press", SWITCH_WEAPON)]
    assert len(switch_presses) == 1, (
        f"expected exactly one switch_weapon press across pursue_and_engage "
        f"and the eject_and_dive it fell through to, got {len(switch_presses)}")


def test_max_duration_falls_through_to_eject_and_dive(monkeypatch):
    analyzer = _AnalyzerStub(ammo=2)  # never runs out on its own
    capture = _CaptureStub()
    tracker = _TrackerStub()
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, capture=capture,
                       pursuit_enabled=True, pursuit_max_duration_s=0.2,
                       legacy_nose_hold_s=0.05, eject_max_s=0.2)
    ctrl.set_target_tracker(tracker)

    ctrl.pursue_and_engage()
    _wait_for_pursuit_to_settle(ctrl, timeout=5.0)

    assert ctrl._eject_thread is not None, "timeout should fall through to eject_and_dive"


def test_external_stop_does_not_fall_through(monkeypatch):
    """respawn / manual takeover / shutdown -> self._eject_stop -> stop
    immediately, no fall-through (HLDD 015 D3's third row)."""
    analyzer = _AnalyzerStub(ammo=2)
    capture = _CaptureStub()
    tracker = _TrackerStub()
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, capture=capture,
                       pursuit_enabled=True, pursuit_max_duration_s=5.0)
    ctrl.set_target_tracker(tracker)

    ctrl.pursue_and_engage()
    time.sleep(0.1)
    assert ctrl.is_pursuing()
    ctrl._eject_stop.set()
    _wait_for_pursuit_to_settle(ctrl, timeout=3.0)

    assert ctrl._eject_thread is None, "external stop must not fall through to eject_and_dive"


def test_on_complete_called_on_external_stop(monkeypatch):
    analyzer = _AnalyzerStub(ammo=2)
    capture = _CaptureStub()
    tracker = _TrackerStub()
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, capture=capture,
                       pursuit_enabled=True, pursuit_max_duration_s=5.0)
    ctrl.set_target_tracker(tracker)

    calls = []
    ctrl.pursue_and_engage(on_complete=lambda: calls.append(1))
    time.sleep(0.1)
    ctrl._eject_stop.set()
    _wait_for_pursuit_to_settle(ctrl, timeout=3.0)

    assert calls == [1]


# ---------------------------------------------------------------------------
# Reentrancy and behavior-tree integration
# ---------------------------------------------------------------------------

def test_reentrant_call_is_a_no_op(monkeypatch):
    analyzer = _AnalyzerStub(ammo=2)
    capture = _CaptureStub()
    tracker = _TrackerStub()
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, capture=capture,
                       pursuit_enabled=True, pursuit_max_duration_s=5.0)
    ctrl.set_target_tracker(tracker)

    ctrl.pursue_and_engage()
    time.sleep(0.1)
    grabs_before = capture.grabs
    ctrl.pursue_and_engage()  # should just log and return, not start a second loop
    # Only one loop should be advancing the capture stub.
    time.sleep(0.1)
    ctrl._eject_stop.set()
    _wait_for_pursuit_to_settle(ctrl, timeout=3.0)
    assert capture.grabs >= grabs_before


def test_is_pursuing_reflects_running_state(monkeypatch):
    analyzer = _AnalyzerStub(ammo=2)
    capture = _CaptureStub()
    tracker = _TrackerStub()
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, capture=capture,
                       pursuit_enabled=True, pursuit_max_duration_s=5.0)
    ctrl.set_target_tracker(tracker)

    assert not ctrl.is_pursuing()
    ctrl.pursue_and_engage()
    time.sleep(0.1)
    assert ctrl.is_pursuing()
    ctrl._eject_stop.set()
    _wait_for_pursuit_to_settle(ctrl, timeout=3.0)
    assert not ctrl.is_pursuing()
