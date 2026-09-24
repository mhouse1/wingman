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


class _ScriptedTracker:
    """Returns one scripted observation per update(), then repeats the last."""

    def __init__(self, script):
        self.script = list(script)
        self.updates = 0

    def update(self, frame):
        obs = self.script[min(self.updates, len(self.script) - 1)]
        self.updates += 1
        return dict(obs)


_SEEN = {"visible": True, "error_norm": 0.5, "error_norm_y": 0.0, "mode": "TRACKING"}
_MISS = {"visible": False, "error_norm": 0.5, "error_norm_y": 0.0, "mode": "LOST_GRACE"}


def _keys(ctrl):
    with ctrl._action_intents_lock:
        return [(i["action_type"], i["key"]) for i in ctrl._action_intents]


def _make_ctrl(monkeypatch, analyzer=None, capture=None, pursuit_enabled=False,
                pursuit_max_duration_s=1.0, legacy_nose_hold_s=0.05,
                eject_max_s=0.2, heatdive_enabled=False, ammo_zero_grace_s=0.0,
                sustained_hold_enabled=False, search_resume_delay_s=0.0,
                empty_confirm_reads=3, search_resume_centre_err=0.15,
                search_resume_centre_delay_s=0.0):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    return Controller(
        (0, 0, 1920, 1200),
        analyzer=analyzer,
        exit_event=threading.Event(),
        capture=capture,
        config=ControllerConfig(
            simulate_os_input=True,
            disable_hotkeys=True,
            tracking={"sustained_hold_enabled": sustained_hold_enabled},
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
                # 0.0 by default here (not config.yaml's shipped 2.0): the
                # pre-2026-09-24 behavior. The delay's own tests below set it.
                "search_resume_delay_s": search_resume_delay_s,
                # 0.0 delay by default here: the near-centre extension is off
                # unless a test sets it.
                "search_resume_centre_err": search_resume_centre_err,
                "search_resume_centre_delay_s": search_resume_centre_delay_s,
                "empty_confirm_reads": empty_confirm_reads,
            },
        ),
    )


def _wait_for_pursuit_to_settle(ctrl, timeout=3.0):
    deadline = time.time() + timeout
    while ctrl.is_pursuing() and time.time() < deadline:
        time.sleep(0.01)
    assert not ctrl.is_pursuing(), "pursue_and_engage did not finish in time"
    # If it fell through, eject_and_dive's own thread needs to finish too.
    # _pursuing is cleared BEFORE eject_and_dive builds and start()s that
    # thread, so join() can land on a Thread that exists but has not started
    # yet (RuntimeError, seen once in a 45-test run) — retry until it has.
    if ctrl._eject_thread is not None:
        join_deadline = time.time() + timeout
        while True:
            try:
                ctrl._eject_thread.join(timeout=timeout)
                break
            except RuntimeError:
                assert time.time() < join_deadline, "eject thread never started"
                time.sleep(0.005)


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


def test_weapon_already_switched_skips_the_switch_and_keeps_the_flag(monkeypatch):
    """mission_su30 (ADR 144) switches to the secondary weapon at its step 2 and
    then activates pursuit. SWITCH_WEAPON is a toggle, so pursue_and_engage must
    not press it again — and must not reset the flag that records the first
    press, or is_secondary_weapon_active() would read False for the whole
    pursuit."""
    analyzer = _AnalyzerStub(ammo=2)
    capture = _CaptureStub()
    tracker = _TrackerStub()
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, capture=capture,
                       pursuit_enabled=True, pursuit_max_duration_s=0.5)
    ctrl.set_target_tracker(tracker)
    ctrl._eject_weapon_switched = True

    ctrl.pursue_and_engage(weapon_already_switched=True)
    _wait_for_pursuit_to_settle(ctrl)

    assert ("key_press", SWITCH_WEAPON) not in _keys(ctrl)
    assert ("key_press", FIRE_ACTIVE_WEAPON) in _keys(ctrl), "pursuit must still fire"
    assert tracker.updates > 0


def test_default_still_switches_weapon(monkeypatch):
    """The missiles-empty path (no argument) is unchanged by ADR 144."""
    analyzer = _AnalyzerStub(ammo=2)
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, capture=_CaptureStub(),
                       pursuit_enabled=True, pursuit_max_duration_s=0.3)
    ctrl.set_target_tracker(_TrackerStub())

    ctrl.pursue_and_engage()
    _wait_for_pursuit_to_settle(ctrl)

    assert ("key_press", SWITCH_WEAPON) in _keys(ctrl)


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


# ---------------------------------------------------------------------------
# Action item 001 (2026-09-24): search rotation must not resume during the
# grace window after a lock. wingman.log 2026-09-24 06:03:38-39: lock at
# 38.251, two missed ROI scans, ROLL_LEFT re-pressed on both, target went
# from screen centre to err=+0.366 before the next lock.
# ---------------------------------------------------------------------------

def _run_scripted_pursuit(monkeypatch, script, search_resume_delay_s):
    analyzer = _AnalyzerStub(ammo=2)
    capture = _CaptureStub()
    tracker = _ScriptedTracker(script)
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, capture=capture,
                       pursuit_enabled=True, pursuit_max_duration_s=0.9,
                       sustained_hold_enabled=True,
                       search_resume_delay_s=search_resume_delay_s)
    ctrl.set_target_tracker(tracker)
    ctrl.pursue_and_engage()
    _wait_for_pursuit_to_settle(ctrl)
    assert tracker.updates >= 3, "the script needs at least a lock and two misses"
    return _keys(ctrl)


def test_miss_after_a_lock_does_not_resume_the_left_search(monkeypatch):
    keys = _run_scripted_pursuit(monkeypatch, [_SEEN, _MISS], search_resume_delay_s=30.0)
    assert ("key_press", ROLL_RIGHT_KEY) in keys        # the lock itself steered right
    assert ("key_release", ROLL_RIGHT_KEY) in keys      # the miss released it
    assert ("key_press", ROLL_LEFT_KEY) not in keys     # and did NOT start searching


def test_zero_delay_still_resumes_the_left_search_on_the_first_miss(monkeypatch):
    keys = _run_scripted_pursuit(monkeypatch, [_SEEN, _MISS], search_resume_delay_s=0.0)
    assert ("key_press", ROLL_LEFT_KEY) in keys


def test_never_seen_still_searches_left_from_the_first_tick(monkeypatch):
    keys = _run_scripted_pursuit(monkeypatch, [_MISS], search_resume_delay_s=30.0)
    assert ("key_press", ROLL_LEFT_KEY) in keys


# ---------------------------------------------------------------------------
# mission_su30's deferred weapon switch (ADR 144 D4, 2026-09-24): pursue with
# whatever is selected, press SWITCH_WEAPON only once it has read empty several
# cycles running. The key is a toggle, so a misread 0 must never press it.
# ---------------------------------------------------------------------------

class _SequenceAnalyzer(_AnalyzerStub):
    """get_ammo_missiles() walks a script, then repeats its last entry."""

    def __init__(self, ammos):
        super().__init__()
        self._ammos = list(ammos)
        self._n = 0

    def get_ammo_missiles(self):
        v = self._ammos[min(self._n, len(self._ammos) - 1)]
        self._n += 1
        return v


def _run_deferred_pursuit(monkeypatch, analyzer, *, confirm=2, max_s=0.9,
                          ammo_grace=30.0, heatdive=False):
    capture = _CaptureStub()
    tracker = _TrackerStub()
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, capture=capture,
                       pursuit_enabled=True, pursuit_max_duration_s=max_s,
                       ammo_zero_grace_s=ammo_grace, empty_confirm_reads=confirm,
                       heatdive_enabled=heatdive)
    ctrl.set_target_tracker(tracker)
    ctrl.pursue_and_engage(defer_switch_until_empty=True)
    _wait_for_pursuit_to_settle(ctrl)
    return ctrl, _keys(ctrl)


def _switch_presses(keys):
    return [k for k in keys if k == ("key_press", SWITCH_WEAPON)]


def test_deferred_switch_presses_nothing_while_the_weapon_has_ammo(monkeypatch):
    ctrl, keys = _run_deferred_pursuit(monkeypatch, _AnalyzerStub(ammo=2))
    assert _switch_presses(keys) == []
    assert ("key_press", FIRE_ACTIVE_WEAPON) in keys
    assert not ctrl.is_secondary_weapon_active()


def test_deferred_switch_presses_once_after_consecutive_zero_reads(monkeypatch):
    ctrl, keys = _run_deferred_pursuit(monkeypatch, _AnalyzerStub(ammo=0), confirm=2)
    assert len(_switch_presses(keys)) == 1
    assert ctrl.is_secondary_weapon_active()


def test_deferred_switch_ignores_a_zero_that_is_not_consecutive(monkeypatch):
    """0, 2, 0, 2 ... never reaches two zeros in a row: a flicker, not an empty
    rack, and swapping away a rack that still has missiles is the failure."""
    ctrl, keys = _run_deferred_pursuit(
        monkeypatch, _SequenceAnalyzer([0, 2, 0, 2, 0, 2]), confirm=2)
    assert _switch_presses(keys) == []
    assert not ctrl.is_secondary_weapon_active()


def test_deferred_switch_ignores_unreadable_ammo(monkeypatch):
    """None (OCR found no digits) is not evidence of an empty rack."""
    ctrl, keys = _run_deferred_pursuit(monkeypatch, _AnalyzerStub(ammo=None), confirm=1)
    assert _switch_presses(keys) == []
    assert ("key_press", FIRE_ACTIVE_WEAPON) in keys, "unreadable must still fail open"


def test_deferred_switch_does_not_end_the_encounter_before_the_switch(monkeypatch, caplog):
    """The ammo==0 fall-through is for the SECONDARY running out. While the
    original weapon is still selected and being confirmed empty it must not
    fire, even with no grace period at all."""
    import logging
    caplog.set_level(logging.INFO, logger="wingman.controller")
    _run_deferred_pursuit(monkeypatch, _SequenceAnalyzer([0, 0, 2, 2, 2]),
                          confirm=3, ammo_grace=0.0)
    assert "ammo exhausted" not in caplog.text


def test_deferred_switch_then_falls_through_only_after_the_switch(monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="wingman.controller")
    ctrl, keys = _run_deferred_pursuit(
        monkeypatch, _AnalyzerStub(ammo=0), confirm=2, ammo_grace=0.0, max_s=5.0)
    text = caplog.text
    assert "switching to the secondary" in text
    assert "ammo exhausted, falling through" in text
    assert text.index("switching to the secondary") < text.index("ammo exhausted")
    assert len(_switch_presses(keys)) == 1, "eject_and_dive must not press it again"


def test_deferred_switch_grace_is_measured_from_the_switch(monkeypatch, caplog):
    """The HUD count lags a switch by seconds (config.yaml, ammo_zero_grace_s),
    so the first 0 after the switch is the old rack's, not an empty secondary.
    A grace longer than the run means the encounter is not ended by it."""
    import logging
    caplog.set_level(logging.INFO, logger="wingman.controller")
    _run_deferred_pursuit(monkeypatch, _AnalyzerStub(ammo=0), confirm=2,
                          ammo_grace=30.0, max_s=0.9)
    assert "switching to the secondary" in caplog.text
    assert "ammo exhausted" not in caplog.text
    assert "max duration" in caplog.text


def test_max_duration_before_empty_does_not_switch_weapons(monkeypatch):
    """Regression (operator, 2026-09-24, 'v' screenshot 08:20:14): the pursuit
    cap fell through to eject_and_dive with the primary still loaded (6/6) and
    the dive pressed SWITCH_WEAPON for its heatdive — five such presses in one
    log. The dive now inherits the deferral: nothing is pressed while the
    selected weapon still has ammo, and the flag still says nothing was pressed.
    (This used to assert exactly one press, eject_and_dive's own.)"""
    ctrl, keys = _run_deferred_pursuit(
        monkeypatch, _AnalyzerStub(ammo=2), max_s=0.5, heatdive=True)
    assert _switch_presses(keys) == [], "no switch while the primary has ammo"
    assert not ctrl.is_secondary_weapon_active()
    assert ("key_press", FIRE_ACTIVE_WEAPON) in keys, "the dive still fires the loaded weapon"


def test_cap_fallthrough_after_the_switch_does_not_press_it_again(monkeypatch):
    """Once the weapon ran out and pursuit switched, the fall-through must tell
    the dive the switch is done — the toggle cannot be pressed twice."""
    ctrl, keys = _run_deferred_pursuit(
        monkeypatch, _AnalyzerStub(ammo=0), confirm=2, ammo_grace=30.0,
        max_s=0.9, heatdive=True)
    assert len(_switch_presses(keys)) == 1


def test_a_miss_after_a_near_centre_lock_stays_neutral_past_the_base_delay(monkeypatch):
    """Operator, 2026-09-24: locked on target, then forced left turn. The base
    delay here is 0, so only the near-centre extension can keep the roll axis
    neutral — err 0.05 is well inside search_resume_centre_err."""
    near = {"visible": True, "error_norm": 0.05, "error_norm_y": 0.0, "mode": "TRACKING"}
    analyzer = _AnalyzerStub(ammo=2)
    tracker = _ScriptedTracker([near, _MISS])
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, capture=_CaptureStub(),
                       pursuit_enabled=True, pursuit_max_duration_s=0.9,
                       sustained_hold_enabled=True, search_resume_delay_s=0.0,
                       search_resume_centre_delay_s=30.0)
    ctrl.set_target_tracker(tracker)
    ctrl.pursue_and_engage()
    _wait_for_pursuit_to_settle(ctrl)
    assert tracker.updates >= 3
    assert ("key_press", ROLL_LEFT_KEY) not in _keys(ctrl)


def test_a_miss_after_a_far_from_centre_lock_still_resumes_the_search(monkeypatch):
    """The extension is for a target the aircraft is already pointing at; one
    last seen far off to the side does not get the longer hold."""
    far = {"visible": True, "error_norm": 0.6, "error_norm_y": 0.0, "mode": "TRACKING"}
    tracker = _ScriptedTracker([far, _MISS])
    ctrl = _make_ctrl(monkeypatch, analyzer=_AnalyzerStub(ammo=2), capture=_CaptureStub(),
                       pursuit_enabled=True, pursuit_max_duration_s=0.9,
                       sustained_hold_enabled=True, search_resume_delay_s=0.0,
                       search_resume_centre_delay_s=30.0)
    ctrl.set_target_tracker(tracker)
    ctrl.pursue_and_engage()
    _wait_for_pursuit_to_settle(ctrl)
    assert ("key_press", ROLL_LEFT_KEY) in _keys(ctrl)


def test_default_pursuit_still_switches_at_its_start(monkeypatch):
    """The missiles-empty path (no deferral) is unchanged."""
    analyzer = _AnalyzerStub(ammo=2)
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, capture=_CaptureStub(),
                       pursuit_enabled=True, pursuit_max_duration_s=0.4)
    ctrl.set_target_tracker(_TrackerStub())
    ctrl.pursue_and_engage()
    _wait_for_pursuit_to_settle(ctrl)
    assert len(_switch_presses(_keys(ctrl))) == 1
    assert ctrl.is_secondary_weapon_active()


# ---------------------------------------------------------------------------
# Action item 001, Cycle 7 (2026-09-24): one INFO summary line per pursuit
# ---------------------------------------------------------------------------

def _summary_lines(caplog, kind):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith(kind + " SUMMARY:")]


def test_pursuit_logs_one_summary_line_at_the_cap(monkeypatch, caplog):
    """The pursuit's outcome used to be rebuilt from DEBUG TRACKPICK lines and
    ammo transitions (a rack switch read as a 6-to-2 'launch'). One INFO line
    now carries end reason, duration, scans, locked scans, time to first lock
    and the ammo reading at both ends."""
    tracker = _ScriptedTracker([_MISS, _SEEN])      # one scan is about 0.35 s here
    ctrl = _make_ctrl(monkeypatch, analyzer=_AnalyzerStub(ammo=2), capture=_CaptureStub(),
                       pursuit_enabled=True, pursuit_max_duration_s=0.9)
    ctrl.set_target_tracker(tracker)
    with caplog.at_level("INFO", logger="wingman.controller"):
        ctrl.pursue_and_engage(defer_switch_until_empty=True)
        _wait_for_pursuit_to_settle(ctrl)
    lines = _summary_lines(caplog, "PURSUIT")
    assert len(lines) == 1, lines
    line = lines[0]
    assert "end=cap" in line
    assert "ammo=2->2" in line
    assert "switched=no" in line
    assert "first_lock=-" not in line, "the script is locked from the second scan on"
    assert "locked=0 " not in line


def test_pursuit_summary_says_ammo_when_it_ended_on_an_empty_rack(monkeypatch, caplog):
    ctrl = _make_ctrl(monkeypatch, analyzer=_AnalyzerStub(ammo=0), capture=_CaptureStub(),
                       pursuit_enabled=True, pursuit_max_duration_s=5.0)
    ctrl.set_target_tracker(_TrackerStub())
    with caplog.at_level("INFO", logger="wingman.controller"):
        ctrl.pursue_and_engage()
        _wait_for_pursuit_to_settle(ctrl)
    (line,) = _summary_lines(caplog, "PURSUIT")
    assert "end=ammo" in line
    assert "ammo=0->0" in line
    assert "switched=yes" in line, "the non-deferred pursuit pressed the switch itself"


def test_pursuit_summary_names_an_external_stop(monkeypatch, caplog):
    ctrl = _make_ctrl(monkeypatch, analyzer=_AnalyzerStub(ammo=2), capture=_CaptureStub(),
                       pursuit_enabled=True, pursuit_max_duration_s=30.0)
    ctrl.set_target_tracker(_TrackerStub())
    with caplog.at_level("INFO", logger="wingman.controller"):
        ctrl.pursue_and_engage(defer_switch_until_empty=True)
        time.sleep(0.5)
        ctrl.stop_eject_sequence("respawn_detected")
        _wait_for_pursuit_to_settle(ctrl)
    (line,) = _summary_lines(caplog, "PURSUIT")
    assert "end=external:respawn_detected" in line
