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


# --- Deaf-listener watchdog (2026-09-26, SAF-001) ----------------------------
# 3 of 21 sessions that day had the :3 listener hear 0 presses in every minute
# from the start, and an ENTER typed into the game window was not heard. The
# tally now compares what it heard with what wingman itself pressed there.

import pytest


@pytest.fixture
def fresh_counts(monkeypatch):
    monkeypatch.setattr(input_linux, "_injected_press_counts", {})


def test_heard_nothing_while_wingman_pressed_keys_is_deaf(fresh_counts):
    tally = _KeyTally(":3", clock=_Clock())
    for _ in range(6):
        input_linux._note_injected_press(":3")
    line = tally.report()
    assert tally.deaf is True
    assert "wingman pressed 6" in line


def test_a_listener_that_heard_something_is_not_deaf(fresh_counts):
    tally = _KeyTally(":3", clock=_Clock())
    for _ in range(6):
        input_linux._note_injected_press(":3")
    tally.note(False, False)
    tally.report()
    assert tally.deaf is False


def test_too_few_presses_to_judge_is_not_deaf(fresh_counts):
    """In the lobby wingman presses few keys; silence there proves nothing."""
    tally = _KeyTally(":3", clock=_Clock())
    for _ in range(input_linux._DEAF_MIN_INJECTED - 1):
        input_linux._note_injected_press(":3")
    tally.report()
    assert tally.deaf is False


def test_presses_on_another_display_do_not_count(fresh_counts):
    tally = _KeyTally(":0", clock=_Clock())
    for _ in range(20):
        input_linux._note_injected_press(":3")
    tally.report()
    assert tally.deaf is False


def test_each_interval_is_judged_on_its_own_presses(fresh_counts):
    tally = _KeyTally(":3", clock=_Clock())
    for _ in range(6):
        input_linux._note_injected_press(":3")
    tally.note(False, False)
    tally.report()                       # heard something: fine
    line = tally.report()                # next interval: no presses at all
    assert tally.deaf is False and "wingman pressed 0" in line
