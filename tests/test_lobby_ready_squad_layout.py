"""ADR 102 rev 2: the READY crop must sit on the squad lobby's READY button.

2026-09-25 09:16:41. Three lobby dwells of 127 to 194 s (09:16, 09:23, 09:28)
ran with a two-player party on screen, "0 / 2 READY" over a big READY button,
while the quick-scan logged:

    Lobby quick-scan: no lobby crops detected (stalled 10.5s)
    Click-to OCR re-enabled: GAME_LOBBY blackout - the forced state may be wrong

The operator pressed 'm' to click the button by hand each time. The screenshot
they captured mid-stall (tests/fixtures/lobby_ready_squad_br.png is its
bottom-right corner, at the offset below) shows a fully visible lobby.

Measured on that frame with the real OCR path, before this change:

    READY    px=[1534, 1038, 1741, 1093] -> detected=False
    PLAY     px=[1668, 1064, 1834, 1128] -> detected=False (it reads READY)
    UNREADY  px=[1490, 1041, 1785, 1088] -> detected=False (nothing to read)
    a crop over the whole button         -> detected=True, text='READY'

The READY crop dates from 2026-04-11. ADR 072 recalibrated the crops after the
August UI update from the integration-test captures, and tests/calibration_map
has no READY or UNREADY entry, so nothing ever refreshed them: the crop covers
only the button's upper-left corner (it starts 72 px left of the button and
ends 53% of the way across and 42% of the way down).

'UNREADY' contains 'READY', so widening the crop over the button has a second
edge: after the click the button reads UNREADY and would match again. The
GAME_LOBBY scan must read that as "already ready", not click a second time.
"""

from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml

from constants import CONFIG_PATH
from wingman.analyzer import _lobby_crop_verdict, _process_crop_region

FIXTURE = Path("tests/fixtures/lobby_ready_squad_br.png")
FIXTURE_OFFSET = (1360, 960)          # x, y of the fixture inside the 1920x1200 frame
FRAME_W, FRAME_H = 1920, 1200
SOLO_LOBBY = Path("test_screenshots/integration_test/P1_000_LOBBY_PLAY.png")


def _cfg():
    with Path(CONFIG_PATH).open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _frame():
    """The fixture pasted onto a black frame, so fractional crops land where
    they land in production."""
    tile = cv2.imread(str(FIXTURE))
    assert tile is not None, f"{FIXTURE} missing"
    frame = np.zeros((FRAME_H, FRAME_W, 3), np.uint8)
    x0, y0 = FIXTURE_OFFSET
    frame[y0:y0 + tile.shape[0], x0:x0 + tile.shape[1]] = tile
    return frame


def _crop_px(name):
    c = _cfg()["crops"][name]["coords"]
    return (round(c[0][0] * FRAME_W), round(c[0][1] * FRAME_H),
            round(c[1][0] * FRAME_W), round(c[1][1] * FRAME_H))


def _button_bbox(frame, region=(1450, 1000, FRAME_W, FRAME_H), thr=235):
    """The READY button is the largest near-white blob in the corner."""
    x0, y0, x1, y1 = region
    mask = (frame[y0:y1, x0:x1].min(axis=2) >= thr).astype(np.uint8)
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    best = max(range(1, n), key=lambda i: stats[i, cv2.CC_STAT_AREA])
    x, y, w, h, _ = stats[best]
    return (x0 + int(x), y0 + int(y), x0 + int(x + w), y0 + int(y + h))


def _overlap(a, b):
    """Fraction of b's width and height that a covers."""
    ow = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    oh = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    return ow / (b[2] - b[0]), oh / (b[3] - b[1])


# --- calibration geometry (fast; no OCR) ---------------------------------------

def test_the_fixture_shows_the_squad_ready_button():
    box = _button_bbox(_frame())
    w, h = box[2] - box[0], box[3] - box[1]
    assert 240 <= w <= 270 and 70 <= h <= 85, box   # 257 x 78 as measured


def test_the_ready_crop_covers_the_button_it_is_meant_to_read():
    button = _button_bbox(_frame())
    frac_w, frac_h = _overlap(_crop_px("READY"), button)
    assert frac_w >= 0.9 and frac_h >= 0.9, (
        f"READY crop {_crop_px('READY')} covers {frac_w:.0%} x {frac_h:.0%} of the "
        f"button {button}; the letters are cut off, so OCR never reads them")


def test_the_ready_crop_stays_on_the_button():
    """A crop that spills far outside reads the party panel and the mode text."""
    button = _button_bbox(_frame())
    cx0, cy0, cx1, cy1 = _crop_px("READY")
    assert cx0 >= button[0] - 12 and cy0 >= button[1] - 12
    assert cx1 <= button[2] + 12 and cy1 <= button[3] + 12


def test_the_click_lands_on_the_button():
    """Clicks go to the crop centre (the 'Clicking PLAY at (1751, 1096)' line)."""
    button = _button_bbox(_frame())
    x0, y0, x1, y1 = _crop_px("READY")
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    margin_x = (button[2] - button[0]) * 0.2
    margin_y = (button[3] - button[1]) * 0.2
    assert button[0] + margin_x <= cx <= button[2] - margin_x
    assert button[1] + margin_y <= cy <= button[3] - margin_y


# --- READY versus UNREADY (fast) -----------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("READY", "READY"),
    ("»READY", "READY"),
    ("READY01", "READY"),
    ("UNREADY", "UNREADY"),
    ("»UNREADY", "UNREADY"),
    (None, "READY"),
])
def test_a_ready_crop_that_reads_unready_is_the_unready_state(text, expected):
    """The button toggles READY <-> UNREADY. Reading UNREADY through the READY
    crop means this player already clicked; clicking again would un-ready them."""
    assert _lobby_crop_verdict("READY", text) == expected


def test_only_the_ready_crop_is_reinterpreted():
    assert _lobby_crop_verdict("PLAY", "PLAY") == "PLAY"
    assert _lobby_crop_verdict("PLAY", "UNREADY") == "PLAY"
    assert _lobby_crop_verdict("UNREADY", "UNREADY") == "UNREADY"


# --- the real OCR path (slow; `make ocr`) -------------------------------------

@pytest.mark.slow
def test_ready_crop_reads_ready_on_the_squad_lobby():
    tokens = _cfg()["crops"]["READY"].get("text") or []
    assert tokens
    detected, _, text = _process_crop_region(_frame(), _cfg_coords("READY"), tokens)
    assert detected and "READY" in (text or ""), f"OCR read {text!r}"


@pytest.mark.slow
def test_ready_crop_does_not_fire_on_the_solo_lobby():
    """The PLAY button occupies the same corner; READY must not match it."""
    if not SOLO_LOBBY.exists():
        pytest.skip(f"{SOLO_LOBBY} not present")
    frame = cv2.imread(str(SOLO_LOBBY))
    if frame is None or not np.any(frame):
        pytest.skip(f"{SOLO_LOBBY} unreadable or an all-black placeholder")
    tokens = _cfg()["crops"]["READY"].get("text") or []
    detected, _, text = _process_crop_region(frame, _cfg_coords("READY"), tokens)
    assert not detected, f"READY crop fired on the solo lobby (OCR read {text!r})"


def _cfg_coords(name):
    c = _cfg()["crops"][name]["coords"]
    return (c[0][0], c[0][1], c[1][0], c[1][1])
