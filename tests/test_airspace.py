"""Design 017 Phase 1: the free-airspace record (wingman/airspace.py) and the log reader that
feeds it (scripts/airspace-record.py)."""

import importlib.util
import math
import sys
from pathlib import Path

import numpy as np
import pytest

from wingman.airspace import (FREE, GROUND, NOT_KNOWN, AirspaceQuery, AirspaceRecord, Crash,
                              HeightMap, Rearm, Sample, build_record, mesh_check)

_SPEC = importlib.util.spec_from_file_location("airspace_record", Path("scripts/airspace-record.py"))
ar = importlib.util.module_from_spec(_SPEC)
sys.modules["airspace_record"] = ar
_SPEC.loader.exec_module(ar)

CELL = 64.0
HALF = 1024.0          # a 32 by 32 grid keeps the tests small


def _flat(height=0.0, n=160, cell=16.0, origin=-1280.0):
    return HeightMap(np.full((n, n), height, np.float32), cell, origin)


def _rec(samples, **kw):
    kw.setdefault("cell", CELL)
    kw.setdefault("half_span", HALF)
    return build_record(samples, **kw)


def _cell_of(rec, x, z):
    return rec.index(x, z)


# --- the record -------------------------------------------------------------------------

def test_floor_is_the_lowest_pass_and_trust_needs_a_second_life():
    rec, _ = _rec([Sample(0.0, 100.0, 100.0, 500.0, life=1)])
    assert rec.floor_at(100.0, 100.0, trusted=False) == 500.0
    assert rec.floor_at(100.0, 100.0) is None              # one pass is not trusted

    rec, _ = _rec([Sample(0.0, 100.0, 100.0, 500.0, life=1),
                   Sample(50.0, 100.0, 100.0, 650.0, life=2)])
    assert rec.floor_at(100.0, 100.0, trusted=False) == 500.0
    assert rec.floor_at(100.0, 100.0) == 650.0             # both lives went this low
    assert rec.passes[_cell_of(rec, 100.0, 100.0)] == 2


def test_two_visits_by_one_life_are_one_pass():
    rec, _ = _rec([Sample(0.0, 100.0, 100.0, 500.0, life=1),
                   Sample(30.0, 100.0, 100.0, 300.0, life=1)])
    ij = _cell_of(rec, 100.0, 100.0)
    assert rec.passes[ij] == 1
    assert rec.floor[ij] == 300.0
    assert math.isnan(rec.floor2[ij])


def test_near_floor_counts_passes_within_50_m():
    rec, _ = _rec([Sample(0.0, 100.0, 100.0, 500.0, life=1),
                   Sample(10.0, 100.0, 100.0, 540.0, life=2),
                   Sample(20.0, 100.0, 100.0, 800.0, life=3)])
    ij = _cell_of(rec, 100.0, 100.0)
    assert rec.passes[ij] == 3
    assert rec.near_floor[ij] == 2
    assert rec.last_t[ij] == 20.0


def test_fill_between_fixes_uses_the_higher_altitude_never_a_lower_one():
    a = Sample(0.0, -500.0, 32.0, 300.0, life=1)
    b = Sample(2.0, 500.0, 32.0, 900.0, life=1)       # 500 m/s
    rec, stats = _rec([a, b])
    assert stats["fill_points"] > 0
    row = rec.floor[_cell_of(rec, 0.0, 32.0)[0]]
    filled = row[np.isfinite(row)]
    assert len(filled) >= 1000 / CELL - 1
    # the endpoints keep their own altitudes; every cell between them is at the higher one
    assert rec.floor_at(-500.0, 32.0, trusted=False) == 300.0
    assert rec.floor_at(500.0, 32.0, trusted=False) == 900.0
    for x in (-300.0, 0.0, 300.0):
        assert rec.floor_at(x, 32.0, trusted=False) == 900.0


@pytest.mark.parametrize("b", [
    Sample(2.0, 500.0, 32.0, 900.0, life=2),        # another life
    Sample(2.5, 500.0, 32.0, 900.0, life=1),        # beyond MAX_JOIN_S
], ids=["other-life", "too-far-apart"])
def test_no_fill_across_lives_or_long_gaps(b):
    rec, stats = _rec([Sample(0.0, -500.0, 32.0, 300.0, life=1), b])
    assert stats["fill_points"] == 0
    assert rec.floor_at(0.0, 32.0, trusted=False) is None


def test_no_fill_for_a_step_faster_than_an_aircraft_flies():
    rec, stats = _rec([Sample(0.0, -900.0, 32.0, 300.0, life=1),
                       Sample(1.0, 900.0, 32.0, 300.0, life=1)])     # 1,800 m in 1 s
    assert stats["segments_too_fast"] == 1
    assert rec.floor_at(0.0, 32.0, trusted=False) is None


def test_a_fix_kilometres_off_between_two_good_ones_is_dropped():
    s = [Sample(0.0, 0.0, 0.0, 500.0, life=1),
         Sample(1.5, 900.0, 900.0, 200.0, life=1),     # 1.3 km in 1.5 s, and back
         Sample(3.0, 150.0, 0.0, 500.0, life=1)]
    rec, stats = _rec(s)
    assert stats["spikes"] == 1
    assert rec.floor_at(900.0, 900.0, trusted=False) is None


def test_a_point_inside_the_ground_is_dropped_but_an_overhang_gap_is_free():
    top = np.zeros((160, 160), np.float32)
    top[80:84, 80:84] = 1000.0                 # a 64 m block, 1 km high, at x, z 0..64
    floor = np.full_like(top, np.nan)
    ceil = np.full_like(top, np.nan)
    hm = HeightMap(top, 16.0, -1280.0, floor, ceil)
    rec, stats = _rec([Sample(0.0, 32.0, 32.0, 400.0, life=1)], height=hm)
    assert stats["samples_inside"] == 1
    assert rec.floor_at(32.0, 32.0, trusted=False) is None

    # the same block as an arch: free air from the ground (0 m) to its underside (600 m)
    floor[80:84, 80:84] = 0.0
    ceil[80:84, 80:84] = 600.0
    hm = HeightMap(top, 16.0, -1280.0, floor, ceil)
    rec, stats = _rec([Sample(0.0, 32.0, 32.0, 400.0, life=1)], height=hm)
    assert stats["samples_inside"] == 0
    assert rec.floor_at(32.0, 32.0, trusted=False) == 400.0
    assert rec.overhang[_cell_of(rec, 32.0, 32.0)]


def test_fill_through_a_ridge_is_dropped_where_it_would_be_inside_rock():
    top = np.zeros((160, 160), np.float32)
    top[:, 80:84] = 2000.0                     # a north-south ridge at x 0..64
    hm = HeightMap(top, 16.0, -1280.0)
    rec, stats = _rec([Sample(0.0, -300.0, 32.0, 500.0, life=1),
                       Sample(1.0, 300.0, 32.0, 500.0, life=1)], height=hm)
    assert stats["fill_inside"] >= 1
    assert rec.floor_at(32.0, 32.0, trusted=False) is None
    assert rec.floor_at(-200.0, 32.0, trusted=False) == 500.0


def test_crash_cell_keeps_the_highest_crash():
    rec, _ = _rec([], crashes=[Crash(0.0, 100.0, 100.0, 300.0, 1.0), Crash(5.0, 110.0, 90.0, 450.0, 0.5)])
    ij = _cell_of(rec, 100.0, 100.0)
    assert rec.crash_alt[ij] == 450.0
    assert rec.crash_n[ij] == 2
    assert rec.crashes.shape == (2, 5)


def test_save_and_load_round_trip(tmp_path):
    rec, _ = _rec([Sample(0.0, 100.0, 100.0, 500.0, life=1), Sample(9.0, 100.0, 100.0, 520.0, life=2)],
                  crashes=[Crash(1.0, 0.0, 0.0, 10.0, 1.0)],
                  rearms=[Rearm(2.0, 5.0, 6.0, 700.0), Rearm(3.0, 7.0, 8.0, 650.0, manual=True)],
                  meta={"arena": "test"})
    path = tmp_path / "test-airspace.npz"
    rec.save(str(path))
    back = AirspaceRecord.load(str(path))
    assert back.meta["arena"] == "test"
    assert back.floor_at(100.0, 100.0) == 520.0
    np.testing.assert_array_equal(back.passes, rec.passes)
    assert back.rearms.tolist() == [[2.0, 5.0, 6.0, 700.0, 0.0], [3.0, 7.0, 8.0, 650.0, 1.0]]


def test_height_map_rows_are_z_and_columns_are_x():
    top = np.zeros((160, 160), np.float32)
    top[90, 70] = 777.0                        # z = -1280 + 90.5 * 16, x = -1280 + 70.5 * 16
    hm = HeightMap(top, 16.0, -1280.0)
    x, z = -1280.0 + 70.5 * 16.0, -1280.0 + 90.5 * 16.0
    assert hm.ground(x, z) == 777.0
    assert hm.ground(z, x) == 0.0
    gmax, gmin, _ = hm.per_cell(CELL, -HALF, int(2 * HALF / CELL))
    i, j = math.floor((z + HALF) / CELL), math.floor((x + HALF) / CELL)
    assert gmax[i, j] == 777.0 and gmin[i, j] == 0.0
    assert gmax[j, i] == 0.0


def test_mesh_check_reports_rock_above_a_floor_inside_its_cell_and_crashes_on_the_ground():
    top = np.zeros((160, 160), np.float32)
    top[80, 80] = 600.0                        # one 16 m spike in the cell at x, z 0..64
    hm = HeightMap(top, 16.0, -1280.0)
    rec, _ = _rec([Sample(0.0, 48.0, 48.0, 200.0, life=1), Sample(9.0, 48.0, 48.0, 220.0, life=2)],
                  crashes=[Crash(0.0, -500.0, -500.0, 20.0, 1.0)], height=hm)
    chk = mesh_check(rec, hm, arena_radius_m=HALF)
    assert chk["flown_cells"] == 1 and chk["trusted_cells"] == 1
    assert chk["rock_above_floor"] == 1 and chk["rock_above_trusted_floor"] == 1
    assert chk["floor_under_all_ground"] == 0
    assert chk["bands"][0]["cells"] == 1                     # below the cell's highest ground
    assert chk["crashes_within_100m"] == 1


# --- the log reader ---------------------------------------------------------------------

def _line(t, text, level="DEBUG"):
    whole = int(t)
    ms = int(round((t - whole) * 1000))
    return f"2026-11-26 12:{whole // 60:02d}:{whole % 60:02d},{ms:03d} [{level}] {text}\n"


def _alt(t, value):
    return _line(t, f"\x1b[93mPADLOCK: OFF | Altitude: {value} | Speed: 543 | Nose: +2\N{DEGREE SIGN} (level)\x1b[0m",
                 "INFO")


def _fix(t, x, z, name="CRIMSON CANYON SUNRISE"):
    return _line(t, f"TERRAIN: map={name} fix={x},{z} score=0.92/0.37 ground=-232 speed=135 "
                    "ttc_y=clear ttc_m=clear arm=map act=- ms=27")


def _write(tmp_path, lines):
    """Lines in time order; a stable sort keeps a WARNING before the PADLOCK line it
    shares a millisecond with, as the analyzer logs them."""
    path = tmp_path / "wingman_test.log"
    path.write_text("".join(sorted(lines, key=lambda line: line[:23])))
    return str(path)


T0 = ar._epoch(_line(0.0, "the epoch of offset 0"))


ARENAS = {"CRIMSON CANYON": "crimson-canyon", "CRIMSON CANYON SUNRISE": "crimson-canyon"}


def _flight(t0, t1, x0=0.0, alt=lambda t: 1000.0, step=1.5, vx=200.0):
    """A battle tick every `step` s: the PADLOCK reading, then the fix 30 ms later."""
    lines, t = [], t0
    while t <= t1:
        lines.append(_alt(t, int(alt(t))))
        lines.append(_fix(t + 0.03, int(x0 + vx * (t - t0)), 0))
        t += step
    return lines


def test_reader_takes_the_higher_bracketing_altitude_and_maps_variants_to_one_arena(tmp_path):
    lines = [_line(0.0, "Entering state GAME_BATTLE. Processing callbacks...")]
    lines += _flight(1.0, 20.0, alt=lambda t: 1000.0 + 50.0 * t)      # climbing: the later read is higher
    ev = ar.parse_log(_write(tmp_path, lines))
    out, counts, manual, _ = ar.extract(ev, ARENAS, life_base=0)
    samples = out["crimson-canyon"]["samples"]
    assert counts["accepted"] == len(samples) > 5
    first = samples[0]
    assert first.alt == 1000.0 + 50.0 * 2.5          # the reading 1.47 s after the fix, not before
    assert not first.manual


def test_reader_drops_a_rejected_reading_and_the_last_8_s_before_a_death(tmp_path):
    lines = [_line(0.0, "Entering state GAME_BATTLE. Processing callbacks...")]
    lines += _flight(1.0, 36.0)                      # the last fix at 35.53
    # a filter rejection pairs with the reading logged in the same millisecond
    lines.insert(5, _line(4.0, "Analyzer: telemetry plausibility filter rejected reading "
                               "(speed_raw=37420 altitude_raw=1000, total_rejected=1)", "WARNING"))
    # death at 42.0 - 5.0 = 37.0: fixes after 29.0 are excluded
    lines.append(_line(42.0, "RespawnLatency: OCR edge \N{EM DASH} since_confirmed=8.28s no_digits_for=5.00s",
                       "INFO"))
    lines.append(_line(49.0, "DEATH CARD: no kill card on 1 read(s) of the respawn screen: "
                             "a crash or self-inflicted", "INFO"))
    ev = ar.parse_log(_write(tmp_path, lines))
    assert len(ev.deaths) == 1
    t_death, t_edge, crash = ev.deaths[0]
    assert (t_death - T0, t_edge - T0, crash) == (pytest.approx(37.0), pytest.approx(42.0), True)
    out, counts, _, _ = ar.extract(ev, ARENAS, life_base=0)
    assert counts["unclean"] == 2                    # the fix either side of the rejected reading
    assert counts["pre-death"] == 5                  # 29.53, 31.03, 32.53, 34.03, 35.53
    assert all(s.t < t_death - 8.0 for s in out["crimson-canyon"]["samples"])
    crashes = out["crimson-canyon"]["crashes"]
    assert len(crashes) == 1
    c = crashes[0]
    last_fix_t, _, last_x, _ = ev.fixes[-1]
    assert c.fix_age_s == pytest.approx(t_death - last_fix_t, abs=0.01)
    assert c.x == pytest.approx(last_x + 200.0 * c.fix_age_s, abs=5.0)   # carried along the last step


def test_reader_starts_a_new_life_at_a_death_and_flags_manual_flight(tmp_path):
    lines = [_line(0.0, "Entering state GAME_BATTLE. Processing callbacks...")]
    lines += _flight(1.0, 10.0)
    lines.append(_line(20.0, "RespawnLatency: OCR edge \N{EM DASH} since_confirmed=8s no_digits_for=4.00s", "INFO"))
    lines.append(_line(22.0, "DEATH CARD: killed by 'x' with 'y' (the card on 1 of 2 reads)", "INFO"))
    lines.append(_line(24.0, "Entering state GAME_BATTLE_MANUAL. Processing callbacks..."))
    lines += _flight(25.0, 34.0)
    lines.append(_line(35.0, "RESUPPLY: confirmed ammo=4; urgency reset, resuming target pursuit", "INFO"))
    ev = ar.parse_log(_write(tmp_path, lines))
    out, _, manual, lives = ar.extract(ev, ARENAS, life_base=100)
    s = out["crimson-canyon"]["samples"]
    before = {x.life for x in s if x.t - T0 < 16.0}
    after = {x.life for x in s if x.t - T0 > 24.0}
    assert len(before) == 1 and len(after) == 1 and before != after
    assert min(before) >= 100
    assert all(x.manual for x in s if x.t - T0 > 24.0) and not any(x.manual for x in s if x.t - T0 < 16.0)
    assert manual["fixes"] > 0 and manual["samples"] > 0
    assert out["crimson-canyon"]["crashes"] == []    # killed by an enemy, not a crash
    rearms = out["crimson-canyon"]["rearms"]
    assert len(rearms) == 1
    assert rearms[0].t - T0 == pytest.approx(31.03)  # the last fix at least 3 s before the confirmation


def test_reader_keeps_the_resupply_run_flown_in_game_battle_eject(tmp_path):
    """GAME_BATTLE_EJECT is "missiles empty" (state.py), not a death. Excluding it dropped 88%
    of a session's fixes on the first build, the resupply runs among them."""
    lines = [_line(0.0, "Entering state GAME_BATTLE. Processing callbacks..."),
             _line(0.5, "Entering state GAME_BATTLE_EJECT. Processing callbacks...")]
    lines += _flight(1.0, 10.0)
    ev = ar.parse_log(_write(tmp_path, lines))
    out, counts, _, _ = ar.extract(ev, ARENAS, life_base=0)
    assert counts["accepted"] == len(ev.fixes) - 1 > 0      # all but the last, which has no later read
    assert len({s.life for s in out["crimson-canyon"]["samples"]}) == 1


def test_reader_skips_fixes_on_maps_without_data(tmp_path):
    lines = [_line(0.0, "Entering state GAME_BATTLE. Processing callbacks...")]
    lines += [_alt(1.0, 900), _fix(1.03, 0, 0, name="NOWHERE"), _alt(2.5, 900)]
    ev = ar.parse_log(_write(tmp_path, lines))
    out, counts, _, _ = ar.extract(ev, ARENAS, life_base=0)
    assert counts["unknown-map"] == 1 and not out


def test_arenas_from_config_keys_by_height_map(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "terrain_service:\n  maps:\n"
        "    ICEBOUND:\n      minimap: d/arctic-3-minimap.png\n      height: d/arctic-3-height-16m.npz\n"
        "    ICEBOUND NIGHT:\n      minimap: d/arctic-3-minimap.png\n      height: d/arctic-3-height-16m.npz\n"
        "    ICEBOUND SNOW:\n      minimap: d/arctic-3-minimap.png\n      height: d/arctic-3-snow-height-16m.npz\n")
    arena_of, height_of, minimap_of = ar.arenas_from_config(str(cfg))
    assert arena_of == {"ICEBOUND": "arctic-3", "ICEBOUND NIGHT": "arctic-3", "ICEBOUND SNOW": "arctic-3-snow"}
    assert height_of["arctic-3-snow"] == "d/arctic-3-snow-height-16m.npz"
    assert minimap_of["arctic-3"] == "d/arctic-3-minimap.png"


def test_the_shipped_config_names_a_height_map_for_every_terrain_map():
    arena_of, height_of, _ = ar.arenas_from_config("wingman/config.yaml")
    assert "CRIMSON CANYON" in arena_of
    assert arena_of["CRIMSON CANYON SUNRISE"] == arena_of["CRIMSON CANYON"]
    assert set(arena_of.values()) == set(height_of)


# --- the per-log cache ------------------------------------------------------------------

def _session(tmp_path, name="wingman_20261126_120000.log", end=20.0):
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    lines = [_line(0.0, "Entering state GAME_BATTLE. Processing callbacks...")] + _flight(1.0, end)
    path = logs / name
    path.write_text("".join(sorted(lines, key=lambda line: line[:23])))
    return path


def test_a_pruned_log_still_counts_from_its_cache(tmp_path):
    """Wingman deletes its oldest rotated logs at each start (about eight days kept): on
    2026-11-26 a rebuild lost a log between the listing and the open, and every log older than
    that window had already left the record."""
    log = _session(tmp_path)
    cache = str(tmp_path / "cache")
    entries, notes = ar.collect([str(log)], ARENAS, cache)
    assert notes["parsed"] == 1 and len(entries) == 1
    first = entries[0][1]["arenas"]["crimson-canyon"]["samples"]
    log.unlink()
    entries, notes = ar.collect([], ARENAS, cache)
    assert notes["kept"] == 1
    again = entries[0][1]["arenas"]["crimson-canyon"]["samples"]
    np.testing.assert_array_equal(first, again)


def test_a_current_cache_is_used_and_a_changed_log_is_read_again(tmp_path):
    log = _session(tmp_path)
    cache = str(tmp_path / "cache")
    ar.collect([str(log)], ARENAS, cache)
    _, notes = ar.collect([str(log)], ARENAS, cache)
    assert notes["cached"] == 1 and notes["parsed"] == 0
    _session(tmp_path, end=40.0)                     # same name, more flight
    entries, notes = ar.collect([str(log)], ARENAS, cache)
    assert notes["parsed"] == 1
    assert len(entries[0][1]["arenas"]["crimson-canyon"]["samples"]) > 12


def test_a_log_that_vanishes_is_skipped_and_the_live_log_is_never_cached(tmp_path):
    cache = tmp_path / "cache"
    entries, notes = ar.collect([str(tmp_path / "logs" / "wingman_gone.log")], ARENAS, str(cache))
    assert notes["vanished"] == 1 and entries == []
    live = _session(tmp_path, name="wingman.log")
    entries, notes = ar.collect([str(live)], ARENAS, str(cache))
    assert notes["parsed"] == 1 and len(entries) == 1
    assert not cache.exists() or not list(cache.iterdir())


def test_lives_stay_separate_across_cached_logs(tmp_path):
    a = _session(tmp_path, name="wingman_20261126_110000.log")
    b = _session(tmp_path, name="wingman_20261126_120000.log")
    entries, _ = ar.collect([str(a), str(b)], ARENAS, str(tmp_path / "cache"))
    assert [e[1]["lives"] for e in entries] == [2, 2]
    # each log numbers its own lives from 0; main() offsets them so two logs never share one
    assert all(int(e[1]["arenas"]["crimson-canyon"]["samples"][:, 4].min()) >= 0 for e in entries)


# --- the planner's query (phase 2) --------------------------------------------------------

def _two_pass(x, z, alt):
    return [Sample(0.0, x, z, alt, life=1), Sample(9.0, x, z, alt, life=2)]


def test_query_free_needs_a_trusted_floor_cleared_by_the_margin():
    rec, _ = _rec(_two_pass(32.0, 32.0, 500.0) + [Sample(0.0, 600.0, 600.0, 500.0, life=1)])
    q = AirspaceQuery(rec, margin_side=0.0, margin_up=50.0)
    codes = q.points([32.0, 32.0, 600.0, -600.0], [32.0, 32.0, 600.0, -600.0], [560.0, 540.0, 900.0, 900.0])
    assert codes.tolist() == [FREE, NOT_KNOWN, NOT_KNOWN, NOT_KNOWN]   # cleared; under the margin; one pass; unflown


def test_query_holds_a_point_to_higher_floors_within_the_side_margin_only():
    near = _two_pass(32.0 + CELL, 32.0, 800.0)              # the next cell east, 64 m away
    far = _two_pass(32.0 - 4 * CELL, 32.0, 2000.0)          # four cells west, 256 m away
    rec, _ = _rec(_two_pass(32.0, 32.0, 500.0) + near + far)
    q = AirspaceQuery(rec, margin_side=100.0, margin_up=50.0)
    assert q.points([32.0], [32.0], [600.0]).tolist() == [NOT_KNOWN]  # under 800 + 50 next door
    assert q.points([32.0], [32.0], [860.0]).tolist() == [FREE]       # the far 2,000 m floor is not held against it


def test_query_ground_comes_from_the_mesh_and_beats_the_record():
    top = np.zeros((160, 160), np.float32)
    top[82, 82] = 900.0                                     # a 16 m spike inside the cell at x, z 0..64
    floor = np.full_like(top, np.nan)
    ceil = np.full_like(top, np.nan)
    hm = HeightMap(top, 16.0, -1280.0, floor, ceil)
    rec, _ = _rec(_two_pass(8.0, 8.0, 300.0))               # flown at 300 m in the cell's corner
    q = AirspaceQuery(rec, hm, margin_side=0.0, margin_up=50.0)
    spike = (16.0 * 2 + 8.0, 16.0 * 2 + 8.0)                # the spike's 16 m cell
    assert q.points([8.0, spike[0]], [8.0, spike[1]], [400.0, 400.0]).tolist() == [FREE, GROUND]
    floor[82, 82], ceil[82, 82] = 0.0, 600.0                # now an arch over free air
    q = AirspaceQuery(rec, HeightMap(top, 16.0, -1280.0, floor, ceil), margin_side=0.0, margin_up=50.0)
    assert q.points([spike[0]], [spike[1]], [400.0]).tolist() == [FREE]


def test_query_path_is_its_worst_point_and_names_the_first_that_is_not_free():
    rec, _ = _rec(_two_pass(32.0, 32.0, 500.0) + _two_pass(32.0 + CELL, 32.0, 500.0))
    q = AirspaceQuery(rec, margin_side=0.0, margin_up=50.0)
    assert q.path([32.0, 96.0], [32.0, 32.0], [600.0, 600.0]) == (FREE, None)
    assert q.path([32.0, 96.0, 160.0], [32.0, 32.0, 32.0], [600.0, 600.0, 600.0]) == (NOT_KNOWN, 2)
    assert q.path([], [], []) == (NOT_KNOWN, None)


# --- re-arms flown by hand ----------------------------------------------------------------

def _bt(t, missiles):
    return _line(t, f"BT[active]: selected=Climb missiles={missiles} rings=0/0/4 alt=900 hdg=90")


def _manual_session(tmp_path, counts, state="GAME_BATTLE_MANUAL"):
    """A battle tick every 1.5 s from 1 s, each with a tick line carrying the next missile count."""
    lines = [_line(0.0, "Entering state GAME_BATTLE. Processing callbacks..."),
             _line(0.5, f"Entering state {state}. Processing callbacks...")]
    lines += _flight(1.0, 1.0 + 1.5 * (len(counts) - 1))
    lines += [_bt(1.0 + 1.5 * k + 0.01, n) for k, n in enumerate(counts)]
    return ar.parse_log(_write(tmp_path, lines))


def test_a_ring_flown_by_hand_is_a_rearm_placed_like_wingmans():
    """Wingman logs `confirmed ammo` only from its own pursuit: 1 of 318 re-arms on record by
    2026-11-26 was flown by hand. The missile count on the tick line shows the operator's."""
    ev = _manual_session(tmp_path=_tmp(), counts=[0, 0, 0, None, 0, 0, 4, 4, 4, 4, 4, 4])
    tl = ar.Timeline(ev, 0)
    rearms = ar.manual_rearms(ev, tl)
    assert [round(t - T0, 2) for t in rearms] == [10.01]        # the first read of the new count
    out, _, _, _ = ar.extract(ev, ARENAS, 0)
    placed, = out["crimson-canyon"]["rearms"]
    assert 4.0 <= rearms[0] - placed.t <= 6.0 + 0.1               # the last fix 3 to 6 s before it


@pytest.mark.parametrize("counts, state", [
    ([4, None, 4, None, 4, 4, None, 4], "GAME_BATTLE_MANUAL"),   # flicker to None is not a rise
    ([0, 0, 75, 75, 0, 0], "GAME_BATTLE_MANUAL"),                # a misread is not a re-arm
    ([0, 4, 0, 4, 0, 4], "GAME_BATTLE_MANUAL"),                  # never stable twice running
    ([0, 0, 4, 4, 4], "GAME_BATTLE"),                            # wingman's own: logged as confirmed ammo
], ids=["flicker", "misread", "unstable", "not-manual"])
def test_noise_and_wingmans_own_flight_are_not_manual_rearms(counts, state):
    ev = _manual_session(tmp_path=_tmp(), counts=counts, state=state)
    assert ar.manual_rearms(ev, ar.Timeline(ev, 0)) == []


def _tmp():
    import tempfile
    return Path(tempfile.mkdtemp())


def test_a_cache_written_before_rearms_carried_manual_still_loads():
    """A log cached under version 1 and since deleted keeps its four-column re-arm rows."""
    old = np.array([[1.0, 2.0, 3.0, 400.0]])
    np.testing.assert_array_equal(ar._width(old, 5), [[1.0, 2.0, 3.0, 400.0, 0.0]])
    assert ar._width(np.zeros((0,)), 5).shape == (0, 5)


# --- the session loop rebuilds the record (Makefile `r`) ----------------------------------

def _r_recipe() -> str:
    lines = Path("Makefile").read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("r: "))
    body = []
    for line in lines[start + 1:]:
        if not line.startswith("\t"):
            break
        body.append(line)
    return "\n".join(body)


def test_the_session_loop_rebuilds_the_record_detached_after_wingman_exits():
    """Design 017: `make airspace` after every session, so the record, the candidates and the
    scenes follow the flying without anyone running it. It must not hold the relaunch, must not
    change the exit status the ADR 202 loop reads, and must not name $(MAKE): a recipe line that
    does runs even under `make -n` (2026-11-23, `make -n r3` started wingman)."""
    recipe = _r_recipe()
    status_at = recipe.index("status=$$?")
    rebuild_at = recipe.index("if [ -d test_screenshots/terrain ]")
    assert status_at < rebuild_at < recipe.index("done;")
    rebuild = recipe[rebuild_at:recipe.index("fi;", rebuild_at) + 3]
    assert "airspace" in rebuild
    assert "$(CYCLE_MAKE)" in rebuild and "$(MAKE)" not in rebuild
    assert "& )" in rebuild                      # detached: the relaunch does not wait for it
    assert "nice -n" in rebuild


def test_a_rearm_takes_the_nearest_clean_altitude_either_side_of_its_fix():
    """2026-11-26 21:20, Stormbreak: both readings before the operator's re-arm fix were rejected
    for a speed misread, and the re-arm was dropped. A point needs only one clean altitude nearby."""
    readings = [(10.0, 1500.0, True), (12.0, 1513.0, False), (13.5, 7510.0, False), (15.0, 1490.0, True)]
    rt = [r[0] for r in readings]
    assert ar._nearest_clean(rt, readings, 13.6, 3.0) == 3          # 1.4 s after, not 3.6 s before
    assert ar._nearest_clean(rt, readings, 13.6, 1.0) is None
