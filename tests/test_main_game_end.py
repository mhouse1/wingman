import logging
from types import SimpleNamespace

from wingman.main import GAME_END_MAX_CLICKS, _click_through_game_end


class _FakeController:
    def __init__(self):
        self.calls = []

    def click_crop(self, coords, block=False, count=1, region_name=None):
        self.calls.append(
            {
                "coords": coords,
                "block": block,
                "count": count,
                "region_name": region_name,
            }
        )


def _analyzer(prompt_reads=(), continue_showing=False, events=None):
    """An analyzer whose prompt re-reads return `prompt_reads` in order, then
    False. `events` collects FSM triggers."""
    reads = list(prompt_reads)
    events = [] if events is None else events
    return SimpleNamespace(
        crops={"click_to": object(), "FINAL_CONTINUE": object(), "PLAY": object()},
        trigger_event=lambda name: events.append(name),
        scan_region_for_click_to=lambda _frame: reads.pop(0) if reads else False,
        scan_region_for_final_continue=lambda _frame: continue_showing,
        events=events,
    )


def _run(ctrl, analyzer, grab_frame=lambda: object(), **kw):
    _click_through_game_end(
        ctrl=ctrl, analyzer=analyzer, logger=logging.getLogger("test"),
        settle_seconds=0.0, sleep_fn=lambda _seconds: None, grab_frame=grab_frame, **kw)


def _regions(ctrl):
    return [c["region_name"] for c in ctrl.calls]


def test_click_through_game_end_transitions_to_lobby():
    """A prompt that stays up takes the full count, one blocking click at a
    time, then the CONTINUE button it leaves behind."""
    ctrl = _FakeController()
    analyzer = _analyzer(prompt_reads=[True] * GAME_END_MAX_CLICKS, continue_showing=True)

    _run(ctrl, analyzer)

    assert _regions(ctrl) == ["click_to_continue"] * GAME_END_MAX_CLICKS + ["FINAL_CONTINUE"]
    assert all(c["count"] == 1 and c["block"] is True for c in ctrl.calls)
    assert analyzer.events == ["continue_clicked"]


def test_the_clicking_stops_when_the_prompt_is_gone():
    """2026-10-11 10:19: the lobby came up partway through a 7-click burst.
    There the same point is a party slot's "+", which opens INVITE PLAYERS."""
    ctrl = _FakeController()
    analyzer = _analyzer(prompt_reads=[True, False])

    _run(ctrl, analyzer)

    assert _regions(ctrl) == ["click_to_continue"] * 2
    assert analyzer.events == ["continue_clicked"]


def test_nothing_is_clicked_at_the_play_position():
    """The old final click went to the PLAY position unseen. On INVITE PLAYERS
    that is the bottom row's INVITE button, and it invited NatetheGreat."""
    ctrl = _FakeController()
    _run(ctrl, _analyzer(prompt_reads=[True] * GAME_END_MAX_CLICKS, continue_showing=True))
    _run(ctrl, _analyzer())
    assert "PLAY" not in _regions(ctrl)


def test_continue_is_clicked_only_when_a_frame_shows_it():
    ctrl = _FakeController()
    analyzer = _analyzer(continue_showing=False)

    _run(ctrl, analyzer)

    assert _regions(ctrl) == ["click_to_continue"]
    assert analyzer.events == ["continue_clicked"], \
        "the FSM must still leave GAME_END_B; the lobby scan takes it from there"


def test_every_read_is_of_a_fresh_frame():
    ctrl = _FakeController()
    seen = []
    frames = iter(range(100))
    analyzer = _analyzer()
    analyzer.scan_region_for_click_to = \
        lambda frame, _reads=[True, True, False]: (seen.append(frame), _reads.pop(0))[1]
    analyzer.scan_region_for_final_continue = lambda frame: (seen.append(frame), True)[1]

    _run(ctrl, analyzer, grab_frame=lambda: next(frames))

    assert seen == [0, 1, 2, 3]            # three prompt re-reads, then CONTINUE


def test_without_a_frame_only_the_first_click_goes_out():
    """Nothing can be re-read, so nothing more is clicked on trust."""
    for grab_frame in (None, lambda: None):
        ctrl = _FakeController()
        analyzer = _analyzer(prompt_reads=[True] * GAME_END_MAX_CLICKS, continue_showing=True)

        _run(ctrl, analyzer, grab_frame=grab_frame)

        assert _regions(ctrl) == ["click_to_continue"]
        assert analyzer.events == ["continue_clicked"]


def test_startup_stall_never_shuts_down_the_host():
    """The startup-stall watchdog exits WINGMAN, never the machine.

    It previously ran `shutdown -h now` (`shutdown /s /t 0` on Windows), taking
    the whole host down on any long stall — a game stuck on a login screen, an
    unfilled matchmaking queue — and destroying the session under investigation
    along with everything else on the box. Source-level assertion: the main loop
    must contain no host-shutdown invocation, and must not import subprocess for
    one. A behavioural test would need the full 10-minute loop.
    """
    import inspect
    import wingman.main as main_module

    source = inspect.getsource(main_module)
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )

    for forbidden in ('"/s"', '"-h"', "poweroff", "systemctl"):
        assert forbidden not in code, (
            f"host-shutdown token {forbidden!r} is back in wingman/main.py — "
            "a stall must exit wingman only"
        )
    assert not hasattr(main_module, "subprocess"), (
        "wingman.main imports subprocess again — it was only ever used to power "
        "off the host on a stall"
    )


def test_on_complete_fires_after_the_final_click():
    """ADR 094: the deferred 'z' exit times its settle from this callback, so it
    must fire after the last click has gone out — not on entry, and not merely
    when the state flips."""
    ctrl = _FakeController()
    fired = []
    analyzer = _analyzer(prompt_reads=[True], continue_showing=True)

    _run(ctrl, analyzer, on_complete=lambda: fired.append(("complete", len(ctrl.calls))))

    assert fired == [("complete", 3)], "must fire only after all three clicks"


def test_on_complete_fires_when_continue_was_not_showing():
    """The settle is then timed from the last prompt click. Not firing would
    leave no settle at all, and 'z' would exit the instant the state flipped."""
    ctrl = _FakeController()
    fired = []

    _run(ctrl, _analyzer(), on_complete=lambda: fired.append(("complete", len(ctrl.calls))))

    assert fired == [("complete", 1)]


def test_a_raising_on_complete_does_not_lose_the_lobby_transition():
    """The callback is operator-supplied bookkeeping. If it throws, the state
    change it is observing must still stand."""
    ctrl = _FakeController()
    analyzer = _analyzer()

    def _boom():
        raise RuntimeError("bookkeeping blew up")

    _run(ctrl, analyzer, on_complete=_boom)

    assert "continue_clicked" in analyzer.events
