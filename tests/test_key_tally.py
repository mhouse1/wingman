"""Cycle 9 (2026-09-24): the hotkey listener's per-display tally.

Synthetic `z` and `v` presses to the nested display were ignored twice in one
day and the log could not say whether the event never arrived or arrived and was
dropped. `_KeyTally` is the liveness signal: wingman's own injected keys are
recorded on the injection display too, so a live listener reports a non-zero
`seen` and a deaf one reports zero. Pure counting; nothing reads it but the log.
"""

import re

from wingman import input_linux
from wingman.input_linux import _KeyTally


class _Clock:
    def __init__(self):
        self.t = 500.0

    def __call__(self):
        return self.t


def test_counts_seen_matched_and_delivered():
    tally = _KeyTally(":3", clock=_Clock())
    tally.note(False, False)     # a key nobody registered (wingman's own 'f', say)
    tally.note(True, False)      # a registered key the delivery filter dropped
    tally.note(True, True)       # a registered key that fired its hotkey
    tally.note(False, False)
    line = tally.report()
    assert "4 KeyPress events" in line
    assert "2 matched a registered key" in line
    assert "1 delivered" in line


def test_report_names_the_display_and_the_interval():
    clock = _Clock()
    tally = _KeyTally(":3", clock=clock)
    clock.t += 60.0
    line = tally.report()
    assert line.startswith("XKey[:3]:")
    assert "in the last 60s" in line


def test_report_starts_a_fresh_interval():
    clock = _Clock()
    tally = _KeyTally(":0", clock=clock)
    tally.note(True, True)
    clock.t += 60.0
    tally.report()
    clock.t += 30.0
    line = tally.report()
    assert "0 KeyPress events in the last 30s" in line
    assert "0 matched" in line and "0 delivered" in line


def test_a_silent_listener_reports_zero_not_nothing():
    """The point of the whole thing: a deaf listener must still say so."""
    tally = _KeyTally(":3", clock=_Clock())
    assert re.search(r"\b0 KeyPress events\b", tally.report())


def test_the_report_interval_is_a_sane_positive_number():
    assert 10.0 <= input_linux._TALLY_INTERVAL_S <= 600.0
