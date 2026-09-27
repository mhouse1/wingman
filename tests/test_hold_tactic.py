"""CR-018-10 Phase B: the held-key tactic lifecycle, written once."""

import threading
import time

import pytest

from wingman.hold_tactic import HoldTactic


def test_the_running_flag_is_set_before_the_thread_exists():
    """The ADR 070 d8 pattern: a second trigger in the same tick sees it."""
    t = HoldTactic("test")
    release = threading.Event()
    assert t.start(lambda: release.wait(2.0)) is True
    assert t.is_running()
    assert t.start(lambda: None) is False, "a second start must not spawn a rival"
    release.set()
    assert t.join(2.0)
    assert not t.is_running()


def test_start_clears_a_stop_left_by_the_previous_run():
    t = HoldTactic("test")
    t.stop("takeover")
    seen = []
    t.start(lambda: seen.append(t.stop_event.is_set()))
    t.join(2.0)
    assert seen == [False]


def test_stop_ends_a_body_that_waits_on_it():
    t = HoldTactic("test")
    t.start(lambda: t.stop_event.wait(10.0))
    t0 = time.time()
    t.stop("shutdown")
    assert t.join(2.0) and time.time() - t0 < 1.0


# The body raises on purpose; the thread's traceback is the expected outcome.
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_the_running_flag_clears_even_when_the_body_raises():
    t = HoldTactic("test")

    def _boom():
        raise RuntimeError("hold failed")

    t.start(_boom)
    t.join(2.0)
    assert not t.is_running()


def test_it_wraps_the_events_a_tactic_already_has():
    """So tactics move onto it one at a time without renaming what they share."""
    running, stop = threading.Event(), threading.Event()
    t = HoldTactic("test", running=running, stop=stop)
    release = threading.Event()
    t.start(lambda: release.wait(2.0))
    assert running.is_set()
    t.stop()
    assert stop.is_set()
    release.set()
    t.join(2.0)
