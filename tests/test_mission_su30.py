"""mission_su30 (ADR 144, docs/missions/su30.md): the scripted Su-30 sequence.

  1. nose up on battle start or respawn
  2. NO weapon switch: the spawn weapon stays selected until it runs out
     (operator, 2026-09-24; ADR 144 D4). The switch to the secondary happens
     only then, in pursue_and_engage's deferred switch (tested in
     test_pursuit_mode.py) or the missiles-empty response.
  3. at 3000 m set the nose angle to -10 deg   4. activate pursuit mode

The mission is deliberately a script, unlike the adaptive mission_j20, so these
tests pin the ORDER of the steps and the things that make a script dangerous on
a live aircraft: pressing a toggle key it should not, commanding on stale telemetry,
writing the pitch axis while another hold owns it, and diving because of a
missing collaborator. Its engagement mode is boresight_engage only — the fire
loop without the padlock camera — and never search_and_destroy, and nothing
else the mission triggers may press the padlock camera either.
"""

import math
import pathlib
import threading
import time

import pytest
import yaml

import wingman.controller as controller_module
from wingman.analyzer import GameState
from wingman.config_schema import validate_config
from wingman.controller import Controller, MISSION_SU30_KEY
from wingman.controller_config import ControllerConfig
from wingman.telemetry import TelemetrySignal, TelemetrySnapshot

_ROOT = pathlib.Path(__file__).resolve().parents[1]

_SPEED_KPH = 900.0


def _snap(alt, angle_deg=0.0, ts=None):
    """A REAL TelemetrySnapshot whose flight-path angle is `angle_deg`.

    Altitude rate is derived from speed and the angle so that the real
    `pitch_angle_deg()` derivation runs (see test_mission_loiter._Snap for why
    a hand-written fake would only prove the code agrees with itself).
    """
    ts = time.time() if ts is None else ts
    rate = (_SPEED_KPH / 3.6) * math.sin(math.radians(angle_deg))
    return TelemetrySnapshot(
        speed=TelemetrySignal(value=_SPEED_KPH, stable_value=_SPEED_KPH, ts=ts, rate=0.0),
        altitude=TelemetrySignal(value=alt, stable_value=alt, ts=ts, rate=rate),
        taken_at_s=ts,
        stale_after_s=6.0,
    )


class _Analyzer:
    """Scripted telemetry. Each get_telemetry() call consumes one entry (the
    last repeats), stamped with a NEW timestamp so the nose-angle step, which
    acts only on fresh samples, sees each one as new evidence."""

    def __init__(self, samples, state=GameState.GAME_BATTLE, refuse_events=()):
        self._samples = list(samples)
        self._n = 0
        self.game_state = state
        self.events = []
        self._refuse = set(refuse_events)
        self._last_battle_event_ts = 0.0

    def get_telemetry(self):
        alt, angle = self._samples[min(self._n, len(self._samples) - 1)]
        self._n += 1
        return _snap(alt, angle, ts=1000.0 + self._n)

    def trigger_event(self, name):
        self.events.append(name)
        return name not in self._refuse

    def get_ammo_missiles(self):
        return 2

    def get_ammo_flares(self):
        return 4

    def get_health(self):
        return 100


class _Tracker:
    def update(self, frame):
        return {"visible": False, "error_norm": None, "error_norm_y": None}


_FAST = {"tick_s": 0.005, "angle_pulse_s": 0.01, "angle_max_s": 2.0,
         "angle_confirm_reads": 2, "angle_tolerance_deg": 4.0,
         "climb_alt_m": 3000, "nose_angle_deg": -10, "lock_timeout_s": 1.0}


def _make_ctrl(monkeypatch, analyzer=None, tracker=None, su30=None, **cfg):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    ctrl = Controller(
        (0, 0, 1920, 1200),
        analyzer=analyzer,
        exit_event=threading.Event(),
        config=ControllerConfig(simulate_os_input=True, disable_hotkeys=True,
                                su30={**_FAST, **(su30 or {})}, **cfg),
    )
    if tracker is not None:
        ctrl.set_target_tracker(tracker)
    return ctrl


def _record(ctrl, monkeypatch):
    """Replace the actuators with recorders on one shared, ordered log."""
    log = []
    monkeypatch.setattr(ctrl, "climb_mode",
                        lambda **kw: log.append(("climb", kw.get("target_alt"))))
    monkeypatch.setattr(ctrl, "switch_weapon",
                        lambda *a, **k: log.append(("switch_weapon",)))
    monkeypatch.setattr(ctrl, "nose_down",
                        lambda *a, **k: log.append(("nose_down",)))
    monkeypatch.setattr(ctrl, "nose_up",
                        lambda *a, **k: log.append(("nose_up",)))
    # The second element is whether the hand-off deferred the weapon switch
    # (defer_switch_until_empty), so ("pursue", True) still reads as "pursuit
    # was activated the way step 4 activates it".
    monkeypatch.setattr(
        ctrl, "pursue_and_engage",
        lambda **kw: log.append(("pursue", kw.get("defer_switch_until_empty"))))
    # ADR 144: the engagement loops. The mission may start boresight_engage and
    # must never start search_and_destroy (its padlock camera is the point).
    monkeypatch.setattr(ctrl, "start_boresight_engage_loop",
                        lambda: log.append(("boresight_start",)))
    monkeypatch.setattr(ctrl, "stop_boresight_engage_loop",
                        lambda: log.append(("boresight_stop",)))
    monkeypatch.setattr(ctrl, "start_search_and_destroy_loop",
                        lambda: log.append(("sdl_start",)))
    return log


def _run_mission(ctrl, **kwargs):
    t = threading.Thread(target=ctrl.mission_su30, kwargs=kwargs, daemon=True)
    t.start()
    return t


def _wait(pred, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


def _drain(ctrl, thread):
    """Cancel and wait the mission out (mission_su30 clears the cancel on entry,
    so keep re-asserting it until the lock is actually free)."""
    deadline = time.time() + 5.0
    while ctrl.is_mission_running() and time.time() < deadline:
        ctrl._mission_cancel.set()
        time.sleep(0.02)
    thread.join(timeout=3.0)
    assert not ctrl.is_mission_running()


# ---------------------------------------------------------------------------
# The sequence
# ---------------------------------------------------------------------------

def test_the_four_steps_run_in_the_documented_order(monkeypatch):
    """climb, (no weapon press), nose angle, pursuit — and only after 3000 m is
    reached."""
    analyzer = _Analyzer([
        (1500, 30), (2200, 30),        # still climbing: the wait must not end
        (3100, 30),                     # level-off altitude reached
        (3100, 30),                     # nose 40 deg high: pulse down
        (3090, -12), (3080, -9),        # within tolerance twice: confirmed
    ])
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, tracker=_Tracker())
    log = _record(ctrl, monkeypatch)

    t = _run_mission(ctrl)
    t.join(timeout=5.0)
    assert not t.is_alive(), "mission_su30 did not finish"

    kinds = [e[0] for e in log]
    assert kinds[0] == "climb", "step 1 (nose up) must come first"
    assert "switch_weapon" not in kinds, (
        "the spawn weapon stays selected until it runs out; the mission must "
        "not press the toggle key at all")
    assert "nose_down" in kinds, "a nose 40 deg high must be pulsed down"
    # Loop teardown is bookkeeping around the steps, not a step itself.
    steps = [k for k in kinds if k != "boresight_stop"]
    assert steps[-1] == "pursue", "pursuit must be the last thing the script does"
    # The level-off climb was aimed at the documented altitude.
    assert ("climb", 3000.0) in log


def test_pursuit_is_told_to_defer_the_weapon_switch(monkeypatch):
    """pursue_and_engage presses the toggle key at its own start unless told
    otherwise — without this step 4 would switch weapons the moment pursuit
    begins, which is exactly what the operator asked not to happen. It must not
    be told the weapon "is already switched" either: that flag would also
    suppress the switch when the weapon does run out."""
    analyzer = _Analyzer([(3100, -10)])
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, tracker=_Tracker())
    captured = {}
    monkeypatch.setattr(ctrl, "climb_mode", lambda **kw: None)
    monkeypatch.setattr(ctrl, "nose_down", lambda *a, **k: None)
    monkeypatch.setattr(ctrl, "nose_up", lambda *a, **k: None)
    monkeypatch.setattr(ctrl, "start_boresight_engage_loop", lambda: None)
    monkeypatch.setattr(ctrl, "stop_boresight_engage_loop", lambda: None)
    monkeypatch.setattr(ctrl, "pursue_and_engage", lambda **kw: captured.update(kw))

    _run_mission(ctrl).join(timeout=5.0)

    assert captured.get("defer_switch_until_empty") is True
    assert not captured.get("weapon_already_switched")


def test_pursuit_goes_through_the_eject_fsm_seam(monkeypatch):
    """Pursuit is a behavior inside GAME_BATTLE_EJECT (HLDD 015 D1): that state
    is what makes the tree yield the airframe and routes a respawn to
    stop_eject_sequence."""
    analyzer = _Analyzer([(3100, -10)])
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, tracker=_Tracker())
    captured = {}
    monkeypatch.setattr(ctrl, "climb_mode", lambda **kw: None)
    monkeypatch.setattr(ctrl, "switch_weapon", lambda *a, **k: None)
    monkeypatch.setattr(
        ctrl, "pursue_and_engage",
        lambda **kw: captured.update(on_complete=kw.get("on_complete")))

    _run_mission(ctrl).join(timeout=5.0)

    assert analyzer.events == ["eject_started"]
    # The completion callback closes the state it opened, and only from it.
    analyzer.game_state = GameState.GAME_BATTLE_EJECT
    captured["on_complete"]()
    assert analyzer.events == ["eject_started", "eject_complete"]
    analyzer.game_state = GameState.GAME_BATTLE
    captured["on_complete"]()
    assert analyzer.events == ["eject_started", "eject_complete"]


def test_the_mission_never_presses_the_weapon_key_or_sets_the_flag(monkeypatch):
    """SWITCH_WEAPON is a toggle and the flag is the only record of whether it
    was pressed this life. The mission does neither: the flag stays False, so
    AMMO_MISSILE is still understood to read the rack that is firing (ADR 088's
    rearm-abort and the crash_with_missiles check assume exactly that)."""
    analyzer = _Analyzer([(3100, -10)])
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, tracker=_Tracker())
    log = _record(ctrl, monkeypatch)

    assert not ctrl.is_secondary_weapon_active()
    _run_mission(ctrl).join(timeout=5.0)
    assert not ctrl.is_secondary_weapon_active()
    assert ("switch_weapon",) not in log

    # Same life, mission started again (hotkey re-press / resume from manual).
    log.clear()
    _run_mission(ctrl).join(timeout=5.0)
    assert ("switch_weapon",) not in log
    assert not ctrl.is_secondary_weapon_active()


def test_a_flag_already_set_this_life_is_left_alone(monkeypatch):
    """The weapon already ran out and was switched earlier in this life (by the
    missiles-empty response), then the mission is re-entered: the flag is the
    record of that press and must survive, and nothing presses the key again."""
    analyzer = _Analyzer([(3100, -10)])
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, tracker=_Tracker())
    log = _record(ctrl, monkeypatch)
    ctrl._eject_weapon_switched = True

    _run_mission(ctrl).join(timeout=5.0)

    assert ("switch_weapon",) not in log
    assert ctrl.is_secondary_weapon_active()


# ---------------------------------------------------------------------------
# Never commanding blind, never a second pitch writer
# ---------------------------------------------------------------------------

def test_stale_altitude_never_advances_the_script(monkeypatch):
    """An altitude that has aged out says nothing about where the aircraft is
    (ADR 038). Above-target but stale must not release the climb or start the
    nose-angle step."""
    # A real snapshot whose signal timestamps are older than stale_after_s.
    def _stale_snapshot():
        return TelemetrySnapshot(
            speed=TelemetrySignal(value=_SPEED_KPH, stable_value=_SPEED_KPH, ts=1.0, rate=0.0),
            altitude=TelemetrySignal(value=5000, stable_value=5000, ts=1.0, rate=0.0),
            taken_at_s=100.0, stale_after_s=6.0)

    analyzer = _Analyzer([(5000, 0)])
    analyzer.get_telemetry = _stale_snapshot
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, tracker=_Tracker())
    log = _record(ctrl, monkeypatch)

    t = _run_mission(ctrl)
    assert _wait(lambda: ctrl.is_mission_running())
    time.sleep(0.15)

    assert [e for e in log if e[0] in ("nose_down", "nose_up", "pursue")] == []
    _drain(ctrl, t)


def test_nose_angle_step_stands_aside_for_a_climb_hold(monkeypatch):
    """3000 m is below ADR 141's 4000 m altitude floor, so the tree's floor climb
    owns the pitch axis around the level-off altitude. Pulsing NOSE_DOWN against
    its NOSE_UP would be two writers on one axis."""
    analyzer = _Analyzer([(3100, 30)])
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, tracker=_Tracker(),
                      su30={"angle_max_s": 0.3})
    log = _record(ctrl, monkeypatch)
    ctrl._climbing.set()                 # a climb hold is running

    ctrl._mission_cancel.clear()
    result = ctrl._su30_set_nose_angle()

    assert result is False, "a step that never got the axis cannot report success"
    assert [e for e in log if e[0] in ("nose_down", "nose_up")] == []


def test_nose_angle_step_pulses_in_the_direction_of_the_error(monkeypatch):
    analyzer = _Analyzer([(3100, 30), (3100, -40), (3100, -10), (3100, -10)])
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, tracker=_Tracker())
    log = _record(ctrl, monkeypatch)

    ctrl._mission_cancel.clear()
    assert ctrl._su30_set_nose_angle() is True

    assert [e[0] for e in log] == ["nose_down", "nose_up"], (
        "+30 is too high (pulse down), -40 is too low (pulse up), -10 is on target")


def test_nose_angle_step_acts_on_new_samples_only(monkeypatch):
    """Telemetry lands every ~3 s; the loop ticks every 0.5 s. Re-pulsing on the
    same sample would stack pulses against a reading that has not moved."""
    analyzer = _Analyzer([(3100, 30)])
    fixed = _snap(3100, 30, ts=5000.0)
    analyzer.get_telemetry = lambda: fixed          # same timestamp every tick
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, tracker=_Tracker(),
                      su30={"angle_max_s": 0.3})
    log = _record(ctrl, monkeypatch)

    ctrl._mission_cancel.clear()
    ctrl._su30_set_nose_angle()

    assert [e[0] for e in log].count("nose_down") == 1


def test_nose_angle_step_is_bounded(monkeypatch):
    """A reading that never converges must not hold the script forever."""
    analyzer = _Analyzer([(3100, 40)])
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, tracker=_Tracker(),
                      su30={"angle_max_s": 0.2})
    _record(ctrl, monkeypatch)

    ctrl._mission_cancel.clear()
    started = time.time()
    assert ctrl._su30_set_nose_angle() is False
    assert time.time() - started < 2.0


def test_a_climb_hold_that_never_lets_go_does_not_wedge_the_script(monkeypatch):
    """The shipped level-off altitude (3000 m) is below the tree's 4000 m floor,
    so the tree restarts a climb every time this mission stops one. The nose-angle
    step must then give up after its bound and hand over to pursuit rather than
    wait forever — pressing no pitch key in the meantime."""
    analyzer = _Analyzer([(3100, 30)])
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, tracker=_Tracker(),
                      su30={"angle_max_s": 0.2})
    log = _record(ctrl, monkeypatch)
    ctrl._climbing.set()                 # the floor climb is always running
    monkeypatch.setattr(ctrl, "_su30_stop_climb", lambda: None)   # tree restarts it

    t = _run_mission(ctrl)
    t.join(timeout=5.0)

    assert not t.is_alive(), "the script wedged behind a climb hold"
    assert ("pursue", True) in log, "pursuit must still be activated"
    assert [e for e in log if e[0] in ("nose_down", "nose_up")] == []


# ---------------------------------------------------------------------------
# Failure paths: no pursuit is better than the wrong pursuit
# ---------------------------------------------------------------------------

def test_missing_tracker_holds_instead_of_diving(monkeypatch):
    """pursue_and_engage falls back to eject_and_dive when no tracker is wired.
    That is right for a missiles-empty aircraft and wrong here: an armed
    aircraft must not be dived because of a wiring fault."""
    analyzer = _Analyzer([(3100, -10)])
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, tracker=None)
    log = _record(ctrl, monkeypatch)

    t = _run_mission(ctrl)
    assert _wait(lambda: any(e[0] == "climb" for e in log))
    time.sleep(0.15)

    assert ("pursue", True) not in log
    assert analyzer.events == [], "eject_started must not fire without a tracker"
    assert ctrl.is_mission_running(), "the mission holds until cancelled"
    _drain(ctrl, t)
    assert ctrl._climb_stop.is_set(), "an abandoned script must not leave a climb running"


def test_fsm_refusal_keeps_pursuit_from_starting(monkeypatch):
    analyzer = _Analyzer([(3100, -10)], refuse_events=("eject_started",))
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, tracker=_Tracker())
    log = _record(ctrl, monkeypatch)

    t = _run_mission(ctrl)
    assert _wait(lambda: analyzer.events == ["eject_started"])
    time.sleep(0.1)

    assert not any(e[0] == "pursue" for e in log)
    _drain(ctrl, t)


def test_cancel_during_the_climb_ends_the_script_and_stops_the_climb(monkeypatch):
    """A respawn or takeover mid-climb must not carry NOSE_UP into the next life."""
    analyzer = _Analyzer([(1000, 20)])       # never reaches 3000 m
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, tracker=_Tracker())
    log = _record(ctrl, monkeypatch)

    t = _run_mission(ctrl)
    assert _wait(lambda: any(e[0] == "climb" for e in log))
    _drain(ctrl, t)

    assert not any(e[0] in ("nose_down", "pursue") for e in log)
    assert ctrl._climb_stop.is_set()
    assert not ctrl.is_mission_running()


def test_the_lock_is_released_after_the_handoff(monkeypatch):
    """pursue_and_engage cancels the mission to own both axes; the mission
    must actually end, or the restart after the next respawn is refused."""
    analyzer = _Analyzer([(3100, -10)])
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, tracker=_Tracker())
    _record(ctrl, monkeypatch)

    t = _run_mission(ctrl)
    t.join(timeout=5.0)

    assert not t.is_alive()
    assert not ctrl.is_mission_running()


# ---------------------------------------------------------------------------
# Engagement mode: boresight_engage only, never search_and_destroy
# ---------------------------------------------------------------------------

def test_engagement_is_boresight_only_and_never_search_and_destroy(monkeypatch):
    """The mission's engagement mode is the fire loop WITHOUT the padlock camera."""
    analyzer = _Analyzer([(3100, -10)])
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, tracker=_Tracker())
    log = _record(ctrl, monkeypatch)

    _run_mission(ctrl).join(timeout=5.0)

    kinds = [e[0] for e in log]
    assert "boresight_start" in kinds, "the mission never started boresight_engage"
    assert "sdl_start" not in kinds, \
        "search_and_destroy presses the padlock camera; su30 must never start it"


def test_boresight_starts_without_any_weapon_switch_before_it(monkeypatch):
    """The loop fires whatever is selected — the spawn weapon — so nothing is
    switched ahead of it. (It used to start after a step-2 switch so it could
    only fire the secondary; the operator reversed that on 2026-09-24.)"""
    analyzer = _Analyzer([(3100, -10)])
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, tracker=_Tracker())
    log = _record(ctrl, monkeypatch)

    _run_mission(ctrl).join(timeout=5.0)

    kinds = [e[0] for e in log]
    assert "boresight_start" in kinds
    assert "switch_weapon" not in kinds


def test_boresight_also_starts_when_the_weapon_was_already_switched(monkeypatch):
    """A second start inside one life, after the weapon has run out and been
    switched, must still bring the engagement loop up."""
    analyzer = _Analyzer([(3100, -10)])
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, tracker=_Tracker())
    log = _record(ctrl, monkeypatch)
    ctrl._eject_weapon_switched = True

    _run_mission(ctrl).join(timeout=5.0)

    kinds = [e[0] for e in log]
    assert "switch_weapon" not in kinds
    assert "boresight_start" in kinds


def test_boresight_is_stopped_before_pursuit_takes_the_fire_key(monkeypatch):
    """Pursuit fires for itself every cycle; two writers on the fire key would
    double its cadence for the moment they overlap."""
    analyzer = _Analyzer([(3100, -10)])
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, tracker=_Tracker())
    log = _record(ctrl, monkeypatch)

    _run_mission(ctrl).join(timeout=5.0)

    kinds = [e[0] for e in log]
    assert kinds.index("boresight_stop") < kinds.index("pursue")


def test_boresight_keeps_running_while_the_mission_holds_without_pursuit(monkeypatch):
    """No tracker: the mission holds (see the test above) and, like mission_j20's
    search-and-destroy during a hold, keeps engaging until it is cancelled."""
    analyzer = _Analyzer([(3100, -10)])
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, tracker=None)
    log = _record(ctrl, monkeypatch)

    t = _run_mission(ctrl)
    assert _wait(lambda: ("boresight_start",) in log)
    time.sleep(0.15)
    assert ("boresight_stop",) not in log, "the hold must not end its engagement loop"

    _drain(ctrl, t)
    assert ("boresight_stop",) in log, "cancel must end the loop with the mission"


def test_cancel_during_the_climb_stops_the_boresight_loop(monkeypatch):
    """A respawn mid-climb must not carry a fire loop into the next life."""
    analyzer = _Analyzer([(1000, 20)])       # never reaches 3000 m
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, tracker=_Tracker())
    log = _record(ctrl, monkeypatch)

    t = _run_mission(ctrl)
    assert _wait(lambda: ("boresight_start",) in log)
    _drain(ctrl, t)

    assert ("boresight_stop",) in log


def test_a_failing_boresight_stop_does_not_leave_the_lock_held(monkeypatch):
    """The mission lock must be released on every exit path, including one
    where the teardown itself raises."""
    analyzer = _Analyzer([(1000, 20)])
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, tracker=_Tracker())
    _record(ctrl, monkeypatch)
    def _boom():
        raise RuntimeError("stop failed")
    monkeypatch.setattr(ctrl, "stop_boresight_engage_loop", _boom)

    t = _run_mission(ctrl)
    assert _wait(lambda: ctrl.is_mission_running())
    _drain(ctrl, t)

    assert not ctrl.is_mission_running()


def test_the_real_loop_fires_without_switching_and_never_presses_padlock(monkeypatch):
    """End to end through the real boresight loop and real key primitives (only
    the pitch actuators are stubbed): the fire key goes down, the weapon toggle
    never does (the spawn weapon stays selected until it runs out), and the
    padlock camera key never goes down at all."""
    from wingman.controller import FIRE_ACTIVE_WEAPON, PADLOCK_CAMERA, SWITCH_WEAPON

    analyzer = _Analyzer([(1000, 20)])       # holds in the climb, loop running
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, tracker=_Tracker(),
                      weapon_loop_interval=0.1)
    monkeypatch.setattr(ctrl, "climb_mode", lambda **kw: None)

    t = _run_mission(ctrl)

    def _keys():
        with ctrl._action_intents_lock:
            return [i["key"] for i in ctrl._action_intents
                    if i["action_type"] == "key_press"]
    assert _wait(lambda: _keys().count(FIRE_ACTIVE_WEAPON) >= 3)
    time.sleep(0.3)
    keys = _keys()
    _drain(ctrl, t)

    assert PADLOCK_CAMERA not in keys, "su30 must never toggle the padlock camera"
    assert SWITCH_WEAPON not in keys, \
        "the mission must not switch weapons; the spawn weapon fires until empty"
    assert ctrl._boresight_thread is None, "the loop outlived the mission"


# ---------------------------------------------------------------------------
# No padlock at all — and mission_j20 keeps its padlock
#
# search_and_destroy is not the only thing that presses the padlock camera. Two
# shared paths do it on their own, and mission_su30 triggers both:
#   * ADR 140 D6 presses to "correct" an Unknown padlock state whenever the
#     secondary weapon is active — which su30 has from the moment its spawn
#     weapon runs out and is switched away from;
#   * the missile target-spread handler presses twice after two missiles "fire",
#     and a primary rack of 4 reading 2 after the weapon switch looks like two.
# The rule is one check: while su30 is the mission in play, none of it runs.
# ---------------------------------------------------------------------------

def _padlock_presses(ctrl):
    from wingman.controller import PADLOCK_CAMERA
    with ctrl._action_intents_lock:
        return [i for i in ctrl._action_intents
                if i["action_type"] == "key_press" and i["key"] == PADLOCK_CAMERA]


def _plain_ctrl(monkeypatch, mission):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    ctrl = Controller(
        (0, 0, 1920, 1200),
        exit_event=threading.Event(),
        config=ControllerConfig(simulate_os_input=True, disable_hotkeys=True,
                                su30=dict(_FAST)),
    )
    ctrl._set_last_mission(mission)
    return ctrl


def test_the_padlock_is_blocked_only_while_su30_is_the_mission(monkeypatch):
    ctrl = _plain_ctrl(monkeypatch, "j20")
    assert ctrl.is_padlock_blocked() is False
    ctrl._set_last_mission("su30")
    assert ctrl.is_padlock_blocked() is True
    ctrl._set_last_mission("loiter")
    assert ctrl.is_padlock_blocked() is False
    ctrl._set_last_mission("j20")
    assert ctrl.is_padlock_blocked() is False


def test_padlock_camera_presses_nothing_during_su30(monkeypatch):
    ctrl = _plain_ctrl(monkeypatch, "su30")
    ctrl._padlock_engaged = False

    ctrl.padlock_camera(hold_seconds=0.01)

    assert _padlock_presses(ctrl) == []
    assert ctrl.padlock_state() is False, \
        "a press that never happened must not mark the padlock state Unknown"


def test_padlock_camera_still_presses_for_j20(monkeypatch):
    """The contrast that gives the test above its meaning."""
    ctrl = _plain_ctrl(monkeypatch, "j20")
    ctrl._padlock_engaged = False

    ctrl.padlock_camera(hold_seconds=0.01)

    assert len(_padlock_presses(ctrl)) == 1
    assert ctrl.padlock_state() is None


def test_a_search_and_destroy_loop_under_su30_never_presses_padlock(monkeypatch):
    """disengage_roll_right starts search_and_destroy itself; it is inert today
    only because the cancel it issued is still set."""
    ctrl = _plain_ctrl(monkeypatch, "su30")

    ctrl.start_search_and_destroy_loop()
    time.sleep(0.4)
    ctrl.stop_search_and_destroy_loop()

    assert _padlock_presses(ctrl) == []


def test_the_unknown_state_correction_does_not_run_during_su30(monkeypatch):
    """ADR 140 D6 — the first of the two shared paths."""
    ctrl = _plain_ctrl(monkeypatch, "su30")
    ctrl._eject_weapon_switched = True          # set once the spawn weapon has run out
    ctrl._padlock_engaged = None                # Unknown, as it starts a life

    ctrl._maybe_correct_padlock_unknown()

    assert _padlock_presses(ctrl) == []
    assert ctrl._padlock_unknown_correction_attempts == 0, \
        "the correction must not even count an attempt"


def test_the_unknown_state_correction_still_runs_for_j20(monkeypatch):
    ctrl = _plain_ctrl(monkeypatch, "j20")
    ctrl._eject_weapon_switched = True
    ctrl._padlock_engaged = None

    ctrl._maybe_correct_padlock_unknown()

    assert len(_padlock_presses(ctrl)) == 1


def test_the_missile_spread_handler_does_not_run_during_su30(monkeypatch, caplog):
    """The second shared path. A weapon switch from a 4-missile primary rack to a
    2-missile secondary reads as two missiles fired. Nothing is pressed and
    nothing is logged: 'switching padlock target' would look like padlock use."""
    import logging
    from wingman.tick_handlers import AmmoEventsHandler
    ctrl = _plain_ctrl(monkeypatch, "su30")
    handler = AmmoEventsHandler(object(), ctrl, {"no_missiles_abort_grace_s": 0.0})

    with caplog.at_level(logging.DEBUG):
        handler.tick_missile_count(4, GameState.GAME_BATTLE)
        handler.tick_missile_count(2, GameState.GAME_BATTLE)    # the weapon switch
        handler.tick_missile_count(0, GameState.GAME_BATTLE)    # both heat-seekers fired
        time.sleep(0.2)

    assert _padlock_presses(ctrl) == []
    assert "padlock" not in caplog.text.lower()


def test_the_missile_spread_handler_still_switches_target_for_j20(monkeypatch):
    from wingman.tick_handlers import AmmoEventsHandler
    ctrl = _plain_ctrl(monkeypatch, "j20")
    handler = AmmoEventsHandler(object(), ctrl, {"no_missiles_abort_grace_s": 0.0})

    handler.tick_missile_count(4, GameState.GAME_BATTLE)
    handler.tick_missile_count(2, GameState.GAME_BATTLE)

    assert _wait(lambda: len(_padlock_presses(ctrl)) == 2)


# ---------------------------------------------------------------------------
# Lock handling: automatic launch skips, operator launch takes over
# ---------------------------------------------------------------------------

def test_automatic_launch_skips_when_a_mission_is_running(monkeypatch):
    ctrl = _make_ctrl(monkeypatch, analyzer=_Analyzer([(3100, -10)]),
                      tracker=_Tracker())
    log = _record(ctrl, monkeypatch)
    assert ctrl._mission_lock.acquire(blocking=False)
    try:
        ctrl.mission_su30()
    finally:
        ctrl._mission_lock.release()

    assert log == [], "a running mission must not be interrupted by a restart"


def test_operator_launch_takes_the_aircraft_from_a_running_mission(monkeypatch):
    """ADR 111: 'o' pressed while mission_j20 is running must not be a silent
    no-op."""
    analyzer = _Analyzer([(3100, -10)])
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, tracker=_Tracker())
    log = _record(ctrl, monkeypatch)

    # A stand-in for the running mission: holds the lock until it is cancelled.
    assert ctrl._mission_lock.acquire(blocking=False)
    def _outgoing():
        ctrl._mission_cancel.wait(timeout=3.0)
        ctrl._mission_lock.release()
    threading.Thread(target=_outgoing, daemon=True).start()

    t = _run_mission(ctrl, preempt=True)
    t.join(timeout=5.0)

    assert ("pursue", True) in log, "the operator's mission never took over"


# ---------------------------------------------------------------------------
# Wiring: hotkey, respawn restart, battle-entry default
# ---------------------------------------------------------------------------

class _KeyboardStub:
    def __init__(self):
        self.handlers = {}

    def on_press_key(self, key, callback, suppress=False):
        self.handlers[key] = callback

    def add_hotkey(self, _key, _callback):
        return None


class _ThreadStub:
    started = []

    def __init__(self, target=None, daemon=None, args=None, kwargs=None):
        self._target, self._kwargs = target, kwargs or {}

    def start(self):
        _ThreadStub.started.append((self._target, self._kwargs))


class _StateAnalyzer:
    def __init__(self, state):
        self.game_state = state
        self.trigger_calls = []
        self._last_battle_event_ts = 0.0

    def trigger_event(self, name):
        self.trigger_calls.append(name)
        return True


def _hotkey_ctrl(monkeypatch, state):
    keyboard = _KeyboardStub()
    monkeypatch.setattr(controller_module, "keyboard_module", keyboard)
    monkeypatch.setattr(controller_module.threading, "Thread", _ThreadStub)
    analyzer = _StateAnalyzer(state)
    ctrl = Controller((0, 0, 1920, 1200), analyzer=analyzer)
    _ThreadStub.started = []
    return ctrl, keyboard, analyzer


def test_su30_hotkey_starts_a_preempting_mission_and_forces_battle(monkeypatch):
    ctrl, keyboard, analyzer = _hotkey_ctrl(monkeypatch, GameState.GAME_LOBBY)
    ctrl.mission_su30 = lambda **k: None

    keyboard.handlers[MISSION_SU30_KEY](object())

    assert analyzer.trigger_calls == ["manual_force_battle"]
    assert ctrl._last_mission == "su30", "respawn restarts whichever mission ran last"
    assert len(_ThreadStub.started) == 1
    target, kwargs = _ThreadStub.started[0]
    assert target == ctrl.mission_su30
    assert kwargs == {"preempt": True}


def test_su30_hotkey_from_battle_does_not_force_the_fsm(monkeypatch):
    ctrl, keyboard, analyzer = _hotkey_ctrl(monkeypatch, GameState.GAME_BATTLE)
    keyboard.handlers[MISSION_SU30_KEY](object())
    assert analyzer.trigger_calls == []


def test_su30_hotkey_ignores_key_repeat(monkeypatch):
    ctrl, keyboard, _ = _hotkey_ctrl(monkeypatch, GameState.GAME_BATTLE)
    handler = keyboard.handlers[MISSION_SU30_KEY]
    handler(object())
    handler(object())
    assert len(_ThreadStub.started) == 1


def _restart_ctrl(monkeypatch, **cfg):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    ctrl = Controller((0, 0, 1920, 1200),
                      config=ControllerConfig(disable_hotkeys=True, **cfg))
    launched = []
    monkeypatch.setattr(ctrl, "mission_j20", lambda: launched.append("j20"))
    monkeypatch.setattr(ctrl, "mission_su30", lambda: launched.append("su30"))
    monkeypatch.setattr(ctrl, "mission_loiter", lambda: launched.append("loiter"))
    return ctrl, launched


def test_respawn_restart_resumes_the_su30_mission(monkeypatch):
    ctrl, launched = _restart_ctrl(monkeypatch)
    ctrl._set_last_mission("su30")

    assert ctrl.restart_last_mission() is True
    assert _wait(lambda: launched == ["su30"])


def test_battle_entry_launches_the_configured_default_mission(monkeypatch):
    ctrl, launched = _restart_ctrl(monkeypatch, default_mission="su30")

    ctrl._start_default_mission()

    assert _wait(lambda: launched == ["su30"])
    assert ctrl._last_mission == "su30"


def test_restart_with_no_prior_mission_uses_the_default(monkeypatch):
    ctrl, launched = _restart_ctrl(monkeypatch, default_mission="su30")
    assert ctrl._last_mission is None

    assert ctrl.restart_last_mission() is True

    assert _wait(lambda: launched == ["su30"])


def test_the_code_level_default_is_j20_when_the_config_names_none(monkeypatch):
    """A ControllerConfig built without the key (tests, replay lanes) still
    launches J20; only the shipped config.yaml selects su30."""
    ctrl, launched = _restart_ctrl(monkeypatch)

    ctrl._start_default_mission()

    assert _wait(lambda: launched == ["j20"])
    assert ctrl._last_mission == "j20"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def shipped_cfg():
    return yaml.safe_load((_ROOT / "wingman" / "config.yaml").read_text(encoding="utf-8"))


def test_shipped_su30_block_matches_the_mission_document(shipped_cfg):
    """docs/missions/su30.md: reach 3000 altitude, then a -10 degree nose."""
    su30 = shipped_cfg["su30_mission"]
    assert su30["climb_alt_m"] == 3000
    assert su30["nose_angle_deg"] == -10


def test_shipped_default_mission_is_su30(shipped_cfg):
    """Operator decision, 2026-09-24: battle entry launches mission_su30. Set
    `mission.default_mission: j20` to get J20 at battle entry again."""
    assert shipped_cfg["mission"]["default_mission"] == "su30"


def test_the_controller_reads_the_shipped_su30_block(shipped_cfg, monkeypatch):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    ctrl = Controller((0, 0, 1920, 1200),
                      config=ControllerConfig.from_config(
                          shipped_cfg, disable_hotkeys=True))
    assert ctrl._su30_climb_alt_m == 3000.0
    assert ctrl._su30_nose_angle_deg == -10.0
    assert ctrl._default_mission == "su30"


def test_the_schema_accepts_su30_and_rejects_unknown_missions(shipped_cfg):
    assert validate_config(shipped_cfg) == []
    bad = yaml.safe_load(yaml.safe_dump(shipped_cfg))
    bad["mission"]["default_mission"] = "mig29"
    assert any("default_mission" in e for e in validate_config(bad))
    bad = yaml.safe_load(yaml.safe_dump(shipped_cfg))
    bad["su30_mission"]["nose_angel_deg"] = -10        # typo
    assert validate_config(bad), "a misspelt su30_mission key must be caught"
