import logging
from types import SimpleNamespace

from wingman.main import _click_through_game_end


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


def test_click_through_game_end_transitions_to_lobby():
    ctrl = _FakeController()
    triggered = []
    analyzer = SimpleNamespace(
        crops={"click_to": object(), "PLAY": object()},
        trigger_event=lambda name: triggered.append(name),
    )

    _click_through_game_end(
        ctrl=ctrl,
        analyzer=analyzer,
        logger=logging.getLogger("test"),
        settle_seconds=0.0,
        sleep_fn=lambda _seconds: None,
    )

    assert len(ctrl.calls) == 2
    assert ctrl.calls[0]["count"] == 7
    assert ctrl.calls[0]["block"] is True
    assert ctrl.calls[0]["region_name"] == "click_to_continue"
    assert ctrl.calls[1]["count"] == 1
    assert ctrl.calls[1]["block"] is True
    assert ctrl.calls[1]["region_name"] == "PLAY"

    assert triggered == ["continue_clicked"]


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
    must fire after the PLAY click has gone out — not on entry, and not merely
    when the state flips."""
    ctrl = _FakeController()
    fired = []
    analyzer = SimpleNamespace(
        crops={"click_to": object(), "PLAY": object()},
        trigger_event=lambda name: fired.append(("trigger", name)),
    )

    _click_through_game_end(
        ctrl=ctrl, analyzer=analyzer, logger=logging.getLogger("test"),
        settle_seconds=0.0, sleep_fn=lambda _s: None,
        on_complete=lambda: fired.append(("complete", len(ctrl.calls))),
    )

    assert ("complete", 2) in fired, "must fire only after both clicks"


def test_on_complete_does_not_fire_when_there_was_no_final_click():
    """The missing-PLAY-crop path returns without clicking. Firing anyway would
    start a settle timed from a click that never happened, and 'z' would exit
    with the game still on the end screen — the bug this callback exists to
    fix, reintroduced silently."""
    ctrl = _FakeController()
    fired = []
    analyzer = SimpleNamespace(
        crops={"click_to": object()},          # no PLAY
        trigger_event=lambda name: fired.append(name),
    )

    _click_through_game_end(
        ctrl=ctrl, analyzer=analyzer, logger=logging.getLogger("test"),
        settle_seconds=0.0, sleep_fn=lambda _s: None,
        on_complete=lambda: fired.append("complete"),
    )

    assert "complete" not in fired
    assert len(ctrl.calls) == 1                # only the centre prompt


def test_a_raising_on_complete_does_not_lose_the_lobby_transition():
    """The callback is operator-supplied bookkeeping. If it throws, the state
    change it is observing must still stand."""
    ctrl = _FakeController()
    triggered = []
    analyzer = SimpleNamespace(
        crops={"click_to": object(), "PLAY": object()},
        trigger_event=lambda name: triggered.append(name),
    )

    def _boom():
        raise RuntimeError("bookkeeping blew up")

    _click_through_game_end(
        ctrl=ctrl, analyzer=analyzer, logger=logging.getLogger("test"),
        settle_seconds=0.0, sleep_fn=lambda _s: None, on_complete=_boom,
    )

    assert "continue_clicked" in triggered
