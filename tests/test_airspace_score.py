"""Design 017 phase 2: the offline scoring of the planner's query (scripts/airspace-score.py)."""

import importlib.util
import math
import sys
from pathlib import Path

import numpy as np
import pytest

from wingman.airspace import FREE, GROUND, NOT_KNOWN

_SPEC = importlib.util.spec_from_file_location("airspace_score", Path("scripts/airspace-score.py"))
sc = importlib.util.module_from_spec(_SPEC)
sys.modules["airspace_score"] = sc
_SPEC.loader.exec_module(sc)


def _line(sec, text, level="INFO"):
    return f"2026-11-26 12:{int(sec) // 60:02d}:{int(sec) % 60:02d},000 [{level}] {text}\n"


def test_resupply_runs_end_at_the_rearm_a_death_card_or_the_next_match(tmp_path):
    log = tmp_path / "wingman_x.log"
    log.write_text("".join([
        _line(10, "RESUPPLY: missiles exhausted; heading for a resupply marker"),
        _line(12, "RESUPPLY: urgency overtook the chase"),             # same run, not a new one
        _line(40, "RESUPPLY: confirmed ammo=4; urgency reset, resuming target pursuit"),
        _line(50, "RESUPPLY: urgency overtook the chase"),
        _line(70, "DEATH CARD: no kill card on 1 read(s) of the respawn screen: a crash or self-inflicted"),
        _line(80, "RESUPPLY: missiles exhausted"),
        _line(90, "MATCH: mode=TEAM DEATHMATCH map=CRUCIBLE"),
    ]))
    runs = sc.resupply_runs(str(log))
    t0 = sc.ar._epoch(_line(0, ""))
    assert [(a - t0, b - t0) for a, b in runs] == [(10, 40), (50, 70), (80, 90)]


def test_path_points_fly_the_last_step_ahead():
    ticks = [("a", 0.0, 0.0, 0.0, 500.0, 1, math.nan, math.nan, math.nan, None),
             ("a", 1.5, 300.0, 0.0, 530.0, 1, 200.0, 0.0, 20.0, None)]
    have, x, z, alt = sc.path_points(ticks, horizon=1.0)
    assert have.tolist() == [1]                         # the first tick has no track
    np.testing.assert_allclose(x[0], [350.0, 400.0, 450.0, 500.0])
    np.testing.assert_allclose(alt[0], [535.0, 540.0, 545.0, 550.0])
    np.testing.assert_allclose(z[0], 0.0)


def _tick(t, life=1, arena="crimson-canyon", state="GAME_BATTLE"):
    return (arena, t, 0.0, 0.0, 500.0, life, 1.0, 0.0, 0.0, state)


def test_score_day_splits_crash_and_killed_approaches_from_ordinary_flight():
    ticks = [_tick(t) for t in (0.0, 1.5, 3.0, 20.0, 21.5)] + [_tick(t, life=2) for t in (40.0, 41.5)]
    codes = np.array([FREE, FREE, FREE, NOT_KNOWN, GROUND, FREE, FREE])
    deaths = [(23.0, True, 1), (43.0, False, 2)]          # a crash, then a kill
    tally = sc.score_day(ticks, deaths, codes)
    crash, = tally["crash"]
    assert crash == {"arena": "crimson-canyon", "ground": True, "flagged": True, "lead": pytest.approx(3.0)}
    killed, = tally["killed"]
    assert killed["flagged"] is False and killed["lead"] is None
    o = tally["ordinary"]
    # only the ticks with no death in the next 10 s are ordinary: 0, 1.5 and 3.0
    assert o[("crimson-canyon", "ticks")] == 3 and o[("crimson-canyon", "not_free")] == 0
    assert o[("crimson-canyon", "ticks@GAME_BATTLE")] == 3


def test_a_tick_without_a_path_is_not_scored():
    ticks = [_tick(0.0), _tick(1.5)]
    tally = sc.score_day(ticks, [], np.array([-1, NOT_KNOWN]))
    assert tally["ordinary"][("crimson-canyon", "ticks")] == 1
    assert tally["ordinary"][("crimson-canyon", "not_free")] == 1


def test_summarise_counts_not_known_only_as_flagged_without_ground():
    rows = [{"ground": True, "flagged": True, "lead": 4.0}, {"ground": False, "flagged": True, "lead": 2.0},
            {"ground": False, "flagged": False, "lead": None}]
    s = sc.summarise(rows)
    assert (s["n"], s["flagged"], s["ground"], s["not_known_only"], s["lead_median"]) == (3, 2, 1, 1, 3.0)
