"""Action item 001, Cycle 7 (2026-09-24): the one-line PURSUIT SUMMARY /
DIVE SUMMARY. Pure tests of `_EngagementTally`; the loops that emit it are
covered in test_pursuit_mode.py and test_eject_heatdive.py."""

import re

from wingman.controller import _EngagementTally


class _Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def _tally():
    clock = _Clock()
    return _EngagementTally(clock=clock), clock


def test_counts_scans_locked_scans_and_time_to_first_lock():
    tally, clock = _tally()
    tally.scan(False, 6)
    clock.t += 3.5
    tally.scan(True, 6)
    clock.t += 1.0
    tally.scan(True, 5)
    tally.scan(False, 5)
    assert (tally.scans, tally.locked) == (4, 2)
    assert tally.first_lock_s == 3.5, "first lock stays at the first locked scan"


def test_line_reports_every_field():
    tally, clock = _tally()
    tally.scan(False, 6)
    clock.t += 8.5
    tally.scan(True, 4)
    clock.t += 11.5
    tally.scan(False, 2)
    line = tally.line("PURSUIT", "cap", switched=False)
    assert line == ("PURSUIT SUMMARY: end=cap dur=20.0s scans=3 locked=1 (33%) "
                    "first_lock=8.5s ammo=6->2 switched=no")


def test_no_lock_and_unreadable_ammo_render_as_dashes():
    tally, _ = _tally()
    tally.scan(False, None)
    tally.scan(False, None)
    line = tally.line("DIVE", "dive-end", switched=True)
    assert "locked=0 (0%)" in line
    assert "first_lock=-" in line
    assert "ammo=-" in line
    assert line.startswith("DIVE SUMMARY: end=dive-end")
    assert line.endswith("switched=yes")


def test_an_empty_engagement_does_not_divide_by_zero():
    tally, _ = _tally()
    assert "scans=0 locked=0 (0%)" in tally.line("PURSUIT", "external:manual", switched=False)


def test_ammo_first_is_the_first_readable_value_and_last_is_the_latest():
    tally, _ = _tally()
    tally.scan(False, None)     # unreadable at the start
    tally.scan(False, 6)
    tally.scan(False, None)     # a dropout must not erase the last good value
    tally.scan(False, 3)
    assert re.search(r"ammo=6->3\b", tally.line("PURSUIT", "cap", switched=False))
