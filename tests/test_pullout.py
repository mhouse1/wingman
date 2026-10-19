"""Code Review 019 cycle 32 (HLDD 001 part 2): the emergency climb on a pull-out
that would not clear the floor.

Time to ground (ADR 086) divides altitude by descent rate, as if the descent
ran on to sea level and the pull-out took no time. The nose comes up slower the
faster the aircraft goes (cycle 21: 21 deg/s at 400-599 KPH, 3 at 1,200-1,399),
so a fast dive needs far more height to level out. Over 373 missions of
2026-10-17/18 (499 kill-card crashes) the lowest-point trigger with a 600 m
floor and 2 s of reaction would have fired 2 s or more before 55% of the
crashes, where the emergency climbs that fired managed 43%, at the same alarm
rate and precision. It ships in shadow: logged, not flown.
"""

import logging
import math
import time

import pytest
import yaml

from tests.test_behavior_tree import FakeClock, make_snap
from wingman.behavior_tree import make_climb_condition
from wingman.pullout import lowest_point_m, pitch_rate_deg_s, pullout_loss_m


# -- the model -------------------------------------------------------------------

@pytest.mark.parametrize("speed, rate", [(450, 21.0), (650, 8.0), (900, 7.0), (1100, 5.0), (1300, 3.0),
                                         (1800, 3.0)])
def test_the_nose_comes_up_slower_the_faster_the_aircraft(speed, rate):
    assert pitch_rate_deg_s(speed) == rate


def test_the_height_a_pull_out_loses_follows_the_measured_rates():
    """From -35 deg: 1.7 s and about 70 m to level at 500 KPH, 11.7 s and about
    1,250 m at 1,300 KPH (HLDD 001's table, from the same medians)."""
    g = math.radians(35)
    shape = (1 - math.cos(g)) / g
    for speed, expected_t in ((500, 35 / 21), (900, 35 / 7), (1300, 35 / 3)):
        assert pullout_loss_m(speed, -35) == pytest.approx(speed / 3.6 * expected_t * shape)
    losses = [pullout_loss_m(s, -35) for s in (500, 700, 900, 1100, 1300)]
    assert losses == sorted(losses) and 60 < losses[0] < 80 and 1150 < losses[-1] < 1350


@pytest.mark.parametrize("path", [0.0, 5.0, 40.0])
def test_level_or_climbing_loses_nothing(path):
    assert pullout_loss_m(1200, path) == 0.0
    assert lowest_point_m(800, 1200, path, 2.0) == 800.0


def test_the_reaction_time_adds_the_descent_before_the_climb_starts():
    without = lowest_point_m(1500, 1100, -25, 0.0)
    with_two = lowest_point_m(1500, 1100, -25, 2.0)
    assert without - with_two == pytest.approx(1100 / 3.6 * math.sin(math.radians(25)) * 2.0)


# -- the trigger -----------------------------------------------------------------

# 1,100 KPH, -30 deg at 1,200 m: about 152 m/s down, 7.9 s to ground, and a
# pull-out started 2 s from now bottoms out near 425 m.
FAST_LOW = dict(altitude=1200.0, altitude_now=1200.0, speed_kph=1100.0, path_deg=-30.0,
                altitude_rate=-152.8)
# The same dive at 3,000 m bottoms out near 2,225 m.
FAST_HIGH = dict(altitude=3000.0, altitude_now=3000.0, speed_kph=1100.0, path_deg=-30.0,
                 altitude_rate=-152.8)


def _cond(**kw):
    opts = dict(confirm_reads=1, clock=FakeClock(), pullout_enabled=True, pullout_shadow=False,
                pullout_floor_m=600.0, pullout_reaction_s=2.0)
    opts.update(kw)
    return make_climb_condition(500, 1000, **opts)


def _settle(cond, **snap):
    """Two clean samples, as after a respawn (the condition's settle rule)."""
    for _ in range(2):
        cond.update_emergency(make_snap(**snap))


def test_a_fast_low_dive_is_a_hard_emergency():
    cond = _cond()
    _settle(cond, **FAST_HIGH)
    assert not cond.pullout_active
    cond.update_emergency(make_snap(**FAST_LOW))
    assert cond.pullout_active and cond.emergency_active and cond.hard_emergency_active
    assert cond.last_pullout_lowest == pytest.approx(lowest_point_m(1200, 1100, -30, 2.0))


def test_in_shadow_it_is_logged_once_an_episode_and_not_flown(caplog):
    cond = _cond(pullout_shadow=True)
    _settle(cond, **FAST_HIGH)
    with caplog.at_level(logging.WARNING, logger="wingman.behavior_tree"):
        cond.update_emergency(make_snap(**FAST_LOW))
        cond.update_emergency(make_snap(**FAST_LOW))
    lines = [r.getMessage() for r in caplog.records if "PULL-OUT" in r.getMessage()]
    assert len(lines) == 1
    assert lines[0].startswith("BT: PULL-OUT WOULD NOT CLEAR — lowest point 425m under the 600m "
                               "floor (alt=1200m 1100KPH path -30deg ttg=")
    assert lines[0].endswith("[SHADOW - not actuating]")
    assert cond.pullout_active and not cond.hard_emergency_active


def test_the_same_dive_high_up_clears_the_floor():
    cond = _cond()
    _settle(cond, **FAST_HIGH)
    cond.update_emergency(make_snap(**FAST_HIGH))
    assert not cond.pullout_active


def test_a_missing_reading_holds_the_verdict_and_a_respawn_clears_it():
    cond = _cond()
    _settle(cond, **FAST_HIGH)
    cond.update_emergency(make_snap(**FAST_LOW))
    cond.update_emergency(make_snap(**dict(FAST_LOW, speed_kph=None, path_deg=None)))
    assert cond.pullout_active
    cond.update_emergency(make_snap(**dict(FAST_LOW, is_respawning=True)))
    assert not cond.pullout_active


def test_not_before_the_new_aircraft_has_two_clean_samples():
    cond = _cond()
    cond.update_emergency(make_snap(**dict(FAST_LOW, is_respawning=True)))
    cond.update_emergency(make_snap(**FAST_LOW))
    assert not cond.pullout_active
    cond.update_emergency(make_snap(**FAST_LOW))
    assert cond.pullout_active


def test_disabled_it_changes_nothing():
    cond = _cond(pullout_enabled=False)
    _settle(cond, **FAST_HIGH)
    cond.update_emergency(make_snap(**FAST_LOW))
    assert not cond.pullout_active


def test_the_shipped_config_flies_it():
    """Cycle 34: live after the MiG-31 shadow baseline of cycle 32."""
    import pathlib
    cfg = yaml.safe_load((pathlib.Path(__file__).resolve().parents[1] / "wingman" / "config.yaml")
                         .read_text(encoding="utf-8"))
    pullout = cfg["behavior_tree"]["climb"]["pullout"]
    assert pullout == {"enabled": True, "shadow": False, "floor_m": 600, "reaction_s": 2.0,
                       "bypass_holdoff_below_m": 300}


# -- the wiring: the real analyzer, handler and shipped config --------------------

def test_a_fast_low_dive_on_the_hud_is_logged_by_the_real_tick(caplog):
    """The tick handler hands the raw altitude, speed and path to the climb
    condition; a fast low dive read off the HUD fires the trigger (live since
    cycle 34)."""
    import pathlib
    from unittest.mock import MagicMock

    import numpy as np

    from wingman.analyzer import GameState, GameStateAnalyzer
    from wingman.tick_handlers import BehaviorTreeHandler

    cfg_path = pathlib.Path(__file__).resolve().parents[1] / "wingman" / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    analyzer = GameStateAnalyzer(cfg)
    try:
        analyzer.state = GameState.GAME_BATTLE.name
        ctrl = MagicMock()
        ctrl.padlock_state.return_value = True        # no forward-view reading needed
        handler = BehaviorTreeHandler(analyzer, ctrl, cfg["behavior_tree"])
        frame = np.zeros((1200, 1920, 3), np.uint8)
        now = time.time()
        # 1,100 KPH losing 153 m a second: a -30 deg path.
        for i, alt in enumerate((1659.0, 1506.0, 1353.0, 1200.0)):
            analyzer._telemetry.update(1100, alt, now - 3.0 + i)
        with caplog.at_level(logging.WARNING, logger="wingman.behavior_tree"):
            for _ in range(3):
                handler.tick(frame, GameState.GAME_BATTLE, {"health": 160})
        lines = [r.getMessage() for r in caplog.records if "PULL-OUT WOULD NOT CLEAR" in r.getMessage()]
        assert lines and "alt=1200m 1100KPH path -30deg" in lines[0] and "[SHADOW" not in lines[0]
        # Cycle 36: the live lowest point reaches the controller every tick.
        pushed = [c.args[0] for c in ctrl.set_pullout_lowest_m.call_args_list]
        assert pushed and pushed[-1] is not None and pushed[-1] < 600
    finally:
        analyzer.cleanup()


# -- cycle 33: a clamped path is a misread ------------------------------------------

def test_a_clamped_path_neither_fires_nor_clears_the_trigger():
    """Live 2026-10-18 20:13:20: the HUD read 2,791, 2,143 and 2,675 m 1.5 s
    apart; the misread's drop outran the airspeed, the path clamped to -90 deg,
    and the trigger fired on it."""
    cond = _cond()
    _settle(cond, **FAST_HIGH)
    misread = dict(altitude=2143.0, altitude_now=2143.0, speed_kph=1441.0, path_deg=-90.0,
                   altitude_rate=-432.0)
    cond.update_emergency(make_snap(**misread))
    assert not cond.pullout_active
    cond.update_emergency(make_snap(**FAST_LOW))
    assert cond.pullout_active
    cond.update_emergency(make_snap(**misread))
    assert cond.pullout_active


@pytest.mark.parametrize("path", [-85.0, -89.0])
def test_paths_at_the_clamp_are_ignored(path):
    cond = _cond()
    _settle(cond, **FAST_HIGH)
    cond.update_emergency(make_snap(**dict(FAST_LOW, path_deg=path)))
    assert not cond.pullout_active


def test_a_clamped_climb_does_not_clear_an_alarm():
    """The misread's rebound (2,675 m 1.5 s later) reads as a steep climb."""
    cond = _cond()
    _settle(cond, **FAST_HIGH)
    cond.update_emergency(make_snap(**FAST_LOW))
    cond.update_emergency(make_snap(**dict(FAST_LOW, path_deg=88.0)))
    assert cond.pullout_active


def test_a_steep_but_possible_dive_still_fires():
    cond = _cond()
    _settle(cond, **FAST_HIGH)
    cond.update_emergency(make_snap(**dict(FAST_LOW, path_deg=-80.0)))
    assert cond.pullout_active


def test_the_live_lowest_point_is_none_in_shadow():
    cond = _cond(pullout_shadow=True)
    _settle(cond, **FAST_HIGH)
    cond.update_emergency(make_snap(**FAST_LOW))
    assert cond.pullout_active and cond.live_pullout_lowest is None
    live = _cond()
    _settle(live, **FAST_HIGH)
    live.update_emergency(make_snap(**FAST_LOW))
    assert live.live_pullout_lowest == pytest.approx(lowest_point_m(1200, 1100, -30, 2.0))
