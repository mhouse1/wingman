"""
Tests for Capture.get_frame() error handling (item 5.4).

Verifies that get_frame() returns None instead of propagating when sct.grab raises,
and that normal operation returns a valid numpy array.
"""

from unittest.mock import MagicMock, patch
import numpy as np
import pytest

from wingman.capture import Capture


@pytest.fixture
def cap():
    region = (0, 0, 100, 100)
    return Capture(region, monitor_index=1)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_get_frame_returns_none_on_grab_exception(cap):
    """get_frame() must return None when sct.grab() raises."""
    with patch.object(cap.sct, "grab", side_effect=Exception("monitor disconnected")):
        result = cap.get_frame()
    assert result is None


def test_get_frame_returns_none_on_monitor_out_of_range():
    """get_frame() must return None when monitor_index is out of range."""
    cap = Capture((0, 0, 100, 100), monitor_index=99)
    result = cap.get_frame()
    assert result is None


def test_get_frame_returns_numpy_array_on_success(cap):
    """get_frame() must return a 3-channel numpy array on a successful grab."""
    # Build a fake BGRA screenshot (mss returns BGRA)
    fake_bgra = np.zeros((100, 100, 4), dtype=np.uint8)
    fake_bgra[:, :, 0] = 42  # B channel

    mock_screenshot = MagicMock()
    mock_screenshot.__array__ = lambda self, dtype=None: fake_bgra

    with patch.object(cap.sct, "grab", return_value=mock_screenshot):
        # Also patch get_monitor_rect to return a known dict
        with patch.object(cap, "get_monitor_rect", return_value={"left": 0, "top": 0, "width": 100, "height": 100}):
            result = cap.get_frame()

    assert result is not None
    assert isinstance(result, np.ndarray)
    assert result.shape == (100, 100, 3), f"Expected (100,100,3), got {result.shape}"
