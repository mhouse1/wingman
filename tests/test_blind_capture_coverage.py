"""Blind-frame capture must sample the whole session, and only real minimaps.

ADR 117 opened this capture to answer "what does blindness look like across maps
and situations". The 2026-09-05 session showed the budget could not: all 40
frames landed between 22:00 and 22:51 of a 4h 36m run — one map — after which 158
blind ticks hit the cap. And 11 of the 40 (27.5%) had no minimap drawn at all:
killcam and transition frames, which are in GAME_BATTLE but carry no HUD.

Ground truth here is the archived corpus, not a fake. A fake built from the
caller's assumptions would reproduce whatever I believed about these frames,
which is the failure mode that put 11 useless frames in the corpus to begin with.
"""

import collections
from pathlib import Path

import cv2
import pytest
import yaml

from wingman.analyzer import GameStateAnalyzer
from wingman.tick_handlers import BehaviorTreeHandler

ROOT = Path(__file__).parent.parent
CORPUS = ROOT / "test_screenshots" / "unknown_anomalies"
# Enumerated, never globbed. A pattern over a directory the running app writes
# into absorbs whatever the next session drops there — the mistake that broke a
# corpus test the day it was written.
NO_MINIMAP = ["blind_20260905_220822_7.png", "blind_20260905_221500_12.png",
              "blind_20260905_222526_19.png", "blind_20260905_223558_27.png"]
WITH_MINIMAP = ["blind_20260905_220722_6.png", "blind_20260905_221239_10.png",
                "blind_20260905_223440_26.png", "rtb_20260905_230545_crossing1.png"]


def _cfg():
    with open(ROOT / "wingman" / "config.yaml") as fh:
        return yaml.safe_load(fh)


@pytest.fixture(scope="module")
def analyzer():
    a = GameStateAnalyzer(_cfg())
    try:
        yield a
    finally:
        a.cleanup()


def _frame(name):
    p = CORPUS / name
    if not p.exists():
        pytest.skip(f"{name} not archived")
    img = cv2.imread(str(p))
    if img is None:
        pytest.skip(f"{name} unreadable")
    return img


# --- is a minimap drawn at all? ---------------------------------------------

@pytest.mark.parametrize("name", NO_MINIMAP)
def test_a_frame_with_no_minimap_is_rejected(analyzer, name):
    """Killcam and transition frames are in GAME_BATTLE with no HUD. They cost
    27.5% of the 2026-09-05 budget and can answer nothing."""
    assert analyzer.minimap_present(_frame(name)) is False


@pytest.mark.parametrize("name", WITH_MINIMAP)
def test_a_frame_with_a_minimap_is_accepted(analyzer, name):
    """Including frames where the detector read NOTHING — a minimap showing no
    boundary is precisely the evidence this capture wants, and rejecting it
    would defeat the whole diagnostic."""
    assert analyzer.minimap_present(_frame(name)) is True


def test_the_threshold_sits_in_the_measured_gap(analyzer):
    """No-minimap frames held 0-5 matching pixels, real minimaps 558+. A
    threshold near either edge would be tuning; this one is in open space."""
    assert 5 < analyzer._minimap_present_min_px < 558


def test_an_unreadable_frame_fails_OPEN(analyzer):
    """A frame we cannot classify is still worth capturing. Failing closed would
    silently switch off the capture that exists to explain a detector nobody can
    otherwise see failing."""
    assert analyzer.minimap_present(object()) is True


# --- the capture gate --------------------------------------------------------

class _AnalyzerStub:
    def __init__(self, present):
        self.present = present
        self.calls = 0

    def minimap_present(self, frame):
        self.calls += 1
        return self.present


def _handler(present=True, interval=300.0, cap=120):
    h = BehaviorTreeHandler.__new__(BehaviorTreeHandler)
    h._analyzer = _AnalyzerStub(present)
    h._boundary_recent = collections.deque(maxlen=3)
    h._blind_capture_max = cap
    h._blind_capture_interval_s = interval
    h._blind_capture_next_ts = 0.0
    h._blind_no_minimap_skips = 0
    h._rtb_capture_max = 5
    h._approach_capture_max = 5
    h._captures = {}
    h._rtb_capture_dir = "/nonexistent-on-purpose"
    return h


def test_a_frame_with_a_minimap_is_captured():
    h = _handler(present=True)
    assert h._maybe_capture_blind(object(), 100.0, None, True, False) is True


def test_a_frame_with_no_minimap_is_skipped():
    h = _handler(present=False)
    assert h._maybe_capture_blind(object(), 100.0, None, True, False) is False
    assert h._blind_no_minimap_skips == 1


def test_a_skip_does_not_spend_the_interval():
    """The heart of it. If a killcam frame advanced the timer, a five-minute
    interval would be burned on a frame carrying no information, and the real
    minimap arriving a second later would wait out the whole interval."""
    h = _handler(present=False)
    h._maybe_capture_blind(object(), 100.0, None, True, False)
    assert h._blind_capture_next_ts == 0.0

    h._analyzer.present = True
    assert h._maybe_capture_blind(object(), 100.0, None, True, False) is True
    assert h._blind_capture_next_ts == pytest.approx(400.0)


def test_a_capture_does_spend_the_interval():
    h = _handler(present=True, interval=300.0)
    h._maybe_capture_blind(object(), 100.0, None, True, False)
    assert h._maybe_capture_blind(object(), 399.0, None, True, False) is False
    assert h._maybe_capture_blind(object(), 400.0, None, True, False) is True


@pytest.mark.parametrize("kw", [
    {"boundary_raw": (0.5, 0.1, 0.0)},      # the detector read something
    {"in_battle": False},                    # no minimap outside battle
    {"is_respawning": True},                 # ADR 117 D4
])
def test_the_existing_gates_still_apply(kw):
    args = dict(boundary_raw=None, in_battle=True, is_respawning=False)
    args.update(kw)
    h = _handler(present=True)
    assert h._maybe_capture_blind(object(), 100.0, **args) is False
    assert h._analyzer.calls == 0, "minimap_present should not be reached"


# --- coverage arithmetic -----------------------------------------------------

def test_the_budget_spans_a_long_session():
    """The defect this fixes was arithmetic, not logic: 40 frames at 45 s is 30
    minutes. Asserted so a future edit that shrinks coverage fails here rather
    than in a session nobody re-reads."""
    m = _cfg()["minimap"]
    hours = m["blind_capture_max"] * m["blind_capture_interval_s"] / 3600.0
    assert hours >= 8.0, f"blind capture only spans {hours:.1f}h"
