"""A teardown we asked for must not be logged as a failure (ADR 121).

Closing the nested display kills the XRecord connection the hotkey listener is
blocked on, and the listener could not tell that from a crash. On every session
that tears :3 down — 2026-09-05 22:35, 2026-09-06 04:28 and 05:13 — it logged
ERROR one millisecond after wingman's own "closing Xwayland" line and then
scheduled a reconnect to the display it had just destroyed:

    04:28:13,803 Nested display: closing Xwayland for :3
    04:28:13,804 [ERROR] XKey listener thread died: Display connection closed by server
    04:28:13,804 XKey: reconnecting display in 3s (attempt 1)

The process exits before the reconnect fires, so nothing broke. The cost is that
an ERROR appeared in every clean shutdown, which is what stops ERROR being usable
to notice the listener dying for a reason that matters.
"""

import logging

import pytest

from wingman import game_shutdown, input_linux
from wingman.input_linux import (close_is_expected, expect_display_close,
                                 _reset_expected_closes)


@pytest.fixture(autouse=True)
def clean():
    _reset_expected_closes()
    yield
    _reset_expected_closes()


# --- the declaration itself --------------------------------------------------

def test_a_declared_close_is_expected():
    expect_display_close(":3")
    assert close_is_expected(":3") is True


def test_an_undeclared_display_is_not_expected():
    """The whole point. A blanket "shutting down" flag would mark this True and
    hide a listener death that is still a real failure."""
    expect_display_close(":3")
    assert close_is_expected(":0") is False


def test_nothing_is_expected_before_a_teardown_is_declared():
    assert close_is_expected(":3") is False


def test_whitespace_and_none_are_handled():
    expect_display_close(" :3 ")
    assert close_is_expected(":3") is True
    expect_display_close(None)
    assert close_is_expected(None) is False


# --- the real shutdown path declares it --------------------------------------

def test_closing_the_nested_display_declares_it_first(monkeypatch, caplog):
    """Driven through the REAL close_nested_display, not a re-implementation.

    The ordering is the whole fix: the declaration has to land BEFORE the
    SIGTERM, because the listener wakes on the disconnection the SIGTERM causes.
    """
    seen = {}

    def fake_kill(pid, sig):
        # Sampled at the moment the signal goes out, which is when the listener
        # will wake up and ask.
        seen["expected_at_signal_time"] = close_is_expected(":3")

    monkeypatch.setattr(game_shutdown, "find_nested_display_pids", lambda d: [4242])
    monkeypatch.setattr(game_shutdown.os, "kill", fake_kill)
    monkeypatch.setattr(game_shutdown, "_alive", lambda p: False)

    with caplog.at_level(logging.INFO, logger="wingman.game_shutdown"):
        game_shutdown.close_nested_display(":3")

    assert seen.get("expected_at_signal_time") is True, \
        "the teardown must be declared BEFORE the SIGTERM that triggers it"


def test_a_display_with_no_server_is_not_declared(monkeypatch):
    """Nothing was torn down, so nothing should be excused. Otherwise a later
    genuine failure on that display would be silently downgraded."""
    monkeypatch.setattr(game_shutdown, "find_nested_display_pids", lambda d: [])
    game_shutdown.close_nested_display(":3")
    assert close_is_expected(":3") is False


def test_shutdown_survives_a_broken_declaration(monkeypatch):
    """Instrumentation must never block the shutdown it is describing — ADR 121
    is about shutdowns that hang, and this runs inside one."""
    def boom(_):
        raise RuntimeError("no")
    monkeypatch.setattr(input_linux, "expect_display_close", boom)
    monkeypatch.setattr(game_shutdown, "find_nested_display_pids", lambda d: [4242])
    monkeypatch.setattr(game_shutdown.os, "kill", lambda p, s: None)
    monkeypatch.setattr(game_shutdown, "_alive", lambda p: False)
    r = game_shutdown.close_nested_display(":3")
    assert r["ok"] is True
