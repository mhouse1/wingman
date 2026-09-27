"""ADR 136 — heat-seeker switch, tracking-guided roll, and fire during the
eject dive.

Controller._eject_heatdive_loop is exercised directly for its firing/ammo
logic; eject_and_dive() end-to-end confirms the start/stop wiring using the
fast legacy (non-closed-loop) branch so timing does not depend on the full
descent-control state machine (already covered by test_eject_closed_loop.py).
"""

import threading
import time

import wingman.controller as controller_module
from wingman.controller_config import ControllerConfig
from wingman.controller import (
    Controller, FIRE_ACTIVE_WEAPON, PADLOCK_CAMERA, ROLL_LEFT_KEY, ROLL_RIGHT_KEY,
    SWITCH_WEAPON,
)


class _AnalyzerStub:
    def __init__(self, ammo=2):
        self.ammo = ammo

    def mark_health_dead_synthetic(self):
        pass

    def get_ammo_missiles(self):
        return self.ammo


class _CaptureStub:
    def __init__(self):
        self.grabs = 0

    def grab_from_thread(self):
        self.grabs += 1
        return object()  # frame content is irrelevant — the tracker stub ignores it


class _TrackerStub:
    def __init__(self, visible=True, error_norm=0.5, padlock_off=True):
        self.visible = visible
        self.error_norm = error_norm
        self.updates = 0
        self.padlock_off = padlock_off
        self.padlock_off_checks = 0

    def update(self, frame):
        self.updates += 1
        return {"visible": self.visible, "error_norm": self.error_norm, "mode": "TRACKING"}

    def detect_padlock_off(self, frame):
        self.padlock_off_checks += 1
        return self.padlock_off


def _intents(ctrl):
    with ctrl._action_intents_lock:
        return list(ctrl._action_intents)


def _keys(ctrl):
    return [(i["action_type"], i["key"]) for i in _intents(ctrl)]


def _make_ctrl(monkeypatch, analyzer=None, capture=None, heatdive_enabled=False,
                legacy_nose_hold_s=0.05, heatdive_padlock_verify=False,
                eject_max_s=0.2, check_interval_s=None, pursuit_mode=None,
                sustained_hold_enabled=False):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    ecl = {
        "enabled": False,               # legacy branch — fast, deterministic
        "legacy_nose_hold_s": legacy_nose_hold_s,
        "eject_max_s": eject_max_s,
        "heatdive_enabled": heatdive_enabled,
        "heatdive_padlock_verify": heatdive_padlock_verify,
    }
    if check_interval_s is not None:
        ecl["check_interval_s"] = check_interval_s
    return Controller(
        (0, 0, 1920, 1200),
        analyzer=analyzer,
        exit_event=threading.Event(),
        capture=capture,
        config=ControllerConfig(
            simulate_os_input=True,
            disable_hotkeys=True,
            telemetry={"eject_closed_loop": ecl, "stale_after_s": 0.3},
            pursuit_mode=pursuit_mode or {},
            tracking={"sustained_hold_enabled": sustained_hold_enabled},
        ),
    )


def _run_eject_and_wait(ctrl, timeout=3.0, **eject_kwargs):
    ctrl.eject_and_dive(**eject_kwargs)
    thread = ctrl._eject_thread
    assert thread is not None
    thread.join(timeout=timeout)
    assert not thread.is_alive(), "eject_and_dive did not complete in time"


# ---------------------------------------------------------------------------
# Default off: zero behavior change (regression guard — ADR 136 Non-Goal 2)
# ---------------------------------------------------------------------------

def test_heatdive_disabled_by_default_presses_no_switch_weapon(monkeypatch):
    analyzer = _AnalyzerStub()
    capture = _CaptureStub()
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, capture=capture, heatdive_enabled=False)
    ctrl.set_target_tracker(_TrackerStub())

    _run_eject_and_wait(ctrl)

    assert ("key_press", SWITCH_WEAPON) not in _keys(ctrl)
    assert capture.grabs == 0


def test_heatdive_enabled_without_tracker_is_a_no_op(monkeypatch):
    analyzer = _AnalyzerStub()
    capture = _CaptureStub()
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, capture=capture, heatdive_enabled=True)
    # set_target_tracker() deliberately not called — self._target_tracker stays None.

    _run_eject_and_wait(ctrl)

    assert ("key_press", SWITCH_WEAPON) not in _keys(ctrl)
    assert capture.grabs == 0


# ---------------------------------------------------------------------------
# Enabled with a tracker wired: switches weapons, tracks, fires, cleans up
# ---------------------------------------------------------------------------

def test_heatdive_enabled_switches_weapon_and_tracks(monkeypatch):
    analyzer = _AnalyzerStub(ammo=2)
    capture = _CaptureStub()
    tracker = _TrackerStub(visible=True, error_norm=0.5)
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, capture=capture,
                       heatdive_enabled=True, legacy_nose_hold_s=0.3)
    ctrl.set_target_tracker(tracker)

    _run_eject_and_wait(ctrl)

    keys = _keys(ctrl)
    assert ("key_press", SWITCH_WEAPON) in keys
    assert ("key_press", FIRE_ACTIVE_WEAPON) in keys
    assert ("key_press", ROLL_RIGHT_KEY) in keys  # positive error_norm -> roll right
    assert tracker.updates > 0
    assert capture.grabs > 0


def test_heatdive_thread_does_not_outlive_the_dive(monkeypatch):
    """The dive's natural-completion path never sets self._eject_stop — the
    heatdive thread must still be told to stop and be joined (ADR 136 D3)."""
    analyzer = _AnalyzerStub(ammo=2)
    capture = _CaptureStub()
    tracker = _TrackerStub()
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, capture=capture,
                       heatdive_enabled=True, legacy_nose_hold_s=0.1)
    ctrl.set_target_tracker(tracker)

    _run_eject_and_wait(ctrl)

    assert not ctrl._eject_stop.is_set(), "natural completion should not set eject_stop"
    # eject_and_dive's finally block joins the heatdive thread before
    # returning control (already proven by _run_eject_and_wait's join
    # succeeding), so no further tracker activity should arrive after
    # eject_and_dive() has returned.
    updates_at_return = tracker.updates
    time.sleep(0.3)
    assert tracker.updates == updates_at_return, "heatdive loop kept running after the dive ended"


def test_heatdive_thread_stops_when_descent_control_ends_not_at_full_dive_completion(monkeypatch):
    """ADR 136 D5. Regression for a live incident (2026-09-12): descent
    control gave up on a transient telemetry loss ("no_telemetry", assuming
    the aircraft had died), telemetry then recovered and the aircraft kept
    flying for 31s with no pitch input at all — while the heatdive loop kept
    re-acquiring targets and rolling toward them the whole time, because
    nothing told it to stop until eject_and_dive()'s outer `finally`, which
    only runs at the END of the hold-until-respawn wait. The heatdive loop
    must stop as soon as descent control itself ends, well before the
    surrounding hold phase (and eject_and_dive() as a whole) completes."""
    analyzer = _AnalyzerStub(ammo=2)
    capture = _CaptureStub()
    tracker = _TrackerStub()
    # Descent "ends naturally" quickly (short nose hold); the surrounding
    # hold-until-respawn phase is deliberately long, so a fix that only
    # stops the heatdive loop at full dive completion — not right after
    # descent control ends — would show tracker.updates still growing well
    # into that hold window.
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, capture=capture,
                       heatdive_enabled=True, legacy_nose_hold_s=0.1,
                       eject_max_s=2.0, check_interval_s=0.1)
    ctrl.set_target_tracker(tracker)

    ctrl.eject_and_dive()
    # Wait past descent-control's own natural end (~0.1s) plus a couple of
    # the heatdive loop's own 0.2s poll cycles, well short of the 2.0s hold.
    time.sleep(0.5)
    assert ctrl.is_ejecting(), "test setup: the hold phase should still be running"
    updates_after_descent_ends = tracker.updates
    time.sleep(0.5)
    assert ctrl.is_ejecting(), "test setup: still mid-hold, not yet complete"
    assert tracker.updates == updates_after_descent_ends, (
        "heatdive loop kept tracking/rolling during the hold-until-respawn "
        "phase, after descent control itself had already ended")

    ctrl._eject_thread.join(timeout=3.0)
    assert not ctrl._eject_thread.is_alive(), "eject_and_dive did not complete in time"


# ---------------------------------------------------------------------------
# _eject_heatdive_loop in isolation: firing stops at ammo == 0, tracking does not
# ---------------------------------------------------------------------------

def test_heatdive_stops_firing_once_ammo_is_exhausted(monkeypatch):
    """The dive continues (tracking keeps running) but firing stops the
    moment Analyzer.get_ammo_missiles() reads 0 — ADR 136 D1 step 4."""
    class _DepletingAnalyzer(_AnalyzerStub):
        def __init__(self):
            super().__init__(ammo=1)
            self.reads = 0

        def get_ammo_missiles(self):
            self.reads += 1
            return 1 if self.reads <= 1 else 0

    analyzer = _DepletingAnalyzer()
    capture = _CaptureStub()
    tracker = _TrackerStub(visible=True, error_norm=0.5)
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, capture=capture)
    ctrl.set_target_tracker(tracker)

    stop_event = threading.Event()
    thread = threading.Thread(target=ctrl._eject_heatdive_loop, args=(stop_event,), daemon=True)
    thread.start()
    deadline = time.time() + 3.0
    while analyzer.reads < 3 and time.time() < deadline:
        time.sleep(0.02)
    stop_event.set()
    thread.join(timeout=2.0)
    assert not thread.is_alive()

    assert analyzer.reads >= 3
    fire_count = _keys(ctrl).count(("key_press", FIRE_ACTIVE_WEAPON))
    assert fire_count == 1, f"expected exactly one fire before ammo hit 0, got {fire_count}"
    assert tracker.updates >= 3, "tracking/rolling should keep running after ammo hits 0"


# ---------------------------------------------------------------------------
# ADR 136 x ADR 088: switching weapons must not trip the rearm-abort check
# (live regression, 2026-09-09 — see docs/adr/136-heatseeker-dive-invokable-mode.md)
# ---------------------------------------------------------------------------

def test_rearm_abort_check_skips_secondary_loadout_reading(monkeypatch):
    """Once heatdive has switched weapons, AMMO_MISSILE reads the secondary
    loadout (fixed at 2) — the ADR 088 rearm-abort check must not mistake
    that for a primary rearm and abort the dive early. Measured live: both
    trial dives false-aborted ~8s in with this exact "2 missile(s) rearmed"
    reason before the fix."""
    analyzer = _AnalyzerStub(ammo=2)  # constant "2" — the secondary loadout, never changes
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, capture=_CaptureStub())
    ctrl._eject_cl_check_interval_s = 0.02
    ctrl._eject_cl_max_s = 0.3
    ctrl._eject_weapon_switched = True  # heatdive already switched weapons

    ctrl._eject_descent_control()

    assert ctrl._eject_phase_exit_reason != "rearmed", (
        "rearm-abort fired on the secondary loadout's own ammo count")


def test_rearm_abort_check_still_fires_without_a_weapon_switch(monkeypatch):
    """Companion guard: ADR 088's original protection must still work when
    heatdive never switched weapons — the fix must not blanket-disable it."""
    analyzer = _AnalyzerStub(ammo=2)
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, capture=_CaptureStub())
    ctrl._eject_cl_check_interval_s = 0.02
    ctrl._eject_cl_max_s = 0.3
    assert ctrl._eject_weapon_switched is False

    cancelled = ctrl._eject_descent_control()

    assert cancelled is True
    assert ctrl._eject_phase_exit_reason == "rearmed"


def test_eject_and_dive_resets_weapon_switched_flag_per_dive(monkeypatch):
    """A stale True from a previous dive must not suppress a real rearm-abort
    on the next one — eject_and_dive() resets the flag every call."""
    analyzer = _AnalyzerStub(ammo=2)
    capture = _CaptureStub()
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, capture=capture)
    ctrl._eject_weapon_switched = True  # simulate a stale flag left by a prior dive

    _run_eject_and_wait(ctrl)

    assert ctrl._eject_weapon_switched is False


def test_weapon_already_switched_skips_the_redundant_press(monkeypatch):
    """Regression (2026-09-23): pursue_and_engage's fall-through already
    pressed SWITCH_WEAPON for this encounter before calling eject_and_dive —
    weapon_already_switched=True must skip the press here (previously
    unconditional, producing two presses ~0.3s apart every fall-through,
    live-confirmed to leave the secondary weapon never actually selected).
    The heatdive tracking/roll/fire thread must still start as usual."""
    analyzer = _AnalyzerStub(ammo=2)
    capture = _CaptureStub()
    tracker = _TrackerStub(visible=True, error_norm=0.5)
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, capture=capture,
                       heatdive_enabled=True, legacy_nose_hold_s=0.3)
    ctrl.set_target_tracker(tracker)

    _run_eject_and_wait(ctrl, weapon_already_switched=True)

    keys = _keys(ctrl)
    assert ("key_press", SWITCH_WEAPON) not in keys
    assert ("key_press", FIRE_ACTIVE_WEAPON) in keys, "heatdive should still run"
    assert tracker.updates > 0


def test_weapon_already_switched_does_not_reset_the_flag(monkeypatch):
    """Companion to test_eject_and_dive_resets_weapon_switched_flag_per_dive:
    when the caller says the weapon is already switched, the flag it set
    for that must survive, not just the redundant key press being skipped."""
    analyzer = _AnalyzerStub(ammo=2)
    capture = _CaptureStub()
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, capture=capture)
    ctrl._eject_weapon_switched = True  # simulate pursue_and_engage's own switch

    _run_eject_and_wait(ctrl, weapon_already_switched=True)

    assert ctrl._eject_weapon_switched is True


def test_stop_eject_sequence_also_clears_weapon_switched_flag(monkeypatch):
    """ADR 137: a respawn or match end restores the primary loadout in-game,
    so the "AMMO_MISSILE currently reads secondary" ambiguity must clear
    right there too — not only at the next dive's own start. Without this,
    a life with no further eject in it would read stale-True for its whole
    duration, wrongly suppressing a genuine crash_with_missiles count."""
    ctrl = _make_ctrl(monkeypatch, analyzer=_AnalyzerStub(ammo=2), capture=_CaptureStub())
    ctrl._eject_weapon_switched = True

    ctrl.stop_eject_sequence()

    assert ctrl._eject_weapon_switched is False


def test_is_secondary_weapon_active_reflects_the_flag(monkeypatch):
    ctrl = _make_ctrl(monkeypatch, analyzer=_AnalyzerStub(ammo=2), capture=_CaptureStub())
    assert ctrl.is_secondary_weapon_active() is False
    ctrl._eject_weapon_switched = True
    assert ctrl.is_secondary_weapon_active() is True


# ---------------------------------------------------------------------------
# ADR 136: padlock must be verified off before tracking-guided roll starts
# ---------------------------------------------------------------------------

def test_ensure_padlock_off_already_off_presses_nothing(monkeypatch):
    tracker = _TrackerStub(padlock_off=True)
    capture = _CaptureStub()
    ctrl = _make_ctrl(monkeypatch, analyzer=_AnalyzerStub(), capture=capture)
    ctrl.set_target_tracker(tracker)

    result = ctrl.ensure_padlock_off()

    assert result is True
    assert tracker.padlock_off_checks == 1
    assert ("key_press", PADLOCK_CAMERA) not in _keys(ctrl)
    assert ctrl._padlock_engaged is False


def test_ensure_padlock_off_presses_once_when_on(monkeypatch):
    class _TogglingTracker(_TrackerStub):
        def detect_padlock_off(self, frame):
            self.padlock_off_checks += 1
            # Off from the second check onward — as if the first press worked.
            return self.padlock_off_checks > 1

    tracker = _TogglingTracker(padlock_off=False)
    capture = _CaptureStub()
    ctrl = _make_ctrl(monkeypatch, analyzer=_AnalyzerStub(), capture=capture)
    ctrl.set_target_tracker(tracker)

    result = ctrl.ensure_padlock_off()

    assert result is True
    assert tracker.padlock_off_checks == 2
    assert _keys(ctrl).count(("key_press", PADLOCK_CAMERA)) == 1


def test_ensure_padlock_off_gives_up_after_max_attempts(monkeypatch):
    tracker = _TrackerStub(padlock_off=False)  # never confirms off
    capture = _CaptureStub()
    ctrl = _make_ctrl(monkeypatch, analyzer=_AnalyzerStub(), capture=capture)
    ctrl.set_target_tracker(tracker)

    result = ctrl.ensure_padlock_off(max_attempts=3)

    assert result is False
    assert tracker.padlock_off_checks == 3
    assert _keys(ctrl).count(("key_press", PADLOCK_CAMERA)) == 3


def test_ensure_padlock_off_without_tracker_is_a_no_op(monkeypatch):
    ctrl = _make_ctrl(monkeypatch, analyzer=_AnalyzerStub(), capture=_CaptureStub())
    # set_target_tracker() deliberately not called.

    result = ctrl.ensure_padlock_off()

    assert result is False
    assert ("key_press", PADLOCK_CAMERA) not in _keys(ctrl)


def test_heatdive_padlock_verify_disabled_by_default(monkeypatch):
    """ADR 136 D4: off by default (2026-09-09) — the detector was found to
    be tracking what a live capture comparison showed is most likely a
    flight-path marker, not a fixed padlock indicator. Blindly toggling
    PADLOCK_CAMERA against that signal was worse than doing nothing, so
    eject_and_dive must not call ensure_padlock_off unless explicitly
    re-enabled via heatdive_padlock_verify."""
    analyzer = _AnalyzerStub(ammo=2)
    capture = _CaptureStub()
    tracker = _TrackerStub(padlock_off=False)  # would trigger presses if checked at all
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, capture=capture,
                       heatdive_enabled=True, legacy_nose_hold_s=0.2)
    ctrl.set_target_tracker(tracker)

    _run_eject_and_wait(ctrl)

    assert tracker.padlock_off_checks == 0
    assert ("key_press", PADLOCK_CAMERA) not in _keys(ctrl)


def test_heatdive_checks_padlock_off_at_dive_start_when_verify_enabled(monkeypatch):
    """Integration: with heatdive_padlock_verify explicitly on, eject_and_dive
    calls ensure_padlock_off once heatdive switches weapons, before the
    tracking-roll loop begins."""
    analyzer = _AnalyzerStub(ammo=2)
    capture = _CaptureStub()
    tracker = _TrackerStub(padlock_off=True)
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, capture=capture,
                       heatdive_enabled=True, legacy_nose_hold_s=0.2,
                       heatdive_padlock_verify=True)
    ctrl.set_target_tracker(tracker)

    _run_eject_and_wait(ctrl)

    assert tracker.padlock_off_checks >= 1
    assert ("key_press", PADLOCK_CAMERA) not in _keys(ctrl)  # already off — no press needed


def test_heatdive_loop_stops_on_eject_stop_event(monkeypatch):
    """External cancellation (self._eject_stop) must stop the loop too, not
    only its own local stop_event — SAF-001 manual-takeover propagation."""
    analyzer = _AnalyzerStub(ammo=5)
    capture = _CaptureStub()
    tracker = _TrackerStub()
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, capture=capture)
    ctrl.set_target_tracker(tracker)

    stop_event = threading.Event()  # deliberately never set by this test
    thread = threading.Thread(target=ctrl._eject_heatdive_loop, args=(stop_event,), daemon=True)
    thread.start()
    deadline = time.time() + 2.0
    while tracker.updates < 1 and time.time() < deadline:
        time.sleep(0.02)
    assert tracker.updates >= 1

    ctrl._eject_stop.set()
    thread.join(timeout=2.0)
    assert not thread.is_alive(), "loop should stop on self._eject_stop even without its own stop_event"


# ---------------------------------------------------------------------------
# 2026-09-24 (operator): the dive's roll loop must not spin left after a lock,
# and a deferred weapon switch must wait for the selected weapon to run out.
# ---------------------------------------------------------------------------

class _ScriptedTracker:
    """One scripted observation per update(); the last one repeats."""

    def __init__(self, script):
        self.script = list(script)
        self.updates = 0

    def update(self, frame):
        obs = self.script[min(self.updates, len(self.script) - 1)]
        self.updates += 1
        return dict(obs)


_SEEN_FAR = {"visible": True, "error_norm": 0.5, "mode": "TRACKING"}
_SEEN_NEAR = {"visible": True, "error_norm": 0.05, "mode": "TRACKING"}
_MISS = {"visible": False, "error_norm": None, "mode": "LOST_GRACE"}


def _run_loop(ctrl, seconds=0.9, **loop_kwargs):
    stop = threading.Event()
    t = threading.Thread(target=ctrl._eject_heatdive_loop, args=(stop,),
                         kwargs=loop_kwargs, daemon=True)
    t.start()
    time.sleep(seconds)
    stop.set()
    t.join(timeout=3.0)
    assert not t.is_alive()
    return _keys(ctrl)


def _dive_ctrl(monkeypatch, script, ammo=2, pursuit_mode=None):
    ctrl = _make_ctrl(monkeypatch, analyzer=_AnalyzerStub(ammo=ammo), capture=_CaptureStub(),
                      heatdive_enabled=True, sustained_hold_enabled=True,
                      pursuit_mode=pursuit_mode)
    ctrl.set_target_tracker(_ScriptedTracker(script))
    return ctrl


def test_dive_miss_after_a_lock_does_not_resume_the_left_search(monkeypatch):
    """Measured (06:51-07:06 log): all 13 target holds in this loop ended
    `-> left/search` the instant the lock dropped, even with the target last
    seen on the right. The default 2.0 s delay now applies here too."""
    ctrl = _dive_ctrl(monkeypatch, [_SEEN_FAR, _MISS])
    keys = _run_loop(ctrl)
    assert ("key_press", ROLL_RIGHT_KEY) in keys
    assert ("key_release", ROLL_RIGHT_KEY) in keys
    assert ("key_press", ROLL_LEFT_KEY) not in keys


def test_dive_never_seen_still_searches_left(monkeypatch):
    ctrl = _dive_ctrl(monkeypatch, [_MISS])
    assert ("key_press", ROLL_LEFT_KEY) in _run_loop(ctrl, seconds=0.5)


def test_dive_near_centre_lock_holds_neutral_past_the_base_delay(monkeypatch):
    """The base delay is 0 here, so only the near-centre extension can keep the
    roll axis neutral after a lock at err 0.05."""
    ctrl = _dive_ctrl(monkeypatch, [_SEEN_NEAR, _MISS], pursuit_mode={
        "search_resume_delay_s": 0.0, "search_resume_centre_err": 0.15,
        "search_resume_centre_delay_s": 30.0})
    assert ("key_press", ROLL_LEFT_KEY) not in _run_loop(ctrl)


def test_dive_far_lock_does_not_get_the_near_centre_hold(monkeypatch, caplog):
    """The search resumes at once after a far lock, toward the side the target
    was last seen on (right, err +0.5; HLDD 015 2026-09-26)."""
    ctrl = _dive_ctrl(monkeypatch, [_SEEN_FAR, _MISS], pursuit_mode={
        "search_resume_delay_s": 0.0, "search_resume_centre_err": 0.15,
        "search_resume_centre_delay_s": 30.0})
    with caplog.at_level("DEBUG", logger="wingman.controller"):
        _run_loop(ctrl)
    assert any("-> right/search" in r.getMessage() for r in caplog.records
               if r.getMessage().startswith("HOLD[roll]:"))


_FAST_EMPTY = {"empty_confirm_reads": 2, "ammo_zero_grace_s": 30.0}


def test_deferred_dive_presses_nothing_while_the_weapon_has_ammo(monkeypatch):
    ctrl = _dive_ctrl(monkeypatch, [_MISS], ammo=6, pursuit_mode=_FAST_EMPTY)
    keys = _run_loop(ctrl, defer_switch_until_empty=True)
    assert (("key_press", SWITCH_WEAPON)) not in keys
    assert ("key_press", FIRE_ACTIVE_WEAPON) in keys
    assert not ctrl.is_secondary_weapon_active()


def test_deferred_dive_switches_once_after_consecutive_zero_reads(monkeypatch):
    ctrl = _dive_ctrl(monkeypatch, [_MISS], ammo=0, pursuit_mode=_FAST_EMPTY)
    keys = _run_loop(ctrl, seconds=1.2, defer_switch_until_empty=True)
    assert len([k for k in keys if k == ("key_press", SWITCH_WEAPON)]) == 1
    assert ctrl.is_secondary_weapon_active()


def test_deferred_dive_ignores_a_zero_that_is_not_consecutive(monkeypatch):
    class _Flicker(_AnalyzerStub):
        n = 0

        def get_ammo_missiles(self):
            _Flicker.n += 1
            return 0 if _Flicker.n % 2 else 6

    ctrl = _make_ctrl(monkeypatch, analyzer=_Flicker(), capture=_CaptureStub(),
                      heatdive_enabled=True, sustained_hold_enabled=True,
                      pursuit_mode=_FAST_EMPTY)
    ctrl.set_target_tracker(_ScriptedTracker([_MISS]))
    keys = _run_loop(ctrl, seconds=1.2, defer_switch_until_empty=True)
    assert ("key_press", SWITCH_WEAPON) not in keys


def test_deferred_dive_keeps_firing_through_the_stale_zero_after_the_switch(monkeypatch):
    """The HUD count still shows the old rack's 0 for seconds after a switch; a
    loop that trusted it would stop firing the secondary it just selected."""
    ctrl = _dive_ctrl(monkeypatch, [_MISS], ammo=0, pursuit_mode=_FAST_EMPTY)
    keys = _run_loop(ctrl, seconds=1.4, defer_switch_until_empty=True)
    after = keys[keys.index(("key_press", SWITCH_WEAPON)):]
    assert ("key_press", FIRE_ACTIVE_WEAPON) in after


def test_default_dive_still_stops_firing_at_zero(monkeypatch):
    """No deferral: unchanged. A 0 read is an empty rack, and no switch is
    made by the loop (eject_and_dive's own start made it, or did not)."""
    ctrl = _dive_ctrl(monkeypatch, [_MISS], ammo=0)
    keys = _run_loop(ctrl, seconds=0.6)
    assert ("key_press", FIRE_ACTIVE_WEAPON) not in keys
    assert ("key_press", SWITCH_WEAPON) not in keys


def test_eject_and_dive_deferred_presses_no_switch_and_skips_the_rearm_abort(monkeypatch):
    """eject_and_dive(defer_switch_until_empty=True) with heatdive on: no switch
    at the start, the flag untouched, and the ADR 088 rearm-abort (whose premise
    is an EMPTY rack) does not fire on the loaded one."""
    ctrl = _make_ctrl(monkeypatch, analyzer=_AnalyzerStub(ammo=6), capture=_CaptureStub(),
                      heatdive_enabled=True, legacy_nose_hold_s=0.05, eject_max_s=0.3)
    ctrl.set_target_tracker(_TrackerStub(visible=False))
    _run_eject_and_wait(ctrl, defer_switch_until_empty=True)
    assert ("key_press", SWITCH_WEAPON) not in _keys(ctrl)
    assert not ctrl.is_secondary_weapon_active()
    assert ctrl._eject_stop_reason != "rearmed"
    assert ctrl._eject_defer_switch is False, "the deferral must not outlive the dive"


def test_dive_loop_logs_one_summary_line(monkeypatch, caplog):
    """Action item 001, Cycle 7: the heatdive loop logs the same one-line outcome
    as the pursuit — scans, locked scans, first lock and ammo at both ends."""
    ctrl = _dive_ctrl(monkeypatch, [_MISS, _SEEN_FAR], ammo=3)
    with caplog.at_level("INFO", logger="wingman.controller"):
        _run_loop(ctrl, seconds=0.7)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("DIVE SUMMARY:")]
    assert len(lines) == 1, lines
    line = lines[0]
    assert "end=dive-end" in line
    assert "ammo=3->3" in line
    assert "first_lock=-" not in line and "locked=0 " not in line
    assert line.endswith("switched=no")
