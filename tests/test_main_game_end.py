import logging
from types import SimpleNamespace

from wingman.main import GAME_END_MAX_CLICKS, _click_through_game_end
from wingman.round_performance import MAX_READS, PanelRead, RoundPerformance


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


# -- the PERFORMANCE panel (operator, 2026-10-26) ------------------------------------

def test_the_panel_is_read_after_the_first_click_and_handed_on():
    """The first click brings the panel up; it is read before the second."""
    ctrl = _FakeController()
    analyzer = _analyzer(prompt_reads=[True, False])
    reading = RoundPerformance(163, 1, 2, 0)
    reads, handed = [], []

    def _read(frame):
        reads.append(len(ctrl.calls))
        return PanelRead(reading, "1/2/0", "163")

    _run(ctrl, analyzer, read_performance=_read, on_performance=handed.append)

    assert reads == [1, 1], "two frames read alike, both between the first two clicks"
    assert handed == [reading]
    assert _regions(ctrl) == ["click_to_continue"] * 2


def test_a_panel_not_read_is_handed_on_as_none():
    ctrl = _FakeController()
    handed = []

    _run(ctrl, _analyzer(prompt_reads=[True, False]),
         read_performance=lambda _frame: None, on_performance=handed.append)

    assert handed == [None]
    assert _regions(ctrl) == ["click_to_continue"] * 2


def test_without_a_reader_nothing_is_read():
    ctrl = _FakeController()
    handed = []

    _run(ctrl, _analyzer(), on_performance=handed.append)

    assert handed == []


def test_a_failing_read_does_not_stop_the_clicking():
    ctrl = _FakeController()
    analyzer = _analyzer(prompt_reads=[True, False])
    handed = []

    def _boom(_frame):
        raise RuntimeError("OCR pool gone")

    _run(ctrl, analyzer, read_performance=_boom, on_performance=handed.append)

    assert handed == [None]
    assert _regions(ctrl) == ["click_to_continue"] * 2
    assert analyzer.events == ["continue_clicked"]


# -- AI-005-1: an unread panel, looked for on later pages and kept ---------------------

_READ = PanelRead(RoundPerformance(163, 1, 2, 0), "1/2/0", "163")
_NO_KDA = PanelRead(None, "")


def _reader(ctrl, first, later):
    """Answers `first` for the first click's frames and `later` after it, noting
    how many clicks had gone out at each read."""
    at = []

    def _read(_frame):
        at.append(len(ctrl.calls))
        return first if len(ctrl.calls) == 1 else later
    return _read, at


def test_an_unread_panel_is_looked_for_on_each_later_page_in_shadow(caplog):
    ctrl = _FakeController()
    read, at = _reader(ctrl, first=_NO_KDA, later=_READ)
    handed = []

    with caplog.at_level(logging.INFO):
        _run(ctrl, _analyzer(prompt_reads=[True, True, False]),
             read_performance=read, on_performance=handed.append)

    assert at == [1] * MAX_READS + [2, 3], "every frame after click 1, then pages 2 and 3"
    assert handed == [None], "shadow: a later page is never booked"
    assert f"ROUND PERFORMANCE: not read — no KDA on any of {MAX_READS} frames" in caplog.text
    assert "ROUND PERFORMANCE: shadow — page 2 shows a KDA: kda '1/2/0' score '163'" \
        in caplog.text
    assert _regions(ctrl) == ["click_to_continue"] * 3


def test_a_read_panel_is_not_looked_for_again():
    ctrl = _FakeController()
    read, at = _reader(ctrl, first=_READ, later=_READ)

    _run(ctrl, _analyzer(prompt_reads=[True, True, False]), read_performance=read)

    assert at == [1, 1]


def test_the_frames_read_are_kept_with_what_was_read():
    """A read round keeps its first frame; an unread one every frame of the
    first page and each later page it was looked for on."""
    names = []

    def keep(frame, name):
        names.append(name)

    ctrl = _FakeController()
    read, _ = _reader(ctrl, first=_READ, later=_READ)
    _run(ctrl, _analyzer(prompt_reads=[True, False]), read_performance=read, save_frame=keep)
    assert [n.split("_", 2)[2] for n in names] == ["p1_f1_read-163-1-2-0"]

    names.clear()
    ctrl = _FakeController()
    read, _ = _reader(ctrl, first=_NO_KDA, later=_NO_KDA)
    _run(ctrl, _analyzer(prompt_reads=[True, True, False]), read_performance=read,
         save_frame=keep)
    assert [n.split("_", 2)[2] for n in names] == [
        f"p1_f{k}_nokda" for k in range(1, MAX_READS + 1)] + ["p2_nokda", "p3_nokda"]
    assert len({n.split("_p")[0] for n in names}) == 1, "one stamp for the round"


# -- AI-005-3: one reading per round -----------------------------------------------

def test_no_round_waiting_reads_nothing(caplog):
    """A second click-through of the same end screens, started by ADR 087's lobby
    scan: nothing is read, booked or saved, and no ROUND PERFORMANCE line goes out."""
    ctrl = _FakeController()
    read, at = _reader(ctrl, first=_NO_KDA, later=_READ)
    handed, names = [], []

    with caplog.at_level(logging.INFO):
        _run(ctrl, _analyzer(prompt_reads=[True, True, False]), read_performance=read,
             on_performance=handed.append, save_frame=lambda _f, n: names.append(n),
             round_awaits=lambda: False)

    assert (at, handed, names) == ([], [], [])
    assert "ROUND PERFORMANCE" not in caplog.text
    assert "no round waits for its PERFORMANCE panel" in caplog.text
    assert _regions(ctrl) == ["click_to_continue"] * 3, "the clicking is unchanged"


def test_a_round_waiting_is_read():
    ctrl = _FakeController()
    read, at = _reader(ctrl, first=_READ, later=_READ)
    handed = []

    _run(ctrl, _analyzer(prompt_reads=[True, False]), read_performance=read,
         on_performance=handed.append, round_awaits=lambda: True)

    assert (at, handed) == ([1, 1], [_READ.reading])


def test_a_read_round_keeps_the_frame_it_was_read_on():
    """2026-10-29: frame 1 was still the podium page in 5 of 10 rounds, so
    keeping frame 1 kept no check on half the readings."""
    ctrl = _FakeController()
    answers = iter([_NO_KDA, _READ, _READ])
    names = []

    _run(ctrl, _analyzer(prompt_reads=[True, False]), read_performance=lambda _f: next(answers),
         save_frame=lambda _f, n: names.append(n))

    assert [n.split("_", 2)[2] for n in names] == ["p1_f2_read-163-1-2-0"]


def test_a_podium_that_outlasts_three_frames_still_leaves_the_panel_to_read():
    """2026-10-29 10:59: the MVP page was up for frames 1 to 3 and the panel came
    on frame 4, the last of four; with no second panel frame the round went unread."""
    ctrl = _FakeController()
    answers = iter([_NO_KDA, _NO_KDA, _NO_KDA, _READ, _READ])
    handed, names = [], []

    _run(ctrl, _analyzer(prompt_reads=[True, False]), read_performance=lambda _f: next(answers),
         on_performance=handed.append, save_frame=lambda _f, n: names.append(n))

    assert handed == [_READ.reading]
    assert [n.split("_", 2)[2] for n in names] == ["p1_f4_read-163-1-2-0"]
