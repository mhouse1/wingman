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
import numpy as np
import pytest
import yaml

from wingman.analyzer import GameStateAnalyzer
from wingman.tick_handlers import BoundaryPerceptionHandler

ROOT = Path(__file__).parent.parent
CORPUS = ROOT / "test_screenshots" / "unknown_anomalies"
# Enumerated, never globbed. A pattern over a directory the running app writes
# into absorbs whatever the next session drops there — the mistake that broke a
# corpus test the day it was written.
NO_MINIMAP = ["blind_20260905_220822_7.png", "blind_20260905_221500_12.png",
              "blind_20260905_222526_19.png", "blind_20260905_223558_27.png"]
WITH_MINIMAP = ["blind_20260905_220722_6.png", "blind_20260905_221239_10.png",
                "blind_20260905_223440_26.png", "rtb_20260905_230545_crossing1.png"]
# ADR 117 D8/D9, operator review 2026-09-20: a minimap is drawn (passes
# minimap_present) but carries no real boundary line — visually confirmed
# by inspection, and by visualizing the matched pixels directly: they sit
# either on the compass rim's decorative band or scattered across brown/dirt
# terrain (which shares the boundary hue), never forming anything thin
# enough to be a plausible line fragment.
NO_BOUNDARY_LINE = ["blind_20260920_161546_1.png", "blind_20260920_162157_2.png",
                    "blind_20260920_162742_3.png"]


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


# --- ADR 117 D9: a minimap with no boundary line is not worth capturing -----

@pytest.mark.parametrize("name", NO_BOUNDARY_LINE)
def test_a_minimap_with_no_boundary_line_is_still_present(analyzer, name):
    """minimap_present() alone can't tell these apart from a fragmented-but-
    real line — both pass its low (50 px) bar. That distinction needs the
    shape-aware check, verified separately below."""
    assert analyzer.minimap_present(_frame(name)) is True


@pytest.mark.parametrize("name", NO_BOUNDARY_LINE)
def test_a_minimap_with_no_boundary_line_has_no_thin_component(analyzer, name):
    """Real evidence, not a threshold picked in the abstract: three actual
    blind captures. A first cut (D8) used a raw pixel-count floor and
    called these "below the real-line range" — but visualizing the matched
    pixels directly (same day) found a raw count is not reliable evidence
    either way: rocky/dirt terrain shares the boundary hue and can produce
    hundreds to thousands of matching pixels with no line present. The
    shape check (is anything actually THIN, not just present) is what
    correctly rejects all three."""
    frame = _frame(name)
    assert analyzer.detect_map_boundary(frame) is None   # still "blind" today
    assert analyzer.get_last_boundary_had_thin_component() is False


# --- ADR 117 D10: a flight-path trail is not a boundary line ---------------
#
# D9 alone (radial exclusion + elongation) still fired on these live, the
# same night: an aircraft's movement trail is thin, elongated, and not near
# the rim, same as a real fragmented line. Visualizing the matched pixels
# found a distinct trail squiggle in each. D10's arc-curvature check is what
# correctly rejects them.
TRAIL_NOT_BOUNDARY = ["blind_20260920_192204_1.png", "blind_20260920_192713_2.png",
                     "blind_20260920_193227_3.png"]
# Confirmed detections (detect_map_boundary returned non-None) from the same
# night — the positive control: D10 must not reject what D9 already
# correctly accepted.
CONFIRMED_BOUNDARY = ["approach_20260920_192949_1.png", "approach_20260920_193512_2.png"]


@pytest.mark.parametrize("name", TRAIL_NOT_BOUNDARY)
def test_a_flight_trail_has_no_thin_component(analyzer, name):
    frame = _frame(name)
    analyzer.detect_map_boundary(frame)
    assert analyzer.get_last_boundary_had_thin_component() is False


@pytest.mark.parametrize("name", CONFIRMED_BOUNDARY)
def test_a_confirmed_boundary_still_has_a_thin_component(analyzer, name):
    """The positive control for D10 — it must not have fixed the trail
    false-positive by breaking real detection."""
    frame = _frame(name)
    assert analyzer.detect_map_boundary(frame) is not None
    assert analyzer.get_last_boundary_had_thin_component() is True


# --- ADR 117 D11: a circular UI marker's rim is not a boundary line --------
#
# D10's arc-residual check alone still fired on this live: a fragment of a
# leader/MVP crown badge's yellow ring is a genuine circular arc — it fits a
# circle just as tightly as a real line, because it IS one. Confirmed by
# cropping tightly around the flagged component: the highlighted pixels sit
# exactly on the badge's own rim. D11's minimum fitted-radius check is what
# correctly rejects it — the badge's rim fits a ~9 px circle, nowhere near
# the 52-287 px real boundary-line range measured the same night.
ICON_RIM_NOT_BOUNDARY = ["blind_20260920_220226_2.png"]


@pytest.mark.parametrize("name", ICON_RIM_NOT_BOUNDARY)
def test_a_marker_icon_rim_has_no_thin_component(analyzer, name):
    frame = _frame(name)
    analyzer.detect_map_boundary(frame)
    assert analyzer.get_last_boundary_had_thin_component() is False


# --- ADR 117 D9: synthetic geometry for the shape-aware signal --------------
#
# detect_map_boundary otherwise only has archived-corpus coverage, skipped
# whenever that corpus isn't present (as it isn't here — see NO_MINIMAP/
# WITH_MINIMAP above). A thin line and a thick blob, both in the exact
# boundary hue, are the two cases the shape check exists to tell apart.

def _hsv_swatch_bgr(h=18, s=180, v=200):
    hsv = np.uint8([[[h, s, v]]])
    return tuple(int(c) for c in cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)[0][0])


def _synthetic_frame(analyzer, draw_fn):
    """A 1920x1200 frame, neutral gray except for the MINIMAP crop, which
    draw_fn paints into (crop-local pixel coordinates). Gray, not black —
    an all-black background reads as the out-of-bounds VOID (ADR 133), which
    corroborates a short span and can make detect_map_boundary accept
    something these tests don't intend to test."""
    frame = np.zeros((1200, 1920, 3), dtype=np.uint8)
    x1, y1, x2, y2 = analyzer.crops["MINIMAP"][:4]
    h, w = frame.shape[:2]
    px1, py1 = int(w * x1), int(h * y1)
    px2, py2 = int(w * x2), int(h * y2)
    crop = frame[py1:py2, px1:px2]
    crop[:] = (90, 90, 90)
    draw_fn(crop)
    return frame


def _draw_arc(crop, color, radius, angle_span_deg, thickness=2):
    """A genuine circular arc of the given radius — mimics a real boundary
    line, which ADR 117 D10 measured as a small, gently-curved arc of a much
    larger circle (fitted radius 52-287 px on a ~316 px crop; the center of
    curvature sits 0.4-2.2 crop-radii away, off-crop). Placed so the arc's
    apex sits near the crop's own center."""
    h, w = crop.shape[:2]
    cx, cy = w / 2.0, h / 2.0 + radius - 20
    angles = np.linspace(-angle_span_deg / 2, angle_span_deg / 2, 300)
    pts = []
    for ang in angles:
        theta = np.radians(90 + ang)
        pts.append((int(round(cx + radius * np.cos(theta))),
                    int(round(cy - radius * np.sin(theta)))))
    cv2.polylines(crop, [np.array(pts, dtype=np.int32).reshape(-1, 1, 2)],
                  False, color, thickness)


def _draw_small_circle_arc(crop, color, radius=20, angle_span_deg=140, thickness=1):
    """A small circle's rim — mimics a circular UI marker icon (a leader/MVP
    badge, ADR 117 D11's live-confirmed confound). Fits a circle just as
    tightly as a real line (residual/span ~0.009, well under the D10
    threshold) — it IS one — but the fitted circle itself is tiny (~20 px,
    vs. 52-287 px measured for real boundary lines), which is the only thing
    D11's minimum-radius check needs to tell them apart."""
    _draw_arc(crop, color, radius, angle_span_deg, thickness)


def _draw_zigzag(crop, color, thickness=2):
    """A sinuous, multiple-direction-change path — mimics an aircraft
    flight-path trail (ADR 117 D10's live-confirmed confound). Thin,
    elongated (4:1), and centered, same as a real short line fragment would
    be — only its curvature differs."""
    h, w = crop.shape[:2]
    pts = [(w // 2 - 35, h // 2 + 8), (w // 2 - 20, h // 2 - 8),
           (w // 2 - 5, h // 2 + 8), (w // 2 + 10, h // 2 - 8),
           (w // 2 + 25, h // 2 + 8), (w // 2 + 38, h // 2 - 6)]
    cv2.polylines(crop, [np.array(pts, dtype=np.int32).reshape(-1, 1, 2)],
                  False, color, thickness)


class TestThinComponentShapeCheck:
    def test_a_real_arc_sets_had_thin_component(self, analyzer):
        """A long enough arc is formally detected too, not just flagged as
        'worth a look' — the shape check must not reject the thing it
        exists to recognize."""
        color = _hsv_swatch_bgr()
        frame = _synthetic_frame(
            analyzer, lambda crop: _draw_arc(crop, color, radius=300, angle_span_deg=60))
        assert analyzer.detect_map_boundary(frame) is not None
        assert analyzer.get_last_boundary_had_thin_component() is True

    def test_a_thick_blob_does_not_set_had_thin_component(self, analyzer):
        """The terrain case: plenty of matching pixels, none of them thin."""
        color = _hsv_swatch_bgr()

        def draw(crop):
            h, w = crop.shape[:2]
            cv2.circle(crop, (w // 2, h // 2), min(w, h) // 4, color, -1)

        frame = _synthetic_frame(analyzer, draw)
        analyzer.detect_map_boundary(frame)
        assert analyzer.get_last_boundary_had_thin_component() is False

    def test_nothing_drawn_reads_no_thin_component(self, analyzer):
        frame = _synthetic_frame(analyzer, lambda crop: None)
        assert analyzer.detect_map_boundary(frame) is None
        assert analyzer.get_last_boundary_had_thin_component() is False

    def test_a_thin_but_too_short_arc_still_sets_had_thin_component(self, analyzer):
        """The exact ADR 108 fragmentation case: thin and curved enough to
        be a real line, too short to pass the span gate and be formally
        detected — this is what D9 exists to keep as 'worth a look', unlike
        a raw pixel-count floor which cannot see the difference between
        this and a solid terrain blob of the same total size."""
        color = _hsv_swatch_bgr()
        frame = _synthetic_frame(
            analyzer, lambda crop: _draw_arc(crop, color, radius=300, angle_span_deg=13))
        assert analyzer.detect_map_boundary(frame) is None   # too short to formally detect
        assert analyzer.get_last_boundary_had_thin_component() is True

    def test_a_sinuous_trail_does_not_set_had_thin_component(self, analyzer):
        """ADR 117 D10: live-confirmed confound, 2026-09-20 — a flight-path
        trail passes every other gate (thin, area 352 >= 20, elongation 4.0
        >= 1.8, radial fraction 0.01) and is excluded only by this check:
        its points do not fit any single circle well (residual/span 0.136,
        vs. 0.003-0.006 measured on real confirmed boundary detections the
        same night)."""
        color = _hsv_swatch_bgr()
        frame = _synthetic_frame(analyzer, lambda crop: _draw_zigzag(crop, color))
        analyzer.detect_map_boundary(frame)
        assert analyzer.get_last_boundary_had_thin_component() is False

    def test_a_small_circle_arc_does_not_set_had_thin_component(self, analyzer):
        """ADR 117 D11: live-confirmed confound, 2026-09-20/21 — a fragment
        of a circular UI marker's rim passes every gate through D10 (thin,
        area 63 >= 20, elongation 2.79 >= 1.8, radial fraction 0.09, and a
        clean arc fit — residual/span 0.009, comfortably under D10's 0.03).
        It is excluded only by D11: the fitted circle itself is tiny (~20 px
        radius), nowhere near the 52-287 px range measured for real
        boundary lines."""
        color = _hsv_swatch_bgr()
        frame = _synthetic_frame(analyzer, lambda crop: _draw_small_circle_arc(crop, color))
        analyzer.detect_map_boundary(frame)
        assert analyzer.get_last_boundary_had_thin_component() is False


# --- the capture gate --------------------------------------------------------

class _AnalyzerStub:
    def __init__(self, present, had_thin_component=True):
        self.present = present
        self.had_thin_component = had_thin_component
        self.calls = 0

    def minimap_present(self, frame):
        self.calls += 1
        return self.present

    def get_last_boundary_had_thin_component(self):
        return self.had_thin_component


def _handler(present=True, interval=300.0, cap=120, had_thin_component=True):
    h = BoundaryPerceptionHandler.__new__(BoundaryPerceptionHandler)
    h._analyzer = _AnalyzerStub(present, had_thin_component)
    h._boundary_recent = collections.deque(maxlen=3)
    h._blind_capture_max = cap
    h._blind_capture_interval_s = interval
    h._blind_capture_next_ts = 0.0
    h._blind_no_minimap_skips = 0
    h._blind_no_boundary_line_skips = 0
    h._rtb_capture_max = 5
    h._approach_capture_max = 5
    h._captures = {}
    h._rtb_capture_dir = "/nonexistent-on-purpose"
    return h


def test_a_frame_with_a_minimap_is_captured():
    h = _handler(present=True)
    assert h.maybe_capture_blind(object(), 100.0, None, True, False) is True


def test_a_frame_with_no_minimap_is_skipped():
    h = _handler(present=False)
    assert h.maybe_capture_blind(object(), 100.0, None, True, False) is False
    assert h._blind_no_minimap_skips == 1


def test_a_skip_does_not_spend_the_interval():
    """The heart of it. If a killcam frame advanced the timer, a five-minute
    interval would be burned on a frame carrying no information, and the real
    minimap arriving a second later would wait out the whole interval."""
    h = _handler(present=False)
    h.maybe_capture_blind(object(), 100.0, None, True, False)
    assert h._blind_capture_next_ts == 0.0

    h._analyzer.present = True
    assert h.maybe_capture_blind(object(), 100.0, None, True, False) is True
    assert h._blind_capture_next_ts == pytest.approx(400.0)


def test_a_capture_does_spend_the_interval():
    h = _handler(present=True, interval=300.0)
    h.maybe_capture_blind(object(), 100.0, None, True, False)
    assert h.maybe_capture_blind(object(), 399.0, None, True, False) is False
    assert h.maybe_capture_blind(object(), 400.0, None, True, False) is True


@pytest.mark.parametrize("kw", [
    {"boundary_raw": (0.5, 0.1, 0.0)},      # the detector read something
    {"in_battle": False},                    # no minimap outside battle
    {"is_respawning": True},                 # ADR 117 D4
])
def test_the_existing_gates_still_apply(kw):
    args = dict(boundary_raw=None, in_battle=True, is_respawning=False)
    args.update(kw)
    h = _handler(present=True)
    assert h.maybe_capture_blind(object(), 100.0, **args) is False
    assert h._analyzer.calls == 0, "minimap_present should not be reached"


# --- ADR 117 D9: skip when nothing on the minimap is thin enough to be a line

def test_a_minimap_with_no_thin_component_is_skipped():
    h = _handler(present=True, had_thin_component=False)
    assert h.maybe_capture_blind(object(), 100.0, None, True, False) is False
    assert h._blind_no_boundary_line_skips == 1


def test_a_minimap_with_a_thin_component_is_captured():
    h = _handler(present=True, had_thin_component=True)
    assert h.maybe_capture_blind(object(), 100.0, None, True, False) is True


def test_a_no_thin_component_skip_does_not_spend_the_interval():
    """Same reasoning as the no-minimap skip: a tick with nothing to see must
    not burn the interval a genuine fragmented-line miss would need."""
    h = _handler(present=True, had_thin_component=False)
    h.maybe_capture_blind(object(), 100.0, None, True, False)
    assert h._blind_capture_next_ts == 0.0

    h._analyzer.had_thin_component = True
    assert h.maybe_capture_blind(object(), 100.0, None, True, False) is True
    assert h._blind_capture_next_ts == pytest.approx(400.0)


# --- coverage arithmetic -----------------------------------------------------

def test_the_budget_spans_a_long_session():
    """The defect this fixes was arithmetic, not logic: 40 frames at 45 s is 30
    minutes. Asserted so a future edit that shrinks coverage fails here rather
    than in a session nobody re-reads."""
    m = _cfg()["minimap"]
    hours = m["blind_capture_max"] * m["blind_capture_interval_s"] / 3600.0
    assert hours >= 8.0, f"blind capture only spans {hours:.1f}h"
