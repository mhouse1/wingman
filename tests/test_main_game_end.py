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
