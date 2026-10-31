"""NestedSizeGuard: the nested screen is set back to nested.size when its window shrinks it (ADR 162)."""

import subprocess
import time
import unittest.mock as mock

import pytest
import yaml

from wingman.config_schema import schema_default
from wingman.nested_size_guard import NestedSizeGuard, parse_size

CONFIG_PATH = "wingman/config.yaml"
QUERY = ("Screen 0: minimum 16 x 16, current 1920 x 1197, maximum 32767 x 32767\n"
         "XWAYLAND0 connected 1920x1197+0+0 508mm x 317mm\n"
         "   5120x2880     59.99 +\n")


def _display(*sizes):
    """A fake display whose root reports each (width, height) in turn, then the last forever."""
    display = mock.MagicMock()
    geometries = [mock.Mock(width=w, height=h) for w, h in sizes]
    display.screen.return_value.root.get_geometry.side_effect = (
        lambda: geometries.pop(0) if len(geometries) > 1 else geometries[0])
    return display


def _xrandr(calls, query=QUERY, set_rc=0, stderr=""):
    def run(cmd, **_kwargs):
        calls.append(cmd)
        if "--query" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=query, stderr="")
        return subprocess.CompletedProcess(cmd, set_rc, stdout="", stderr=stderr)
    return run


def test_a_screen_shorter_than_the_configured_size_is_set_back():
    calls = []
    guard = NestedSizeGuard(":3", "1920x1200", run=_xrandr(calls))
    assert guard.check_once(_display((1920, 1197))) is True
    assert calls == [
        ["xrandr", "--display", ":3", "--query"],
        ["xrandr", "--display", ":3", "--output", "XWAYLAND0", "--mode", "1920x1200"],
    ], "the nested display by name, the output it reports, the configured mode"
    assert guard.restores == 1


def test_a_narrower_screen_is_set_back_too():
    calls = []
    guard = NestedSizeGuard(":3", "1920x1200", run=_xrandr(calls))
    guard.check_once(_display((1900, 1200)))
    assert calls[-1][-2:] == ["--mode", "1920x1200"]


@pytest.mark.parametrize("size", [(1920, 1200), (2560, 1440)])
def test_the_configured_size_or_larger_is_left_alone(size):
    """Larger still holds the capture region, and a -fullscreen window must not be fought."""
    calls = []
    guard = NestedSizeGuard(":3", "1920x1200", run=_xrandr(calls))
    assert guard.check_once(_display(size)) is True
    assert calls == [] and guard.restores == 0


def test_the_output_name_is_looked_up_once():
    calls = []
    guard = NestedSizeGuard(":3", "1920x1200", run=_xrandr(calls))
    guard.check_once(_display((1920, 1197)))
    guard.check_once(_display((1920, 1190)))
    assert [c for c in calls if "--query" in c] == [["xrandr", "--display", ":3", "--query"]]
    assert guard.restores == 2


def test_a_refused_mode_is_reported_and_not_counted(caplog):
    guard = NestedSizeGuard(":3", "1920x1200",
                            run=_xrandr([], set_rc=1, stderr="xrandr: cannot find mode"))
    with caplog.at_level("WARNING", logger="wingman.nested_size_guard"):
        assert guard.check_once(_display((1920, 1197))) is False
    assert "cannot find mode" in caplog.text and "1920x1197" in caplog.text
    assert guard.restores == 0


def test_a_missing_xrandr_is_reported_without_raising(caplog):
    def missing(cmd, **_kwargs):
        raise FileNotFoundError("xrandr")
    guard = NestedSizeGuard(":3", "1920x1200", run=missing)
    with caplog.at_level("WARNING", logger="wingman.nested_size_guard"):
        assert guard.check_once(_display((1920, 1197))) is False
    assert "could not be restored" in caplog.text


def test_no_connected_output_is_reported_without_raising(caplog):
    guard = NestedSizeGuard(":3", "1920x1200", run=_xrandr([], query="Screen 0: nothing\n"))
    with caplog.at_level("WARNING", logger="wingman.nested_size_guard"):
        assert guard.check_once(_display((1920, 1197))) is False
    assert "no connected output" in caplog.text


def test_the_thread_checks_until_stopped_and_closes_its_connection():
    calls = []
    display = _display((1920, 1200), (1920, 1197), (1920, 1200))
    opened = []
    guard = NestedSizeGuard(":3", "1920x1200", check_every_s=0.01, run=_xrandr(calls),
                            open_display=lambda name: (opened.append(name), display)[1])
    guard.start()
    time.sleep(0.1)
    guard.stop()
    assert opened == [":3"]
    assert guard.restores == 1, "restored once, then the screen was the right size again"
    assert display.close.called and guard._thread is None


def test_a_display_that_cannot_be_opened_is_logged_and_does_not_raise(caplog):
    def refuse(_name):
        raise OSError("no such display")
    guard = NestedSizeGuard(":9", "1920x1200", open_display=refuse)
    with caplog.at_level("WARNING", logger="wingman.nested_size_guard"):
        guard.start()
        time.sleep(0.05)
        guard.stop()
    assert "capture fails" in caplog.text


@pytest.mark.parametrize("bad", ["", "1920", "1920x", "big"])
def test_a_malformed_size_is_refused_at_construction(bad):
    with pytest.raises(ValueError):
        parse_size(bad)


def test_the_shipped_config_keeps_the_guard_on():
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        nested = yaml.safe_load(fh)["nested"]
    assert nested["restore_size"] is True
    assert schema_default("nested.restore_size") is True
    assert parse_size(nested["size"]) == (1920, 1200)
