"""ADR 146 (2026-09-24): the generic close-button finder and the GAME_UNKNOWN
recovery policy around it.

Motivation: a full-screen "A-10 Thunderbolt, limited time offer" window covered
the lobby at start-up, matched no known popup, and left the classifier in
GAME_UNKNOWN for three minutes. ESC is not a safe blind key (on a plain lobby it
opens "Exit to Desktop" with Exit highlighted), so the recovery clicks the
window's own white-cross close button instead.
"""

from pathlib import Path

import cv2
import numpy as np
import pytest

from wingman import close_button
from wingman.close_button import (
    CloseHit, GenericCloseRecovery, _SPRITE, click_region, find_close_button,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "a10_promo_close_region.png"
_CROSS_IN_FIXTURE = (120, 94)       # where the cross sits inside the 200 x 180 crop
_SCREENSHOTS = Path(__file__).resolve().parent.parent / "test_screenshots"


def _black(w=1920, h=1200):
    return np.zeros((h, w, 3), dtype=np.uint8)


def _paste_sprite(frame, cx, cy, scale):
    """Draw the close cross at (cx, cy), `scale` times its 29 px source size."""
    size = max(3, int(round(29 * scale)))
    patch = cv2.resize(_SPRITE, (size, size), interpolation=cv2.INTER_NEAREST) > 0.5
    x0, y0 = cx - size // 2, cy - size // 2
    frame[y0:y0 + size, x0:x0 + size][patch] = (255, 255, 255)
    return frame


def _near(hit, x, y, tol=4):
    return hit is not None and abs(hit.x - x) <= tol and abs(hit.y - y) <= tol


# --- the finder, on synthetic frames -----------------------------------------

@pytest.mark.parametrize("scale", [1.0, 1.3, 1.55])
def test_finds_the_cross_at_every_size_the_game_draws(scale):
    """29, 38 and 45 px are the sizes measured on the A-10 window, another
    player's profile screen and the flight-pass window."""
    hit = find_close_button(_paste_sprite(_black(), 1500, 300, scale))
    assert _near(hit, 1500, 300), hit
    assert hit.score >= 0.9


def test_finds_it_over_a_busy_photographic_background():
    rng = np.random.default_rng(3)
    frame = rng.integers(20, 200, size=(1200, 1920, 3), dtype=np.uint8)   # noise, never white
    hit = find_close_button(_paste_sprite(frame, 900, 640, 1.3))
    assert _near(hit, 900, 640), hit


def test_an_upright_plus_is_not_a_close_button():
    frame = _black()
    frame[280:320, 1493:1507] = 255        # vertical bar
    frame[293:307, 1480:1520] = 255        # horizontal bar
    assert find_close_button(frame) is None


def test_plain_white_shapes_are_not_close_buttons():
    frame = _black()
    frame[100:220, 300:900] = 255          # a big white banner
    frame[500:540, 1000:1040] = 255        # a white square
    cv2.circle(frame, (1400, 700), 30, (255, 255, 255), -1)
    assert find_close_button(frame) is None


def test_no_white_at_all_is_not_a_close_button():
    assert find_close_button(_black()) is None


def test_bad_input_returns_none():
    assert find_close_button(None) is None
    assert find_close_button(np.zeros((1200, 1920), dtype=np.uint8)) is None      # not BGR
    assert find_close_button(np.zeros((100, 100, 3), dtype=np.uint8)) is None     # too small


def test_threshold_is_honoured():
    frame = _paste_sprite(_black(), 700, 500, 1.3)
    assert find_close_button(frame, min_score=0.5) is not None
    assert find_close_button(frame, min_score=1.01) is None


# --- the real motivating case -------------------------------------------------

def test_the_a10_promo_close_button_is_found():
    """The window that stalled start-up for three minutes. The fixture is the
    200 x 180 region around its close button (the cross sits at (120, 94) in it),
    pasted onto an otherwise empty frame at its real position (1700, 254)."""
    patch = cv2.imread(str(_FIXTURE))
    assert patch is not None, "fixture missing"
    frame = _black()
    frame[160:160 + patch.shape[0], 1580:1580 + patch.shape[1]] = patch
    hit = find_close_button(frame)
    assert _near(hit, 1700, 254), hit
    assert hit.score >= 0.82


# --- real archived frames (untracked corpus: skip when absent) -----------------

def _real(name):
    path = _SCREENSHOTS / name
    if not path.exists():
        pytest.skip(f"{path} not present (untracked test corpus, ADR 100 D7)")
    return cv2.imread(str(path))


def test_flight_pass_window_close_button_is_found():
    hit = find_close_button(_real("NEW_FLIGHT_PASS.png"))
    assert _near(hit, 1809, 195), hit
    assert hit.score >= 0.9


def test_other_players_profile_screen_close_button_is_found():
    """STALL_PROFILE.png: a profile opened from a list, which (unlike your own
    profile page) carries a close cross beside its back chevron."""
    hit = find_close_button(_real("STALL_PROFILE.png"))
    assert _near(hit, 1844, 52), hit


def test_exit_to_desktop_dialog_is_not_mistaken_for_one():
    """The best false match measured over 3,258 frames (0.747, on the 'x' of
    'Exit'). Clicking here would land on the dialog, not close anything."""
    assert find_close_button(_real("STALL_EXIT_TO_DESKTOP.png")) is None


def test_a_plain_lobby_has_no_close_button():
    assert find_close_button(_real("integration_test/P1_000_LOBBY_PLAY.png")) is None


# --- the recovery policy --------------------------------------------------------

class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


_HIT = CloseHit(1700, 254, 0.93, 1.0)


def _recovery(monkeypatch, hit=_HIT, **cfg):
    clock = _Clock()
    calls = []

    def fake(frame, min_score=0.82, scales=None):
        calls.append(min_score)
        return hit

    monkeypatch.setattr(close_button, "find_close_button", fake)
    base = {"enabled": True, "min_stuck_s": 25.0, "retry_interval_s": 15.0, "max_clicks": 3}
    base.update(cfg)
    return GenericCloseRecovery(base, clock=clock), clock, calls


_FRAME = object()


def test_disabled_by_default(monkeypatch):
    rec, clock, calls = _recovery(monkeypatch)
    rec = GenericCloseRecovery({}, clock=clock)
    rec.tick(_FRAME, True)
    clock.t += 500
    assert rec.tick(_FRAME, True) is None
    assert calls == []


def test_nothing_happens_before_the_state_has_been_unknown_long_enough(monkeypatch):
    rec, clock, calls = _recovery(monkeypatch)
    assert rec.tick(_FRAME, True) is None           # arms the clock
    clock.t += 24.0
    assert rec.tick(_FRAME, True) is None
    assert calls == [], "no search inside min_stuck_s"


def test_clicks_once_the_state_has_been_unknown_long_enough(monkeypatch):
    rec, clock, calls = _recovery(monkeypatch)
    rec.tick(_FRAME, True)
    clock.t += 25.0
    assert rec.tick(_FRAME, True) == _HIT
    assert len(calls) == 1


def test_searches_at_most_once_per_retry_interval(monkeypatch):
    rec, clock, calls = _recovery(monkeypatch)
    rec.tick(_FRAME, True)
    clock.t += 25.0
    assert rec.tick(_FRAME, True) == _HIT
    clock.t += 5.0
    assert rec.tick(_FRAME, True) is None
    clock.t += 10.0
    assert rec.tick(_FRAME, True) == _HIT
    assert len(calls) == 2


def test_stops_after_max_clicks(monkeypatch):
    rec, clock, calls = _recovery(monkeypatch, max_clicks=2)
    rec.tick(_FRAME, True)
    clock.t += 25.0
    clicks = 0
    for _ in range(10):
        if rec.tick(_FRAME, True) is not None:
            clicks += 1
        clock.t += 15.0
    assert clicks == 2


def test_keeps_searching_when_nothing_is_found_without_using_up_clicks(monkeypatch):
    rec, clock, calls = _recovery(monkeypatch, hit=None, max_clicks=1)
    rec.tick(_FRAME, True)
    clock.t += 25.0
    for _ in range(4):
        assert rec.tick(_FRAME, True) is None
        clock.t += 15.0
    assert len(calls) == 4, "a miss retries every interval"
    monkeypatch.setattr(close_button, "find_close_button", lambda *a, **k: _HIT)
    assert rec.tick(_FRAME, True) == _HIT, "the click budget was never spent on misses"


def test_leaving_the_unknown_state_ends_the_episode(monkeypatch):
    rec, clock, calls = _recovery(monkeypatch, max_clicks=1)
    rec.tick(_FRAME, True)
    clock.t += 25.0
    assert rec.tick(_FRAME, True) == _HIT
    assert rec.tick(_FRAME, False) is None            # classified again: episode over
    rec.tick(_FRAME, True)                            # a new episode arms its own clock
    clock.t += 24.0
    assert rec.tick(_FRAME, True) is None
    clock.t += 1.0
    assert rec.tick(_FRAME, True) == _HIT, "the click budget was refilled"


def test_the_threshold_from_config_is_passed_through(monkeypatch):
    rec, clock, calls = _recovery(monkeypatch, min_score=0.9)
    rec.tick(_FRAME, True)
    clock.t += 25.0
    rec.tick(_FRAME, True)
    assert calls == [0.9]


# --- turning a hit into a click ---------------------------------------------------

def test_click_region_is_centred_on_the_hit_in_frame_fractions():
    x1, y1, x2, y2 = click_region(CloseHit(1700, 254, 0.93, 1.0), (1200, 1920, 3))
    assert (x1 + x2) / 2 * 1920 == pytest.approx(1700, abs=0.5)
    assert (y1 + y2) / 2 * 1200 == pytest.approx(254, abs=0.5)
    assert 0.0 <= x1 < x2 <= 1.0 and 0.0 <= y1 < y2 <= 1.0


def test_click_region_is_clamped_at_the_frame_edge():
    x1, y1, x2, y2 = click_region(CloseHit(5, 3, 0.9, 1.0), (1200, 1920, 3))
    assert (x1, y1) == (0.0, 0.0)
    x1, y1, x2, y2 = click_region(CloseHit(1915, 1198, 0.9, 1.0), (1200, 1920, 3))
    assert (x2, y2) == (1.0, 1.0)
