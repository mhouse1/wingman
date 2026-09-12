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


def _standby_block():
    """The `elif standby_armed:` branch runs to the end of main() — nothing
    else follows it, so slicing from there to EOF isolates it."""
    from pathlib import Path
    src = Path("wingman/main.py").read_text()
    return src.split("elif standby_armed:")[1]


def test_standby_polls_for_the_game_going_away():
    """2026-09-12 AM: game_watch was only ever polled from the main tick loop,
    which STANDBY parks outside of. An operator who exited MetalStorm by hand
    while parked in STANDBY had no path back to a closed nested display short
    of the second Backspace — Ctrl-C left it orphaned too.

    2026-09-12 PM: the first fix called GamePresenceWatch.game_has_gone()
    here, but that method is debounced for the main loop's cadence (only
    actually re-scans every poll_interval_s, and needs absent_reads agreeing
    reads before it reports True) — up to ~10s. A Ctrl-C landing 3.7s into
    STANDBY still read "still running" and the window stayed orphaned. STANDBY
    must use its own prompt, unthrottled check instead."""
    block = _standby_block()
    wait_loop = block.split("while not ctrl.wait_for_close_all")[1].split(
        "except KeyboardInterrupt:")[0]
    assert "game_watch.game_has_gone()" not in wait_loop, \
        "the debounced watch method is too slow for STANDBY's own poll"
    assert "_game_confirmed_gone()" in wait_loop


def test_standby_game_gone_skips_close_game():
    """Same ADR 105 rule as the main-loop teardown: when the game is already
    gone there is nothing running for close_game to protect or kill, so the
    STANDBY game-gone path must close only the nested display."""
    block = _standby_block()
    branch = block.split("if _standby_game_gone:")[1].split("else:")[0]
    assert "close_nested_display(nested_display" in branch
    assert "close_game(" not in branch
    assert "_close_session()" not in branch


def test_standby_confirmed_gone_is_two_reads_not_one():
    """A single scan can still race a crash or a relaunch mid-flight — the
    same gap ADR 105's absent_reads guards against — and a false "gone" would
    yank the nested display out from under a game that is still there. The
    passive wait loop has idle time to spend on a second read, so it should."""
    from pathlib import Path
    src = Path("wingman/main.py").read_text()
    helper = src.split("def _game_confirmed_gone():")[1].split(
        "try:\n                while not ctrl.wait_for_close_all")[0]
    assert helper.count("_game_pids_now()") == 2
    assert "time.sleep" in helper


def test_standby_interrupt_checks_presence_without_a_confirmation_sleep():
    """The except branch reacts to the operator's own Ctrl-C, so it must not
    spend the passive loop's confirmation sleep — a second Ctrl-C landing
    inside that sleep would raise out of this except block uncaught rather
    than being handled by it (observed pattern on 2026-09-08 for the
    second-Backspace race, same shape of hazard here)."""
    block = _standby_block()
    except_branch = block.split("except KeyboardInterrupt:")[1]
    assert "_game_pids_now()" in except_branch
    assert "_game_confirmed_gone()" not in except_branch
    assert "close_nested_display(nested_display" in except_branch
