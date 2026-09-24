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
