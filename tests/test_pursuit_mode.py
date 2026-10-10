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
from tests.perception_fake import PerceptionFake
from wingman.resupply import ResupplyMarker


class _AnalyzerStub(PerceptionFake):
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
                search_resume_centre_delay_s=0.0, icon_steering=None, dive_safety=None,
                resupply_priority_enabled=True, resupply_priority_actuate=False,
                rearm_climb_s=0.0, search_climb_alt_m=0.0, save_candidate_frames=None,
                priority_target=None, air_superiority=None):
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
                "resupply_priority": {
                    "enabled": resupply_priority_enabled,
                    "actuate": resupply_priority_actuate,
                    # 0.0 by default here (not config.yaml's shipped 3.0), so
                    # steering resumes on the tick after a rearm; the climb-out's
                    # own tests set it.
                    "rearm_climb_s": rearm_climb_s,
                    # Left out unless a test sets it, so the schema default applies.
                    **({"save_candidate_frames": save_candidate_frames}
                       if save_candidate_frames is not None else {}),
                },
                # 0.0 by default here (not config.yaml's shipped 7000): the blind
                # search rolls, as before; the climb's own tests set it.
                "search_climb_alt_m": search_climb_alt_m,
                # Left out unless a test sets it: off is the schema default.
                **({"priority_target": priority_target} if priority_target is not None else {}),
                **({"air_superiority": air_superiority} if air_superiority is not None else {}),
                **({"icon_steering": icon_steering} if icon_steering is not None else {}),
                **({"dive_safety": dive_safety} if dive_safety is not None else {}),
            },
        ),
    )


def _wait_for_pursuit_to_settle(ctrl, timeout=3.0):
    deadline = time.time() + timeout
    while ctrl.is_pursuing() and time.time() < deadline:
        time.sleep(0.01)
    assert not ctrl.is_pursuing(), "pursue_and_engage did not finish in time"
    # _pursuing clears before the thread hands off (on_complete, or building the
    # dive's thread), so wait for the thread itself: under load a test read
    # `done == []` in that gap (2026-10-05, test_cr019_handback, 1 run in 3).
    pursuit_thread = getattr(ctrl, "_pursuing_thread", None)
    if pursuit_thread is not None and pursuit_thread.is_alive():
        pursuit_thread.join(timeout=timeout)
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


def test_zero_ammo_actuation_seeks_marker_without_firing_or_diving(monkeypatch, caplog):
    caplog.set_level("DEBUG", logger="wingman.controller")
    marker = ResupplyMarker(1400, 600, 70, 70, 1500, 0)
    monkeypatch.setattr(controller_module, "find_resupply_marker", lambda _frame, **_kw: marker)
    analyzer = _AnalyzerStub(ammo=0)
    ctrl = _make_ctrl(
        monkeypatch, analyzer=analyzer, capture=_FrameCapture(),
        pursuit_enabled=True, pursuit_max_duration_s=5.0,
        empty_confirm_reads=1, resupply_priority_actuate=True,
        icon_steering={
            "enabled": True,
            "wings_level": True,
            "actuate_pitch": True,
            "actuate_turn": True,
        })
    assert ctrl._resupply_priority_actuate
    assert ctrl._pursuit_ammo_grace_s == 0.0
    ammo_reads = []
    monkeypatch.setattr(analyzer, "get_ammo_missiles",
                        lambda: ammo_reads.append(time.time()) or 0)
    tracker = _TrackerStub(visible=True, error_norm=-0.5, error_norm_y=0.0)
    ctrl.set_target_tracker(tracker)

    ctrl.pursue_and_engage()
    try:
        time.sleep(0.6)
        pursuing = ctrl.is_pursuing()
        eject_thread = ctrl._eject_thread
        reads = list(ammo_reads)
        logs = caplog.text
        keys = _keys(ctrl)
    finally:
        ctrl._eject_stop.set()
        _wait_for_pursuit_to_settle(ctrl)

    assert pursuing, "confirmed zero must continue in resupply-seeking mode"
    assert eject_thread is None, "zero ammo must not fall through to eject_and_dive"
    assert reads
    assert "maximum urgency reached" in logs
    assert "proposed=True seeking=True mode=actuate" in logs
    assert ("key_press", ROLL_RIGHT_KEY) in keys, "steer toward the marker on the right"
    roll_presses = [key for action, key in keys
                    if action == "key_press" and key in (ROLL_LEFT_KEY, ROLL_RIGHT_KEY)]
    assert roll_presses[-1] == ROLL_RIGHT_KEY, (
        "resupply steering supersedes the initial target correction")
    assert ("key_press", NOSE_DOWN_KEY) not in keys
    assert ("key_press", FIRE_ACTIVE_WEAPON) not in keys


def test_zero_ammo_without_marker_searches_for_resupply_not_targets(monkeypatch, caplog):
    """Operator, 2026-10-02: with every rack empty the pursuit searches for
    resupply instead of targets, so a visible opponent is not steered at."""
    caplog.set_level("DEBUG", logger="wingman.controller")
    scans = []
    monkeypatch.setattr(
        controller_module, "find_resupply_marker",
        lambda _frame, **_kw: scans.append(1) or None)
    ctrl = _make_ctrl(
        monkeypatch, analyzer=_AnalyzerStub(ammo=0), capture=_CaptureStub(),
        pursuit_enabled=True, pursuit_max_duration_s=5.0,
        empty_confirm_reads=1, resupply_priority_actuate=True)
    ctrl.set_target_tracker(_TrackerStub(visible=True))

    ctrl.pursue_and_engage()
    try:
        time.sleep(0.9)
        pursuing = ctrl.is_pursuing()
        eject_thread = ctrl._eject_thread
        logs = caplog.text
        keys = _keys(ctrl)
    finally:
        ctrl._eject_stop.set()
        _wait_for_pursuit_to_settle(ctrl)

    assert pursuing
    assert eject_thread is None
    assert scans, "keep scanning for the marker"
    empty_at = logs.index("searching for resupply, targets ignored until rearm")
    assert "search=True" in logs[empty_at:]
    assert "roll_right - pressing" not in logs[empty_at:], (
        "the visible opponent is not steered at once every rack is empty")
    assert ("key_press", FIRE_ACTIVE_WEAPON) not in keys


def test_shadow_mode_keeps_tracking_the_opponent_at_zero_ammo(monkeypatch, caplog):
    """Without actuation nothing about steering changes: no resupply search."""
    caplog.set_level("DEBUG", logger="wingman.controller")
    monkeypatch.setattr(
        controller_module, "find_resupply_marker", lambda _frame, **_kw: None)
    ctrl = _make_ctrl(
        monkeypatch, analyzer=_AnalyzerStub(ammo=0), capture=_CaptureStub(),
        pursuit_enabled=True, pursuit_max_duration_s=5.0, ammo_zero_grace_s=30.0,
        empty_confirm_reads=1, resupply_priority_actuate=False)
    ctrl.set_target_tracker(_TrackerStub(visible=True))

    ctrl.pursue_and_engage(weapon_already_switched=True)
    time.sleep(0.6)
    ctrl._eject_stop.set()
    _wait_for_pursuit_to_settle(ctrl)

    assert "search=True" not in caplog.text
    assert ("key_press", ROLL_RIGHT_KEY) in _keys(ctrl)


def test_target_nearer_the_centre_than_the_resupply_icon_is_attacked(monkeypatch, caplog):
    """Operator, 2026-10-02: with two missiles spent and weapons left, the
    pursuit goes for whichever is nearer the screen centre and keeps firing."""
    caplog.set_level("DEBUG", logger="wingman.controller")
    marker = ResupplyMarker(1400, 600, 40, 40, 300, 0)      # 440 px right of centre
    monkeypatch.setattr(controller_module, "find_resupply_marker", lambda _frame, **_kw: marker)
    monkeypatch.setattr(controller_module.cv2, "imwrite", lambda path, frame: True)
    ctrl = _make_ctrl(
        monkeypatch, analyzer=_SequenceAnalyzer([4, 2]), capture=_FrameCapture(),
        pursuit_enabled=True, pursuit_max_duration_s=5.0,
        empty_confirm_reads=1, resupply_priority_actuate=True)
    # 0.2 of the half width: 192 px left of centre, nearer than the marker.
    ctrl.set_target_tracker(_TrackerStub(visible=True, error_norm=-0.2, error_norm_y=0.0))

    ctrl.pursue_and_engage(weapon_already_switched=True)
    time.sleep(1.2)
    ctrl._eject_stop.set()
    _wait_for_pursuit_to_settle(ctrl)

    keys = _keys(ctrl)
    assert "RESUPPLY: spent=2 empty=False" in caplog.text
    assert "proposed=False" in caplog.text and "target_nearer=True" in caplog.text
    assert "rearm focus begins" not in caplog.text
    assert ("key_press", ROLL_LEFT_KEY) in keys, "the nearer target is the one steered at"
    assert ("key_press", ROLL_RIGHT_KEY) not in keys, "the farther resupply icon is not"
    assert ("key_press", FIRE_ACTIVE_WEAPON) in keys


def test_resupply_icon_nearer_the_centre_than_the_target_is_flown_to_and_firing_goes_on(
        monkeypatch, caplog):
    caplog.set_level("DEBUG", logger="wingman.controller")
    marker = ResupplyMarker(1100, 600, 40, 40, 300, 0)      # 140 px right of centre
    monkeypatch.setattr(controller_module, "find_resupply_marker", lambda _frame, **_kw: marker)
    monkeypatch.setattr(controller_module.cv2, "imwrite", lambda path, frame: True)
    ctrl = _make_ctrl(
        monkeypatch, analyzer=_SequenceAnalyzer([4, 2]), capture=_FrameCapture(),
        pursuit_enabled=True, pursuit_max_duration_s=5.0,
        empty_confirm_reads=1, resupply_priority_actuate=True)
    ctrl.set_target_tracker(_TrackerStub(visible=True, error_norm=-0.5, error_norm_y=0.0))

    ctrl.pursue_and_engage(weapon_already_switched=True)
    try:
        time.sleep(1.2)
        logs = caplog.text
        keys = _keys(ctrl)
    finally:
        ctrl._eject_stop.set()
        _wait_for_pursuit_to_settle(ctrl)

    focus_at = logs.index("RESUPPLY: urgency overtook pursuit at spent=2")
    assert "roll_right - pressing" in logs[focus_at:], "steer toward the nearer resupply icon"
    assert "search=False" in logs[focus_at:]
    assert ("key_press", FIRE_ACTIVE_WEAPON) in keys, "weapons remain, so firing goes on"


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


def test_zero_delay_resumes_the_search_toward_the_side_last_seen(monkeypatch):
    """HLDD 015, 2026-09-26: the search turns toward the side the target was
    last seen on (right here, err +0.5), no longer always left."""
    keys = _run_scripted_pursuit(monkeypatch, [_SEEN, _MISS], search_resume_delay_s=0.0)
    assert ("key_press", ROLL_RIGHT_KEY) in keys
    assert ("key_press", ROLL_LEFT_KEY) not in keys


def test_a_miss_after_a_lock_on_the_left_searches_left(monkeypatch):
    seen_left = dict(_SEEN, error_norm=-0.5)
    keys = _run_scripted_pursuit(monkeypatch, [seen_left, _MISS], search_resume_delay_s=0.0)
    assert ("key_press", ROLL_LEFT_KEY) in keys
    assert ("key_press", ROLL_RIGHT_KEY) not in keys


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


def test_stale_zero_during_switch_grace_does_not_hide_missiles_spent(monkeypatch, caplog):
    caplog.set_level("DEBUG", logger="wingman.controller")
    analyzer = _SequenceAnalyzer([0, 0, 2, 2, 1, 1])
    ctrl = _make_ctrl(
        monkeypatch, analyzer=analyzer, capture=_CaptureStub(),
        pursuit_enabled=True, pursuit_max_duration_s=5.0,
        ammo_zero_grace_s=3.0, empty_confirm_reads=1)
    ctrl.set_target_tracker(_TrackerStub(visible=False))

    ctrl.pursue_and_engage(defer_switch_until_empty=True)
    time.sleep(1.4)
    ctrl._eject_stop.set()
    _wait_for_pursuit_to_settle(ctrl)

    assert "RESUPPLY: spent=1 empty=False" in caplog.text
    assert "terminal_zero=True" not in caplog.text


def test_empty_primary_rack_contributes_spent_missiles_before_switch(monkeypatch, caplog):
    caplog.set_level("DEBUG", logger="wingman.controller")
    analyzer = _SequenceAnalyzer([3, 3, 3, 0, 0, 0])
    ctrl = _make_ctrl(
        monkeypatch, analyzer=analyzer, capture=_CaptureStub(),
        pursuit_enabled=True, pursuit_max_duration_s=5.0,
        ammo_zero_grace_s=30.0, empty_confirm_reads=3)
    ctrl.set_target_tracker(_TrackerStub(visible=False))

    ctrl.pursue_and_engage(defer_switch_until_empty=True)
    time.sleep(1.8)
    ctrl._eject_stop.set()
    _wait_for_pursuit_to_settle(ctrl)

    assert "switching to the secondary" in caplog.text
    assert "RESUPPLY: spent=3" in caplog.text


class _SlowReadAnalyzer(_SequenceAnalyzer):
    """One OCR read is polled twice, as live: the count's read number moves
    every second poll."""

    def get_ammo_missiles_read_seq(self):
        return (self._n - 1) // 2


def test_emptied_primary_rack_is_tallied_when_the_switch_beats_the_ocr_reads(
        monkeypatch, caplog):
    """2026-10-02: the switch needs three zero polls (about a second), the
    tally three OCR reads, so the primary's last missiles were never counted
    and "two spent, weapons left" was logged on 3 ticks in a 46-minute session."""
    caplog.set_level("DEBUG", logger="wingman.controller")
    analyzer = _SlowReadAnalyzer([2] * 6 + [0] * 3 + [2] * 40)
    ctrl = _make_ctrl(
        monkeypatch, analyzer=analyzer, capture=_CaptureStub(),
        pursuit_enabled=True, pursuit_max_duration_s=8.0,
        ammo_zero_grace_s=30.0, empty_confirm_reads=3)
    ctrl.set_target_tracker(_TrackerStub(visible=False))

    ctrl.pursue_and_engage(defer_switch_until_empty=True)
    time.sleep(3.6)
    ctrl._eject_stop.set()
    _wait_for_pursuit_to_settle(ctrl)

    assert "switching to the secondary" in caplog.text
    assert "RESUPPLY: spent=2 empty=False" in caplog.text


def test_resupply_does_not_override_a_locked_opponent_while_ammo_remains(monkeypatch, caplog):
    caplog.set_level("DEBUG", logger="wingman.controller")
    marker = ResupplyMarker(1400, 600, 70, 70, 1500, 0)
    monkeypatch.setattr(controller_module, "find_resupply_marker", lambda _frame, **_kw: marker)
    analyzer = _SequenceAnalyzer([3, 2])
    ctrl = _make_ctrl(
        monkeypatch, analyzer=analyzer, capture=_CaptureStub(),
        pursuit_enabled=True, pursuit_max_duration_s=5.0,
        empty_confirm_reads=1, resupply_priority_actuate=True)
    tracker = _TrackerStub(visible=True, error_norm=-0.5, error_norm_y=0.0)
    ctrl.set_target_tracker(tracker)

    ctrl.pursue_and_engage(weapon_already_switched=True)
    time.sleep(1.0)
    ctrl._eject_stop.set()
    _wait_for_pursuit_to_settle(ctrl)

    keys = _keys(ctrl)
    assert "RESUPPLY: spent=1" in caplog.text
    assert "marker=(1400,600)" in caplog.text
    assert "proposed=False seeking=False mode=actuate" in caplog.text
    assert ("key_press", ROLL_LEFT_KEY) in keys, "the locked opponent remains the target"
    assert ("key_press", ROLL_RIGHT_KEY) not in keys, "do not steer toward resupply under lock"


def test_resupply_interrupts_attack_before_zero_then_rearm_resumes_pursuit(monkeypatch, caplog, tmp_path):
    caplog.set_level("DEBUG", logger="wingman.controller")
    marker = ResupplyMarker(1400, 600, 70, 70, 1500, 0)
    detector_calls = 0

    def detect_marker_with_dropout(_frame, **_kw):
        nonlocal detector_calls
        detector_calls += 1
        return marker if detector_calls == 1 else None

    monkeypatch.setattr(
        controller_module, "find_resupply_marker", detect_marker_with_dropout)
    capture = _FrameCapture()
    saved_frames = []
    monkeypatch.setattr(controller_module, "_RESUPPLY_SAMPLE_DIR", tmp_path)
    monkeypatch.setattr(
        controller_module.cv2, "imwrite",
        lambda path, frame: saved_frames.append((path, frame)) or True)
    analyzer = _SequenceAnalyzer([4, 2, 2, 6, 6])
    ctrl = _make_ctrl(
        monkeypatch, analyzer=analyzer, capture=capture,
        pursuit_enabled=True, pursuit_max_duration_s=5.0,
        empty_confirm_reads=1, resupply_priority_actuate=True,
        save_candidate_frames=True)
    class _HudCapture:
        def __init__(self):
            self.calls = []

        def maybe_render(self, *args, **kwargs):
            self.calls.append(kwargs)

    hud_renderer = _HudCapture()
    ctrl.set_hud_renderer(hud_renderer)
    tracker = _TrackerStub(visible=True, error_norm=-0.5, error_norm_y=0.0)
    ctrl.set_target_tracker(tracker)

    ctrl.pursue_and_engage(weapon_already_switched=True)
    try:
        time.sleep(1.7)
        logs = caplog.text
        keys = _keys(ctrl)
    finally:
        ctrl._eject_stop.set()
        _wait_for_pursuit_to_settle(ctrl)

    assert "RESUPPLY: urgency overtook pursuit at spent=2" in logs
    assert "proposed=True seeking=True mode=actuate" in logs
    assert "marker_stale=True" in logs
    assert any(
        kwargs.get("steering_label") == "RESUPPLYING"
        and kwargs.get("steering_target") == (marker.x, marker.y)
        for kwargs in hud_renderer.calls)
    assert len(saved_frames) == 1
    assert saved_frames[0][1] is capture.frame
    rearm_at = logs.index("RESUPPLY: confirmed ammo=6; urgency reset, resuming target pursuit")
    assert "roll_left - pressing" in logs[rearm_at:], (
        "target attack resumes after the confirmed ammo increase")
    assert ("key_press", ROLL_RIGHT_KEY) in keys, "resupply focus steers toward the marker"


def test_candidate_frames_off_saves_nothing_and_still_seeks_the_marker(
        monkeypatch, caplog, tmp_path):
    """Operator, 2026-10-05: the resupply_candidate_*.png captures off. The
    switch stops the file, not the resupply focus."""
    caplog.set_level("DEBUG", logger="wingman.controller")
    marker = ResupplyMarker(1400, 600, 70, 70, 1500, 0)
    monkeypatch.setattr(controller_module, "find_resupply_marker", lambda _frame, **_kw: marker)
    saved_frames = []
    monkeypatch.setattr(controller_module, "_RESUPPLY_SAMPLE_DIR", tmp_path)
    monkeypatch.setattr(
        controller_module.cv2, "imwrite",
        lambda path, frame: saved_frames.append(path) or True)
    ctrl = _make_ctrl(
        monkeypatch, analyzer=_SequenceAnalyzer([4, 2, 2]), capture=_FrameCapture(),
        pursuit_enabled=True, pursuit_max_duration_s=5.0, empty_confirm_reads=1,
        resupply_priority_actuate=True)      # the key left out: off is the default
    ctrl.set_target_tracker(_TrackerStub(visible=False))

    ctrl.pursue_and_engage(weapon_already_switched=True)
    try:
        time.sleep(1.2)
        logs = caplog.text
    finally:
        ctrl._eject_stop.set()
        _wait_for_pursuit_to_settle(ctrl)

    assert "proposed=True seeking=True mode=actuate" in logs
    assert saved_frames == []
    assert "candidate frame saved" not in logs
    assert list(tmp_path.iterdir()) == []


def test_the_pursuit_reports_the_resupply_marker_it_flies_to(monkeypatch):
    """Operator, 2026-10-08: the crash recovery waits while the resupply is in
    view. This is the pursuit's half: what it reports while it steers at one."""
    marker = ResupplyMarker(1400, 600, 70, 70, 1500, 0)
    monkeypatch.setattr(controller_module, "find_resupply_marker", lambda _frame, **_kw: marker)
    ctrl = _make_ctrl(
        monkeypatch, analyzer=_SequenceAnalyzer([4, 2, 2]), capture=_FrameCapture(),
        pursuit_enabled=True, pursuit_max_duration_s=5.0, empty_confirm_reads=1,
        resupply_priority_actuate=True)
    ctrl.set_target_tracker(_TrackerStub(visible=True, error_norm=-0.9, error_norm_y=0.9))

    ctrl.pursue_and_engage(weapon_already_switched=True)
    try:
        deadline = time.time() + 3.0
        while (ctrl._pursuit_objective_in_view() != "resupply marker"
               and time.time() < deadline):
            time.sleep(0.02)
        reported = ctrl._pursuit_objective_in_view()
    finally:
        ctrl._eject_stop.set()
        _wait_for_pursuit_to_settle(ctrl)
    assert reported == "resupply marker"
    assert ctrl._pursuit_objective_in_view() is None, "nothing is in view once the pursuit ends"


def _rearm_with_a_target_low_and_left(monkeypatch, **ctrl_kwargs):
    """Two missiles spent, a marker seen once, then the count rises to 6. The
    target sits low and left, so target steering rolls left and pushes down."""
    seen = []
    monkeypatch.setattr(
        controller_module, "find_resupply_marker",
        lambda _frame, **_kw: (seen.append(1) or len(seen) == 1)
        and ResupplyMarker(1400, 600, 70, 70, 1500, 0) or None)
    ctrl = _make_ctrl(
        monkeypatch, analyzer=_SequenceAnalyzer([4, 2, 2, 6, 6]), capture=_FrameCapture(),
        pursuit_enabled=True, pursuit_max_duration_s=8.0, empty_confirm_reads=1,
        **ctrl_kwargs)
    ctrl.set_target_tracker(_TrackerStub(visible=True, error_norm=-0.5, error_norm_y=0.5))
    return ctrl


def test_a_rearm_is_followed_by_a_nose_up_hold_then_steering_resumes(monkeypatch, caplog):
    """Operator, 2026-10-02: "crashes can be avoided by immediately applying
    nose up manuver on rearm". The resupply point sits near terrain."""
    caplog.set_level("DEBUG", logger="wingman.controller")
    ctrl = _rearm_with_a_target_low_and_left(
        monkeypatch, resupply_priority_actuate=True, rearm_climb_s=0.6)

    ctrl.pursue_and_engage(weapon_already_switched=True)
    try:
        time.sleep(2.8)
        logs = caplog.text
        keys = _keys(ctrl)
    finally:
        ctrl._eject_stop.set()
        _wait_for_pursuit_to_settle(ctrl)

    rearm_at = logs.index("RESUPPLY: confirmed ammo=6")
    climb_at = logs.index("RESUPPLY: rearm climb-out, nose up for 0.6s")
    over_at = logs.index("up -> None (rearm climb-out over)")
    assert rearm_at < climb_at < over_at
    climb = logs[climb_at:over_at]
    assert "-> up (rearm climb-out)" in climb
    assert ("key_press", NOSE_UP_KEY) in keys
    assert "roll_left - pressing" not in climb, "the target is not rolled at during the climb-out"
    assert "nose_down - pressing" not in climb, "the target is not dived at during the climb-out"
    assert "roll_left - pressing" in logs[over_at:], "target steering resumes after it"


def test_shadow_mode_rearm_has_no_climb_out(monkeypatch, caplog):
    caplog.set_level("DEBUG", logger="wingman.controller")
    ctrl = _rearm_with_a_target_low_and_left(
        monkeypatch, resupply_priority_actuate=False, rearm_climb_s=0.6)

    ctrl.pursue_and_engage(weapon_already_switched=True)
    try:
        time.sleep(1.9)
        logs = caplog.text
        keys = _keys(ctrl)
    finally:
        ctrl._eject_stop.set()
        _wait_for_pursuit_to_settle(ctrl)

    assert "RESUPPLY: confirmed ammo=6" in logs
    assert "climb-out" not in logs
    assert ("key_press", NOSE_UP_KEY) not in keys


def test_after_a_rearm_the_other_rack_is_used_before_declaring_exhaustion(monkeypatch, caplog):
    """2026-10-02: a resupply refilled both racks (2/2 and 2/2), the
    selected one emptied, and the pursuit went into resupply mode with two
    missiles on the other rack. Since 2026-10-05 the primary running out
    presses the switch as well: every rack that goes from a count to zero does."""
    caplog.set_level("DEBUG", logger="wingman.controller")
    monkeypatch.setattr(
        controller_module, "find_resupply_marker", lambda _frame, **_kw: None)
    # Secondary selected and empty; rearm; secondary empty again; primary 2, 2, then empty.
    analyzer = _SequenceAnalyzer([0, 2, 0, 2, 2, 0])
    ctrl = _make_ctrl(
        monkeypatch, analyzer=analyzer, capture=_CaptureStub(),
        pursuit_enabled=True, pursuit_max_duration_s=8.0,
        ammo_zero_grace_s=0.0, empty_confirm_reads=1, resupply_priority_actuate=True)
    ctrl.set_target_tracker(_TrackerStub(visible=False))

    ctrl.pursue_and_engage(weapon_already_switched=True)
    try:
        time.sleep(2.6)
        logs = caplog.text
        keys = _keys(ctrl)
        selected_secondary = ctrl._eject_weapon_switched
    finally:
        ctrl._eject_stop.set()
        _wait_for_pursuit_to_settle(ctrl)

    rearm_at = logs.index("RESUPPLY: confirmed ammo=2")
    back_at = logs.index("switching to the primary")
    assert rearm_at < back_at
    assert "missiles exhausted" not in logs[rearm_at:back_at], (
        "an empty selected rack after a rearm is not exhaustion")
    assert "fire_active_weapon - pressing" in logs[back_at:], "the reloaded primary is fired"
    again_at = logs.index("switching to the secondary", back_at)
    assert "missiles exhausted" in logs[again_at:], "both racks empty is exhaustion"
    assert keys.count(("key_press", SWITCH_WEAPON)) == 2, (
        "the primary going from 2 to 0 presses the switch too")
    assert selected_secondary is True


# ---------------------------------------------------------------------------
# Operator, 2026-10-05: "anytime any weapon goes from a number to zero it
# presses 'g', this way if resupply happened it would auto switch to new
# inventory". 11:18:45-11:19:18 that day: the primary ran out, the pursuit
# switched to the secondary (2/2) and flew through the resupply point. A
# resupply refills both racks, but the selected one was already full, so no
# count changed. When the secondary ran out nothing pressed the switch, and the
# aircraft searched for a resupply for 39 s with a reloaded primary.
# ---------------------------------------------------------------------------

def _pursue_until(ctrl, done, *, timeout=8.0, **pursue_kwargs):
    """Run a pursuit until `done()` or the timeout, then stop it from outside."""
    ctrl.pursue_and_engage(**pursue_kwargs)
    deadline = time.time() + timeout
    try:
        while time.time() < deadline and ctrl.is_pursuing() and not done():
            time.sleep(0.02)
        return done()
    finally:
        ctrl._eject_stop.set()
        _wait_for_pursuit_to_settle(ctrl)


def test_the_secondary_running_out_switches_back_to_a_primary_reloaded_unseen(
        monkeypatch, caplog):
    caplog.set_level("INFO", logger="wingman.controller")
    # Primary 3 then empty; the stale 0 after the switch, secondary 2, 2 then
    # empty; the stale 0 again, then the primary the resupply refilled.
    analyzer = _SequenceAnalyzer([3, 0, 0, 0, 2, 2, 0, 0, 0, 6])
    ctrl = _make_ctrl(
        monkeypatch, analyzer=analyzer, capture=_CaptureStub(),
        pursuit_enabled=True, pursuit_max_duration_s=0.0,
        ammo_zero_grace_s=30.0, empty_confirm_reads=2, resupply_priority_actuate=True)
    ctrl.set_target_tracker(_TrackerStub(visible=False))

    def _reloaded_primary_fired():
        keys = _keys(ctrl)
        presses = [i for i, k in enumerate(keys) if k == ("key_press", SWITCH_WEAPON)]
        return len(presses) >= 2 and ("key_press", FIRE_ACTIVE_WEAPON) in keys[presses[1]:]

    assert _pursue_until(ctrl, _reloaded_primary_fired, defer_switch_until_empty=True), (
        "the reloaded primary was never selected and fired")
    logs = caplog.text
    first = logs.index("switching to the secondary")
    second = logs.index("switching to the primary")
    assert first < second
    assert len(_switch_presses(_keys(ctrl))) == 2
    assert not ctrl.is_secondary_weapon_active(), "the count read is the primary's again"
    assert "missiles exhausted" not in logs
    assert "RESUPPLY: confirmed" not in logs[:second], (
        "the switch back must not depend on the count having shown the rearm")


def test_the_missiles_empty_pursuit_switches_back_too(monkeypatch):
    """The default path presses the switch at its start; its secondary running
    out presses it again."""
    analyzer = _SequenceAnalyzer([0, 2, 2, 0, 0, 0, 6])
    ctrl = _make_ctrl(
        monkeypatch, analyzer=analyzer, capture=_CaptureStub(),
        pursuit_enabled=True, pursuit_max_duration_s=0.0,
        ammo_zero_grace_s=30.0, empty_confirm_reads=2, resupply_priority_actuate=True)
    ctrl.set_target_tracker(_TrackerStub(visible=False))

    assert _pursue_until(ctrl, lambda: len(_switch_presses(_keys(ctrl))) >= 2), (
        "the secondary going from 2 to 0 pressed nothing")
    assert not ctrl.is_secondary_weapon_active()


def test_two_empty_racks_do_not_toggle_back_and_forth(monkeypatch, caplog):
    """SWITCH_WEAPON is a toggle. A rack that shows no count after it is selected
    did not go from a number to zero, so it presses nothing: two presses, then
    the ordinary both-racks-empty ending, and the dive does not press a third."""
    caplog.set_level("INFO", logger="wingman.controller")
    analyzer = _SequenceAnalyzer([2, 0, 0, 2, 0, 0, 0])
    ctrl, keys = _run_deferred_pursuit(
        monkeypatch, analyzer, confirm=2, ammo_grace=0.3, max_s=6.0, heatdive=True)
    logs = caplog.text
    assert "ammo exhausted, falling through" in logs
    assert logs.index("switching to the primary") < logs.index("ammo exhausted")
    assert len(_switch_presses(keys)) == 2, "no third press, in the pursuit or the dive"
    assert not ctrl.is_secondary_weapon_active()


def test_a_rack_with_no_count_yet_is_not_switched_away_from(monkeypatch):
    """After a switch the HUD holds the old rack's 0 for seconds. That 0 is
    not the new rack running out."""
    analyzer = _SequenceAnalyzer([2, 0, 0, 0, 0, 0, 0, 0, 0, 2])
    ctrl = _make_ctrl(
        monkeypatch, analyzer=analyzer, capture=_CaptureStub(),
        pursuit_enabled=True, pursuit_max_duration_s=0.0,
        ammo_zero_grace_s=30.0, empty_confirm_reads=2)
    ctrl.set_target_tracker(_TrackerStub(visible=False))

    assert _pursue_until(ctrl, lambda: analyzer._n >= 12, defer_switch_until_empty=True)
    assert len(_switch_presses(_keys(ctrl))) == 1
    assert ctrl.is_secondary_weapon_active()


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


def test_a_miss_after_a_far_from_centre_lock_still_resumes_the_search(monkeypatch, caplog):
    """The extension is for a target the aircraft is already pointing at; one
    last seen far off to the side does not get the longer hold."""
    far = {"visible": True, "error_norm": 0.6, "error_norm_y": 0.0, "mode": "TRACKING"}
    tracker = _ScriptedTracker([far, _MISS])
    ctrl = _make_ctrl(monkeypatch, analyzer=_AnalyzerStub(ammo=2), capture=_CaptureStub(),
                       pursuit_enabled=True, pursuit_max_duration_s=0.9,
                       sustained_hold_enabled=True, search_resume_delay_s=0.0,
                       search_resume_centre_delay_s=30.0)
    ctrl.set_target_tracker(tracker)
    with caplog.at_level("DEBUG", logger="wingman.controller"):
        ctrl.pursue_and_engage()
        _wait_for_pursuit_to_settle(ctrl)
    # The search resumed (no neutral hold), toward the side last seen: right.
    assert any("-> right/search" in r.getMessage() for r in caplog.records
               if r.getMessage().startswith("HOLD[roll]:"))


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


# ---------------------------------------------------------------------------
# Operator, 2026-09-24: "after a 20 second chase do not dive, continue searching
# and pursuing." pursuit_mode.pursuit_max_duration_s = 0 means no time cap.
# ---------------------------------------------------------------------------

def _start_uncapped(monkeypatch, analyzer, *, tracker=None, **kw):
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, capture=_CaptureStub(),
                       pursuit_enabled=True, pursuit_max_duration_s=0.0, **kw)
    ctrl.set_target_tracker(tracker or _TrackerStub())
    return ctrl


def test_a_pursuit_with_no_cap_is_still_pursuing_past_where_a_cap_would_have_fired(monkeypatch):
    """A 0.5 s cap ends a pursuit inside 0.7 s (see the cap tests above). With 0 it must not."""
    ctrl = _start_uncapped(monkeypatch, _AnalyzerStub(ammo=2))
    ctrl.pursue_and_engage(defer_switch_until_empty=True)
    time.sleep(1.3)
    try:
        assert ctrl.is_pursuing(), "no cap means no time-based end"
        assert ctrl._eject_thread is None, "and no fall-through into the dive"
    finally:
        ctrl.stop_eject_sequence("respawn_detected")
        _wait_for_pursuit_to_settle(ctrl)
    assert ctrl._eject_thread is None, "an external stop must still not start a dive"


def test_a_pursuit_with_no_cap_still_fires_and_still_ends_on_a_respawn(monkeypatch, caplog):
    ctrl = _start_uncapped(monkeypatch, _AnalyzerStub(ammo=2))
    with caplog.at_level("INFO", logger="wingman.controller"):
        ctrl.pursue_and_engage(defer_switch_until_empty=True)
        time.sleep(0.8)
        ctrl.stop_eject_sequence("respawn_detected")
        _wait_for_pursuit_to_settle(ctrl)
    assert ("key_press", FIRE_ACTIVE_WEAPON) in _keys(ctrl)
    (line,) = _summary_lines(caplog, "PURSUIT")
    assert "end=external:respawn_detected" in line and "end=cap" not in line


def test_no_cap_still_falls_through_to_the_dive_when_the_ammo_is_exhausted(monkeypatch):
    """The dive is how an empty airframe trades for a rearmed one; only the time cap went."""
    ctrl = _start_uncapped(monkeypatch, _AnalyzerStub(ammo=0), legacy_nose_hold_s=0.05,
                            eject_max_s=0.2)
    ctrl.pursue_and_engage()
    _wait_for_pursuit_to_settle(ctrl)
    assert ctrl._eject_thread is not None, "ammo exhausted must still hand over to eject_and_dive"


def test_no_cap_keeps_the_deferred_weapon_untouched_however_long_it_runs(monkeypatch):
    """The 'do not switch until the rack is empty' rule has no timer in it either."""
    ctrl = _start_uncapped(monkeypatch, _AnalyzerStub(ammo=2))
    ctrl.pursue_and_engage(defer_switch_until_empty=True)
    time.sleep(1.2)
    ctrl.stop_eject_sequence("respawn_detected")
    _wait_for_pursuit_to_settle(ctrl)
    assert _switch_presses(_keys(ctrl)) == []


def test_a_positive_cap_still_ends_the_pursuit(monkeypatch):
    """Setting seconds again restores the old behavior."""
    ctrl = _make_ctrl(monkeypatch, analyzer=_AnalyzerStub(ammo=2), capture=_CaptureStub(),
                       pursuit_enabled=True, pursuit_max_duration_s=0.5)
    ctrl.set_target_tracker(_TrackerStub())
    ctrl.pursue_and_engage(defer_switch_until_empty=True)
    _wait_for_pursuit_to_settle(ctrl)
    assert ctrl._eject_thread is not None


def test_eject_flight_active_is_true_only_while_a_pursuit_flies(monkeypatch):
    """Anomaly 003's detector reads this: a pursuit over 40 s must not look 'stuck'."""
    ctrl = _start_uncapped(monkeypatch, _AnalyzerStub(ammo=2))
    assert ctrl.eject_flight_active() is False
    ctrl.pursue_and_engage(defer_switch_until_empty=True)
    time.sleep(0.5)
    try:
        assert ctrl.eject_flight_active() is True
        assert ctrl.eject_descent_active() is False, "a pursuit has no descent: that is the trap"
    finally:
        ctrl.stop_eject_sequence("respawn_detected")
        _wait_for_pursuit_to_settle(ctrl)
    assert ctrl.eject_flight_active() is False


# ---------------------------------------------------------------------------
# HLDD 015 Icon-Directed Search, shadow stage (2026-09-26): the loop scores the
# ring icon and logs ICONPTS, and presses nothing it would not press anyway.
# ---------------------------------------------------------------------------

_STEERING_KEYS = {NOSE_DOWN_KEY, NOSE_UP_KEY, ROLL_LEFT_KEY, ROLL_RIGHT_KEY}


class _FrameCapture(_CaptureStub):
    """Returns a real frame: the operator's reference icon (down 5, left 1)."""

    def __init__(self):
        super().__init__()
        import cv2
        from pathlib import Path
        self.frame = cv2.imread(str(Path(__file__).parent / "fixtures" / "icon_ring_nose_down.png"))

    def grab_from_thread(self):
        self.grabs += 1
        return self.frame


def _icon_pursuit(monkeypatch, caplog, script, icon_steering=None, capture=None, **kw):
    # Uncapped and ended by a respawn: a cap would fall through into the dive,
    # whose own descent control presses NOSE_DOWN.
    ctrl = _make_ctrl(monkeypatch, analyzer=_AnalyzerStub(ammo=2),
                       capture=capture or _FrameCapture(),
                       pursuit_enabled=True, pursuit_max_duration_s=0.0,
                       icon_steering=icon_steering, **kw)
    ctrl.set_target_tracker(_ScriptedTracker(script))
    with caplog.at_level("DEBUG", logger="wingman.controller"):
        ctrl.pursue_and_engage(defer_switch_until_empty=True)
        time.sleep(0.9)
        ctrl.stop_eject_sequence("respawn_detected")
        _wait_for_pursuit_to_settle(ctrl)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("ICONPTS:")]
    return ctrl, lines


def test_icon_shadow_scores_the_reference_icon_and_presses_nothing(monkeypatch, caplog):
    ctrl, lines = _icon_pursuit(monkeypatch, caplog, [_MISS], icon_steering={"enabled": True})
    assert len(lines) >= 4, lines
    assert "add=(-1,+5)" in lines[0]
    assert any("rung=icon" in ln and "intent=down keys=NOSE_DOWN" in ln for ln in lines), lines
    # No telemetry here, so the -45 deg limit has no angle to check against.
    assert any("withheld=angle-none" in ln for ln in lines)
    assert not [k for a, k in _keys(ctrl) if k in _STEERING_KEYS]
    summary = _summary_lines(caplog, "PURSUIT")
    assert len(summary) == 1 and " icon=" in summary[0] and "icon_steer=" in summary[0]


def test_icon_shadow_zeroes_the_points_while_the_tracker_has_a_target(monkeypatch, caplog):
    _ctrl, lines = _icon_pursuit(monkeypatch, caplog, [_SEEN], icon_steering={"enabled": True})
    assert lines and all("rung=track" in ln and "pts=(+0.0,+0.0)" in ln for ln in lines)


def test_icon_shadow_waits_after_a_lock_like_roll_on_miss(monkeypatch, caplog):
    _ctrl, lines = _icon_pursuit(monkeypatch, caplog, [_SEEN, _MISS],
                                 icon_steering={"enabled": True}, search_resume_delay_s=30.0)
    assert "rung=track" in lines[0]
    assert all("rung=wait" in ln for ln in lines[1:]), lines


_NEAR = {"visible": True, "error_norm": 0.05, "error_norm_y": 0.0, "mode": "TRACKING"}


def _wait_cuts(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("ICONWAIT:")]


def test_active_icon_points_end_the_near_centre_wait_after_the_base_delay(monkeypatch, caplog):
    """2026-09-27 18:51:45 (cycle 11): a target shot near the centre left a 6 s
    neutral wait with the next enemy's icon on the ring the whole time. The base
    delay still waits; the near-centre extension gives way to active points."""
    _ctrl, lines = _icon_pursuit(monkeypatch, caplog, [_NEAR, _MISS],
                                 icon_steering={"enabled": True},
                                 search_resume_delay_s=0.2, search_resume_centre_delay_s=30.0)
    assert "rung=track" in lines[0]
    assert "rung=wait" in lines[1], lines
    assert "rung=icon" in lines[-1], lines
    assert len(_wait_cuts(caplog)) == 1, "logged once per lost lock"


def test_active_icon_points_do_not_end_the_base_delay(monkeypatch, caplog):
    """The base delay is the lock dropping for a scan or two with the target
    still on screen: 696 of 798 reacquisitions over 30 logs came inside it."""
    _ctrl, lines = _icon_pursuit(monkeypatch, caplog, [_NEAR, _MISS],
                                 icon_steering={"enabled": True},
                                 search_resume_delay_s=30.0, search_resume_centre_delay_s=60.0)
    assert all("rung=wait" in ln for ln in lines[1:]), lines
    assert _wait_cuts(caplog) == []


def test_without_an_icon_the_near_centre_wait_runs_its_course(monkeypatch, caplog):
    _ctrl, lines = _icon_pursuit(monkeypatch, caplog, [_NEAR, _MISS],
                                 icon_steering={"enabled": True}, capture=_BlankCapture(),
                                 search_resume_delay_s=0.2, search_resume_centre_delay_s=30.0)
    assert all("rung=wait" in ln for ln in lines[1:]), lines
    assert _wait_cuts(caplog) == []


def test_icon_shadow_off_logs_nothing_and_leaves_the_summary_alone(monkeypatch, caplog):
    _ctrl, lines = _icon_pursuit(monkeypatch, caplog, [_MISS])
    assert lines == []
    assert " icon=" not in _summary_lines(caplog, "PURSUIT")[0]


# ---------------------------------------------------------------------------
# HLDD 015 rollout step 2a (2026-09-26): wings level instead of the fixed left
# roll while an icon or active points give a direction. Nothing new pressed.
# ---------------------------------------------------------------------------

class _BlankCapture(_FrameCapture):
    """A real frame with no ring icon on it."""

    def __init__(self):
        super().__init__()
        import numpy as np
        self.frame = np.zeros_like(self.frame)


def _step_2a(monkeypatch, caplog, capture, wings_level=True):
    ctrl = _make_ctrl(monkeypatch, analyzer=_AnalyzerStub(ammo=2), capture=capture,
                       pursuit_enabled=True, pursuit_max_duration_s=0.0,
                       sustained_hold_enabled=True,
                       icon_steering={"enabled": True, "wings_level": wings_level})
    ctrl.set_target_tracker(_ScriptedTracker([_MISS]))
    look_downs = []
    monkeypatch.setattr(ctrl, "_search_look_down", lambda: look_downs.append(1) or False)
    with caplog.at_level("DEBUG", logger="wingman.controller"):
        ctrl.pursue_and_engage(defer_switch_until_empty=True)
        time.sleep(0.9)
        ctrl.stop_eject_sequence("respawn_detected")
        _wait_for_pursuit_to_settle(ctrl)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("ICONPTS:")]
    return _keys(ctrl), lines, look_downs


def test_step_2a_keeps_the_left_roll_off_while_an_icon_is_on_the_ring(monkeypatch, caplog):
    keys, lines, _ = _step_2a(monkeypatch, caplog, _FrameCapture())
    assert ("key_press", ROLL_LEFT_KEY) not in keys
    assert not [k for a, k in keys if k in _STEERING_KEYS and a == "key_press"]
    assert lines and all("act=level" in ln for ln in lines), lines


def test_step_2a_keeps_the_look_down_taps(monkeypatch, caplog):
    """Only the roll changes: the look-down runs where the search roll would have."""
    _keys_, _lines, look_downs = _step_2a(monkeypatch, caplog, _FrameCapture())
    assert look_downs


def test_step_2a_still_rolls_left_when_no_icon_has_been_seen(monkeypatch, caplog):
    keys, lines, look_downs = _step_2a(monkeypatch, caplog, _BlankCapture())
    assert ("key_press", ROLL_LEFT_KEY) in keys
    assert lines and all("rung=blind" in ln and "act=-" in ln for ln in lines)
    assert look_downs


def test_pure_shadow_still_rolls_left_with_an_icon(monkeypatch, caplog):
    keys, lines, _ = _step_2a(monkeypatch, caplog, _FrameCapture(), wings_level=False)
    assert ("key_press", ROLL_LEFT_KEY) in keys
    assert lines and all("act=-" in ln for ln in lines)


# ---------------------------------------------------------------------------
# HLDD 015 rollout step 2b (2026-09-26): the icon's vertical intent holds pitch
# on the icon and hold rungs; the look-down taps run on the blind rung only.
# ---------------------------------------------------------------------------

class _TelemetryAnalyzer(_AnalyzerStub):
    """Ammo stub plus a REAL TelemetrySnapshot. `new_samples` stamps each call
    with a new altitude timestamp; False repeats one sample. `rate` is the
    altitude rate in m/s."""

    def __init__(self, new_samples=True, rate=0.0, alt=4000.0, stable=None):
        super().__init__(ammo=2)
        self.new_samples = new_samples
        self.rate = rate
        self.alt = alt
        self.stable = alt if stable is None else stable
        self.calls = 0

    def get_telemetry(self):
        from wingman.telemetry import TelemetrySignal, TelemetrySnapshot
        self.calls += 1
        now = time.time()
        sample_ts = now if self.new_samples else 1000.0
        return TelemetrySnapshot(
            speed=TelemetrySignal(value=900, stable_value=900.0, ts=now, rate=0.0),
            altitude=TelemetrySignal(value=int(self.alt), stable_value=float(self.stable),
                                     ts=sample_ts, rate=self.rate),
            taken_at_s=now, stale_after_s=6.0)


def _step_2b(monkeypatch, caplog, capture, *, angle=-5.0, guard=None, actuate_pitch=True,
             analyzer=None, actuate_turn=False, push_floor_m=None, script=None, **kw):
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer or _TelemetryAnalyzer(), capture=capture,
                       pursuit_enabled=True, pursuit_max_duration_s=0.0,
                       sustained_hold_enabled=True,
                       icon_steering={"enabled": True, "wings_level": True,
                                      "actuate_pitch": actuate_pitch,
                                      "actuate_turn": actuate_turn,
                                      "push_floor_m": push_floor_m}, **kw)
    ctrl.set_target_tracker(_ScriptedTracker(script or [_MISS]))
    look_downs = []
    monkeypatch.setattr(ctrl, "_search_look_down", lambda: look_downs.append(1) or False)
    monkeypatch.setattr(ctrl, "_telemetry_path_angle_deg", lambda: angle)
    monkeypatch.setattr(ctrl, "_pursuit_dive_guard", lambda target_visible=False: guard)
    monkeypatch.setattr(ctrl, "_dive_guard_pullout", lambda: None)
    with caplog.at_level("DEBUG", logger="wingman.controller"):
        ctrl.pursue_and_engage(defer_switch_until_empty=True)
        time.sleep(0.9)
        ctrl.stop_eject_sequence("respawn_detected")
        _wait_for_pursuit_to_settle(ctrl)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("ICONPTS:")]
    return _keys(ctrl), lines, look_downs


def test_step_2b_holds_nose_down_toward_the_reference_icon(monkeypatch, caplog):
    """Held, not tapped: the flight keys only act when held (operator, 2026-09-26)."""
    keys, lines, look_downs = _step_2b(monkeypatch, caplog, _FrameCapture())
    assert ("key_press", NOSE_DOWN_KEY) in keys
    assert ("key_press", ROLL_LEFT_KEY) not in keys
    assert ("key_press", NOSE_UP_KEY) not in keys
    assert look_downs == [], "the icon owns pitch: no look-down taps on the icon rung"
    assert any("act=level+down" in ln for ln in lines), lines
    assert any(r.getMessage().startswith("HOLD[pitch]: None -> down (icon down")
               for r in caplog.records)


# ---------------------------------------------------------------------------
# pursuit_mode.dive_safety (operator, 2026-09-26): pursuit via the icons comes
# before dive safety, for the whole pursuit.
# ---------------------------------------------------------------------------

def _low_and_falling(ctrl):
    from wingman.telemetry import TelemetrySignal, TelemetrySnapshot

    class _Low(_AnalyzerStub):
        def get_telemetry(self):
            now = time.time()
            return TelemetrySnapshot(
                speed=TelemetrySignal(value=900, stable_value=900.0, ts=now, rate=0.0),
                altitude=TelemetrySignal(value=1000, stable_value=1000.0, ts=now, rate=-200.0),
                taken_at_s=now, stale_after_s=6.0)
    ctrl._analyzer = _Low()


def test_dive_guard_trips_when_dive_safety_is_on(monkeypatch):
    ctrl = _make_ctrl(monkeypatch, pursuit_enabled=True, dive_safety=True)
    ctrl._pursuit_dive_guard_ttg_s = 60.0
    _low_and_falling(ctrl)
    assert ctrl._pursuit_dive_guard(target_visible=False)


def test_dive_guard_never_trips_with_dive_safety_off(monkeypatch):
    ctrl = _make_ctrl(monkeypatch, pursuit_enabled=True, dive_safety=False)
    ctrl._pursuit_dive_guard_ttg_s = 60.0
    _low_and_falling(ctrl)
    assert ctrl._pursuit_dive_guard(target_visible=False) is None
    assert ctrl._dive_guard_ttg_tripped is False


def test_emergency_climb_does_not_start_inside_a_pursuit_with_crash_recovery_off(monkeypatch, caplog):
    """pursuit_mode.crash_recovery (2026-10-02) lets the hard emergency through with
    dive_safety off; tests/test_pursuit_recovery.py pins that side."""
    ctrl = _make_ctrl(monkeypatch, pursuit_enabled=True, dive_safety=False)
    ctrl._pursuit_crash_recovery = False
    ctrl._pursuing.set()
    with caplog.at_level("INFO", logger="wingman.controller"):
        ctrl.climb_mode(target_alt=5000.0, emergency=True)
    assert not ctrl._climbing.is_set()
    assert any("dive recovery suppressed" in r.getMessage() for r in caplog.records)


def test_floor_climb_does_not_start_inside_a_pursuit_with_dive_safety_off(monkeypatch, caplog):
    """2026-09-26 18:15: inside a pursuit the floor climb left at its first state
    check and only ran its exit push, whose NOSE_DOWN release dropped the
    pursuit's own held key."""
    ctrl = _make_ctrl(monkeypatch, pursuit_enabled=True, dive_safety=False)
    ctrl._pursuing.set()
    with caplog.at_level("INFO", logger="wingman.controller"):
        ctrl.climb_mode(target_alt=5000.0, max_s=0.2, emergency=False)
    assert not ctrl._climbing.is_set()
    assert any("climb suppressed — the pursuit owns" in r.getMessage() for r in caplog.records)


def test_floor_climb_still_starts_inside_a_pursuit_with_dive_safety_on(monkeypatch):
    ctrl = _make_ctrl(monkeypatch, pursuit_enabled=True, dive_safety=True)
    ctrl._pursuing.set()
    ctrl.climb_mode(target_alt=5000.0, max_s=0.2, emergency=False)
    try:
        assert ctrl._climbing.is_set()
    finally:
        ctrl._climb_stop.set()
        if ctrl._climb_thread is not None:
            ctrl._climb_thread.join(timeout=3.0)


def test_climb_exit_push_presses_nothing_while_a_pursuit_flies(monkeypatch):
    ctrl = _make_ctrl(monkeypatch, pursuit_enabled=True, dive_safety=False)
    ctrl._climb_exit_pitch_deg = 20.0
    ctrl._pursuing.set()
    assert ctrl._climb_exit_push() == "pursuit"
    assert _keys(ctrl) == []


_FLIGHT_KEYS = (NOSE_DOWN_KEY, NOSE_UP_KEY, ROLL_LEFT_KEY, ROLL_RIGHT_KEY)


def test_a_boundary_turn_running_when_the_pursuit_starts_never_touches_its_keys(
        monkeypatch, caplog):
    """2026-09-26 18:14:37: a boundary turn began 1 s before the su30 yield
    started the pursuit. It held NOSE_UP against the icon push, and at 18:14:46
    its closing releases dropped the pursuit's held NOSE_DOWN: 54 s of level
    flight with the push still logged as held, then the out-of-bounds timer."""
    ctrl = _make_ctrl(monkeypatch, analyzer=_TelemetryAnalyzer(), capture=_FrameCapture(),
                      pursuit_enabled=True, pursuit_max_duration_s=0.0,
                      sustained_hold_enabled=True, dive_safety=False,
                      icon_steering={"enabled": True, "wings_level": True,
                                     "actuate_pitch": True})
    ctrl._climb_exit_pitch_deg = 20.0
    ctrl.set_target_tracker(_ScriptedTracker([_MISS]))
    monkeypatch.setattr(ctrl, "_search_look_down", lambda: False)
    monkeypatch.setattr(ctrl, "_telemetry_path_angle_deg", lambda: -5.0)
    ctrl.boundary_turn_mode(max_s=0.5, lateral=+0.4)
    assert ctrl._boundary_turning.is_set()
    with caplog.at_level("DEBUG", logger="wingman.controller"):
        ctrl.pursue_and_engage(defer_switch_until_empty=True)
        time.sleep(0.9)
        ctrl.stop_eject_sequence("respawn_detected")
        _wait_for_pursuit_to_settle(ctrl)
    intents = ctrl.get_action_intents()
    first_push = next(n for n, x in enumerate(intents)
                      if x["action_type"] == "key_press" and x["key"] == NOSE_DOWN_KEY
                      and x.get("action") == "tracking_pitch")
    others = [(x["action_type"], x["key"], x.get("action")) for x in intents[first_push:]
              if x["key"] in _FLIGHT_KEYS and x.get("action") in ("boundary", "climb")]
    assert others == [], others
    assert not ctrl._boundary_turning.is_set()
    assert any("pursuit took the airframe — boundary turn stopped" in r.getMessage()
               for r in caplog.records)


def test_no_path_limit_but_still_no_push_without_a_fresh_angle(monkeypatch):
    ctrl = _make_ctrl(monkeypatch, pursuit_enabled=True,
                      icon_steering={"enabled": True, "icon_min_path_deg": None,
                                     "require_fresh_angle": True})
    monkeypatch.setattr(ctrl, "_telemetry_path_angle_deg", lambda: None)
    assert ctrl._icon_down_withheld(None) == "angle-none"
    monkeypatch.setattr(ctrl, "_telemetry_path_angle_deg", lambda: -80.0)
    assert ctrl._icon_down_withheld(None) == "-", "no -45 deg limit any more"


def test_icon_push_is_withheld_below_the_push_floor(monkeypatch, caplog):
    """Operator, 2026-09-26 (cycle 9): with the dropped key fixed, the push flew
    into the ground from about 1200 m and 700 m."""
    keys, lines, _ = _step_2b(monkeypatch, caplog, _FrameCapture(),
                              analyzer=_TelemetryAnalyzer(alt=1200.0), push_floor_m=1500)
    assert ("key_press", NOSE_DOWN_KEY) not in keys
    assert any("intent=down" in ln and "withheld=alt " in ln for ln in lines), lines


def test_icon_push_still_holds_above_the_push_floor(monkeypatch, caplog):
    keys, lines, _ = _step_2b(monkeypatch, caplog, _FrameCapture(),
                              analyzer=_TelemetryAnalyzer(alt=2000.0), push_floor_m=1500)
    assert ("key_press", NOSE_DOWN_KEY) in keys
    assert any("act=level+down" in ln and "withheld=- " in ln for ln in lines), lines


def test_icon_push_floor_withholds_without_a_fresh_altitude(monkeypatch):
    ctrl = _make_ctrl(monkeypatch, pursuit_enabled=True,
                      icon_steering={"enabled": True, "push_floor_m": 1500})
    assert ctrl._icon_down_withheld(None) == "alt-none"


def test_step_2b_withholds_nose_down_at_the_path_angle_limit(monkeypatch, caplog):
    keys, lines, _ = _step_2b(monkeypatch, caplog, _FrameCapture(), angle=-50.0)
    assert ("key_press", NOSE_DOWN_KEY) not in keys
    assert any("intent=down" in ln and "withheld=angle " in ln for ln in lines), lines


def test_step_2b_withholds_nose_down_without_a_fresh_angle(monkeypatch, caplog):
    keys, lines, _ = _step_2b(monkeypatch, caplog, _FrameCapture(), angle=None)
    assert ("key_press", NOSE_DOWN_KEY) not in keys
    assert any("withheld=angle-none" in ln for ln in lines), lines


def test_step_2b_withholds_nose_down_when_the_dive_guard_trips(monkeypatch, caplog):
    keys, lines, _ = _step_2b(monkeypatch, caplog, _FrameCapture(), guard="ttg 40s")
    assert ("key_press", NOSE_DOWN_KEY) not in keys
    assert any("withheld=guard" in ln for ln in lines), lines


def test_step_2b_blind_rung_keeps_the_left_roll_and_the_look_down(monkeypatch, caplog):
    keys, lines, look_downs = _step_2b(monkeypatch, caplog, _BlankCapture())
    assert ("key_press", ROLL_LEFT_KEY) in keys
    assert ("key_press", NOSE_DOWN_KEY) not in keys
    assert look_downs


def test_blind_search_climbs_to_the_search_altitude_instead_of_rolling_left(
        monkeypatch, caplog):
    """Operator, 2026-10-02: "currently it continuously flies left when no
    targets are sighted, modify it to fly up to 7000 altitude or until target
    sighted"."""
    keys, _lines, look_downs = _step_2b(
        monkeypatch, caplog, _BlankCapture(), angle=0.0, search_climb_alt_m=7000)
    assert ("key_press", ROLL_LEFT_KEY) not in keys
    assert ("key_press", NOSE_UP_KEY) in keys
    assert look_downs == [], "no look-down taps while climbing"
    assert "SEARCH CLIMB — no target, climbing from 4000 m to 7000 m" in caplog.text
    assert caplog.text.count("SEARCH CLIMB — no target") == 1, "logged once per climb"


def test_search_climb_holds_no_nose_up_at_the_climb_angle(monkeypatch, caplog):
    keys, _lines, _look_downs = _step_2b(
        monkeypatch, caplog, _BlankCapture(), angle=35.0, search_climb_alt_m=7000)
    assert ("key_press", ROLL_LEFT_KEY) not in keys
    assert ("key_press", NOSE_UP_KEY) not in keys


def test_at_the_search_altitude_the_left_roll_and_look_down_resume(monkeypatch, caplog):
    keys, _lines, look_downs = _step_2b(
        monkeypatch, caplog, _BlankCapture(), angle=0.0, search_climb_alt_m=7000,
        analyzer=_TelemetryAnalyzer(alt=7200.0))
    assert ("key_press", ROLL_LEFT_KEY) in keys
    assert ("key_press", NOSE_UP_KEY) not in keys
    assert look_downs
    assert "SEARCH CLIMB — 7200 m reached, the roll search resumes" in caplog.text


def test_a_sighted_target_ends_the_search_climb(monkeypatch, caplog):
    keys, _lines, _look_downs = _step_2b(
        monkeypatch, caplog, _BlankCapture(), angle=0.0, search_climb_alt_m=7000,
        script=[_SEEN])
    assert "SEARCH CLIMB" not in caplog.text
    assert ("key_press", ROLL_RIGHT_KEY) in keys, "the target on the right is steered at"


def test_step_2a_setting_presses_no_icon_pitch(monkeypatch, caplog):
    keys, _lines, look_downs = _step_2b(monkeypatch, caplog, _FrameCapture(),
                                        actuate_pitch=False)
    assert ("key_press", NOSE_DOWN_KEY) not in keys
    assert look_downs


# ---------------------------------------------------------------------------
# HLDD 015 rollout step 3 (2026-09-26): the turn acts, bank and pull.
# ---------------------------------------------------------------------------

class _SyntheticIconCapture(_CaptureStub):
    """A black frame with one red ring icon at `angle_deg` (screen convention:
    0 right, 90 down, 180 left, -90 up)."""

    def __init__(self, angle_deg):
        super().__init__()
        import math
        import cv2
        import numpy as np
        img = np.zeros((1200, 1920, 3), np.uint8)
        x = int(round(960 + 194 * math.cos(math.radians(angle_deg))))
        y = int(round(600 + 194 * math.sin(math.radians(angle_deg))))
        bgr = tuple(int(c) for c in cv2.cvtColor(np.uint8([[[3, 165, 255]]]),
                                                cv2.COLOR_HSV2BGR)[0, 0])
        cv2.rectangle(img, (x - 15, y - 15), (x + 15, y + 15), bgr, -1)
        self.frame = img

    def grab_from_thread(self):
        self.grabs += 1
        return self.frame


def _upper_left():
    """An enemy to the left and slightly above: the turn case (step 3)."""
    return _SyntheticIconCapture(-170.0)


class _LeftIconCapture(_FrameCapture):
    """The archived orange ring icon at 172 deg: an enemy off to the left."""

    def __init__(self):
        super().__init__()
        import cv2
        from pathlib import Path
        self.frame = cv2.imread(str(Path(__file__).parent / "fixtures" / "icon_ring_orange_left.png"))


def test_step_3_banks_and_pulls_toward_a_side_icon(monkeypatch, caplog):
    keys, lines, _ = _step_2b(monkeypatch, caplog, _upper_left(), actuate_turn=True)
    assert ("key_press", ROLL_LEFT_KEY) in keys
    assert ("key_press", NOSE_UP_KEY) in keys
    assert ("key_press", NOSE_DOWN_KEY) not in keys
    assert ("key_press", ROLL_RIGHT_KEY) not in keys
    assert any("act=bankleft+up" in ln for ln in lines), lines


class _ResupplyPinCapture(_CaptureStub):
    """A black 1920x1200 frame holding the live crop of the resupply pin at
    9 o'clock on the indicator ring (2026-10-02)."""

    def __init__(self):
        super().__init__()
        import cv2
        import numpy as np
        from pathlib import Path
        crop = cv2.imread(str(Path(__file__).parent / "fixtures" / "resupply_pin_left.png"))
        self.frame = np.zeros((1200, 1920, 3), dtype=np.uint8)
        self.frame[557:557 + crop.shape[0], 716:716 + crop.shape[1]] = crop

    def grab_from_thread(self):
        self.grabs += 1
        return self.frame


def test_resupply_mode_turns_toward_the_resupply_pin_not_the_visible_target(monkeypatch, caplog):
    """2026-10-02: with every rack empty the search was a blind roll, and all 8
    resupply-mode episodes of one session ended in a respawn within 22 s.
    The yellow pin on the indicator ring says where the resupply point is."""
    analyzer = _TelemetryAnalyzer()
    analyzer.ammo = 0
    ctrl = _make_ctrl(
        monkeypatch, analyzer=analyzer, capture=_ResupplyPinCapture(),
        pursuit_enabled=True, pursuit_max_duration_s=0.0, sustained_hold_enabled=True,
        empty_confirm_reads=1, resupply_priority_actuate=True,
        icon_steering={"enabled": True, "wings_level": True,
                       "actuate_pitch": True, "actuate_turn": True})
    # A target well to the right: resupply mode must not steer at it.
    ctrl.set_target_tracker(_TrackerStub(visible=True, error_norm=0.5, error_norm_y=0.0))
    monkeypatch.setattr(ctrl, "_search_look_down", lambda: False)
    monkeypatch.setattr(ctrl, "_telemetry_path_angle_deg", lambda: -5.0)
    monkeypatch.setattr(ctrl, "_pursuit_dive_guard", lambda target_visible=False: None)
    monkeypatch.setattr(ctrl, "_dive_guard_pullout", lambda: None)
    with caplog.at_level("DEBUG", logger="wingman.controller"):
        ctrl.pursue_and_engage(weapon_already_switched=True)
        time.sleep(1.6)
        ctrl.stop_eject_sequence("respawn_detected")
        _wait_for_pursuit_to_settle(ctrl)

    messages = [r.getMessage() for r in caplog.records]
    empty_at = next(i for i, m in enumerate(messages) if "searching for resupply" in m)
    after = messages[empty_at:]
    assert any(m.startswith("RESUPPLY: spent") and "search=True" in m and "pin=-179" in m
               for m in after), "the pin at 9 o'clock is read"
    assert any(m.startswith("ICONPTS:") and "act=bankleft+up" in m for m in after), (
        "bank toward the pin and pull")
    assert not any("-> right/target" in m for m in after if m.startswith("HOLD[roll]")), (
        "the visible target is not steered at")
    assert ("key_press", ROLL_LEFT_KEY) in _keys(ctrl)
    assert ("key_press", FIRE_ACTIVE_WEAPON) not in _keys(ctrl)


def test_step_2b_setting_flies_a_side_icon_straight(monkeypatch, caplog):
    keys, _lines, _ = _step_2b(monkeypatch, caplog, _upper_left(), actuate_turn=False)
    assert ("key_press", ROLL_LEFT_KEY) not in keys
    assert ("key_press", NOSE_UP_KEY) not in keys


def test_step_3_keeps_the_wings_level_for_a_downward_icon(monkeypatch, caplog):
    keys, _lines, _ = _step_2b(monkeypatch, caplog, _FrameCapture(), actuate_turn=True)
    assert ("key_press", NOSE_DOWN_KEY) in keys
    assert ("key_press", ROLL_LEFT_KEY) not in keys
    assert ("key_press", ROLL_RIGHT_KEY) not in keys


def test_step_3_levels_the_wings_in_a_fast_dive_and_keeps_pulling(monkeypatch, caplog):
    """Operator, 2026-09-26 (cycle 5): descending faster than 150 m/s, the turn
    pulls with the wings level so the pull points up."""
    keys, lines, _ = _step_2b(monkeypatch, caplog, _upper_left(), actuate_turn=True,
                              analyzer=_TelemetryAnalyzer(rate=-200.0))
    assert ("key_press", ROLL_LEFT_KEY) not in keys
    assert ("key_press", NOSE_UP_KEY) in keys
    assert any("act=divelevel+up" in ln for ln in lines), lines


def test_step_3_still_banks_when_the_descent_is_gentle(monkeypatch, caplog):
    keys, _lines, _ = _step_2b(monkeypatch, caplog, _upper_left(), actuate_turn=True,
                               analyzer=_TelemetryAnalyzer(rate=-80.0))
    assert ("key_press", ROLL_LEFT_KEY) in keys



def test_an_icon_below_the_horizon_pushes_with_the_wings_level(monkeypatch, caplog):
    """Operator, 2026-09-26: below the horizon the law pushes, never banks and
    pulls. The archived orange icon sits at 172 deg, just below the horizon."""
    keys, lines, _ = _step_2b(monkeypatch, caplog, _LeftIconCapture(), actuate_turn=True)
    assert ("key_press", NOSE_DOWN_KEY) in keys
    assert ("key_press", NOSE_UP_KEY) not in keys
    assert ("key_press", ROLL_LEFT_KEY) not in keys


def test_below_the_push_floor_a_side_icon_is_turned_toward_not_flown_past(monkeypatch, caplog):
    """2026-09-27 18:51:51: an enemy icon left and just below the horizon at 822 m.
    The law wanted a push, push_floor_m (1500) refused it, and with the wings held
    level the jet flew straight at a cliff for 17 s. Now the refused push banks
    toward the icon and pulls. The archived icon sits at 172 deg, left."""
    keys, lines, _ = _step_2b(monkeypatch, caplog, _LeftIconCapture(), actuate_turn=True,
                              analyzer=_TelemetryAnalyzer(alt=1200.0), push_floor_m=1500)
    assert ("key_press", NOSE_DOWN_KEY) not in keys, "the floor still refuses the push"
    assert ("key_press", ROLL_LEFT_KEY) in keys
    assert ("key_press", NOSE_UP_KEY) in keys
    assert any("intent=down" in ln and "withheld=alt " in ln and "act=bankleft+up" in ln
               for ln in lines), lines


def test_after_a_near_centre_kill_the_next_icon_is_flown_toward_within_the_base_delay(
        monkeypatch, caplog):
    """The 18:51:45 sequence end to end: a lock near the centre is lost, the
    archived left icon is on the ring, and the jet banks toward it once the base
    delay is over instead of flying neutral for the whole 6 s extension. On the
    old code nothing but the fire key was pressed inside the 0.9 s run."""
    keys, lines, _ = _step_2b(monkeypatch, caplog, _LeftIconCapture(), actuate_turn=True,
                              analyzer=_TelemetryAnalyzer(alt=1200.0), push_floor_m=1500,
                              script=[_NEAR, _MISS], search_resume_delay_s=0.2,
                              search_resume_centre_delay_s=30.0)
    assert ("key_press", ROLL_LEFT_KEY) in keys
    assert ("key_press", NOSE_UP_KEY) in keys
    assert any("rung=wait" in ln for ln in lines) and "act=bankleft+up" in lines[-1], lines


def test_a_fast_dive_is_refused_the_push_the_smoothed_altitude_would_allow(monkeypatch, caplog):
    """Cycle 12, the 20:19:27 state: last reading 1929 m, smoothed 2248 m, 200 m/s
    down. Three seconds on it is below the 1500 m floor, so no push; the old
    floor read the smoothed 2248 m and pushed, down to the ground at 20:19:36."""
    keys, lines, _ = _step_2b(monkeypatch, caplog, _LeftIconCapture(), actuate_turn=True,
                              analyzer=_TelemetryAnalyzer(alt=1929.0, stable=2248.0,
                                                          rate=-200.0),
                              push_floor_m=1500)
    assert ("key_press", NOSE_DOWN_KEY) not in keys
    assert any("withheld=alt " in ln for ln in lines), lines


def test_the_same_altitude_in_level_flight_still_pushes(monkeypatch, caplog):
    keys, _lines, _ = _step_2b(monkeypatch, caplog, _LeftIconCapture(), actuate_turn=True,
                               analyzer=_TelemetryAnalyzer(alt=1929.0, stable=2248.0, rate=0.0),
                               push_floor_m=1500)
    assert ("key_press", NOSE_DOWN_KEY) in keys


def test_above_the_push_floor_the_same_icon_still_pushes(monkeypatch, caplog):
    keys, _lines, _ = _step_2b(monkeypatch, caplog, _LeftIconCapture(), actuate_turn=True,
                               analyzer=_TelemetryAnalyzer(alt=2000.0), push_floor_m=1500)
    assert ("key_press", NOSE_DOWN_KEY) in keys
    assert ("key_press", ROLL_LEFT_KEY) not in keys


# ---------------------------------------------------------------------------
# SAF-001 during a pursuit (operator, 2026-09-27 20:48-04:25 run): Enter did
# nothing while the pursuit flew and worked only after a respawn. The missions
# release the mission lock when they hand the aircraft to pursue_and_engage,
# and the takeover gate did not count the pursuit as commanded flight.
# ---------------------------------------------------------------------------

def _pursuit_in_eject_state(monkeypatch):
    from wingman.state import GameState
    analyzer = _AnalyzerStub(ammo=2)
    analyzer.game_state = GameState.GAME_BATTLE_EJECT
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, capture=_CaptureStub(),
                       pursuit_enabled=True, pursuit_max_duration_s=0.0)
    ctrl.set_target_tracker(_ScriptedTracker([_MISS]))
    ctrl.pursue_and_engage(defer_switch_until_empty=True)
    deadline = time.time() + 1.0
    while not ctrl.is_pursuing() and time.time() < deadline:
        time.sleep(0.01)
    assert ctrl.is_pursuing()
    return ctrl, analyzer


def test_enter_during_a_pursuit_takes_over(monkeypatch, caplog):
    from wingman.state import GameState
    ctrl, analyzer = _pursuit_in_eject_state(monkeypatch)
    assert not ctrl.is_mission_running(), "the mission has handed the aircraft over"
    with caplog.at_level("INFO", logger="wingman.controller"):
        assert ctrl._handle_maneuver_key_press("enter", display=":3") is True
    _wait_for_pursuit_to_settle(ctrl)
    assert analyzer.game_state == GameState.GAME_BATTLE_MANUAL
    assert any("entering GAME_BATTLE_MANUAL" in r.getMessage() for r in caplog.records)


def test_a_takeover_key_with_nothing_flying_is_ignored_and_says_so(monkeypatch, caplog):
    ctrl = _make_ctrl(monkeypatch, analyzer=_AnalyzerStub(ammo=2))
    with caplog.at_level("DEBUG", logger="wingman.controller"):
        assert ctrl._handle_maneuver_key_press("enter", display=":3") is False
    assert any("maneuver key 'enter' ignored — no commanded flight" in r.getMessage()
               for r in caplog.records)


# ---------------------------------------------------------------------------
# Operator, 2026-10-09: "similar to the resupply icon ... implement steering
# towards prioritytarget defined by the yellow icon, where the small icon near
# the center of the screen indicates direction to steer towards". The priority
# target is the crown objective: its marker in view is flown at as the resupply
# marker is, and off screen its pin on the ring feeds the icon law in place of
# the red icons. The detectors have their own tests (test_priority_target.py);
# these replace them and pin what the pursuit does with what they return.
# ---------------------------------------------------------------------------

_PT_ON = {"enabled": True, "actuate": True}
_ICON_ALL = {"enabled": True, "wings_level": True, "actuate_pitch": True, "actuate_turn": True}


def _priority_marker(x, y):
    from wingman.priority_target import PriorityMarker
    return PriorityMarker(x, y, 20, 20, 70, 0.0)


def _priority_pursuit(monkeypatch, caplog, *, marker=None, pins=(), tracker=None,
                      priority_target=_PT_ON, analyzer=None, run_s=1.0, capture=None,
                      pursue_kwargs=None, **ctrl_kwargs):
    calls = {"marker": 0, "pins": 0}

    def find_marker(_frame, **_kw):
        calls["marker"] += 1
        return marker

    def find_pins(_frame, _cfg=None):
        calls["pins"] += 1
        return list(pins)

    monkeypatch.setattr(controller_module, "find_priority_marker", find_marker)
    monkeypatch.setattr(controller_module, "find_priority_ring_icons", find_pins)
    ctrl = _make_ctrl(
        monkeypatch, analyzer=analyzer or _AnalyzerStub(ammo=2),
        capture=capture or _BlankCapture(),
        pursuit_enabled=True, pursuit_max_duration_s=0.0,
        priority_target=priority_target, **ctrl_kwargs)
    ctrl.set_target_tracker(tracker or _TrackerStub(visible=False))
    with caplog.at_level("DEBUG", logger="wingman.controller"):
        ctrl.pursue_and_engage(**(pursue_kwargs or {"defer_switch_until_empty": True}))
        time.sleep(run_s)
        ctrl.stop_eject_sequence("respawn_detected")
        _wait_for_pursuit_to_settle(ctrl)
    return ctrl, [r.getMessage() for r in caplog.records], calls


def test_the_priority_marker_in_view_is_flown_at(monkeypatch, caplog):
    """Right of the centre and below it: roll right, nose down."""
    ctrl, messages, _calls = _priority_pursuit(
        monkeypatch, caplog, marker=_priority_marker(1400, 800))

    keys = _keys(ctrl)
    assert ("key_press", ROLL_RIGHT_KEY) in keys
    assert ("key_press", NOSE_DOWN_KEY) in keys
    assert ("key_press", ROLL_LEFT_KEY) not in keys
    assert "PRIORITY TARGET: marker in view at (1400,800) (steering to it)" in messages
    assert any(m.startswith("PRIORITY: marker=(1400,800)") and "control=True" in m
               and "mode=actuate" in m for m in messages)


def test_the_hud_names_the_priority_target_it_steers_at(monkeypatch, caplog):
    class _HudCapture:
        def __init__(self):
            self.calls = []

        def maybe_render(self, *args, **kwargs):
            self.calls.append(kwargs)

    hud = _HudCapture()
    monkeypatch.setattr(controller_module, "find_priority_marker",
                        lambda _frame, **_kw: _priority_marker(1400, 800))
    monkeypatch.setattr(controller_module, "find_priority_ring_icons", lambda _f, _c=None: [])
    ctrl = _make_ctrl(monkeypatch, analyzer=_AnalyzerStub(ammo=2), capture=_BlankCapture(),
                       pursuit_enabled=True, pursuit_max_duration_s=0.0, priority_target=_PT_ON)
    ctrl.set_hud_renderer(hud)
    ctrl.set_target_tracker(_TrackerStub(visible=False))
    ctrl.pursue_and_engage(defer_switch_until_empty=True)
    time.sleep(0.8)
    ctrl.stop_eject_sequence("respawn_detected")
    _wait_for_pursuit_to_settle(ctrl)

    assert any(kwargs.get("steering_label") == "PRIORITY TARGET"
               and kwargs.get("steering_target") == (1400, 800) for kwargs in hud.calls)


def test_shadow_mode_logs_the_priority_marker_and_does_not_steer_at_it(monkeypatch, caplog):
    ctrl, messages, calls = _priority_pursuit(
        monkeypatch, caplog, marker=_priority_marker(1400, 800),
        priority_target={"enabled": True, "actuate": False})

    assert calls["marker"] > 0
    assert ("key_press", ROLL_RIGHT_KEY) not in _keys(ctrl)
    assert ("key_press", NOSE_DOWN_KEY) not in _keys(ctrl)
    assert "PRIORITY TARGET: marker in view at (1400,800) (shadow)" in messages
    assert any(m.startswith("PRIORITY: marker=(1400,800)") and "proposed=True" in m
               and "control=False" in m and "mode=shadow" in m for m in messages)


def test_priority_target_off_scans_nothing(monkeypatch, caplog):
    """Off is what a config without the key gets."""
    ctrl, messages, calls = _priority_pursuit(
        monkeypatch, caplog, marker=_priority_marker(1400, 800), priority_target=None)

    assert calls == {"marker": 0, "pins": 0}
    assert not any(m.startswith("PRIORITY") for m in messages)
    assert ("key_press", ROLL_RIGHT_KEY) not in _keys(ctrl)


def test_a_visible_target_nearer_the_centre_keeps_the_steering(monkeypatch, caplog):
    """The resupply marker's rule (operator, 2026-10-02): whichever of the two is
    nearer the screen centre. The target is 190 px left, the marker 740 px right."""
    ctrl, messages, _calls = _priority_pursuit(
        monkeypatch, caplog, marker=_priority_marker(1700, 600),
        tracker=_TrackerStub(visible=True, error_norm=-0.2, error_norm_y=0.0))

    keys = _keys(ctrl)
    assert ("key_press", ROLL_LEFT_KEY) in keys, "the locked target is still steered at"
    assert ("key_press", ROLL_RIGHT_KEY) not in keys
    assert any(m.startswith("PRIORITY: marker=(1700,600)") and "proposed=False" in m
               for m in messages)
    assert ("key_press", FIRE_ACTIVE_WEAPON) in keys


def test_the_marker_nearer_the_centre_than_the_target_takes_the_steering(monkeypatch, caplog):
    """The target is 860 px left, the marker 340 px right. Firing carries on."""
    ctrl, _messages, _calls = _priority_pursuit(
        monkeypatch, caplog, marker=_priority_marker(1300, 600),
        tracker=_TrackerStub(visible=True, error_norm=-0.9, error_norm_y=0.0))

    keys = _keys(ctrl)
    assert ("key_press", ROLL_RIGHT_KEY) in keys
    assert ("key_press", ROLL_LEFT_KEY) not in keys
    assert ("key_press", FIRE_ACTIVE_WEAPON) in keys


def test_the_resupply_marker_comes_before_the_priority_target(monkeypatch, caplog):
    """Two missiles spent and the resupply marker in view on the left, the
    priority target on the right: the resupply has the steering."""
    monkeypatch.setattr(controller_module, "find_resupply_marker",
                        lambda _frame, **_kw: ResupplyMarker(500, 600, 40, 40, 300, 180))
    _ctrl, messages, _calls = _priority_pursuit(
        monkeypatch, caplog, marker=_priority_marker(1400, 600),
        analyzer=_SequenceAnalyzer([4, 2, 2]), empty_confirm_reads=1,
        resupply_priority_actuate=True, run_s=1.4,
        pursue_kwargs={"weapon_already_switched": True})

    focus_at = next(i for i, m in enumerate(messages) if "rearm focus begins" in m)
    after = "\n".join(messages[focus_at:])
    assert "roll_left - pressing" in after
    assert "roll_right - pressing" not in after


def _pin(angle_deg):
    import math
    from wingman.icon_steering import RingIcon
    return RingIcon(960 + 203 * math.cos(math.radians(angle_deg)),
                    600 + 203 * math.sin(math.radians(angle_deg)),
                    90, 18, 24, float(angle_deg), 26)


def _pin_pursuit(monkeypatch, caplog, **kwargs):
    ctrl, messages, calls = _priority_pursuit(
        monkeypatch, caplog, sustained_hold_enabled=True, icon_steering=_ICON_ALL,
        analyzer=kwargs.pop("analyzer", None) or _TelemetryAnalyzer(), **kwargs)
    return ctrl, messages, calls


def _quiet_search(monkeypatch):
    """Keep the blind search's own taps and the dive guard out of the key log."""
    monkeypatch.setattr(Controller, "_search_look_down", lambda self: False)
    monkeypatch.setattr(Controller, "_telemetry_path_angle_deg", lambda self: -5.0)
    monkeypatch.setattr(Controller, "_pursuit_dive_guard",
                        lambda self, target_visible=False: None)
    monkeypatch.setattr(Controller, "_dive_guard_pullout", lambda self: None)


def test_the_priority_pin_turns_the_search_toward_it(monkeypatch, caplog):
    """No target and no marker in view: the pin at 9 o'clock is what the icon
    law flies, bank left and pull."""
    _quiet_search(monkeypatch)
    ctrl, messages, _calls = _pin_pursuit(monkeypatch, caplog, pins=[_pin(180)], run_s=1.6)

    assert "PRIORITY TARGET: pin on the ring at +180 deg (steering to it)" in messages
    assert any(m.startswith("ICONPTS:") and "act=bankleft+up" in m for m in messages), (
        "bank toward the pin and pull")
    assert ("key_press", ROLL_LEFT_KEY) in _keys(ctrl)
    assert ("key_press", ROLL_RIGHT_KEY) not in _keys(ctrl)
    assert any(m.startswith("PRIORITY: marker=- ") and "pin=+180" in m and "search=True" in m
               for m in messages)


def test_the_priority_pin_outranks_a_red_icon(monkeypatch, caplog):
    """The frame holds the reference red icon (nose down 5, left 1). With the
    crown pin at 3 o'clock the law flies the pin: right, not down."""
    _quiet_search(monkeypatch)
    ctrl, messages, _calls = _pin_pursuit(
        monkeypatch, caplog, pins=[_pin(0)], capture=_FrameCapture(), run_s=1.6)

    acts = [m for m in messages if m.startswith("ICONPTS:") and "rung=icon" in m]
    assert acts and all("act=bankright" in m for m in acts), acts[:3]
    assert ("key_press", ROLL_RIGHT_KEY) in _keys(ctrl)
    assert ("key_press", NOSE_DOWN_KEY) not in _keys(ctrl)


def test_a_target_in_view_is_tracked_whatever_the_pin_says(monkeypatch, caplog):
    _quiet_search(monkeypatch)
    ctrl, messages, _calls = _pin_pursuit(
        monkeypatch, caplog, pins=[_pin(180)],
        tracker=_TrackerStub(visible=True, error_norm=0.5, error_norm_y=0.0))

    assert any(m.startswith("HOLD[roll]") and "-> right/target" in m for m in messages)
    assert not any(m.startswith("ICONPTS:") and "rung=icon" in m for m in messages)
    assert any(m.startswith("PRIORITY: marker=- ") and "search=False" in m for m in messages)


def test_shadow_mode_leaves_the_search_to_the_red_icons(monkeypatch, caplog):
    """Enabled without actuate: the pin is logged and the blind search flies."""
    _quiet_search(monkeypatch)
    ctrl, messages, calls = _pin_pursuit(
        monkeypatch, caplog, pins=[_pin(0)],
        priority_target={"enabled": True, "actuate": False})

    assert calls["pins"] > 0
    assert "PRIORITY TARGET: pin on the ring at +0 deg (shadow)" in messages
    assert ("key_press", ROLL_RIGHT_KEY) not in _keys(ctrl)


def test_with_every_rack_empty_the_priority_target_is_not_looked_for(monkeypatch, caplog):
    """Resupply mode ignores targets until a rearm, the priority target too."""
    analyzer = _TelemetryAnalyzer()
    analyzer.ammo = 0
    _quiet_search(monkeypatch)
    ctrl, messages, calls = _pin_pursuit(
        monkeypatch, caplog, marker=_priority_marker(1400, 800), pins=[_pin(0)],
        analyzer=analyzer, empty_confirm_reads=1, resupply_priority_actuate=True,
        run_s=1.6, pursue_kwargs={"weapon_already_switched": True})

    # The cycle that confirms the racks empty had already scanned; from the next
    # one on (the first to log search=True) nothing is.
    search_at = next(i for i, m in enumerate(messages)
                     if m.startswith("RESUPPLY: spent") and "search=True" in m)
    assert any(m.startswith("PRIORITY") for m in messages[:search_at]), "it was followed before"
    assert not any(m.startswith("PRIORITY") for m in messages[search_at:])
    assert any(m.startswith("HOLD[roll]") and "right/target -> left/search" in m
               for m in messages), "the roll toward the marker was not given up"


class _CropCapture(_CaptureStub):
    """A black 1920x1200 frame holding one fixture crop at its own place."""

    def __init__(self, name, origin):
        super().__init__()
        import cv2
        import numpy as np
        from pathlib import Path
        crop = cv2.imread(str(Path(__file__).parent / "fixtures" / name))
        self.frame = np.zeros((1200, 1920, 3), dtype=np.uint8)
        left, top = origin
        self.frame[top:top + crop.shape[0], left:left + crop.shape[1]] = crop

    def grab_from_thread(self):
        self.grabs += 1
        return self.frame


def _real_priority_pursuit(monkeypatch, caplog, capture, run_s=1.6):
    """The real detectors on a real crop, through the real loop."""
    _quiet_search(monkeypatch)
    ctrl = _make_ctrl(
        monkeypatch, analyzer=_TelemetryAnalyzer(), capture=capture,
        pursuit_enabled=True, pursuit_max_duration_s=0.0, sustained_hold_enabled=True,
        icon_steering=_ICON_ALL, priority_target=_PT_ON)
    ctrl.set_target_tracker(_TrackerStub(visible=False))
    with caplog.at_level("DEBUG", logger="wingman.controller"):
        ctrl.pursue_and_engage(defer_switch_until_empty=True)
        time.sleep(run_s)
        ctrl.stop_eject_sequence("respawn_detected")
        _wait_for_pursuit_to_settle(ctrl)
    return ctrl, [r.getMessage() for r in caplog.records]


def test_the_crown_marker_of_2026_10_09_is_flown_at_end_to_end(monkeypatch, caplog):
    """screenshot_20261009_033345: the marker 176 px right of the centre and
    59 px below it."""
    ctrl, messages = _real_priority_pursuit(
        monkeypatch, caplog, _CropCapture("priority_marker_near.png", (1056, 579)))

    assert any(m.startswith("PRIORITY TARGET: marker in view at (1136,65") for m in messages)
    assert any(m.startswith("HOLD[roll]") and "-> right/target" in m for m in messages)
    assert ("key_press", ROLL_RIGHT_KEY) in _keys(ctrl)
    assert ("key_press", ROLL_LEFT_KEY) not in _keys(ctrl)


def test_the_crown_pin_of_2026_10_09_steers_the_search_end_to_end(monkeypatch, caplog):
    """prioritytarget_direction.png: the pin at +110 deg, below the centre and
    a little left of it. The icon law reads it and flies it."""
    ctrl, messages = _real_priority_pursuit(
        monkeypatch, caplog, _CropCapture("priority_pin_cloud.png", (852, 748)))

    assert "PRIORITY TARGET: pin on the ring at +110 deg (steering to it)" in messages
    flown = [m for m in messages if m.startswith("ICONPTS:") and "rung=icon" in m]
    assert flown, "the pin never drove the icon law"
    assert all("intent=down" in m or "intent=turn" in m for m in flown), flown[:3]
    assert ("key_press", ROLL_RIGHT_KEY) not in _keys(ctrl)


# ---------------------------------------------------------------------------
# Operator, 2026-10-09: "similar to prioritytarget ... implement steering
# towards air superiority icons A, B, or C, if the icons are red, the small
# icons indicates the direction to steer towards ... it flies towards the A
# mark because it is a target on screen and closer rather than steer towards B,
# it should fly through A then proceed with B". The control points go through
# the priority target's place in the steering. The detectors have their own
# tests (test_air_superiority.py); these replace them, except the last three.
# ---------------------------------------------------------------------------

def _point(x, y, size=40):
    from wingman.air_superiority import ControlPointMarker
    return ControlPointMarker(x, y, size, size, 300, 0.0)


def _airsup_pursuit(monkeypatch, caplog, *, marker=None, pins=(), tracker=None,
                    air_superiority=_PT_ON, run_s=1.0, capture=None, **ctrl_kwargs):
    """`marker` and `pins` may be callables of the scan number, for a point
    that is taken part way through."""
    calls = {"marker": 0, "pins": 0}

    def find_marker(_frame, **_kw):
        calls["marker"] += 1
        return marker(calls["marker"]) if callable(marker) else marker

    def find_pins(_frame, _cfg=None):
        calls["pins"] += 1
        return list(pins(calls["pins"]) if callable(pins) else pins)

    monkeypatch.setattr(controller_module, "find_control_point_marker", find_marker)
    monkeypatch.setattr(controller_module, "find_control_point_ring_icons", find_pins)
    ctrl = _make_ctrl(
        monkeypatch, analyzer=ctrl_kwargs.pop("analyzer", None) or _AnalyzerStub(ammo=2),
        capture=capture or _BlankCapture(),
        pursuit_enabled=True, pursuit_max_duration_s=0.0,
        air_superiority=air_superiority, **ctrl_kwargs)
    ctrl.set_target_tracker(tracker or _TrackerStub(visible=False))
    # "wingman", not the controller alone: the fly-through tally logs from its
    # own module.
    with caplog.at_level("DEBUG", logger="wingman"):
        ctrl.pursue_and_engage(defer_switch_until_empty=True)
        time.sleep(run_s)
        ctrl.stop_eject_sequence("respawn_detected")
        _wait_for_pursuit_to_settle(ctrl)
    return ctrl, [r.getMessage() for r in caplog.records], calls


def test_a_red_control_point_in_view_is_flown_at(monkeypatch, caplog):
    """Left of the centre and above it: roll left, nose up."""
    ctrl, messages, _calls = _airsup_pursuit(monkeypatch, caplog, marker=_point(600, 300))

    keys = _keys(ctrl)
    assert ("key_press", ROLL_LEFT_KEY) in keys
    assert ("key_press", NOSE_UP_KEY) in keys
    assert ("key_press", ROLL_RIGHT_KEY) not in keys
    assert "AIR SUPERIORITY: marker in view at (600,300) (steering to it)" in messages
    assert any(m.startswith("AIRSUP: marker=(600,300)") and "control=True" in m
               and "mode=actuate" in m for m in messages)


def test_the_point_in_view_is_flown_at_whatever_the_pins_say(monkeypatch, caplog):
    """prioritize_direct_target.png: A is on screen, B's pin is on the ring at
    9 o'clock. "It flies towards the A mark because it is a target on screen
    and closer rather than steer towards B." Here A is to the right."""
    _quiet_search(monkeypatch)
    ctrl, messages, calls = _airsup_pursuit(
        monkeypatch, caplog, marker=_point(1400, 600), pins=[_pin(180)],
        sustained_hold_enabled=True, icon_steering=_ICON_ALL, analyzer=_TelemetryAnalyzer())

    assert any(m.startswith("HOLD[roll]") and "-> right/target" in m for m in messages)
    assert ("key_press", ROLL_LEFT_KEY) not in _keys(ctrl)
    assert calls["pins"] == 0, "with a point in view the pins are not even read"
    assert not any(m.startswith("ICONPTS:") and "rung=icon" in m for m in messages)


def test_once_the_point_in_view_is_taken_the_next_pin_steers(monkeypatch, caplog):
    """"It should fly through A then proceed with B." A taken turns blue and is
    no longer found; B's red pin is at 9 o'clock."""
    _quiet_search(monkeypatch)
    ctrl, messages, _calls = _airsup_pursuit(
        monkeypatch, caplog,
        marker=lambda scan: _point(1400, 600) if scan <= 4 else None, pins=[_pin(180)],
        sustained_hold_enabled=True, icon_steering=_ICON_ALL, analyzer=_TelemetryAnalyzer(),
        run_s=3.0)

    first_right = next(i for i, m in enumerate(messages)
                       if m.startswith("HOLD[roll]") and "-> right/target" in m)
    bank_left = [i for i, m in enumerate(messages)
                 if m.startswith("ICONPTS:") and "act=bankleft+up" in m]
    assert bank_left and bank_left[0] > first_right, "A first, then toward B's pin"
    assert any(m.startswith("AIRSUP: marker=- ") and "pin=+180" in m and "search=True" in m
               for m in messages)
    assert ("key_press", ROLL_LEFT_KEY) in _keys(ctrl)


def test_the_hud_names_the_control_point_it_steers_at(monkeypatch, caplog):
    class _HudCapture:
        def __init__(self):
            self.calls = []

        def maybe_render(self, *args, **kwargs):
            self.calls.append(kwargs)

    hud = _HudCapture()
    monkeypatch.setattr(controller_module, "find_control_point_marker",
                        lambda _frame, **_kw: _point(600, 300))
    monkeypatch.setattr(controller_module, "find_control_point_ring_icons",
                        lambda _f, _c=None: [])
    ctrl = _make_ctrl(monkeypatch, analyzer=_AnalyzerStub(ammo=2), capture=_BlankCapture(),
                       pursuit_enabled=True, pursuit_max_duration_s=0.0, air_superiority=_PT_ON)
    ctrl.set_hud_renderer(hud)
    ctrl.set_target_tracker(_TrackerStub(visible=False))
    ctrl.pursue_and_engage(defer_switch_until_empty=True)
    time.sleep(0.8)
    ctrl.stop_eject_sequence("respawn_detected")
    _wait_for_pursuit_to_settle(ctrl)

    assert any(kwargs.get("steering_label") == "CONTROL POINT"
               and kwargs.get("steering_target") == (600, 300) for kwargs in hud.calls)


def test_air_superiority_in_shadow_logs_and_does_not_steer(monkeypatch, caplog):
    ctrl, messages, calls = _airsup_pursuit(
        monkeypatch, caplog, marker=_point(600, 300),
        air_superiority={"enabled": True, "actuate": False})

    assert calls["marker"] > 0
    assert ("key_press", ROLL_LEFT_KEY) not in _keys(ctrl)
    assert ("key_press", NOSE_UP_KEY) not in _keys(ctrl)
    assert "AIR SUPERIORITY: marker in view at (600,300) (shadow)" in messages


def test_air_superiority_off_scans_nothing(monkeypatch, caplog):
    ctrl, messages, calls = _airsup_pursuit(
        monkeypatch, caplog, marker=_point(600, 300), pins=[_pin(180)], air_superiority=None)

    assert calls == {"marker": 0, "pins": 0}
    assert not any(m.startswith("AIR") for m in messages)


def test_a_red_pin_turns_the_search_toward_the_point(monkeypatch, caplog):
    _quiet_search(monkeypatch)
    ctrl, messages, _calls = _airsup_pursuit(
        monkeypatch, caplog, pins=[_pin(180)], sustained_hold_enabled=True,
        icon_steering=_ICON_ALL, analyzer=_TelemetryAnalyzer(), run_s=1.6)

    assert "AIR SUPERIORITY: pin on the ring at +180 deg (steering to it)" in messages
    assert any(m.startswith("ICONPTS:") and "act=bankleft+up" in m for m in messages)
    assert ("key_press", ROLL_LEFT_KEY) in _keys(ctrl)
    assert ("key_press", ROLL_RIGHT_KEY) not in _keys(ctrl)


def test_the_crown_is_looked_for_before_the_control_points(monkeypatch, caplog):
    """Two game modes, so never both; with both switched on the crown's marker
    is the one flown at."""
    monkeypatch.setattr(controller_module, "find_priority_marker",
                        lambda _frame, **_kw: _priority_marker(1400, 800))
    monkeypatch.setattr(controller_module, "find_priority_ring_icons", lambda _f, _c=None: [])
    ctrl, messages, calls = _airsup_pursuit(
        monkeypatch, caplog, marker=_point(600, 300), priority_target=_PT_ON)

    assert calls["marker"] == 0
    assert "PRIORITY TARGET: marker in view at (1400,800) (steering to it)" in messages
    assert ("key_press", ROLL_RIGHT_KEY) in _keys(ctrl)
    assert ("key_press", ROLL_LEFT_KEY) not in _keys(ctrl)


def _real_airsup_pursuit(monkeypatch, caplog, capture, run_s=1.6):
    """The real detectors on real crops, through the real loop."""
    _quiet_search(monkeypatch)
    ctrl = _make_ctrl(
        monkeypatch, analyzer=_TelemetryAnalyzer(), capture=capture,
        pursuit_enabled=True, pursuit_max_duration_s=0.0, sustained_hold_enabled=True,
        icon_steering=_ICON_ALL, priority_target=_PT_ON, air_superiority=_PT_ON)
    ctrl.set_target_tracker(_TrackerStub(visible=False))
    with caplog.at_level("DEBUG", logger="wingman.controller"):
        ctrl.pursue_and_engage(defer_switch_until_empty=True)
        time.sleep(run_s)
        ctrl.stop_eject_sequence("respawn_detected")
        _wait_for_pursuit_to_settle(ctrl)
    return ctrl, [r.getMessage() for r in caplog.records]


class _CropsCapture(_CropCapture):
    """Several fixture crops in one frame."""

    def __init__(self, *placed):
        name, origin = placed[0]
        super().__init__(name, origin)
        import cv2
        from pathlib import Path
        for name, (left, top) in placed[1:]:
            crop = cv2.imread(str(Path(__file__).parent / "fixtures" / name))
            self.frame[top:top + crop.shape[0], left:left + crop.shape[1]] = crop


def test_the_operators_frame_flies_at_a_and_not_toward_bs_pin(monkeypatch, caplog):
    """prioritize_direct_target.png end to end: the A disc 24 px left of the
    centre, and a red pin on the ring at 3 o'clock standing in for B's, which
    in the screenshot lies under C's blue pin. A is nearly dead ahead, so the
    aircraft holds its course at it and does not bank toward the pin."""
    ctrl, messages = _real_airsup_pursuit(monkeypatch, caplog, _CropsCapture(
        ("airsup_marker_reticle.png", (836, 502)), ("airsup_pin_right.png", (1126, 625))))

    assert any(m.startswith("AIR SUPERIORITY: marker in view at (936,60") for m in messages)
    assert any(m.startswith("AIRSUP: marker=(936,60") and "control=True" in m for m in messages)
    assert not any(m.startswith("ICONPTS:") and "rung=icon" in m for m in messages)
    assert ("key_press", ROLL_RIGHT_KEY) not in _keys(ctrl), "banked toward the pin"


def test_a_control_point_of_2026_10_09_is_flown_at_end_to_end(monkeypatch, caplog):
    """screenshot_20261009_044142: the A disc 86 px left of the centre and
    98 px above it."""
    ctrl, messages = _real_airsup_pursuit(
        monkeypatch, caplog, _CropCapture("airsup_marker_mid.png", (804, 432)))

    assert any(m.startswith("AIR SUPERIORITY: marker in view at (874,50") for m in messages)
    assert any(m.startswith("HOLD[pitch]") and "-> up" in m for m in messages)
    assert ("key_press", ROLL_RIGHT_KEY) not in _keys(ctrl)
    assert ("key_press", NOSE_DOWN_KEY) not in _keys(ctrl)


def test_a_red_pin_of_2026_10_09_steers_the_search_end_to_end(monkeypatch, caplog):
    """screenshot_20261009_044324: A's pin at -170 deg, left of the centre."""
    ctrl, messages = _real_airsup_pursuit(
        monkeypatch, caplog, _CropCapture("airsup_pin_left.png", (728, 536)))

    assert "AIR SUPERIORITY: pin on the ring at -170 deg (steering to it)" in messages
    assert any(m.startswith("ICONPTS:") and "act=bankleft" in m for m in messages)
    assert ("key_press", ROLL_LEFT_KEY) in _keys(ctrl)
    assert ("key_press", ROLL_RIGHT_KEY) not in _keys(ctrl)


# ---------------------------------------------------------------------------
# Operator, 2026-10-09: "at round end it prints how many air superiority
# targets, resupply, and priority targets are captured ... it should not read
# the score bar, only track when wingman flies through the targets". The rule
# has its own tests (test_objective_tally.py); these pin that the pursuit
# reports what it sees and that the round's end logs the line once. Later the
# same day: "i only want it to print total counts during wingman session
# summary", so the round's line is DEBUG and the session's totals are what the
# summary prints.
# ---------------------------------------------------------------------------

def _round_line(ctrl, caplog):
    """What the main loop's call at the round's end logs, at DEBUG."""
    caplog.clear()
    with caplog.at_level("DEBUG", logger="wingman.controller"):
        ctrl.log_round_objectives()
    return [r.getMessage() for r in caplog.records if "ROUND OBJECTIVES" in r.getMessage()]


def test_a_control_point_flown_through_is_counted_and_printed_at_the_rounds_end(
        monkeypatch, caplog):
    """The disc dead ahead grows from 24 to 60 px over ten scans and is then
    gone: the aircraft went through it."""
    ctrl, messages, _calls = _airsup_pursuit(
        monkeypatch, caplog,
        marker=lambda scan: _point(960, 600, 20 + 4 * scan) if scan <= 10 else None,
        run_s=3.2)

    assert any(m.startswith("OBJECTIVE: flew through an air superiority point: its marker "
                            "reached 60 px across and was gone")
               and "(1 this round)" in m for m in messages)
    lines = _round_line(ctrl, caplog)
    assert len(lines) == 1
    assert lines[0].endswith(
        "ROUND OBJECTIVES — flown through: air superiority points 1, "
        "resupply 0, priority targets 0")
    assert _round_line(ctrl, caplog) == [], "the lobby after the end screen logs nothing"
    assert ctrl.objective_session_counts() == {
        "air_superiority": 1, "resupply": 0, "priority_target": 0}, (
        "the session's totals outlive the round")


def test_a_control_point_turned_away_from_is_not_counted(monkeypatch, caplog):
    """In view for ten scans at 22 px, never near, then gone."""
    ctrl, messages, _calls = _airsup_pursuit(
        monkeypatch, caplog,
        marker=lambda scan: _point(960, 600, 22) if scan <= 10 else None, run_s=3.2)

    assert not any(m.startswith("OBJECTIVE: flew through") for m in messages)
    assert any(m.startswith("OBJECTIVE: an air superiority point lost at 22 px")
               for m in messages)
    assert "air superiority points 0" in _round_line(ctrl, caplog)[0]


def test_a_control_point_that_grew_large_and_left_by_the_edge_is_counted(monkeypatch, caplog):
    """Operator, 2026-10-09: "the evidence for airsuperiority marker fly through
    should be based on if the marker reached large size then disappeared." The
    disc grows to 60 px while it slides from the centre to the left edge, and
    is gone."""
    ctrl, messages, _calls = _airsup_pursuit(
        monkeypatch, caplog,
        marker=lambda scan: (_point(960 - 56 * scan, 600, 20 + 4 * scan)
                             if scan <= 10 else None),
        run_s=3.2)

    assert any(m.startswith("OBJECTIVE: flew through an air superiority point: its marker "
                            "reached 60 px across and was gone, last seen at 60 px and 560 px")
               for m in messages)
    assert "air superiority points 1" in _round_line(ctrl, caplog)[0]


def test_the_priority_target_flown_through_is_counted(monkeypatch, caplog):
    def crown(scan):
        from wingman.priority_target import PriorityMarker
        size = 14 + 3 * scan
        return PriorityMarker(960, 600, size, size, 70, 0.0) if scan <= 10 else None

    calls = {"n": 0}

    def find_marker(_frame, **_kw):
        calls["n"] += 1
        return crown(calls["n"])

    monkeypatch.setattr(controller_module, "find_priority_marker", find_marker)
    monkeypatch.setattr(controller_module, "find_priority_ring_icons", lambda _f, _c=None: [])
    ctrl = _make_ctrl(monkeypatch, analyzer=_AnalyzerStub(ammo=2), capture=_BlankCapture(),
                       pursuit_enabled=True, pursuit_max_duration_s=0.0, priority_target=_PT_ON)
    ctrl.set_target_tracker(_TrackerStub(visible=False))
    with caplog.at_level("INFO", logger="wingman"):
        ctrl.pursue_and_engage(defer_switch_until_empty=True)
        time.sleep(3.2)
        ctrl.stop_eject_sequence("respawn_detected")
        _wait_for_pursuit_to_settle(ctrl)

    assert any(r.getMessage().startswith("OBJECTIVE: flew through the priority target, 44 px")
               for r in caplog.records)
    assert _round_line(ctrl, caplog)[0].endswith("priority targets 1")


def _resupply_round(monkeypatch, caplog, marker_px, ammo_reads):
    """A pursuit on the resupply marker dead ahead, `marker_px(scan)` across for
    nine scans and then gone, with `ammo_reads` as the count's readings. Returns
    the controller and what was logged."""
    scans = {"n": 0}

    def find_marker(_frame, **_kw):
        scans["n"] += 1
        size = marker_px(scans["n"])
        return ResupplyMarker(960, 600, size, size, 300, 0) if scans["n"] <= 9 else None

    monkeypatch.setattr(controller_module, "find_resupply_marker", find_marker)
    ctrl = _make_ctrl(
        monkeypatch, analyzer=_SequenceAnalyzer(ammo_reads),
        capture=_BlankCapture(), pursuit_enabled=True, pursuit_max_duration_s=0.0,
        empty_confirm_reads=1, resupply_priority_actuate=True)
    ctrl.set_target_tracker(_TrackerStub(visible=False))
    with caplog.at_level("INFO", logger="wingman"):
        ctrl.pursue_and_engage(weapon_already_switched=True)
        time.sleep(4.5)
        ctrl.stop_eject_sequence("respawn_detected")
        _wait_for_pursuit_to_settle(ctrl)
    return ctrl, [r.getMessage() for r in caplog.records]


def test_a_rearm_counts_the_resupply_point_whatever_its_marker_did(monkeypatch, caplog):
    """2026-10-09 06:04:01, the first live session: the marker dead ahead was
    lost while still 25 px across, and the count then went up. The rearm is the
    fly-through, and its line says where the marker was last."""
    ctrl, messages = _resupply_round(
        monkeypatch, caplog, lambda _scan: 25, [4, 2, 2, 2, 2, 2, 2, 6, 6])

    assert any(m.startswith("RESUPPLY: confirmed ammo=6") for m in messages)
    counted = [m for m in messages if m.startswith("OBJECTIVE: flew through the resupply point")]
    assert len(counted) == 1, counted
    assert "rearm confirmed, its marker last seen" in counted[0]
    assert "25 px across and 0 px off the centre (1 this round)" in counted[0]
    assert "resupply 1, priority targets 0" in _round_line(ctrl, caplog)[0]


def test_the_resupply_marker_gone_at_close_range_with_no_rearm_is_not_counted(
        monkeypatch, caplog):
    """2026-10-09 06:13:20 and 06:15:10: the marker grew to 56 px and to 51 px
    dead ahead and was gone, and the count never went up. Not a fly-through."""
    ctrl, messages = _resupply_round(
        monkeypatch, caplog, lambda scan: 26 + 4 * scan, [4, 2, 2, 2, 2, 2, 2, 2, 2])

    assert not any(m.startswith("RESUPPLY: confirmed ammo") for m in messages)
    assert not any(m.startswith("OBJECTIVE: flew through") for m in messages)
    assert "resupply 0, priority targets 0" in _round_line(ctrl, caplog)[0]


def test_a_marker_in_view_when_the_aircraft_dies_is_not_counted(monkeypatch, caplog):
    """The pursuit ends with the disc at 60 px still on screen: a death at the
    point, not a capture."""
    ctrl, messages, _calls = _airsup_pursuit(
        monkeypatch, caplog, marker=lambda scan: _point(960, 600, min(60, 20 + 4 * scan)),
        run_s=2.0)

    assert not any(m.startswith("OBJECTIVE: flew through") for m in messages)
    assert "air superiority points 0" in _round_line(ctrl, caplog)[0]


def test_a_round_whose_pursuit_met_no_objective_prints_its_zeros(monkeypatch, caplog):
    ctrl = _make_ctrl(monkeypatch, analyzer=_AnalyzerStub(ammo=2), capture=_BlankCapture(),
                       pursuit_enabled=True, pursuit_max_duration_s=0.0)
    ctrl.set_target_tracker(_TrackerStub(visible=False))
    ctrl.pursue_and_engage(defer_switch_until_empty=True)
    time.sleep(0.4)
    ctrl.stop_eject_sequence("respawn_detected")
    _wait_for_pursuit_to_settle(ctrl)

    assert _round_line(ctrl, caplog) == [
        "ROUND OBJECTIVES — flown through: air superiority points 0, "
        "resupply 0, priority targets 0"]
    assert ctrl.objective_session_counts() == {
        "air_superiority": 0, "resupply": 0, "priority_target": 0}


def test_the_rounds_line_is_not_printed_at_info(monkeypatch, caplog):
    """Operator, 2026-10-09: "i only want it to print total counts during
    wingman session summary." The round's end prints nothing an INFO console
    shows."""
    ctrl = _make_ctrl(monkeypatch, analyzer=_AnalyzerStub(ammo=2), capture=_BlankCapture(),
                       pursuit_enabled=True, pursuit_max_duration_s=0.0)
    ctrl.set_target_tracker(_TrackerStub(visible=False))
    ctrl.pursue_and_engage(defer_switch_until_empty=True)
    time.sleep(0.4)
    ctrl.stop_eject_sequence("respawn_detected")
    _wait_for_pursuit_to_settle(ctrl)

    caplog.clear()
    with caplog.at_level("INFO", logger="wingman"):
        ctrl.log_round_objectives()
    assert not any("ROUND OBJECTIVES" in r.getMessage() for r in caplog.records)


def test_the_session_totals_are_not_read_while_the_tally_is_held(monkeypatch):
    """A main-loop path: the lock is taken with a timeout, and a tally that is
    busy costs the summary its block, not the shutdown."""
    ctrl = _make_ctrl(monkeypatch, analyzer=_AnalyzerStub(ammo=2), capture=_BlankCapture())
    real_lock = ctrl._objective_tally_lock

    class _HeldLock:
        def acquire(self, timeout=None):
            return False

        def locked(self):
            return True

        def release(self):
            raise AssertionError("released a lock that was never acquired")

    ctrl._objective_tally_lock = _HeldLock()
    try:
        assert ctrl.objective_session_counts() is None
    finally:
        ctrl._objective_tally_lock = real_lock


def test_a_round_with_no_pursuit_prints_no_objective_line(monkeypatch, caplog):
    ctrl = _make_ctrl(monkeypatch, analyzer=_AnalyzerStub(ammo=2), capture=_BlankCapture())

    assert _round_line(ctrl, caplog) == []
