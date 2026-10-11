"""tracking.hold_model: a target hold timed from the lag between a key and the picture.

Operator, 2026-10-11: in tracking and pursuit the "bore sight bounces up and
down left and right". Measured that day: the picture answers a steering key
0.20 to 0.30 s late, so a hold that ended when the picture showed the target at
the centre ended a quarter of a second too late, and 70 to 80 percent of pitch
presses were the opposite of the one before.

The first half is `wingman/hold_model.py` alone, on a clock the test owns. The
second is the controller's sustained holds with the model on.
"""

import threading
import time

import numpy as np
import pytest
import yaml

import wingman.controller as controller_module
from wingman.config_schema import schema_default
from wingman.controller import (
    NOSE_DOWN_KEY, NOSE_UP_KEY, ROLL_LEFT_KEY, ROLL_RIGHT_KEY, Controller,
)
from wingman.controller_config import ControllerConfig
from wingman.hold_model import HoldModel, PictureClock
from tests.test_pursuit_mode import (
    _AnalyzerStub, _TrackerStub, _keys, _wait_for_pursuit_to_settle,
)

DEADBAND = 0.05


# ---------------------------------------------------------------------------
# HoldModel
# ---------------------------------------------------------------------------

def _model(rate=1.0, lag=0.25):
    return HoldModel(rate, lag)


class TestKeyTime:
    def test_nothing_pressed_is_no_key_time(self):
        assert _model().key_time(0.0, 10.0) == 0.0

    def test_a_hold_counts_only_inside_the_window(self):
        m = _model()
        m.key(1, 10.0)
        m.key(0, 10.4)
        assert m.key_time(9.0, 11.0) == pytest.approx(0.4)
        assert m.key_time(10.1, 10.3) == pytest.approx(0.2)
        assert m.key_time(10.3, 12.0) == pytest.approx(0.1)
        assert m.key_time(11.0, 12.0) == 0.0

    def test_a_key_still_down_counts_to_the_end_of_the_window(self):
        m = _model()
        m.key(1, 10.0)
        assert m.key_time(9.0, 10.3) == pytest.approx(0.3)

    def test_the_other_key_counts_against(self):
        m = _model()
        m.key(1, 10.0)
        m.key(-1, 10.3)
        m.key(0, 10.4)
        assert m.key_time(9.0, 11.0) == pytest.approx(0.2)

    def test_old_history_is_dropped_but_a_key_still_down_is_remembered(self):
        m = HoldModel(1.0, 0.25, memory_s=1.0)
        m.key(1, 0.0)                     # down, and never let go
        for i in range(50):
            m.key(1, 10.0 + i)
        assert len(m._events) < 10
        assert m.key_time(58.0, 59.0) == pytest.approx(1.0)


class TestPlan:
    def test_a_first_press_lasts_the_error_over_the_rate(self):
        plan = _model(rate=1.15).plan(0.30, 10.0, 10.0, deadband=DEADBAND)
        assert plan.sign == 1
        assert plan.seconds == pytest.approx(0.30 / 1.15)
        assert plan.predicted == pytest.approx(0.30)

    def test_a_negative_error_takes_the_other_key(self):
        plan = _model().plan(-0.30, 10.0, 10.0, deadband=DEADBAND)
        assert plan.sign == -1 and plan.seconds == pytest.approx(0.30)

    def test_the_time_already_held_comes_off_while_the_picture_has_not_answered(self):
        """0.17 s into a hold the picture still shows the whole error."""
        m = _model(rate=1.15)
        m.key(1, 10.0)
        plan = m.plan(0.30, 10.17, 10.17, deadband=DEADBAND)
        assert plan.sign == 1
        assert plan.seconds == pytest.approx(0.30 / 1.15 - 0.17)

    def test_no_key_once_the_travel_commanded_covers_the_error(self):
        """The key came up after error / rate; the picture, a lag behind, still
        shows most of the error. Nothing more is pressed."""
        m = _model(rate=1.0, lag=0.25)
        m.key(1, 10.0)
        m.key(0, 10.3)
        # At 10.34 the picture shows the key time up to 10.09.
        plan = m.plan(0.30 - 0.09, 10.34, 10.34, deadband=DEADBAND)
        assert plan.sign == 0
        assert abs(plan.predicted) <= DEADBAND

    def test_a_hold_is_stopped_a_lag_early(self):
        """Key down throughout: the picture is a lag of travel behind."""
        m = _model(rate=1.0, lag=0.25)
        m.key(1, 10.0)
        assert m.plan(0.40, 11.0, 11.0, deadband=DEADBAND).sign == 1
        assert m.plan(0.28, 11.0, 11.0, deadband=DEADBAND).sign == 0

    def test_a_repeated_picture_is_charged_all_the_key_time_since_it_was_first_seen(self):
        m = _model(rate=1.0, lag=0.25)
        m.key(1, 10.0)
        # The same picture, first grabbed at 10.0, steered on again at 10.5:
        # 0.5 of travel is already commanded against an error of 0.4.
        stale = m.plan(0.40, 10.0, 10.5, deadband=DEADBAND)
        assert stale.sign == 0
        # Taken as new, the same error would keep the key down.
        assert m.plan(0.40, 10.5, 10.5, deadband=DEADBAND).sign == 1

    def test_an_unchanged_picture_is_not_believed_older_than_the_memory(self):
        m = HoldModel(1.0, 0.25, memory_s=1.0)
        m.key(1, 10.0)
        m.key(0, 10.4)
        assert m.plan(0.40, 10.0, 10.5, deadband=DEADBAND).sign == 0
        assert m.plan(0.40, 10.0, 20.0, deadband=DEADBAND).sign == 1

    def test_inside_the_deadband_is_no_key(self):
        assert _model().plan(0.04, 10.0, 10.0, deadband=DEADBAND).sign == 0

    def test_no_key_against_an_error_the_picture_shows_outside_the_deadband(self):
        """The model may say the nose will go past; with the target plainly on
        one side, only the picture may say which key."""
        m = _model(rate=1.0, lag=0.25)
        m.key(1, 10.0)
        plan = m.plan(0.10, 10.2, 10.2, deadband=DEADBAND)   # predicted -0.10
        assert plan.sign == 0 and plan.predicted == pytest.approx(-0.10)

    def test_the_shortest_hold_is_the_minimum_pulse(self):
        plan = _model(rate=1.15).plan(0.06, 10.0, 10.0, deadband=DEADBAND, min_pulse_s=0.08)
        assert plan.seconds == 0.08

    def test_travel_left_over_from_the_other_key_lengthens_the_hold(self):
        """A search roll the other way is still turning the nose."""
        m = _model(rate=0.4, lag=0.25)
        m.key(-1, 5.0)
        plan = m.plan(0.20, 10.0, 10.0, deadband=DEADBAND)
        assert plan.predicted == pytest.approx(0.20 + 0.4 * 0.25)


class TestTargetMotion:
    """The target's own motion across the screen, read from the pictures."""

    def _watch(self, m, errors, start=10.0, step=0.17):
        t = start
        for e in errors:
            plan = m.plan(e, t, t, deadband=DEADBAND)
            t += step
        return plan

    def test_a_target_standing_still_has_no_motion(self):
        m = _model()
        self._watch(m, [0.02] * 8)
        assert m.drift_per_s == pytest.approx(0.0)

    def test_a_steady_crossing_is_learnt_within_a_second(self):
        m = _model()
        self._watch(m, [-0.04 + 0.3 * 0.17 * i for i in range(2)])   # two pictures
        assert 0.0 < m.drift_per_s < 0.3
        m = _model()
        self._watch(m, [-0.04 + 0.3 * 0.17 * i for i in range(7)])
        assert m.drift_per_s == pytest.approx(0.3, abs=0.03)

    def test_travel_the_keys_account_for_is_not_the_targets_motion(self):
        """The error falls exactly as the key held a lag earlier says."""
        m = _model(rate=1.0, lag=0.25)
        m.key(1, 9.0)
        t = 10.0
        for i in range(8):
            m.plan(0.9 - 1.0 * 0.17 * i, t, t, deadband=DEADBAND)
            t += 0.17
        assert m.drift_per_s == pytest.approx(0.0, abs=1e-6)

    def test_a_crossing_target_is_led_from_inside_the_deadband(self):
        m = _model(rate=1.0, lag=0.25)
        plan = self._watch(m, [-0.20 + 0.4 * 0.17 * i for i in range(4)])   # now at +0.004
        assert abs(-0.20 + 0.4 * 0.17 * 3) <= DEADBAND
        assert plan.sign == 1 and plan.predicted > DEADBAND

    def test_a_target_moving_away_gets_a_longer_hold(self):
        still, moving = _model(rate=1.0), _model(rate=1.0)
        self._watch(still, [0.30] * 6)
        self._watch(moving, [0.30 - 0.2 * 0.17 * (5 - i) for i in range(6)])   # ends at 0.30
        a = still.plan(0.30, 12.0, 12.0, deadband=DEADBAND)
        b = moving.plan(0.30 + 0.2 * (12.0 - 10.85), 12.0, 12.0, deadband=DEADBAND)
        assert b.seconds > a.seconds

    def test_a_jump_to_another_target_is_not_motion(self):
        m = _model()
        self._watch(m, [0.02, 0.02, 0.02, -0.40])
        assert m.drift_per_s == 0.0

    def test_a_gap_forgets_the_motion(self):
        m = _model()
        self._watch(m, [0.3 * 0.17 * i for i in range(6)])
        assert m.drift_per_s > 0.2
        m.plan(0.30, 20.0, 20.0, deadband=DEADBAND)
        assert m.drift_per_s == 0.0

    def test_the_same_picture_again_teaches_nothing(self):
        m = _model()
        self._watch(m, [0.0, 0.05, 0.10])
        learnt = m.drift_per_s
        m.plan(0.10, 10.34, 10.6, deadband=DEADBAND)     # still the picture of 10.34
        assert m.drift_per_s == learnt

    def test_the_lead_is_capped(self):
        m = _model()
        m.drift_per_s = 1.4
        m._sample = (10.0, 0.0)
        plan = m.plan(0.0, 10.0, 10.0, deadband=DEADBAND)
        assert plan.predicted == pytest.approx(HoldModel.LEAD_MAX)


def _fly(rate_true, model_rate, error0, lag=0.25, tick=0.17, duration=4.0,
         stale_every=0, deadband=DEADBAND, modelled=True, drift=0.0):
    """A point that moves `rate_true` per second of key, `lag` late, steered
    every `tick`; with `drift` it also moves that much a second by itself.
    Returns (error at each tick, number of key presses)."""
    truth = HoldModel(rate_true, lag, memory_s=1e9)        # what the keys really did
    model = HoldModel(model_rate, lag)
    step = 0.001
    held = 0
    release_at = None
    presses = 0
    errors = []
    picture = (0.0, error0)       # (first seen, error)
    next_tick = 0.0
    n = 0
    t = 0.0
    while t < duration:
        if release_at is not None and t >= release_at and held:
            held = 0
            truth.key(0, t)
            model.key(0, t)
            release_at = None
        if t >= next_tick:
            next_tick += tick
            n += 1
            shown = error0 + drift * t - rate_true * truth.key_time(0.0, t - lag)
            if not (stale_every and n % stale_every == 0):
                picture = (t, shown)
            errors.append(shown)
            if modelled:
                plan = model.plan(picture[1], picture[0], t, deadband=deadband,
                                  min_pulse_s=0.08)
                want, release_at = plan.sign, (t + plan.seconds if plan.sign else None)
            else:   # the hold as it was: until the picture shows the deadband
                want = 0 if abs(picture[1]) <= deadband else (1 if picture[1] > 0 else -1)
                release_at = None
            if want != held:
                presses += 1 if want else 0
                held = want
                truth.key(want, t)
                model.key(want, t)
        t += step
    return errors, presses


class TestClosedLoop:
    """The model against a simulated point with the measured lag."""

    def test_the_hold_as_it_was_never_settles(self):
        errors, presses = _fly(1.15, 1.15, 0.30, modelled=False)
        late = [abs(e) for e in errors[len(errors) // 2:]]
        assert max(late) > 0.25, "holding until the picture is centred overshoots"
        assert presses >= 4

    def test_the_model_settles_in_one_hold(self):
        errors, presses = _fly(1.15, 1.15, 0.30)
        assert presses == 1
        assert all(abs(e) <= DEADBAND for e in errors[len(errors) // 2:])
        assert min(errors) > -DEADBAND, "never past the centre by more than the deadband"

    def test_a_slower_airframe_than_modelled_closes_without_crossing(self):
        errors, presses = _fly(0.6, 1.15, 0.30)
        assert min(errors) > -DEADBAND
        assert abs(errors[-1]) <= DEADBAND
        assert presses <= 4

    def test_a_faster_airframe_than_modelled_still_settles(self):
        errors, _ = _fly(1.5, 1.15, 0.30)
        assert all(abs(e) <= DEADBAND for e in errors[-5:])

    def test_a_crossing_target_is_kept_near_the_centre(self):
        """0.4 half-frames a second across the screen. Without the target's
        motion the hold trails it at about 0.2 and never arrives."""
        errors, _ = _fly(1.15, 1.15, 0.0, drift=0.4, duration=12.0)
        late = sorted(abs(e) for e in errors[len(errors) // 3:])
        assert late[len(late) // 2] <= 0.08
        assert late[int(len(late) * 0.9)] <= 0.15

    def test_the_hold_as_it_was_swings_across_a_crossing_target(self):
        errors, _ = _fly(1.15, 1.15, 0.0, drift=0.4, duration=12.0, modelled=False)
        late = sorted(abs(e) for e in errors[len(errors) // 3:])
        assert late[int(len(late) * 0.9)] > 0.3

    def test_repeated_pictures_do_not_lengthen_the_hold(self):
        """Every third grab returns the grab before; a quarter did, measured."""
        errors, presses = _fly(1.15, 1.15, 0.30, stale_every=3)
        assert presses == 1
        assert min(errors) > -DEADBAND


# ---------------------------------------------------------------------------
# PictureClock
# ---------------------------------------------------------------------------

class TestPictureClock:
    def _frame(self, value=0):
        frame = np.zeros((1200, 1920, 3), dtype=np.uint8)
        frame[::32, ::32] = value
        return frame

    def test_a_new_picture_is_seen_now(self):
        clock = PictureClock()
        assert clock.seen(self._frame(1), 10.0) == 10.0
        assert clock.seen(self._frame(2), 10.2) == 10.2

    def test_the_same_picture_keeps_its_first_time(self):
        clock = PictureClock()
        clock.seen(self._frame(1), 10.0)
        assert clock.seen(self._frame(1), 10.2) == 10.0
        assert clock.seen(self._frame(1), 10.4) == 10.0
        assert clock.seen(self._frame(2), 10.6) == 10.6

    def test_the_caller_may_reuse_its_buffer(self):
        clock = PictureClock()
        frame = self._frame(1)
        clock.seen(frame, 10.0)
        frame[:] = 7
        assert clock.seen(frame, 10.2) == 10.2

    def test_no_picture_is_taken_as_new(self):
        clock = PictureClock()
        assert clock.seen(None, 10.0) == 10.0
        assert clock.seen(object(), 10.2) == 10.2


# ---------------------------------------------------------------------------
# Controller: the sustained holds with the model on
# ---------------------------------------------------------------------------

def _make_ctrl(monkeypatch, *, hold_model=None, capture=None, analyzer=None):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    model = {"enabled": True, "lag_s": 0.25, "pitch_rate": 1.0, "roll_rate": 1.0,
             "min_pulse_s": 0.08}
    model.update(hold_model or {})
    return Controller(
        (0, 0, 1920, 1200),
        analyzer=analyzer,
        exit_event=threading.Event(),
        capture=capture,
        config=ControllerConfig(
            simulate_os_input=True,
            disable_hotkeys=True,
            tracking={"sustained_hold_enabled": True, "pitch_lead_s": 0.15,
                      "hold_model": model},
            pursuit_mode={
                "enabled": True,
                "pursuit_max_duration_s": 0.0,
                "pursuit_padlock_verify": False,
                "ammo_zero_grace_s": 0.0,
                "search_resume_delay_s": 0.0,
                "search_resume_centre_delay_s": 0.0,
            },
        ),
    )


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class TestTimedHold:
    def test_the_pitch_key_comes_up_by_itself_after_the_error_over_the_rate(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch)
        t0 = time.monotonic()
        assert ctrl.orient_pitch_to_target(0.20, sustained_hold=True) == "down"
        assert ("key_press", NOSE_DOWN_KEY) in _keys(ctrl)
        assert _wait_until(lambda: ("key_release", NOSE_DOWN_KEY) in _keys(ctrl))
        held = time.monotonic() - t0
        assert 0.17 <= held <= 0.6, held         # 0.20 at 1.0 a second
        assert ctrl._pitch_held is None

    def test_the_roll_key_comes_up_by_itself(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch)
        assert ctrl.orient_nose_to_target(-0.15, sustained_hold=True) == "left"
        assert _wait_until(lambda: ("key_release", ROLL_LEFT_KEY) in _keys(ctrl))
        assert ctrl._roll_held is None and ctrl._roll_hold_reason is None

    def test_the_timed_release_logs_its_own_reason(self, monkeypatch, caplog):
        ctrl = _make_ctrl(monkeypatch)
        with caplog.at_level("DEBUG", logger="wingman.controller"):
            ctrl.orient_pitch_to_target(0.10, sustained_hold=True)
            ctrl.orient_nose_to_target(0.10, sustained_hold=True)
            assert _wait_until(lambda: ctrl._pitch_held is None and ctrl._roll_held is None)
        lines = [r.getMessage() for r in caplog.records]
        assert "HOLD[pitch]: None -> down (target err_y=+0.100 pred=+0.100 hold=0.10s)" in lines
        assert "HOLD[pitch]: down -> None (timed 0.10s)" in lines
        assert ("HOLD[roll]: None/None -> right/target "
                "(target err=+0.100 pred=+0.100 hold=0.10s)") in lines
        assert "HOLD[roll]: right/target -> None/None (timed 0.10s)" in lines

    def test_a_large_error_is_still_held_at_the_next_tick(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch)
        ctrl.orient_pitch_to_target(0.90, sustained_hold=True)
        time.sleep(0.15)
        assert ctrl.orient_pitch_to_target(0.90, sustained_hold=True) == "down"
        assert _keys(ctrl).count(("key_press", NOSE_DOWN_KEY)) == 1
        assert ("key_release", NOSE_DOWN_KEY) not in _keys(ctrl)
        ctrl.release_tracking_holds()

    def test_nothing_is_pressed_again_while_the_picture_has_not_answered(self, monkeypatch):
        """The key came up on its timer; the next picture, a lag behind, still
        shows the whole error."""
        ctrl = _make_ctrl(monkeypatch)
        ctrl.orient_pitch_to_target(0.10, sustained_hold=True)
        assert _wait_until(lambda: ctrl._pitch_held is None)
        assert ctrl.orient_pitch_to_target(0.10, sustained_hold=True) is None
        assert _keys(ctrl).count(("key_press", NOSE_DOWN_KEY)) == 1

    def test_the_same_error_is_pressed_again_once_the_lag_has_passed(self, monkeypatch):
        """A hold that did not move the nose is not credited for ever."""
        ctrl = _make_ctrl(monkeypatch, hold_model={"lag_s": 0.05})
        ctrl.orient_pitch_to_target(0.10, sustained_hold=True)
        assert _wait_until(lambda: ctrl._pitch_held is None)
        time.sleep(0.2)
        assert ctrl.orient_pitch_to_target(0.10, sustained_hold=True) == "down"
        ctrl.release_tracking_holds()

    def test_a_repeated_picture_does_not_keep_the_key_down(self, monkeypatch):
        """0.3 of error, first seen 0.5 s ago with the key down since: the
        travel is commanded, the key comes up."""
        ctrl = _make_ctrl(monkeypatch)
        seen = time.monotonic()
        ctrl.orient_pitch_to_target(0.90, sustained_hold=True, seen_at=seen)
        time.sleep(0.5)
        assert ctrl.orient_pitch_to_target(0.30, sustained_hold=True, seen_at=seen) is None
        assert ("key_release", NOSE_DOWN_KEY) in _keys(ctrl)

    def test_a_reversal_is_still_a_release_and_a_press(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch)
        ctrl.orient_pitch_to_target(0.90, sustained_hold=True)
        assert ctrl.orient_pitch_to_target(-0.90, sustained_hold=True) == "up"
        assert _keys(ctrl) == [("key_press", NOSE_DOWN_KEY), ("key_release", NOSE_DOWN_KEY),
                               ("key_press", NOSE_UP_KEY)]
        ctrl.release_tracking_holds()

    def test_the_deadband_releases_and_says_so(self, monkeypatch, caplog):
        ctrl = _make_ctrl(monkeypatch)
        ctrl.orient_pitch_to_target(0.90, sustained_hold=True)
        with caplog.at_level("DEBUG", logger="wingman.controller"):
            assert ctrl.orient_pitch_to_target(0.01, sustained_hold=True) is None
        assert any("HOLD[pitch]: down -> None (deadband" in r.getMessage()
                   for r in caplog.records)


class TestLeadingAMovingTarget:
    def _crossing(self, ctrl, axis, per_s=0.6):
        """The axis's model has watched the target cross at `per_s` and last
        saw it 0.1 s ago, that much short of the centre."""
        model = ctrl._hold_models[axis]
        model.drift_per_s = per_s
        model._sample = (time.monotonic() - 0.1, -per_s * 0.1)

    def test_roll_leads_a_target_crossing_the_centre(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch)
        self._crossing(ctrl, "roll")
        assert ctrl.orient_nose_to_target(0.0, sustained_hold=True) == "right"
        assert _keys(ctrl) == [("key_press", ROLL_RIGHT_KEY)]
        ctrl.release_tracking_holds()

    def test_a_target_at_the_centre_and_standing_still_is_left_alone(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch)
        assert ctrl.orient_nose_to_target(0.0, sustained_hold=True) is None
        assert ctrl.orient_pitch_to_target(0.0, sustained_hold=True) is None
        assert _keys(ctrl) == []

    def test_pitch_is_not_led_downward_under_the_dive_guard(self, monkeypatch, caplog):
        """Target at the centre and moving down: the model would press
        nose-down before the error is positive, which the pursuit's own test
        of the error's sign lets through."""
        ctrl = _make_ctrl(monkeypatch)
        self._crossing(ctrl, "pitch")
        with caplog.at_level("DEBUG", logger="wingman.controller"):
            assert ctrl.orient_pitch_to_target(
                0.0, sustained_hold=True, down_allowed=False) is None
        assert _keys(ctrl) == []
        # The same crossing without the guard does press it.
        free = _make_ctrl(monkeypatch)
        self._crossing(free, "pitch")
        assert free.orient_pitch_to_target(0.0, sustained_hold=True) == "down"
        free.release_tracking_holds()

    def test_the_dive_guard_lets_go_of_a_nose_down_hold(self, monkeypatch, caplog):
        ctrl = _make_ctrl(monkeypatch)
        ctrl.orient_pitch_to_target(0.90, sustained_hold=True)
        with caplog.at_level("DEBUG", logger="wingman.controller"):
            assert ctrl.orient_pitch_to_target(
                0.90, sustained_hold=True, down_allowed=False) is None
        assert ("key_release", NOSE_DOWN_KEY) in _keys(ctrl)
        assert any("HOLD[pitch]: down -> None (dive guard" in r.getMessage()
                   for r in caplog.records)

    def test_nose_up_is_never_refused_by_the_dive_guard(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch)
        assert ctrl.orient_pitch_to_target(
            -0.30, sustained_hold=True, down_allowed=False) == "up"
        ctrl.release_tracking_holds()


class TestOtherWritersOfTheAxis:
    def test_a_release_drops_the_timer(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch)
        ctrl.orient_pitch_to_target(0.30, sustained_hold=True)
        ctrl.orient_nose_to_target(0.30, sustained_hold=True)
        ctrl.release_tracking_holds(why="cancel_mission")
        assert ctrl._hold_timers == {"roll": None, "pitch": None}
        time.sleep(0.4)
        assert _keys(ctrl).count(("key_release", NOSE_DOWN_KEY)) == 1
        assert _keys(ctrl).count(("key_release", ROLL_RIGHT_KEY)) == 1

    def test_the_icons_pitch_hold_is_not_cut_by_a_target_holds_timer(self, monkeypatch):
        """The rearm climb-out takes the axis with the same key already down."""
        ctrl = _make_ctrl(monkeypatch)
        ctrl.orient_pitch_to_target(-0.10, sustained_hold=True)
        ctrl.hold_pitch_for_icon("up", "rearm climb-out")
        time.sleep(0.3)
        assert ctrl._pitch_held == "up"
        assert ("key_release", NOSE_UP_KEY) not in _keys(ctrl)
        ctrl.release_tracking_holds()

    def test_the_search_roll_is_not_cut_by_a_target_holds_timer(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch)
        ctrl.orient_nose_to_target(-0.10, sustained_hold=True)
        ctrl.engage_roll_search("left")
        time.sleep(0.3)
        assert (ctrl._roll_held, ctrl._roll_hold_reason) == ("left", "search")
        assert ("key_release", ROLL_LEFT_KEY) not in _keys(ctrl)
        ctrl.release_tracking_holds()

    def test_the_search_roll_is_counted_when_a_target_takes_the_axis(self, monkeypatch):
        """Still turning left from the search: the hold right is longer."""
        ctrl = _make_ctrl(monkeypatch)
        ctrl.engage_roll_search("left")
        time.sleep(0.3)
        plan = ctrl._hold_plan("roll", 0.20, DEADBAND, None)
        assert plan.predicted == pytest.approx(0.20 + 0.25, abs=0.03)
        ctrl.release_tracking_holds()

    def test_a_stale_timer_releases_nothing(self, monkeypatch):
        """A timer that fires after a later decision replaced it."""
        ctrl = _make_ctrl(monkeypatch)
        ctrl.orient_pitch_to_target(0.90, sustained_hold=True)
        ctrl._hold_timer_fired("pitch", 0.1)      # not the armed timer's thread
        assert ctrl._pitch_held == "down"
        ctrl.release_tracking_holds()


class TestOffIsAsBefore:
    def test_without_the_key_the_hold_waits_for_the_deadband(self, monkeypatch):
        monkeypatch.setattr(controller_module, "keyboard_module", None)
        ctrl = Controller(
            (0, 0, 1920, 1200), exit_event=threading.Event(),
            config=ControllerConfig(simulate_os_input=True, disable_hotkeys=True,
                                    tracking={"sustained_hold_enabled": True}))
        assert ctrl._hold_models == {}
        ctrl.orient_pitch_to_target(0.10, sustained_hold=True)
        ctrl.orient_nose_to_target(0.10, sustained_hold=True)
        time.sleep(0.3)
        assert ctrl._pitch_held == "down" and ctrl._roll_held == "right"
        assert ctrl._hold_timers == {"roll": None, "pitch": None}
        ctrl.release_tracking_holds()

    def test_shipped_values(self):
        with open("wingman/config.yaml", encoding="utf-8") as f:
            shipped = yaml.safe_load(f)["tracking"]["hold_model"]
        assert shipped == {"enabled": True, "lag_s": 0.25, "pitch_rate": 1.15,
                           "roll_rate": 0.4, "min_pulse_s": 0.08}
        # A config without the key steers as it did.
        assert schema_default("tracking.hold_model.enabled") is False
        for key in ("lag_s", "pitch_rate", "roll_rate", "min_pulse_s"):
            assert schema_default("tracking.hold_model." + key) == shipped[key]
        # The shortest pitch hold must land inside the deadband, not across it.
        assert shipped["pitch_rate"] * shipped["min_pulse_s"] <= 2 * 0.05


# ---------------------------------------------------------------------------
# The pursuit loop tells the steering when its picture was first seen
# ---------------------------------------------------------------------------

class _FrozenCapture:
    """Every grab returns the same picture, as a held frame does."""

    def __init__(self):
        self._frame = np.zeros((1200, 1920, 3), dtype=np.uint8)

    def grab_from_thread(self):
        return self._frame


def test_the_pursuit_does_not_hold_a_key_on_a_picture_that_does_not_change(monkeypatch):
    """Measured 2026-10-11 14:39:15: nose-down held 0.5 s on one repeated
    picture, and the marker went from 34 px below the centre to 302 px above.
    Nose-up here, which the dive guard leaves alone."""
    ctrl = _make_ctrl(monkeypatch, capture=_FrozenCapture(), analyzer=_AnalyzerStub(ammo=2))
    ctrl.set_target_tracker(_TrackerStub(visible=True, error_norm=0.2, error_norm_y=-0.2))
    ctrl.pursue_and_engage(defer_switch_until_empty=True)
    time.sleep(1.5)
    keys = _keys(ctrl)
    ctrl.stop_eject_sequence("respawn_detected")
    _wait_for_pursuit_to_settle(ctrl)
    # One hold of 0.2 s on each axis, let go by its timer, and no second press:
    # the picture never showed that the first had not been enough.
    assert keys.count(("key_press", NOSE_UP_KEY)) == 1
    assert keys.count(("key_press", ROLL_RIGHT_KEY)) == 1
    assert ("key_release", NOSE_UP_KEY) in keys
    assert ("key_release", ROLL_RIGHT_KEY) in keys
