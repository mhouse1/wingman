"""Design 017 phase 4: golden-path candidates from the re-arms on record (scripts/airspace-golden.py)."""

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np

from wingman.airspace import HeightMap

_SPEC = importlib.util.spec_from_file_location("airspace_golden", Path("scripts/airspace-golden.py"))
gp = importlib.util.module_from_spec(_SPEC)
sys.modules["airspace_golden"] = gp
_SPEC.loader.exec_module(gp)


def _flat(height=0.0):
    return HeightMap(np.full((1600, 1600), height, np.float32), 16.0, -12800.0)


def _entry(rearm_t=100.0, crash_t=None, gap_at=None, alt=600.0, life=0, by_hand=False, extra_rearms=()):
    """One log's extraction: a life flying east at 200 m/s, a tick every 1.5 s, re-arming at rearm_t."""
    ts = np.arange(40.0, 160.0, 1.5)
    if gap_at is not None:
        ts = ts[(ts < gap_at) | (ts > gap_at + 6.0)]
    x = (ts - 100.0) * 200.0
    samples = np.column_stack([ts, x, np.zeros_like(ts), np.full_like(ts, alt), np.full_like(ts, life),
                               np.zeros_like(ts)])
    k = np.argmin(np.abs(ts - rearm_t))
    rearms = np.array([[ts[k], x[k], 0.0, alt, float(by_hand)]] + [list(r) for r in extra_rearms])
    crashes = np.zeros((0, 5)) if crash_t is None else np.array([[crash_t, 0.0, 0.0, 0.0, 1.0]])
    return ("wingman_x.log", {"first_t": 0.0, "lives": 1,
                              "arenas": {"a": {"samples": samples, "crashes": crashes, "rearms": rearms}}})


def test_transit_altitude_is_the_highest_ground_inside_the_ring_plus_the_margin():
    top = np.zeros((1600, 1600), np.float32)
    top[800, 800] = 1500.0                  # inside the 10 km ring
    top[10, 10] = 4000.0                    # a corner, outside it
    assert gp.transit_altitude(HeightMap(top, 16.0, -12800.0)) == 1500.0 + gp.TRANSIT_MARGIN_M


def test_a_clean_rearm_over_flat_ground_is_a_candidate_cut_30_s_before_and_15_after():
    rows, transit, _ = gp.candidates_for("a", [_entry()], _flat())
    row, = rows
    assert row["verdict"] == "candidate", row["reasons"]
    wp = np.array(row["waypoints"])
    assert wp[0, 0] >= row["t"] - 30.0 and wp[-1, 0] <= row["t"] + 15.0
    assert len(wp) >= gp.MIN_WAYPOINTS


def test_a_crash_within_30_s_or_a_long_gap_rejects_it():
    crashed, = gp.candidates_for("a", [_entry(crash_t=115.0)], _flat())[0]
    assert crashed["reject"] == ["crash"]
    gapped, = gp.candidates_for("a", [_entry(gap_at=90.0)], _flat())[0]
    assert gapped["reject"] == ["gap"]


def test_a_waypoint_whose_climb_meets_the_mesh_has_no_way_out():
    top = np.zeros((1600, 1600), np.float32)
    top[:, 800 + 160:800 + 170] = 5000.0    # a wall 2.6 km east of the centre, far above any climb
    row, = gp.candidates_for("a", [_entry()], HeightMap(top, 16.0, -12800.0))[0]
    assert "no way out" in row["reject"]


def test_accepting_copies_a_candidate_once_and_refuses_a_rejected_one(tmp_path):
    rows = gp.candidates_for("a", [_entry()], _flat())[0] + gp.candidates_for("a", [_entry(crash_t=110.0)], _flat())[0]
    rows[1]["id"] = 1
    path = str(tmp_path / "a-golden.json")
    said = gp.accept_paths(path, "a", rows, {0, 1})
    assert said[0].endswith("accepted as a golden path") and "not accepted" in said[1]
    assert gp.accept_paths(path, "a", rows, {0}) == ["a:0 already a golden path"]
    with open(path) as fh:
        assert len(json.load(fh)["paths"]) == 1


def test_a_rearm_flown_by_hand_away_from_a_confirmed_site_is_kept_but_unconfirmed():
    """The missile count also rises on a rack switch, so a hand-flown re-arm away from a ring site
    wingman confirmed is marked for the operator to check, not dropped: on a map with no confirmed
    site the operator's pass is the only way to learn the ring (Stormbreak, 2026-11-26)."""
    lone = gp.candidates_for("a", [_entry(by_hand=True)], _flat())[0]
    assert lone[0]["verdict"] == "candidate" and lone[0]["unconfirmed"] is True
    confirmed = [(10.0, 300.0, 0.0, 600.0, 0.0), (20.0, -300.0, 0.0, 600.0, 0.0)]   # a site at the origin
    rows = gp.candidates_for("a", [_entry(by_hand=True, extra_rearms=confirmed)], _flat())[0]
    hand = next(r for r in rows if r["manual"] and r["t"] > 50.0)
    assert hand["unconfirmed"] is False


def test_any_way_out_takes_a_turn_where_the_climb_alone_would_hit():
    """Design 020's way-out set: a wall straight ahead that no climb clears, with open air to the
    side, leaves a way out (a turn); the same wall across the whole map leaves none."""
    wp = np.array([[0.0, -400.0, 0.0, 300.0], [1.5, -100.0, 0.0, 300.0], [3.0, 200.0, 0.0, 300.0]])
    top = np.zeros((1600, 1600), np.float32)
    top[800 - 20:800 + 20, 800 + 55:800 + 60] = 6000.0     # a 640 m wide wall about 900 m ahead
    assert gp.way_out_any(HeightMap(top, 16.0, -12800.0), wp[:2]) == []
    top[:, 800 + 55:800 + 60] = 6000.0                     # now across the whole map
    assert gp.way_out_any(HeightMap(top, 16.0, -12800.0), wp) != []
