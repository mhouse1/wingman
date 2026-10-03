"""CR-018-17: a reflex that raises is visible at INFO, not only at DEBUG.

The per-tick reflexes run inside ``except Exception`` so one failure cannot take
the main loop down. Before CR-018-17 those handlers logged at DEBUG only, so a
reflex that raised on every tick left an INFO session silent while it was dead.
"""

import logging
import pathlib

import pytest

import wingman.suppressed as suppressed
from wingman.game_shutdown import GamePresenceWatch
from wingman.mission_stats import MissionStatsTracker
from wingman.suppressed import log_suppressed, reset_suppressed_counts, suppressed_counts

_log = logging.getLogger("test.suppressed")


@pytest.fixture(autouse=True)
def _clean_counts():
    reset_suppressed_counts()
    yield
    reset_suppressed_counts()


def _fail(name):
    try:
        raise RuntimeError(f"{name} broke")
    except RuntimeError as exc:
        log_suppressed(_log, name, exc)


def _records(caplog, level):
    return [r for r in caplog.records if r.name == "test.suppressed" and r.levelno == level]


def test_the_first_failure_warns_with_its_traceback(caplog):
    with caplog.at_level(logging.DEBUG, logger="test.suppressed"):
        _fail("note_stall_prevention")
    warns = _records(caplog, logging.WARNING)
    assert len(warns) == 1
    assert "note_stall_prevention failed" in warns[0].getMessage()
    assert warns[0].exc_info is not None, "the traceback is the point of the warning"


def test_repeats_drop_to_debug_then_warn_again_every_nth(caplog, monkeypatch):
    """A reflex failing every tick must not flood the log, and must not go
    quiet for the rest of the session either."""
    monkeypatch.setattr(suppressed, "WARN_EVERY", 3)
    with caplog.at_level(logging.DEBUG, logger="test.suppressed"):
        for _ in range(7):
            _fail("detect_terrain_ahead")
    # Failures 1, 3 and 6 warn; 2, 4, 5 and 7 are DEBUG.
    assert len(_records(caplog, logging.WARNING)) == 3
    assert len(_records(caplog, logging.DEBUG)) == 4


def test_counts_are_kept_per_name():
    _fail("note_incoming")
    _fail("note_incoming")
    _fail("climb_emergency_update_fn")
    assert suppressed_counts() == {"note_incoming": 2, "climb_emergency_update_fn": 1}


def test_a_failing_presence_scan_warns_and_the_watch_keeps_running(caplog):
    """The watch that notices the game has gone must not die silently."""
    def _broken_finder(_name):
        raise OSError("/proc unreadable")

    watch = GamePresenceWatch(poll_interval_s=0.0, finder=_broken_finder)
    with caplog.at_level(logging.DEBUG, logger="wingman.game_shutdown"):
        assert watch.game_has_gone() is False
    assert any(r.levelno == logging.WARNING and "GamePresenceWatch scan failed" in r.getMessage()
               for r in caplog.records)
    assert suppressed_counts() == {"GamePresenceWatch scan": 1}


def test_every_per_tick_reflex_routes_through_log_suppressed():
    """The tick itself needs a full frame pipeline to run, so this pins the
    wiring in the source: each guarded reflex call reports via log_suppressed,
    and none of the old DEBUG-only lines is left."""
    src = pathlib.Path("wingman/tick_handlers.py").read_text()
    for name in ("detect_map_boundary", "note_incoming", "note_afterburner_cruise",
                 "note_stall_prevention", "note_boundary", "detect_terrain_ahead",
                 "climb_emergency_update_fn"):
        assert f'log_suppressed(logger, "{name}", exc)' in src, name
    for old in ('"note_incoming failed"', '"note_stall_prevention failed"',
                '"detect_terrain_ahead failed"', '"climb_emergency_update_fn failed"',
                '"note_afterburner_cruise failed"', '"note_boundary failed"',
                '"Boundary read failed"'):
        assert old not in src, old


def test_the_session_summary_lists_handler_failures(tmp_path, caplog):
    t = MissionStatsTracker(version="test", output_dir=str(tmp_path))
    t.finalize(extra={"suppressed_failures": {"note_stall_prevention": 812,
                                              "note_incoming": 1}})
    with caplog.at_level("INFO"):
        t.print_summary()
    assert "Handler failures  : note_incoming 1, note_stall_prevention 812" in caplog.text


def test_a_clean_session_prints_no_failure_line(tmp_path, caplog):
    t = MissionStatsTracker(version="test", output_dir=str(tmp_path))
    t.finalize(extra={"suppressed_failures": {}})
    with caplog.at_level("INFO"):
        t.print_summary()
    assert "Handler failures" not in caplog.text
