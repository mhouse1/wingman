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
    analyzer = SimpleNamespace(
        crops={"click_to": object(), "ready_button": object()},
        _game_end_b=True,
        _game_lobby=False,
    )

    _click_through_game_end(
        ctrl=ctrl,
        analyzer=analyzer,
        logger=logging.getLogger("test"),
        settle_seconds=0.0,
        sleep_fn=lambda _seconds: None,
    )

    assert len(ctrl.calls) == 2
    assert ctrl.calls[0]["count"] == 5
    assert ctrl.calls[0]["block"] is True
    assert ctrl.calls[0]["region_name"] == "click_to_continue"
    assert ctrl.calls[1]["count"] == 1
    assert ctrl.calls[1]["block"] is True
    assert ctrl.calls[1]["region_name"] == "ready_button"

    assert analyzer._game_end_b is False
    assert analyzer._game_lobby is True
