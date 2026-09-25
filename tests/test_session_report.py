"""`make session-report`: the log parser behind the one-page session report.

Built from a synthetic log so each counter has a known answer. Cross-checked once against a
real 24-minute session (2,193 ticks, 252 lock ticks, 33 acquisitions of which 24 outside the
old box, `clu` 233/18/1, 111/570 pursuit and 128/1275 dive locked scans) that had first been
counted by hand with a different script; the two agreed exactly.
"""

import importlib.util
from pathlib import Path

import pytest
import yaml

_SPEC = importlib.util.spec_from_file_location(
    "session_report", Path(__file__).resolve().parent.parent / "scripts" / "session-report.py")
R = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(R)


def _ts(n):
    return f"2026-09-24 10:00:{n:02d},000"


def _pick(n, path, sel=None, gate="reject", glyphs=0, clu=None):
    s = f"({sel[0]},{sel[1]})" if sel else "-"
    line = (f"{_ts(n)} [DEBUG] TRACKPICK: path={path} sel={s} tall=- n_tall=0 red_won=True "
            f"gate={gate} glyphs={glyphs} rm_px=500 edges=- blob=-")
    return line + (f" clu={clu}" if clu is not None else "")


_LOG = [
    f"{_ts(1)} [INFO] Configuration loaded",
    _pick(2, "none"), _pick(3, "none"), _pick(4, "none"),
    _pick(5, "redmass", (960, 500), "pass", 25, 1),          # acquisition INSIDE the old box
    _pick(6, "redmass", (970, 505), "pass", 26, 1),          # continuing lock, not an acquisition
    _pick(7, "none"), _pick(8, "none"), _pick(9, "none"),
    _pick(10, "redmass", (1020, 900), "pass", 28, 2),        # acquisition below the old box
    _pick(11, "none", glyphs=14, gate="reject"),             # rejected with glyphs
    _pick(12, "none"), _pick(13, "none"), _pick(14, "none"),
    _pick(15, "redmass", (1700, 150), "pass", 22, 1),        # lock inside the minimap zone: a bug
    f"{_ts(16)} [INFO] \x1b[91m\U0001F4A5 DIED ARMED — 4 missile(s), cause=enemy_fire (incoming 6.0s ago), alt=None rate=None (x)\x1b[0m",
    f"{_ts(17)} [INFO] \x1b[91m⚠ RESPAWN DETECTED - Cancelling active missions\x1b[0m",
    f"{_ts(18)} [DEBUG] Controller: switch_weapon - pressing 'g' key for 0.1 seconds",
    f"{_ts(19)} [INFO] Controller: eject heatdive — selected weapon empty (3 consecutive zero reads), switching",
    f"{_ts(20)} [INFO] \x1b[91m\U0001F680 PURSUIT CAP — cancelling mission\x1b[0m",
    f"{_ts(21)} [INFO] PURSUIT SUMMARY: end=cap dur=20.1s scans=60 locked=10 (17%) first_lock=5.0s ammo=6->4 switched=no",
    f"{_ts(22)} [INFO] PURSUIT SUMMARY: end=external:match_ended dur=6.0s scans=18 locked=0 (0%) first_lock=- ammo=6->6 switched=no",
    f"{_ts(23)} [INFO] DIVE SUMMARY: end=dive-end dur=40.0s scans=120 locked=12 (10%) first_lock=4.0s ammo=4->1 switched=no",
    f"{_ts(24)} [ERROR] GAME_UNKNOWN startup classification timeout after 91.0s",
    f"{_ts(25)} [ERROR] GAME_UNKNOWN startup classification timeout after 92.0s",
    f"{_ts(26)} [ERROR] Something else broke: boom",
    f"{_ts(27)} [WARNING] a warning",
    f"{_ts(28)} [INFO] \x1b[93m\U0001F4CB GenericCloseRecovery: GAME_UNKNOWN for 26s — clicking a close button at (1701,255)\x1b[0m",
    f"{_ts(29)} [DEBUG] ROIFOLLOW: gate rejected a red mass cut by the right crop edge (glyphs=15 rm_px=914)",
    f"{_ts(30)} [INFO] Wingman Session Summary",
    "━" * 30,
    "Session duration  : 1m 00s",
    "Missions started  : 2",
]


def _r():
    return R.analyse(_LOG)


def test_time_span_and_line_count():
    r = _r()
    assert (r["first_ts"], r["last_ts"], r["lines"]) == ("2026-09-24 10:00:01", "2026-09-24 10:00:30", len(_LOG))


def test_errors_are_split_from_the_startup_timeout_noise():
    r = _r()
    assert r["classification_timeouts"] == 2
    assert sum(r["errors"].values()) == 1 and "Something else broke" in next(iter(r["errors"]))
    assert r["warnings"] == 1


def test_engagement_summaries_are_parsed():
    r = _r()
    kinds = [(e["kind"], e["end"], e["locked"], e["scans"]) for e in r["engagements"]]
    assert kinds == [("PURSUIT", "cap", 10, 60), ("PURSUIT", "external:match_ended", 0, 18), ("DIVE", "dive-end", 12, 120)]
    assert R._fired("6->4") and R._fired("4->1") and not R._fired("6->6") and not R._fired("-")


def test_locks_acquisitions_and_where_they_are():
    r = _r()
    assert (r["ticks"], r["locks"]) == (14, 4)
    assert r["acquisitions"] == 3, "three non-lock ticks, then a lock, counts once each time"
    assert r["acq_outside_old"] == 2                          # the one below and the minimap one
    assert dict(r["acq_sides"]) == {"below": 1, "aboveright": 1}
    assert r["lock_outside_old"] == 2
    assert r["lock_in_hud_zone"] == 1, "the lock at (1700, 150) is inside the minimap zone"
    assert dict(r["clu"]) == {"1": 3, "2": 1}
    assert r["gate_reject_some_glyphs"] == 1


def test_a_keep_tick_is_a_lock_but_never_an_acquisition():
    """path=keep (2026-09-24) is a lock held on the looser keep test: it counts as a lock
    tick, and a redmass tick after three of them continues the lock rather than acquiring."""
    log = [
        _pick(1, "none"), _pick(2, "none"), _pick(3, "none"),
        _pick(4, "redmass", (960, 500), "pass", 25, 1),                   # acquisition
        _pick(5, "keep", (965, 505), "keep", 12, 0),
        _pick(6, "keep", (970, 505), "keep", 11, 0),
        _pick(7, "keep", (975, 505), "keep", 10, 0),
        _pick(8, "redmass", (980, 505), "pass", 24, 1),
    ]
    r = R.analyse(log)
    assert (r["ticks"], r["locks"], r["acquisitions"]) == (8, 5, 1)
    assert dict(r["clu"]) == {"1": 2, "0": 3}


def test_weapon_life_and_recovery_counters():
    r = _r()
    assert (r["weapon_presses"], r["empty_switches"], r["pursuit_caps"]) == (1, 1, 1)
    assert r["respawns"] == 1 and dict(r["died_armed"]) == {"enemy_fire": 1}
    assert r["close_recovery_clicks"] == 1 and r["roi_follows"] == 1


def test_the_session_summary_block_is_captured():
    assert _r()["session_summary"][:2] == ["Session duration  : 1m 00s", "Missions started  : 2"]


def test_render_names_the_numbers_and_flags_a_hud_lock():
    text = " ".join(R.render(_r(), "x.log").split())          # ignore column spacing
    assert "pursuit 2 engagements any lock 1 (50%)" in text
    assert "length median 20s, longest 20s" in text            # pursuits of 20.1 s and 6.0 s, sorted, upper median
    assert "locked scans 10/78 (13%; pooled before the widened region 5.8%)" in text
    assert "in an excluded HUD zone 1 (must be 0)" in text
    assert "acquisitions 3 outside the old box 2 (67%)" in text
    assert "generic close clicks 1" in text


def test_a_log_without_trackpick_says_so_instead_of_reporting_zeros():
    text = R.render(R.analyse([l for l in _LOG if "TRACKPICK" not in l]))
    assert "no TRACKPICK lines: this needs a DEBUG log" in text
    assert "acquisitions" not in text


def test_an_empty_log_does_not_crash():
    text = R.render(R.analyse([]))
    assert "none in this log" in text


def test_the_old_box_and_zones_match_the_shipped_config():
    """The report's constants are copies of config values; if config.yaml moves, this fails."""
    cfg = yaml.safe_load((Path(__file__).resolve().parent.parent / "wingman" / "config.yaml").read_text())
    zones = cfg["tracking"]["red_mass_exclude_zones_pct"]
    assert len(zones) == len(R.HUD_ZONES)
    for (x1, y1, x2, y2), zone in zip(R.HUD_ZONES, zones, strict=True):
        assert (x1 / 1920, y1 / 1200, x2 / 1920, y2 / 1200) == pytest.approx(tuple(zone), abs=0.002)


# --- ADR 147: the altitude a chase settles at --------------------------------------------

def _alt(n, alt):
    return f"{_ts(n)} [INFO] PADLOCK: OFF | Altitude: {alt} | Speed: 409 | Nose: -4° (level)"


_ALT_LOG = [
    f"{_ts(1)} [INFO] Controller: mission_su30 - step 4/4: activating pursuit mode",
    f"{_ts(1)} [WARNING] Controller: mission_su30 - nose angle -10 deg not confirmed within 20s, continuing to pursuit",
    _alt(5, 5000),                                                   # before the pursuit window
    _alt(11, 3000), _alt(13, 3100), _alt(15, 3200), _alt(17, 3050), _alt(19, 2950),
    f"{_ts(20)} [INFO] PURSUIT SUMMARY: end=external:match_ended dur=10.0s scans=30 locked=3 (10%) first_lock=2.0s ammo=4->4 switched=no",
    _alt(25, 100),                                                   # after it
    f"{_ts(26)} [WARNING] BT: ALTITUDE FLOOR — 2900m below 3000m — climb forced (operator directive)",
    f"{_ts(27)} [WARNING] BT: ALTITUDE FLOOR — 2950m below 3000m — climb forced (operator directive)",
    f"{_ts(28)} [WARNING] BT: ALTITUDE FLOOR — 3990m below 4000m — climb forced (operator directive)",
]


def test_only_altitude_readings_inside_a_pursuit_window_are_kept():
    r = R.analyse(_ALT_LOG)
    assert r["pursuit_alts"] == [2950, 3000, 3050, 3100, 3200]


def test_floor_events_are_counted_by_the_floor_they_cite():
    r = R.analyse(_ALT_LOG)
    assert dict(r["floor_events"]) == {3000: 2, 4000: 1}


def test_su30_handoffs_and_the_unconfirmed_nose_angle_are_counted():
    r = R.analyse(_ALT_LOG)
    assert (r["su30_handoffs"], r["su30_nose_unconfirmed"]) == (1, 1)


def test_the_report_prints_the_altitude_block():
    text = R.render(R.analyse(_ALT_LOG))
    assert "pursuit altitude median 3050 m" in text
    assert "10th to 90th percentile 2950 to 3100 m" in text
    assert "(5 readings inside pursuit windows)" in text
    assert "ALTITUDE FLOOR events by floor {3000: 2, 4000: 1}" in text
    assert "su30 hand-offs 1, nose angle not confirmed 1" in text


def test_the_report_says_so_when_no_altitude_falls_in_a_pursuit():
    text = R.render(R.analyse([_alt(5, 4000)]))
    assert "no altitude readings inside a pursuit window" in text
    assert "ALTITUDE FLOOR events by floor none" in text


# --- lock runs and how close to centre the steering point sits ----------------------------

_LOCK_LOG = [
    _pick(1, "redmass", (960, 600), "pass", 25, 1), _pick(2, "keep", (970, 610), "keep", 12, 0),   # run of 2
    _pick(3, "none"),
    _pick(4, "redmass", (1000, 700), "pass", 25, 1),                                              # run of 1
    _pick(5, "none"), _pick(6, "none"),
    _pick(7, "redmass", (960, 620), "pass", 25, 1), _pick(8, "keep", (960, 640), "keep", 12, 0),
    _pick(9, "keep", (960, 660), "keep", 12, 0),                                                  # run of 3
]


def test_lock_runs_are_consecutive_lock_ticks():
    r = R.analyse(_LOCK_LOG)
    assert r["lock_runs"] == [1, 2, 3]


def test_the_steering_point_offsets_are_from_the_frame_centre():
    r = R.analyse(_LOCK_LOG)
    assert r["lock_dx"] == [0, 0, 0, 0, 10, 40]
    assert r["lock_dy"] == [0, 10, 20, 40, 60, 100]


def test_a_run_still_open_at_the_end_of_the_log_is_counted():
    r = R.analyse([_pick(1, "redmass", (960, 600), "pass", 25, 1), _pick(2, "keep", (960, 600), "keep", 12, 0)])
    assert r["lock_runs"] == [2]


def test_the_report_prints_run_lengths_and_centring():
    text = R.render(R.analyse(_LOCK_LOG))
    assert "lock runs 3 (median 2 ticks, longest 3, 0 of 4 or more)" in text
    assert "|dx| median 0 px (100% inside the roll deadband)" in text
    assert "|dy| median 40 px (50% inside the pitch deadband)" in text


def test_the_deadband_constants_match_the_shipped_config():
    cfg = yaml.safe_load((Path(__file__).resolve().parent.parent / "wingman" / "config.yaml").read_text(encoding="utf-8"))
    t = cfg["tracking"]
    expected = (round(t["deadband"] * R.FRAME_CENTRE[0]), round(t["pitch_deadband"] * R.FRAME_CENTRE[1]))
    assert expected == R.DEADBAND_PX


# --- the HUD zones are masked on the nameplate, the steering point sits 100 px below it -----

def test_a_steering_point_below_a_legitimate_label_is_not_a_hud_lock():
    """2026-09-25 00:05:01: a label at y 1004 (above the weapons panel, which starts at 1060) put its
    steering point at y 1104, inside the panel, and the report called it a HUD lock."""
    r = R.analyse([_pick(1, "redmass", (1749, 1104), "pass", 25, 1)])
    assert r["locks"] == 1 and r["lock_in_hud_zone"] == 0


def test_a_label_that_really_is_inside_a_hud_zone_still_counts():
    r = R.analyse([_pick(1, "redmass", (1749, 1190), "pass", 25, 1)])      # label at y 1090, in the panel
    assert r["lock_in_hud_zone"] == 1


def test_the_aim_offset_constant_matches_the_shipped_config():
    cfg = yaml.safe_load((Path(__file__).resolve().parent.parent / "wingman" / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["tracking"]["red_mass_aim_offset_px"] == R.AIM_OFFSET_PX
