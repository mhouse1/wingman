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
    Controller, FIRE_ACTIVE_WEAPON, PADLOCK_CAMERA, ROLL_RIGHT_KEY, SWITCH_WEAPON,
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
                legacy_nose_hold_s=0.05, heatdive_padlock_verify=False):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    ecl = {
        "enabled": False,               # legacy branch — fast, deterministic
        "legacy_nose_hold_s": legacy_nose_hold_s,
        "eject_max_s": 0.2,
        "heatdive_enabled": heatdive_enabled,
        "heatdive_padlock_verify": heatdive_padlock_verify,
    }
    return Controller(
        (0, 0, 1920, 1200),
        analyzer=analyzer,
        exit_event=threading.Event(),
        capture=capture,
        config=ControllerConfig(
            simulate_os_input=True,
            disable_hotkeys=True,
            telemetry={"eject_closed_loop": ecl, "stale_after_s": 0.3},
        ),
    )


def _run_eject_and_wait(ctrl, timeout=3.0):
    ctrl.eject_and_dive()
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
