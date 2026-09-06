"""mission_loiter: the survival hold (behaviour-driven).

The previous implementation was thirteen scripted manoeuvres run once, with no
feedback: it could not tell whether it had gained altitude, dropped flares on a
timer rather than on a threat, and ended after ~90 s leaving the aircraft
wherever the script had put it.
"""

import threading
import time
import unittest.mock as mock

from wingman.telemetry import TelemetrySignal, TelemetrySnapshot


def _Snap(alt, fresh=True, speed=900, alt_rate=0.0):
    """A REAL TelemetrySnapshot, not a fake mirroring the caller.

    The original fake exposed `altitude.stable` and made `altitude_fresh` a
    property. The real signal field is `stable_value` and `altitude_fresh` is a
    method — so the fake reproduced the caller's two bugs instead of exposing
    them, and nine tests passed against code that raised AttributeError on its
    first live tick (2026-09-04 07:14).

    ADR 114 needs `pitch_angle_deg()` as well, which is derived from the
    altitude RATE against speed. Building the real object means the tests get
    the real derivation — a hand-written `pitch_angle_deg` on a fake would once
    again only prove the code agrees with itself.
    """
    now = time.time()
    ts = None if not fresh else now
    return TelemetrySnapshot(
        speed=TelemetrySignal(value=speed, stable_value=speed, ts=ts, rate=0.0),
        altitude=TelemetrySignal(value=alt, stable_value=alt, ts=ts,
                                 rate=alt_rate),
        taken_at_s=now,
        stale_after_s=6.0,
    )


def _loiter_ctrl(snaps):
    """Controller stub running only the loiter loop."""
    from wingman.controller import Controller
    c = Controller.__new__(Controller)
    c._mission_lock = threading.Lock()
    c._mission_complete = threading.Event()
    c._mission_cancel = threading.Event()
    c._climb_stop = threading.Event()
    c._loitering = threading.Event()      # ADR 109: the survival-hold flag
    c._ejecting = threading.Event()       # ADR 111: an eject may be in flight
    c._eject_stop = threading.Event()
    c._eject_stop_reason = ""
    c._exit_event = threading.Event()
    c._analyzer = mock.MagicMock()
    c._analyzer.get_telemetry.side_effect = list(snaps) + [snaps[-1]] * 200
    c._loiter_target_alt = 7000.0
    c._loiter_hysteresis_m = 500.0
    c._loiter_orbit_direction = "right"
    c._loiter_orbit_interval_s = 0.0     # every tick, for the test
    c._loiter_orbit_hold_s = 0.6
    c._loiter_orbit_pitch_s = 0.35
    c._loiter_orbit_deadband_m = 150.0   # ADR 112: the pitch deadband
    c._loiter_level_band_deg = 20.0      # ADR 114: level before circling
    c._loiter_recover_hold_s = 0.8
    c._loiter_entry_pullup_s = 5.0       # ADR 123: entry pull-up
    c._loiter_lock_timeout_s = 5.0
    c._loiter_boundary = None             # ADR 111: orbit direction inputs
    c._loiter_window_min = None
    c._loiter_prev_window_min = None
    c._loiter_window_started = 0.0
    c._loiter_avoid_frac = 0.60
    c._loiter_orbit_window_s = 15.0
    c._loiter_closing_margin = 0.05
    c._loiter_boundary_max_age_s = 4.0
    c._loiter_tick_s = 0.01
    c.climb_mode = mock.MagicMock()
    c.roll_right = mock.MagicMock()
    c.nose_up = mock.MagicMock()      # ADR 112: pitch corrects toward target
    c.nose_down = mock.MagicMock()
    c.roll_left = mock.MagicMock()
    return c


def _run_briefly(c, seconds=0.2):
    t = threading.Thread(target=c.mission_loiter, daemon=True)
    t.start()
    time.sleep(seconds)
    c.cancel_mission = lambda: None
    c._mission_cancel.set()
    t.join(timeout=2.0)


def test_below_the_band_it_climbs():
    c = _loiter_ctrl([_Snap(2000)])
    _run_briefly(c)
    assert c.climb_mode.called, "below the hold band the objective is altitude"
    assert not c.roll_right.called


def test_inside_the_band_it_orbits():
    c = _loiter_ctrl([_Snap(7200)])
    _run_briefly(c)
    assert c.roll_right.called, "at altitude the objective is to keep turning"
    assert not c.climb_mode.called


def test_the_hysteresis_prevents_chattering_at_the_edge():
    """6600 m is below target but inside the band — climbing there would fight
    the orbit every few seconds."""
    c = _loiter_ctrl([_Snap(6600)])
    _run_briefly(c)
    assert not c.climb_mode.called
    assert c.roll_right.called


def test_stale_telemetry_commands_nothing():
    """A climb ordered on an aged-out reading is a climb ordered blind."""
    c = _loiter_ctrl([_Snap(2000, fresh=False)])
    _run_briefly(c)
    assert not c.climb_mode.called
    assert not c.roll_right.called and not c.roll_left.called


def test_it_keeps_holding_rather_than_running_a_sequence_once():
    """The old script ended after ~90 s. A survival hold that stops holding is
    not one."""
    c = _loiter_ctrl([_Snap(7200)])
    _run_briefly(c, seconds=0.3)
    assert c.roll_right.call_count > 1


def test_cancel_stops_the_loop_and_any_climb():
    c = _loiter_ctrl([_Snap(2000)])
    _run_briefly(c)
    assert c._mission_complete.is_set()
    assert c._climb_stop.is_set(), "a climb left running would keep the pitch axis"
    assert not c._mission_lock.locked()


def test_flares_are_not_on_a_timer():
    import pathlib
    src = pathlib.Path("wingman/controller.py").read_text()
    # Slice to the NEXT method, not to a fixed byte count. The window was 6000
    # chars, and adding comments to mission_loiter pushed its body past that —
    # the test then failed with ValueError instead of saying anything about
    # flares.
    start = src.index("def mission_loiter")
    end = src.index("\n    def ", start + 100)
    body = src[start:end]
    assert "deploy_flares" not in body, \
        "flares belong to the incoming detector, which knows when one is inbound"


# --- the pin_memory warning is silenced, and only that one ------------------

def test_the_pin_memory_warning_is_filtered_by_message():
    """EasyOCR sets pin_memory=True unconditionally and torch warns on a
    CPU-only host — correct, and unactionable, since CPU-only is the design
    (ADR 020).

    It repeated because heap_census uses warnings.catch_warnings(), and leaving
    that block bumps the global filters version, invalidating every module's
    __warningregistry__ and defeating the once-per-location dedupe.

    Filtered by MESSAGE rather than by muting UserWarning or the torch module,
    so an unrelated warning from the same place still reaches the operator."""
    import pathlib
    src = pathlib.Path("wingman/main.py").read_text()
    i = src.index("warnings.filterwarnings(")
    call = src[i:i + 240]
    assert "pin_memory" in call
    assert "category=UserWarning" in call
    assert 'module=' not in call, "muting the whole module would hide real warnings"


def test_the_filter_survives_a_catch_warnings_block():
    """The dedupe reset is what made this warning repeat, so the fix has to
    hold across one. Warnings are recorded rather than read off stderr: pytest
    installs its own capture, so redirecting stderr sees nothing."""
    import warnings
    msg = ("'pin_memory' argument is set as true but no accelerator is found, "
           "then device pinned memory won't be used.")
    with warnings.catch_warnings(record=True) as rec:
        warnings.resetwarnings()
        # First match wins and filterwarnings PREPENDS, so the catch-all is
        # registered first and the ignore second, leaving the ignore in front.
        warnings.filterwarnings("always")
        warnings.filterwarnings(
            "ignore", message=r".*pin_memory.*no accelerator is found.*",
            category=UserWarning)
        warnings.warn(msg, UserWarning, stacklevel=2)
        with warnings.catch_warnings():
            pass                      # the thing that reset the dedupe
        warnings.warn(msg, UserWarning, stacklevel=2)
        warnings.warn("an unrelated torch warning", UserWarning, stacklevel=2)
    texts = [str(w.message) for w in rec]
    assert not any("pin_memory" in t for t in texts), texts
    assert any("an unrelated torch warning" in t for t in texts), texts


def test_the_survival_hold_flag_is_set_while_running_and_cleared_on_cancel():
    """ADR 109 V4. The flag suppresses Eject, so a stuck one leaves an aircraft
    loiter no longer owns unable to eject — quieter than the bug it fixes, and
    harder to attribute. Cleared in `finally`, so every exit path drops it."""
    c = _loiter_ctrl([_Snap(2000)])
    t = threading.Thread(target=c.mission_loiter, daemon=True)
    t.start()
    time.sleep(0.1)
    assert c.is_survival_hold(), "Eject is not suppressed while loiter runs"
    c.cancel_mission = lambda: None
    c._mission_cancel.set()
    t.join(timeout=2.0)
    assert not c.is_survival_hold(), "the hold outlived the mission"


def test_the_flag_clears_even_when_the_loop_raises():
    """`finally`, not the end of the loop body — an exception mid-loop must not
    leave Eject suppressed for the rest of the session."""
    c = _loiter_ctrl([_Snap(2000)])
    c._analyzer.get_telemetry.side_effect = RuntimeError("telemetry exploded")
    t = threading.Thread(target=c.mission_loiter, daemon=True)
    t.start()
    t.join(timeout=2.0)
    assert not c.is_survival_hold()
    assert not c._mission_lock.locked(), "the lock must be released too"


def test_below_target_in_the_band_the_orbit_pulls_up():
    """ADR 110. A banked turn with no back-pressure descends: on 2026-09-04 the
    orbit sank 6709 m -> 6385 m in 18 s, which re-triggered the climb, which
    overshot to +90 deg at 67 KPH, stalled, and dived 5600 m into the ground.
    Four times in five minutes. Below target, pitch arrests the sink."""
    c = _loiter_ctrl([_Snap(6700)])
    _run_briefly(c, seconds=0.3)
    assert c.roll_right.called, "no turn"
    assert c.nose_up.called, "rolled without back-pressure — the orbit sinks"
    assert not c.nose_down.called


def test_above_target_in_the_band_the_orbit_pushes_down():
    """ADR 112. Holding nose-up unconditionally is the same error mirrored.
    Measured 2026-09-04 18:47: the orbit climbed 6634 m -> 10189 m, 3200 m ABOVE
    target, with speed decaying 1782 -> 528 KPH, toward the same stall from the
    other side. A hold that climbs out of its band is not holding."""
    c = _loiter_ctrl([_Snap(7400)])
    _run_briefly(c, seconds=0.3)
    assert c.roll_right.called, "no turn"
    assert c.nose_down.called, "climbing away from the hold with no correction"
    assert not c.nose_up.called


def test_inside_the_deadband_the_orbit_commands_no_pitch():
    """The point of the deadband. Correcting on every tick is what drove the
    aircraft out of the band in BOTH directions; at target the orbit is roll
    only."""
    c = _loiter_ctrl([_Snap(7050)])       # 50 m error, inside the 150 m band
    _run_briefly(c, seconds=0.3)
    assert c.roll_right.called, "the circle is unconditional"
    assert not c.nose_up.called and not c.nose_down.called, \
        "pitched inside the deadband — this is the oscillation, not a fix"


def test_starting_a_hold_cancels_an_eject_already_in_flight():
    """ADR 111. ADR 109 keeps Eject from being SELECTED during a hold; it does
    nothing about a dive already running when the hold begins.

    2026-09-04 15:40:55: 'y' pressed six seconds into an eject, loiter started,
    and the dive kept pulsing at nose -58 deg for another eleven seconds until
    the aircraft hit the ground. The operator asked to stay alive."""
    c = _loiter_ctrl([_Snap(2000)])
    c._ejecting.set()
    _run_briefly(c, seconds=0.15)
    assert c._eject_stop.is_set(), "the dive kept flying the aircraft down"
    assert c._eject_stop_reason == "survival_hold"


def test_no_eject_running_means_nothing_to_cancel():
    """The stop must not be set speculatively — a stray stop event would abort
    the NEXT eject before it starts."""
    c = _loiter_ctrl([_Snap(2000)])
    _run_briefly(c, seconds=0.15)
    assert not c._eject_stop.is_set()


def test_the_hold_pre_empts_a_running_mission():
    """ADR 111. GAME_BATTLE starts mission_j20 automatically; 'y' is an operator
    command and outranks it.

    The old acquire(blocking=False) made the keypress a silent no-op whenever
    j20 held the lock — the operator pressed 'y' and nothing happened."""
    c = _loiter_ctrl([_Snap(2000)])
    cancelled = []
    c._mission_lock.acquire()                    # j20 is running
    def _cancel():
        cancelled.append(True)
        if c._mission_lock.locked():
            c._mission_lock.release()            # the outgoing mission lets go
    c.cancel_mission = _cancel

    t = threading.Thread(target=c.mission_loiter, daemon=True)
    t.start()
    time.sleep(0.2)
    assert cancelled, "the running mission was never cancelled"
    assert c.is_survival_hold(), "the hold did not start"
    c.cancel_mission = lambda: None
    c._mission_cancel.set()
    t.join(timeout=2.0)


def test_a_stuck_teardown_refuses_rather_than_double_booking():
    """Two missions on one airframe is worse than none, so a lock that never
    releases means the hold does not start."""
    c = _loiter_ctrl([_Snap(2000)])
    c._mission_lock.acquire()
    c.cancel_mission = lambda: None              # teardown is stuck
    c._loiter_lock_timeout_s = 0.2
    t = threading.Thread(target=c.mission_loiter, daemon=True)
    t.start()
    t.join(timeout=8.0)
    assert not c.is_survival_hold(), "started a second mission on a held lock"


def _dir_ctrl():
    from wingman.controller import Controller
    c = Controller.__new__(Controller)
    c._loiter_orbit_direction = "right"
    c._loiter_boundary = None
    c._loiter_window_min = None
    c._loiter_prev_window_min = None
    c._loiter_window_started = 0.0
    c._loiter_avoid_frac = 0.60
    c._loiter_orbit_window_s = 15.0
    c._loiter_closing_margin = 0.05
    c._loiter_boundary_max_age_s = 4.0
    return c


def _feed(c, samples, t0=100.0, step=1.0):
    for i, d in enumerate(samples):
        c.note_boundary(d, 0.10)
        c._loiter_pick_orbit_direction(t0 + i * step)


def test_a_circle_whose_range_merely_oscillates_is_left_alone():
    """The defect this rule replaces. In a circle the range naturally rises and
    falls — you approach the edge on one half and recede on the other. The
    per-sample rule read that as failure and reversed six times in thirty
    seconds (2026-09-04 18:19), so the aircraft wove instead of circling."""
    c = _dir_ctrl()
    _feed(c, [0.46, 0.51, 0.36, 0.48, 0.43, 0.48, 0.55, 0.50] * 6)
    assert c._loiter_orbit_direction == "right", "reversed on ordinary oscillation"


def test_a_circle_drifting_into_the_edge_reverses():
    """Two consecutive orbits whose CLOSEST approach is materially nearer: the
    circle is walking into the edge, not just varying."""
    c = _dir_ctrl()
    _feed(c, [0.50] * 16, t0=100.0)          # first window, closest 0.50
    _feed(c, [0.30] * 16, t0=120.0)          # second window, closest 0.30
    assert c._loiter_orbit_direction == "left"


def test_one_orbit_is_not_enough_to_reverse():
    """A trend needs two windows; acting on the first is acting on no history."""
    c = _dir_ctrl()
    _feed(c, [0.20] * 16, t0=100.0)
    assert c._loiter_orbit_direction == "right"


def test_leaving_the_band_forgets_the_history():
    """Comparing across an excursion compares geometries that have nothing to do
    with each other."""
    c = _dir_ctrl()
    _feed(c, [0.30] * 16, t0=100.0)
    _feed(c, [0.95], t0=120.0)
    assert c._loiter_prev_window_min is None
    assert c._loiter_window_started == 0.0


def test_a_boundary_outside_the_band_never_reverses_the_orbit():
    c = _dir_ctrl()
    _feed(c, [0.90, 0.85, 0.88] * 10)
    assert c._loiter_orbit_direction == "right"


def test_a_stale_reading_is_not_acted_on():
    """A gap in perception says nothing about where the edge is now."""
    c = _dir_ctrl()
    c.note_boundary(0.30, 0.1)
    c._loiter_boundary = (0.30, 0.1, 0.0)      # ancient
    c._loiter_pick_orbit_direction(1000.0)
    assert c._loiter_orbit_direction == "right"


def test_no_reading_leaves_the_orbit_alone():
    c = _dir_ctrl()
    c.note_boundary(None, None)
    c._loiter_pick_orbit_direction(100.0)
    assert c._loiter_orbit_direction == "right"


def test_a_steep_nose_up_handover_levels_before_circling():
    """ADR 114. The climb exits on ALTITUDE and says nothing about attitude.

    Measured 2026-09-04 21:20:54: handover at 7113 m with the nose at +82 deg
    and 962 KPH. The aircraft zoomed on to 8401 m, bled to 207 KPH, stalled,
    departed to -69 deg and was descending through 6208 m thirty seconds later.
    A 0.35 s orbit pulse cannot argue with that, and rolling an aircraft with
    no energy is how the departure happens.
    """
    snap = _Snap(7113, speed=962, alt_rate=200.0)
    assert abs(snap.pitch_angle_deg()) > 20.0, "premise: this is a steep climb"
    c = _loiter_ctrl([snap])
    _run_briefly(c, seconds=0.3)
    assert c.nose_down.called, "did not level a +82 deg handover"
    assert not c.roll_right.called, "banked an aircraft that is still zooming"
    assert not c.roll_left.called


def test_a_departed_nose_down_aircraft_is_recovered_not_orbited():
    """The other half of the same cycle: at -69 deg the hold must pull, not
    circle."""
    snap = _Snap(7113, speed=800, alt_rate=-200.0)
    assert snap.pitch_angle_deg() < -20.0, "premise: this is a steep dive"
    c = _loiter_ctrl([snap])
    _run_briefly(c, seconds=0.3)
    assert c.nose_up.called, "did not recover a departed aircraft"
    assert not c.roll_right.called and not c.roll_left.called


def test_a_level_aircraft_at_altitude_still_orbits():
    """Recovery must not swallow the normal case — this is the regression that
    would turn the hold into a no-op."""
    snap = _Snap(7050, speed=900, alt_rate=0.0)
    assert abs(snap.pitch_angle_deg()) <= 20.0, "premise: this is level flight"
    c = _loiter_ctrl([snap])
    _run_briefly(c, seconds=0.3)
    assert c.roll_right.called, "a level aircraft at altitude must circle"


def test_recovery_uses_a_longer_input_than_the_orbit_nudge():
    """The orbit's 0.35 s pulse is a trim correction; recovery is a manoeuvre.
    Using the trim value here is what made the live failure unrecoverable."""
    snap = _Snap(7113, speed=962, alt_rate=200.0)
    c = _loiter_ctrl([snap])
    _run_briefly(c, seconds=0.3)
    held = c.nose_down.call_args.kwargs["hold_seconds"]
    assert held == c._loiter_recover_hold_s > c._loiter_orbit_pitch_s


def test_climbing_from_below_is_not_treated_as_a_recovery():
    """Below the band a steep nose-up is the climb doing its job. Levelling
    there would fight climb_mode and the hold would never reach altitude."""
    snap = _Snap(2000, speed=900, alt_rate=200.0)
    assert abs(snap.pitch_angle_deg()) > 20.0
    c = _loiter_ctrl([snap])
    _run_briefly(c, seconds=0.3)
    assert c.climb_mode.called, "stopped climbing to level the nose"
    assert not c.nose_down.called


def test_the_loiter_config_block_actually_reaches_the_controller():
    """ADR 116. It did not, for the life of the feature.

    `Controller.__init__` read `config.get("loiter_mission")` behind an
    `isinstance(config, dict)` guard, and `config` is a ControllerConfig
    dataclass — so the guard was always False and every loiter value silently
    used its hardcoded default. Nothing revealed it because the defaults
    happened to equal the YAML, right up until 2026-09-04 21:41 when a changed
    target_alt of 5000 produced a live log line reading "target 7000 m".

    This asserts the WIRING, with values deliberately unlike any default.
    """
    from wingman.controller_config import ControllerConfig
    cfg = ControllerConfig.from_config({
        "loiter_mission": {
            "target_alt": 1234,
            "hysteresis_m": 321,
            "orbit_deadband_m": 77,
            "level_band_deg": 11,
            "recover_hold_s": 1.75,
        }})
    # Read the block exactly the way Controller.__init__ does.
    lo = getattr(cfg, "loiter", None) or {}
    assert lo, "the loiter block did not survive ControllerConfig.from_config"
    assert float(lo.get("target_alt", 7000)) == 1234.0
    assert float(lo.get("hysteresis_m", 500)) == 321.0
    assert float(lo.get("orbit_deadband_m", 150.0)) == 77.0
    assert float(lo.get("level_band_deg", 20.0)) == 11.0
    assert float(lo.get("recover_hold_s", 0.8)) == 1.75


def test_the_shipped_config_is_what_the_controller_would_use():
    """The live failure was a config value that never took effect. This reads
    the SHIPPED config.yaml through the real ControllerConfig and checks the
    value the hold would actually fly."""
    import yaml
    from wingman.controller_config import ControllerConfig
    with open("wingman/config.yaml") as fh:
        cfg = ControllerConfig.from_config(yaml.safe_load(fh))
    assert cfg.loiter.get("target_alt") is not None, \
        "loiter_mission is not reaching the controller"
    assert cfg.loiter["target_alt"] == 5000, \
        "ADR 115 chose 5000 m; a change here needs its own ADR and evidence"


# --- ADR 123: NOSE_DIRECTION and the entry pull-up ---------------------------

def test_a_nose_down_entry_pulls_up_before_the_hold_begins():
    """ADR 123. Measured 2026-09-05 21:31:41: the hold was started with the
    aircraft at 139 m in a -74 deg dive at 2652 KPH, and it hit the ground five
    seconds later. The tree's own recovery could not help — below the band the
    hold climbs, and climb_mode takes seconds to establish while the ground
    arrives in one."""
    from wingman.analyzer import NOSE_DOWN
    c = _loiter_ctrl([_Snap(2000)])
    c._analyzer.nose_direction.return_value = NOSE_DOWN
    _run_briefly(c, seconds=0.3)
    assert c.nose_up.called, "entered a hold nose-down without pulling up"
    kw = c.nose_up.call_args_list[0].kwargs
    assert kw["hold_seconds"] == 5.0, "pull-up was not the configured duration"
    assert kw["block"] is True, \
        "a non-blocking pulse is what the dive already outran"


def test_a_nose_up_entry_does_not_pull_up():
    from wingman.analyzer import NOSE_UP
    c = _loiter_ctrl([_Snap(7050)])
    c._analyzer.nose_direction.return_value = NOSE_UP
    _run_briefly(c, seconds=0.3)
    assert not c.nose_up.called


def test_an_unknown_nose_direction_does_not_pull_up():
    """No evidence of a descent is not evidence of one. Five seconds of held
    back-pressure is itself a way to stall a healthy entry."""
    from wingman.analyzer import NOSE_UNKNOWN
    c = _loiter_ctrl([_Snap(7050)])
    c._analyzer.nose_direction.return_value = NOSE_UNKNOWN
    _run_briefly(c, seconds=0.3)
    assert not c.nose_up.called


def test_nose_direction_tracks_the_altitude_rate():
    """The state itself. Rising is UP, falling is DOWN."""
    from wingman.analyzer import (GameStateAnalyzer, NOSE_UP, NOSE_DOWN,
                                  NOSE_UNKNOWN)
    a = GameStateAnalyzer.__new__(GameStateAnalyzer)
    a._nose_direction = NOSE_UNKNOWN
    a._nose_direction_deadband_mps = 5.0
    assert a.nose_direction() == NOSE_UNKNOWN
    a._update_nose_direction(_Snap(5000, alt_rate=+40.0))
    assert a.nose_direction() == NOSE_UP
    a._update_nose_direction(_Snap(5000, alt_rate=-40.0))
    assert a.nose_direction() == NOSE_DOWN


def test_the_deadband_holds_the_last_direction():
    """Level flight jitters around zero. A direction that flips every tick is
    not a direction — the entry check reads it once, at an instant it did not
    choose."""
    from wingman.analyzer import GameStateAnalyzer, NOSE_UP, NOSE_UNKNOWN
    a = GameStateAnalyzer.__new__(GameStateAnalyzer)
    a._nose_direction = NOSE_UNKNOWN
    a._nose_direction_deadband_mps = 5.0
    a._update_nose_direction(_Snap(5000, alt_rate=+40.0))
    for jitter in (+1.0, -2.0, +0.5, -4.9):
        a._update_nose_direction(_Snap(5000, alt_rate=jitter))
        assert a.nose_direction() == NOSE_UP, f"flipped on {jitter} m/s"


def test_a_stale_reading_does_not_change_the_direction():
    """A stale altitude rate describes the past. Holding the last known answer
    is the point of tracking it as state."""
    from wingman.analyzer import GameStateAnalyzer, NOSE_UP, NOSE_UNKNOWN
    a = GameStateAnalyzer.__new__(GameStateAnalyzer)
    a._nose_direction = NOSE_UNKNOWN
    a._nose_direction_deadband_mps = 5.0
    a._update_nose_direction(_Snap(5000, alt_rate=+40.0))
    a._update_nose_direction(_Snap(5000, fresh=False, alt_rate=-99.0))
    assert a.nose_direction() == NOSE_UP


def test_the_entry_pull_up_survives_the_pre_emption_cancel():
    """ADR 123, found live 2026-09-05 10:12:59.

    `mission_loiter` calls `cancel_mission()` to pre-empt whatever is running
    (ADR 111), which sets `_mission_cancel`. The pull-up was placed BEFORE that
    flag is cleared, so `nose_up(block=True)` honoured it and returned after ten
    milliseconds:

        nose_up - pressing 'i' key for 5.0 seconds
        nose_up cancelled

    The hold entered a -64 deg dive having done nothing. Every hold that
    pre-empts a mission takes this path, which is nearly all of them.
    """
    from wingman.analyzer import NOSE_DOWN
    c = _loiter_ctrl([_Snap(2000)])
    c._analyzer.nose_direction.return_value = NOSE_DOWN
    # The state the pre-emption leaves behind, which the old fixture never had.
    c._mission_cancel.set()

    seen = {}

    def _nose_up(hold_seconds=None, block=None):
        seen["cancel_set_during_pull_up"] = c._mission_cancel.is_set()
        seen["hold_seconds"] = hold_seconds

    c.nose_up = _nose_up
    _run_briefly(c, seconds=0.3)
    assert "hold_seconds" in seen, "the pull-up never ran"
    assert seen["cancel_set_during_pull_up"] is False, \
        "pull-up ran with _mission_cancel set — nose_up will abort immediately"
    assert seen["hold_seconds"] == 5.0


def test_a_cut_short_entry_pull_up_is_reported_loudly(caplog):
    """ADR 126. The 2026-09-05 10:12:59 failure was silent: `nose_up` returned
    after ten milliseconds and the log showed only that the pull-up had been
    requested. The hold entered a -64 deg dive looking, in the log, exactly like
    one that had recovered.

    A guard that can silently not run is not a guard.
    """
    import logging
    from wingman.analyzer import NOSE_DOWN
    c = _loiter_ctrl([_Snap(2000)])
    c._analyzer.nose_direction.return_value = NOSE_DOWN
    c.nose_up = mock.MagicMock()          # returns instantly, as the bug did
    with caplog.at_level(logging.INFO):
        _run_briefly(c, seconds=0.3)
    text = caplog.text
    assert "ENTRY PULL-UP CUT SHORT" in text, \
        "a pull-up that did not hold was not reported"
    assert any(r.levelno >= logging.ERROR for r in caplog.records), \
        "cut-short pull-up must be an ERROR — it is a silent safety failure"


def test_a_full_length_entry_pull_up_is_not_reported_as_cut_short(caplog):
    """The guard must not cry wolf, or the real one will be ignored."""
    import logging
    import time as _time
    from wingman.analyzer import NOSE_DOWN
    c = _loiter_ctrl([_Snap(2000)])
    c._analyzer.nose_direction.return_value = NOSE_DOWN
    c._loiter_entry_pullup_s = 0.05       # keep the test quick
    c.nose_up = lambda **kw: _time.sleep(kw.get("hold_seconds", 0))
    with caplog.at_level(logging.INFO):
        _run_briefly(c, seconds=0.4)
    assert "ENTRY PULL-UP CUT SHORT" not in caplog.text
    assert "entry pull-up held" in caplog.text
