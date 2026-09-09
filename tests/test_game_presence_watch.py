"""ADR 105: end the session — and the nested display — when the game exits.

2026-09-01: the game servers went into maintenance, MetalStorm exited at about
22:22, and wingman ran a further 4h51m capturing an empty display. Every crop
came back empty, the health streak reached 17,261 s, and nothing noticed,
because the teardown in main only runs for an OPERATOR stop ('z' or Backspace).
On the nested lane the leftover is visible: a black "Xwayland on :3" window with
nothing behind it.
"""

from wingman.game_shutdown import GamePresenceWatch


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def tick(self, seconds=5.0):
        self.now += seconds
        return self.now


def _watch(sequence, clock, **kw):
    """A watch whose scan returns each entry of `sequence` in turn."""
    calls = iter(sequence)

    def finder(_name):
        return [4242] if next(calls) else []

    return GamePresenceWatch(clock=clock, finder=finder, **kw)


def test_a_game_that_never_started_does_not_stop_the_session():
    """A session can start before the client finishes launching. Firing there
    would make wingman unable to start at all."""
    clock = _Clock()
    w = _watch([False] * 6, clock)
    for _ in range(6):
        assert w.game_has_gone() is False
        clock.tick()
    assert not w.armed


def test_the_watch_arms_once_the_game_is_seen():
    clock = _Clock()
    w = _watch([True], clock)
    assert w.game_has_gone() is False
    assert w.armed


def test_a_game_that_exits_ends_the_session():
    clock = _Clock()
    w = _watch([True, False, False], clock)
    assert w.game_has_gone() is False          # seen
    clock.tick()
    assert w.game_has_gone() is False          # one absent read is not enough
    clock.tick()
    assert w.game_has_gone() is True


def test_a_single_missed_scan_does_not_end_the_session():
    """The gap between a crash and a relaunch, and a /proc scan racing process
    teardown, both look like one absent read."""
    clock = _Clock()
    w = _watch([True, False, True, False], clock)
    for expected in (False, False, False, False):
        assert w.game_has_gone() is expected
        clock.tick()


def test_the_scan_is_rate_limited():
    """Called every 1.5 s tick, but a /proc scan per tick is wasted work when
    the answer changes on the scale of a session."""
    clock = _Clock()
    scans = []

    def finder(_name):
        scans.append(clock.now)
        return [1]

    w = GamePresenceWatch(clock=clock, finder=finder, poll_interval_s=5.0)
    for _ in range(10):
        w.game_has_gone()
        clock.tick(1.0)
    assert len(scans) == 2, f"expected 2 scans in 10 s, got {len(scans)}"


def test_a_failing_scan_never_takes_the_main_loop_down():
    clock = _Clock()

    def finder(_name):
        raise OSError("proc unreadable")

    w = GamePresenceWatch(clock=clock, finder=finder)
    assert w.game_has_gone() is False


def test_the_teardown_closes_the_display_without_the_close_game_gate():
    """close_game protects a RUNNING game from being killed. When the game has
    already gone there is nothing for it to protect, and honouring it would
    leave exactly the black window this ADR exists to remove."""
    from pathlib import Path
    src = Path("wingman/main.py").read_text()
    branch = src.split("if game_gone_exit:")[1].split("elif not _close_enabled:")[0]
    assert "close_nested_display(nested_display" in branch
    assert "_close_enabled" not in branch, \
        "the game-gone teardown must not be gated on close_game"


def test_the_game_gone_exit_needs_no_safe_point():
    """The guards wait for a lobby so they never abandon an aircraft in flight.
    There is no aircraft when there is no game."""
    from pathlib import Path
    src = Path("wingman/main.py").read_text()
    check = src.split("if game_watch.game_has_gone():")[1].split("break")[0]
    assert "_safe" not in check
