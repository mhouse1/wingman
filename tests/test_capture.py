"""Capture.get_frame() error handling and mss backend geometry.

get_frame() must swallow a failing grab and return None rather than propagate:
a monitor that disconnects mid-session, or a display that goes away, must cost
one frame and not the run.

These tests drive a fake mss so they are hermetic. The previous version built a
real Capture and reached for `cap.sct`, which the backend refactor moved to
`_backend._sct`; on a Wayland host the constructor also selected the PipeWire
backend and tried a real portal ScreenCast handshake. Neither failure was
visible, because the file was in no Makefile target (ADR 100 follow-up).
"""

from unittest.mock import patch

import numpy as np
import pytest

import wingman.capture as capture_module
from wingman.capture import Capture

MON_1 = {"left": 1920, "top": 0, "width": 2560, "height": 1440}
ALL_MONITORS = [{"left": 0, "top": 0, "width": 4480, "height": 1440}, MON_1]

REGION = (10, 20, 100, 100)


class _FakeMss:
    """Stands in for an mss context. Records what it was asked for."""

    def __init__(self, display=None, monitors=None, grab_error=None):
        self.display = display
        self.monitors = monitors if monitors is not None else ALL_MONITORS
        self.grab_error = grab_error
        self.grabbed = []

    def grab(self, rect):
        self.grabbed.append(rect)
        if self.grab_error is not None:
            raise self.grab_error
        shot = np.zeros((rect["height"], rect["width"], 4), dtype=np.uint8)
        shot[:, :, 0] = 42          # B channel; mss hands back BGRA
        return shot

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


@pytest.fixture
def fake_mss(monkeypatch):
    """Install a fake mss and force the X11 backend regardless of host session."""
    made = []

    def factory(display=None, **_kw):
        inst = _FakeMss(display=display)
        made.append(inst)
        return inst

    monkeypatch.setattr(capture_module, "mss", factory)
    monkeypatch.setattr(capture_module, "_is_wayland", lambda: False)
    return made


def _cap(monitor_index=1, **kw):
    return Capture(REGION, monitor_index=monitor_index, **kw)


# --- get_frame never propagates ----------------------------------------------

def test_get_frame_returns_none_on_grab_exception(fake_mss):
    cap = _cap()
    fake_mss[0].grab_error = Exception("monitor disconnected")
    assert cap.get_frame() is None


def test_get_frame_returns_none_on_monitor_out_of_range(fake_mss):
    """The rect is computed before the grab, so an out-of-range index raises
    inside get_frame — it must be caught there like any other failure."""
    assert _cap(monitor_index=99).get_frame() is None


def test_get_frame_returns_bgr_array_on_success(fake_mss):
    frame = _cap().get_frame()
    assert isinstance(frame, np.ndarray)
    assert frame.shape == (100, 100, 3), "alpha channel must be dropped"
    assert frame[0, 0, 0] == 42


def test_a_failed_grab_does_not_advance_the_frame_clock(fake_mss):
    """seconds_since_last_frame() feeds the stale-capture injection guard. A
    failed grab that refreshed the timestamp would report a healthy pipeline
    and let keys be injected into whatever window is actually focused."""
    cap = _cap()
    assert cap.seconds_since_last_frame() is None
    fake_mss[0].grab_error = Exception("display lost")
    cap.get_frame()
    assert cap.seconds_since_last_frame() is None


# --- geometry ----------------------------------------------------------------

def test_monitor_rect_is_the_region_offset_into_the_chosen_monitor(fake_mss):
    assert _cap().get_monitor_rect() == {
        "left": MON_1["left"] + REGION[0],
        "top": MON_1["top"] + REGION[1],
        "width": REGION[2],
        "height": REGION[3],
    }


def test_game_screen_offset_is_the_rect_origin_on_mss(fake_mss):
    """The click paths resolve absolute coordinates through this property. It
    used to consult only the PipeWire backend, so on X11 every click failed
    with "game window offset not known yet" — the frames come from the monitor
    rect, so that rect's origin is the answer, not a guess."""
    assert _cap().game_screen_offset == (MON_1["left"] + REGION[0],
                                         MON_1["top"] + REGION[1])


def test_a_configured_offset_wins_over_the_derived_one(fake_mss):
    assert _cap(game_window_offset=(7, 9)).game_screen_offset == (7, 9)


# --- ADR 099: an explicit display is an X11 grab whatever the host says -------

def test_an_explicit_display_selects_mss_even_on_wayland(monkeypatch):
    made = []

    def factory(display=None, **_kw):
        made.append(display)
        return _FakeMss(display=display)

    monkeypatch.setattr(capture_module, "mss", factory)
    monkeypatch.setattr(capture_module, "_is_wayland", lambda: True)
    with patch.object(capture_module, "_PipeWireBackend") as pipewire:
        cap = Capture(REGION, monitor_index=1, display=":3")
    pipewire.assert_not_called()
    assert made == [":3"], "the nested display must reach mss, not just DISPLAY"
    assert cap.get_frame().shape == (100, 100, 3)
