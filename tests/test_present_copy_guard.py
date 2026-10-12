"""PresentCopyGuard: one pixel kept on top of the game on the nested display (ADR 099 V3)."""

import time
import unittest.mock as mock

import yaml
from Xlib import X

from wingman.config_schema import schema_default
from wingman.present_copy_guard import PresentCopyGuard

CONFIG_PATH = "wingman/config.yaml"


def _fake_display():
    display = mock.MagicMock()
    screen = display.screen.return_value
    screen.root_depth = 24
    screen.black_pixel = 0
    window = screen.root.create_window.return_value
    return display, screen, window


def _run(guard, seconds=0.08):
    guard.start()
    time.sleep(seconds)
    guard.stop()


def test_it_puts_one_pixel_on_top_of_the_game_and_keeps_it_there():
    display, screen, window = _fake_display()
    opened = []
    guard = PresentCopyGuard(":3", raise_every_s=0.01,
                             open_display=lambda name: (opened.append(name), display)[1])
    _run(guard)
    assert opened == [":3"], "the nested display, by name, not whatever DISPLAY says (Design 009)"
    args, kwargs = screen.root.create_window.call_args
    assert args[:4] == (0, 0, 1, 1), "one pixel in the corner"
    assert args[6] == X.InputOutput, "an input-only window overlaps nothing"
    assert kwargs["override_redirect"] is True, "no focus, no frame, nothing for a window manager"
    assert window.map.called
    raises = [c for c in window.configure.call_args_list if c.kwargs == {"stack_mode": X.Above}]
    assert len(raises) >= 3, "put back on top again and again: the game can restack itself"


def test_a_click_on_the_pixel_goes_to_the_game():
    display, _screen, window = _fake_display()
    _run(PresentCopyGuard(":3", raise_every_s=0.01, open_display=lambda _name: display))
    assert window.shape_rectangles.call_args.args[:2] == (0, 2), "set the INPUT region"
    assert window.shape_rectangles.call_args.args[-1] == [], "to nothing"


def test_stopping_takes_the_pixel_away_and_closes_its_connection():
    display, _screen, window = _fake_display()
    guard = PresentCopyGuard(":3", raise_every_s=0.01, open_display=lambda _name: display)
    _run(guard)
    assert window.destroy.called and display.close.called
    assert guard._thread is None


def test_a_display_that_cannot_be_opened_is_logged_and_does_not_raise(caplog):
    def refuse(_name):
        raise OSError("no such display")
    guard = PresentCopyGuard(":9", open_display=refuse)
    with caplog.at_level("WARNING", logger="wingman.present_copy_guard"):
        _run(guard)
    assert "one frame a second" in caplog.text


def test_no_input_shape_support_still_leaves_the_pixel_up():
    display, _screen, window = _fake_display()
    window.shape_rectangles.side_effect = AttributeError("no shape extension")
    _run(PresentCopyGuard(":3", raise_every_s=0.01, open_display=lambda _name: display))
    assert window.map.called


def test_starting_twice_runs_one_guard():
    display, screen, _window = _fake_display()
    guard = PresentCopyGuard(":3", raise_every_s=0.01, open_display=lambda _name: display)
    guard.start()
    guard.start()
    time.sleep(0.05)
    guard.stop()
    assert screen.root.create_window.call_count == 1


def test_it_ships_switched_off_until_a_covered_window_flight_confirms_it():
    assert schema_default("nested.block_page_flips") is False
    with open(CONFIG_PATH, encoding="utf-8") as f:
        assert yaml.safe_load(f)["nested"]["block_page_flips"] is False
