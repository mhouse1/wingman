"""ADR 133 — a shorter fragment, corroborated by the out-of-bounds void.

The strict gate needs the boundary to arrive as ONE connected component of 0.5R.
The post-2026-09-02 minimap does not reliably draw it that way: measured across
447 blind frames, EVERY rejection was a span rejection and the thickness test
never fired once. The line is present, in the right colour, and discarded for
being in pieces.

The void — the dark, desaturated region outside the arena — is an AREA measure,
so the same fragmentation cannot break it. It corroborates a fragment the span
gate would reject.

Scored against the archived corpus with the REAL analyzer. The positives are
labelled independently of anything under test: every `rtb_*` frame was captured
with the RETURN TO BATTLE banner on screen.
"""

from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml

from wingman.analyzer import GameStateAnalyzer

ROOT = Path(__file__).parent.parent
CORPUS = ROOT / "test_screenshots" / "unknown_anomalies"
CROSSINGS = sorted(CORPUS.glob("rtb_*.png"))
BLIND = sorted(CORPUS.glob("blind_2026090[567]_*.png"))


def _cfg(relaxed=None):
    with open(ROOT / "wingman" / "config.yaml") as fh:
        c = yaml.safe_load(fh)
    if relaxed is not None:
        c["minimap"] = dict(c["minimap"])
        c["minimap"]["boundary_relaxed_span_frac"] = relaxed
    return c


def _analyzer(relaxed=None):
    return GameStateAnalyzer(_cfg(relaxed))


def _detect_rate(a, files):
    hit = 0
    for f in files:
        img = cv2.imread(str(f))
        if img is None:
            continue
        if a.detect_map_boundary(img) is not None:
            hit += 1
    return hit, len(files)


needs_corpus = pytest.mark.skipif(len(CROSSINGS) < 20,
                                  reason="crossing corpus not present")


# --- the recall it was built for ---------------------------------------------

@needs_corpus
def test_it_detects_more_confirmed_crossings_than_the_strict_gate():
    """The positives are independent of the mechanism under test — every frame
    carried the RETURN TO BATTLE banner, so the boundary is certainly there."""
    strict = _analyzer(relaxed=0.0)
    relaxed = _analyzer()
    try:
        s_hit, n = _detect_rate(strict, CROSSINGS)
        r_hit, _ = _detect_rate(relaxed, CROSSINGS)
    finally:
        strict.cleanup()
        relaxed.cleanup()
    assert r_hit > s_hit, f"no improvement: {r_hit} vs {s_hit} of {n}"
    assert r_hit >= 0.90 * n, f"only {r_hit}/{n} crossings detected"


@needs_corpus
def test_it_reads_frames_the_strict_gate_called_blind():
    """The 90%-of-blind-frames-have-a-visible-boundary finding (ADR 117) is what
    this recovers. Every one of these returned None before."""
    a = _analyzer()
    try:
        hit, n = _detect_rate(a, BLIND)
    finally:
        a.cleanup()
    assert hit > 0.20 * n, f"only {hit}/{n} previously-blind frames now read"


# --- the safety property ------------------------------------------------------

@needs_corpus
def test_the_void_corroboration_rejects_no_real_crossing():
    """The whole design rests on this. If the void can be absent on a genuine
    crossing, corroboration would VETO real detections — turning a recall fix
    into a recall regression."""
    a = _analyzer()
    try:
        voids = []
        for f in CROSSINGS:
            img = cv2.imread(str(f))
            if img is None:
                continue
            from wingman.crop_region import get_crop
            crop = get_crop(img, *a.crops["MINIMAP"][:4])
            h, w = crop.shape[:2]
            hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
            voids.append(a.minimap_void_fraction(hsv, w, h, min(w, h) / 2.0))
        v = np.array(voids)
        assert (v > a._boundary_void_min_frac).all(), (
            f"{(v <= a._boundary_void_min_frac).sum()} of {len(v)} real crossings "
            f"would be vetoed; lowest void {v.min():.4f} vs "
            f"threshold {a._boundary_void_min_frac}")
    finally:
        a.cleanup()


@needs_corpus
def test_the_relaxed_path_only_ever_lowers_the_bar():
    """It must never cost a detection that already worked. Asserted frame by
    frame, not on the totals — an aggregate can improve while individual frames
    regress."""
    strict = _analyzer(relaxed=0.0)
    relaxed = _analyzer()
    try:
        lost = []
        for f in CROSSINGS:
            img = cv2.imread(str(f))
            if img is None:
                continue
            if strict.detect_map_boundary(img) is not None \
                    and relaxed.detect_map_boundary(img) is None:
                lost.append(f.name)
    finally:
        strict.cleanup()
        relaxed.cleanup()
    assert not lost, f"relaxed path LOST detections: {lost[:5]}"


def test_a_zero_relaxed_frac_restores_the_strict_gate():
    """The escape hatch. ADR 108 behaviour must remain reachable by one config
    value, so a regression can be bisected without reverting code."""
    a = _analyzer(relaxed=0.0)
    try:
        assert a._boundary_relaxed_span_frac == 0.0
    finally:
        a.cleanup()


# --- the trap the void measure had to avoid ----------------------------------

def test_the_void_ignores_the_compass_rim():
    """A first pass sampled the whole disc and read the dark compass ring as
    void on every frame, which would corroborate everything everywhere. The
    sample radius must stay inside the rim."""
    a = _analyzer()
    try:
        assert a._boundary_void_radius_frac < a._minimap_mask_radius_frac
        h = w = 200
        hsv = np.zeros((h, w, 3), np.uint8)
        hsv[..., 2] = 200          # a uniformly BRIGHT disc: no void anywhere
        assert a.minimap_void_fraction(hsv, w, h, 100.0) == 0.0
    finally:
        a.cleanup()


def test_an_all_dark_frame_reads_as_all_void():
    a = _analyzer()
    try:
        hsv = np.zeros((200, 200, 3), np.uint8)    # V=0, S=0 everywhere
        assert a.minimap_void_fraction(hsv, 200, 200, 100.0) == pytest.approx(1.0)
    finally:
        a.cleanup()
