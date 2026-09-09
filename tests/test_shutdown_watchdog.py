"""Cleanup must not be able to hang silently. ADR 121.

Observed 2026-09-05 08:02:03: wingman took SIGTERM, stopped logging on the same
second, never wrote "Exit requested, shutting down", and was still alive five
minutes later. It had to be SIGKILLed, so no session summary and no stats JSON
were written, and the next start rotated its log away. The overnight 01:10
session left exactly that signature — no archive, no stats, nothing to review.
"""

import logging
import threading
import time
import unittest.mock as mock

from wingman.main import _arm_shutdown_watchdog


def test_a_healthy_shutdown_is_not_forced():
    """The timer must not fire when cleanup finishes normally — a watchdog that
    kills healthy exits is worse than the hang it guards."""
    with mock.patch("os._exit") as exit_:
        _arm_shutdown_watchdog(timeout_s=30.0)
        time.sleep(0.2)
        exit_.assert_not_called()


def test_a_stalled_shutdown_is_forced():
    with mock.patch("os._exit") as exit_:
        _arm_shutdown_watchdog(timeout_s=0.1)
        time.sleep(0.6)
        exit_.assert_called_once_with(2)


def test_the_stall_dumps_thread_stacks_before_exiting():
    """The dump is the whole point: the live hang left NO record of where it
    stuck, which is why it could not be diagnosed."""
    with mock.patch("os._exit"), \
         mock.patch("faulthandler.dump_traceback") as dump:
        _arm_shutdown_watchdog(timeout_s=0.1)
        time.sleep(0.6)
        assert dump.called, "forced exit without recording where it hung"
        assert dump.call_args.kwargs.get("all_threads") is True, \
            "dumped one thread — the hang is in whichever thread is stuck"


def test_the_timer_is_a_daemon():
    """It must not itself keep a healthy interpreter alive."""
    with mock.patch("os._exit"):
        _arm_shutdown_watchdog(timeout_s=30.0)
    timers = [t for t in threading.enumerate() if isinstance(t, threading.Timer)]
    assert timers, "no watchdog timer was armed"
    assert all(t.daemon for t in timers), "a non-daemon watchdog blocks exit"
    for t in timers:
        t.cancel()


def test_the_dump_goes_to_the_log_file_not_only_stderr(tmp_path):
    """An unattended soak has no terminal. If the dump only reached stderr it
    would be lost exactly when it matters."""
    log_file = tmp_path / "w.log"
    handler = logging.FileHandler(log_file, encoding="utf-8")
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        with mock.patch("os._exit"), \
             mock.patch("faulthandler.dump_traceback") as dump:
            _arm_shutdown_watchdog(timeout_s=0.1)
            time.sleep(0.6)
        streams = [c.kwargs.get("file") for c in dump.call_args_list]
        assert any(getattr(s, "name", None) == str(log_file) for s in streams), \
            "the dump never reached the log file"
    finally:
        root.removeHandler(handler)
        handler.close()


def test_the_watchdog_can_be_cancelled():
    """ADR 124. STANDBY is an unbounded wait BY DESIGN — the operator flies for
    as long as they like — and it sits inside the same `finally` block the
    watchdog is armed in. On 2026-09-05 10:45:17 the watchdog fired on a live
    standby and force-exited it, taking the SAF-010 handback with it."""
    from wingman.main import _cancel_shutdown_watchdog
    with mock.patch("os._exit") as exit_:
        _arm_shutdown_watchdog(timeout_s=0.1)
        _cancel_shutdown_watchdog()
        time.sleep(0.6)
        exit_.assert_not_called()


def test_cancelling_twice_is_harmless():
    from wingman.main import _cancel_shutdown_watchdog
    _cancel_shutdown_watchdog()
    _cancel_shutdown_watchdog()


def test_arming_replaces_a_previous_watchdog():
    """The standby path re-arms for the bounded close that follows the second
    Backspace. A second timer must not leave the first one running."""
    from wingman.main import _cancel_shutdown_watchdog
    with mock.patch("os._exit"):
        _arm_shutdown_watchdog(timeout_s=30.0)
        first = _watchdog_timers()
        _arm_shutdown_watchdog(timeout_s=30.0)
        second = _watchdog_timers()
    assert len(first) == 1 and len(second) == 1, \
        f"timers leaked: {len(first)} then {len(second)}"
    _cancel_shutdown_watchdog()


def _watchdog_timers():
    return [t for t in threading.enumerate()
            if isinstance(t, threading.Timer) and t.is_alive()]
