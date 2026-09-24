"""Screen-space target tracking for MetalStorm J-20 combat.

Detects enemy target-marker bars in the HUD via HSV contour filtering,
maintains a scan-mode state machine (ACQUIRING → TRACKING → LOST_GRACE),
and returns a per-frame observation dict consumed by the main loop.

Call update(frame) once per main-loop tick while in GAME_BATTLE.
Call reset() when leaving GAME_BATTLE.
Pass observation["error_norm"] to Controller.orient_nose_to_target().
"""

import logging
import time
from enum import Enum, auto

import cv2
import numpy as np

logger = logging.getLogger(__name__)


class TrackMode(Enum):
    SEARCHING = auto()    # not in battle / between missions
    ACQUIRING = auto()    # scanning global acquisition region for first lock
    TRACKING = auto()     # locked; using local ROI each tick
    LOST_GRACE = auto()   # target lost; brief hold before reacquire or fallback


class TargetTracker:
    """Detect target-marker bars and compute proportional roll-error signal.

    Target bars are tall, narrow, bright-green (non-locked) or red (locked)
    vertical elements in the MetalStorm HUD.  The lock-on reticle (dashed
    circle) is rejected by the aspect-ratio filter (height/width < 2.5).

    Thread-safety: not thread-safe; call only from the main loop thread.
    """

    # A red mask pixel this close to a crop border counts as touching it
    # (anti-aliased glyph edges rarely land exactly on the border column).
    _CLIP_EDGE_MARGIN_PX = 2

    def __init__(self, config: dict) -> None:
        cfg = config.get("tracking", {})
        self._enabled = bool(cfg.get("enabled", False))

        acq = cfg.get("acquisition_region_pct", [0.20, 0.18, 0.80, 0.68])
        self._acq_x1 = float(acq[0])
        self._acq_y1 = float(acq[1])
        self._acq_x2 = float(acq[2])
        self._acq_y2 = float(acq[3])

        self._lost_timeout = float(cfg.get("lost_timeout_sec", 0.70))
        self._prefer_red = bool(cfg.get("prefer_red_lock", True))
        # Direct operator instruction (2026-09-23): steer toward the centroid
        # of all red pixels in the scanned crop, overriding the tall-bar
        # contour pick below whenever any red pixel is present. Default
        # False here (not in shipped config.yaml, where it is True) purely so
        # the many synthetic single-shape tests in test_target_tracking.py
        # that construct a bare {"tracking": {...}} dict without this key —
        # several of them deliberately exercising the aspect-ratio/area
        # filter this override bypasses — keep testing that filter
        # unaffected by this flag's existence.
        self._red_mass_steering = bool(cfg.get("red_mass_steering", False))
        # Fixed-position HUD chrome exclusion for the above (2026-09-23):
        # the "NO LOCK" / lock-status text is bright red and boresight-
        # relative, not world content — measured at the identical fractional
        # bbox, (0.474-0.526, 0.762-0.777), across 4 frames from different
        # sessions, camera zoom, and terrain (game_battle_eject_20260923_
        # 094830_187.png and 3 others). Because it is always present and
        # always inside the acq/ROI crop, red_mass_steering would otherwise
        # lock onto it and never escape: the local ROI narrows around it,
        # which finds it again next tick, indefinitely. [x1,y1,x2,y2],
        # fractional, with margin over the measured bbox; None disables
        # exclusion entirely (default — existing tests never set this key).
        red_mass_exclude = cfg.get("red_mass_exclude_pct")
        self._red_mass_exclude_pct = (
            [float(v) for v in red_mass_exclude] if red_mass_exclude else None
        )
        # Action item 001, Cycle 12 (2026-09-24): more fixed-position HUD zones
        # to exclude from the red mask, [[x1,y1,x2,y2], ...] in full-frame
        # fractions, applied exactly like the single rectangle above. A wider
        # acquisition region reaches the scoreboard and team rosters, the
        # minimap, the weapons panel and the squad logo, all of which carry red;
        # measured on 300 archived frames, the unmasked full screen passed the
        # nameplate gate on 88 frames against 59 with those zones masked, and
        # moved the steering point of already-locked frames a median 331 px
        # against 134. Empty (default) changes nothing.
        self._red_mass_exclude_zones = [
            [float(v) for v in z] for z in (cfg.get("red_mass_exclude_zones_pct") or [])
        ]
        # Same date: steer on ONE nameplate cluster, not on the mean of every red
        # pixel in the crop. With a wide region two nameplates are often on
        # screen at once (two in the 16:48:21 frame the operator flagged) and
        # their mean is a point between them where nothing is. The glyph gate is
        # then counted per cluster (a label's glyphs inside `glyph_window_px`,
        # width x height), and the steering point is the mean of the red pixels
        # in `pixel_window_px` = (half width, reach up, reach down) around the
        # chosen cluster: the label plus the marker drawn above it (measured
        # 162-287 px above, on a small sample) and its underline bar. The chosen
        # cluster is the one nearest the previous lock, or the screen centre when
        # there is none. False (default) keeps the old whole-crop rule.
        self._cluster_select = bool(cfg.get("red_mass_cluster_select", False))
        _gw = cfg.get("red_mass_cluster_glyph_window_px", [300, 170])
        self._cluster_glyph_window = (int(_gw[0]), int(_gw[1]))
        _pw = cfg.get("red_mass_cluster_pixel_window_px", [150, 300, 100])
        self._cluster_pixel_window = (int(_pw[0]), int(_pw[1]), int(_pw[2]))
        # Own-afterburner exclusion for the above (2026-09-23): unlike "NO
        # LOCK" above, the afterburner glow is not fixed screen position (it
        # moves with the airframe's on-screen attitude), so a region-based
        # exclusion doesn't work here — but its color reliably does. Measured
        # directly (game_battle_eject_20260923_123703-05_35/36/37.png):
        # afterburner glow sits at hue 7-10 (orange, bordering true orange),
        # the real target icon at hue 3-5 (pure red) — non-overlapping in
        # bulk, unlike per-blob pixel area, which was checked first and
        # rejected (270 vs 271, 222 vs 309 px in these same frames — no
        # reliable size threshold exists). Narrows red_mass_centroid's own
        # upper hue bound only; _detect_targets' tracking_hsv.red_upper is
        # untouched, since its aspect-ratio filter already guards it against
        # this specific confusion. None (default) keeps the same upper bound
        # as tracking_hsv.red_upper — no behavior change unless configured.
        red_mass_hue_max = cfg.get("red_mass_hue_max")
        self._red_mass_hue_max = int(red_mass_hue_max) if red_mass_hue_max is not None else None
        # Afterburner-rim exclusion for the above (2026-09-23): the hue cutoff
        # above targets the flame's orange bulk, but its outer rim fades
        # through the same red hue band the real target uses (measured:
        # game_battle_eject_20260923_171137-39_0/1/2.png) — hue alone can't
        # separate a gradient's edge from a flat color. Brightness can: the
        # real target is a flat-shaded icon (value ~255 on ~86% of its
        # qualifying pixels); the flame's fading rim only reaches value
        # >=245 on 9-11% of its qualifying pixels, the rest being visibly
        # dimmer as it fades to background. Raises red_mass_centroid's own
        # value floor only — tracking_hsv's shared 150 floor (and
        # _detect_targets' use of it) is untouched. None (default) keeps
        # that same 150 floor — no behavior change unless configured.
        red_mass_value_min = cfg.get("red_mass_value_min")
        self._red_mass_value_min = int(red_mass_value_min) if red_mass_value_min is not None else None
        # HLDD 005 nameplate gate (2026-09-23): hue_max/value_min above each
        # separate a real target from one specific known false positive
        # (afterburner bulk, afterburner rim) — this session found a third
        # and fourth kind in one engagement alone (an enemy flare effect,
        # then the player's own engine exhaust; pursuit_mode_20260923_
        # 195448/50/53_81/83/85.png), meaning color/brightness alone will
        # keep finding new false positives as fast as they're patched. Every
        # real enemy contact renders a HUD nameplate (name/distance/type)
        # nearby; neither a flare nor engine exhaust ever does. Counting
        # small glyph-shaped components in the same mask red_mass_centroid
        # already computes (no OCR, no new color sample) measured 33 such
        # components for the real target vs 8-9 for each false positive in
        # the three frames above — a roughly 4x gap, but from 3 frames in one
        # engagement, not a tuned threshold. False here — implement and gate
        # off, the same shape as tracking.sustained_hold_enabled, until more
        # encounters validate the numbers.
        self._red_mass_nameplate_gate_enabled = bool(
            cfg.get("red_mass_nameplate_gate_enabled", False))
        self._red_mass_nameplate_min_glyphs = int(
            cfg.get("red_mass_nameplate_min_glyphs", 20))
        glyph_area = cfg.get("red_mass_nameplate_glyph_area", [10, 200])
        self._red_mass_nameplate_glyph_area = (int(glyph_area[0]), int(glyph_area[1]))
        self._red_mass_nameplate_glyph_max_dim = int(
            cfg.get("red_mass_nameplate_glyph_max_dim", 25))
        # Action item 001 (2026-09-23): whether a nameplate-gate REJECTION
        # falls back to the tall-bar pick. It always did — `selected` keeps
        # the _select_target result whenever _red_mass_centroid returns None —
        # so the gate could narrow the red-mass override but never reject a
        # lock. Measured on the 66 archived pursuit/eject frames that carry a
        # PURSUING marker (replay of the real functions + visual
        # classification, so inferred rather than logged): every lock the gate
        # accepted sat on a real enemy nameplate (22 of 22); of the 44 the
        # gate rejected, about 4 were real and about 35 false — the INCOMING
        # banner's shards, own exhaust, terrain, fireballs. The banner
        # mechanism itself was reproduced with the real detector on real
        # banner pixels (a 6x18, area-41 contour), and 779 of 812 historical
        # acquisitions in the banner's row near an INCOMING event fell inside
        # its x-extent. False here means "gate rejection is final" and takes
        # effect only while red_mass_steering AND the gate are both on. Default
        # True (old behavior) so every scene the existing tests build is
        # unchanged; shipped config.yaml sets it false.
        self._red_mass_tallbar_fallback = bool(
            cfg.get("red_mass_tallbar_fallback", True))
        # Action item 001, Cycle 5 (2026-09-24): re-centre the local ROI on a
        # gate-rejected red mass that touches the crop's edge. A real
        # nameplate plus its aircraft fills much of the local ROI, so when the
        # target drifts between scans the label is cut by the crop edge, the
        # glyph count falls under the gate's threshold, and the lock drops
        # even though the target is right there. Measured over the 07:34-08:56
        # pursuit and dive logs: 125 of 182 lock drops had red pixels present
        # and a gate rejection, and the four drop ticks whose raw crop was
        # archived and replayed to the logged glyph count (14, 19, 19, 6) all
        # showed a real nameplate cut by the crop edge, the red mean sitting
        # 82-173 px from the crop centre toward that edge. A rejection never
        # moved the ROI (only a lock or the miss ladder did), so the next
        # scan clipped the same label. Lock decisions, steering and the gate's
        # threshold are all unchanged: only the next tick's scan window
        # moves. False here means today's behaviour; shipped config.yaml sets
        # it true.
        self._roi_follow_enabled = bool(cfg.get("local_roi_follow_on_clip", False))
        self._roi_follow_min_px = int(cfg.get("local_roi_follow_min_px", 150))
        # HLDD 005 Selection Hardening Phase 2 (2026-09-21): shadow-only —
        # ranked_lock_priority stays false in shipped config; while false,
        # the ranked-pool rule below is computed and compared every tick but
        # never changes what _select_target actually returns (Phase 3, not
        # built here, is what would consume `true`).
        self._ranked_lock_priority = bool(cfg.get("ranked_lock_priority", False))
        # Named guess, not measured — the shadow log this gates is what
        # should correct it, same status as HLDD 013's seek_center_* starts.
        self._ranked_priority_tolerance_px = float(
            cfg.get("ranked_priority_tolerance_px", 40.0))

        self._local_roi_enabled = bool(cfg.get("local_roi_enabled", True))
        self._roi_scale = float(cfg.get("local_roi_scale", 0.22))
        roi_min = cfg.get("local_roi_min_px", [140, 90])
        self._roi_min_w = int(roi_min[0])
        self._roi_min_h = int(roi_min[1])
        self._roi_expand = float(cfg.get("local_roi_expand_factor", 1.25))
        self._roi_max_scale = float(cfg.get("local_roi_max_scale", 0.45))
        self._roi_reacquire_cycles = int(cfg.get("local_roi_reacquire_cycles", 3))

        hsv = config.get("tracking_hsv", {})
        self._red_lower = np.array(hsv.get("red_lower", [0, 150, 150]), dtype=np.uint8)
        self._red_upper = np.array(hsv.get("red_upper", [10, 255, 255]), dtype=np.uint8)
        self._green_lower = np.array(hsv.get("green_lower", [45, 150, 150]), dtype=np.uint8)
        self._green_upper = np.array(hsv.get("green_upper", [75, 255, 255]), dtype=np.uint8)
        self._min_area = float(hsv.get("min_contour_area", 12))
        self._min_aspect = float(hsv.get("min_aspect_ratio", 2.5))

        # ADR 136: padlock-off indicator — a dashed green ring centered on
        # screen when padlock is disengaged (calibrated against a live
        # capture 2026-09-09: ~40 short dash segments, H 40-75/S 30-200/
        # V 100-255, spanning roughly [0.42,0.39]-[0.67,0.67] of the frame —
        # not a single filled dot, an earlier draft of this detector assumed
        # a dot and never found the ring at all). Own config block, own HSV
        # bounds — deliberately not reusing tracking_hsv/acquisition_region
        # so calibrating one detector can't drift the other.
        pli = config.get("padlock_indicator", {})
        self._padlock_region_pct = [float(v) for v in pli.get(
            "region_pct", [0.42, 0.39, 0.67, 0.67])]
        self._padlock_green_lower = np.array(
            pli.get("green_lower", [40, 30, 100]), dtype=np.uint8)
        self._padlock_green_upper = np.array(
            pli.get("green_upper", [75, 200, 255]), dtype=np.uint8)
        self._padlock_min_area = float(pli.get("min_contour_area", 3))
        self._padlock_max_area = float(pli.get("max_contour_area", 30))
        self._padlock_min_dashes = int(pli.get("min_dashes", 6))

        # ADR 140: a small solid green dot fixed at exact screen center —
        # a different element from the dashed ring above, with its own
        # config block deliberately kept separate so calibrating one never
        # drifts the other. Measured 2026-09-17 against 8 real padlock-off
        # captures: OpenCV HSV (55, 76, 224), byte-identical across all 8
        # regardless of roll/pitch/target state, unlike the ring (which
        # moved to a different screen position in every one of those same
        # frames). Padlock-ON comparison not yet done — see ADR 140 Open
        # Question 1.
        pci = config.get("padlock_center_indicator", {})
        self._padlock_center_region_pct = [float(v) for v in pci.get(
            "region_pct", [0.485, 0.47, 0.515, 0.53])]
        self._padlock_center_green_lower = np.array(
            pci.get("green_lower", [50, 60, 200]), dtype=np.uint8)
        self._padlock_center_green_upper = np.array(
            pci.get("green_upper", [60, 90, 255]), dtype=np.uint8)
        self._padlock_center_min_pixels = int(pci.get("min_pixels", 3))

        self._mode = TrackMode.SEARCHING
        self._last_x: "float | None" = None
        self._last_y: "float | None" = None
        self._last_seen_ts: float = 0.0
        self._current_roi_scale: float = 0.0
        self._roi_rect: "tuple[int, int, int, int] | None" = None
        self._roi_miss_count: int = 0
        # HLDD 005 Selection Hardening (2026-09-21): rate-limited rationale
        # logging counter, same shape as every other shadow counter in this
        # codebase (ADR 117 D9 / HLDD 013 Phase 1) — 1st/10th/100th, then
        # every 500th. Pure logging; never changes what gets selected.
        self._selection_shadow_count: int = 0
        # Phase 2's own counter — separate from the one above so each
        # shadow log's "N so far" reflects its own occurrence count, not a
        # shared one two different conditions would otherwise both bump.
        self._ranked_shadow_count: int = 0
        # Action item 001: counts tall-bar picks the nameplate gate vetoed,
        # for the INFO-level line in _log_pick_path (same 1st/10th/100th,
        # then every 500th, shape as the two counters above).
        self._suppressed_pick_count: int = 0
        # Action item 001, Cycle 5: where the last clipped-nameplate follow
        # centred the ROI (absolute frame coords) — the miss ladder's
        # expansion re-centres there instead of on the stale last lock, which
        # would otherwise undo the follow. None whenever no follow is active.
        self._roi_centre_hint: "tuple[float, float] | None" = None
        self._roi_follow_count: int = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def mode(self) -> TrackMode:
        return self._mode

    def reset(self) -> None:
        """Clear all tracking state. Call when leaving GAME_BATTLE."""
        self._mode = TrackMode.SEARCHING
        self._last_x = None
        self._last_y = None
        self._last_seen_ts = 0.0
        self._current_roi_scale = 0.0
        self._roi_rect = None
        self._roi_miss_count = 0
        self._roi_centre_hint = None
        logger.debug("TargetTracker: reset to SEARCHING")

    def update(self, frame: np.ndarray, ts: "float | None" = None) -> dict:
        """Process one frame; update state machine; return observation dict.

        Returns:
            mode        — TrackMode name string
            visible     — True if a target was detected this tick
            centroid_x  — frame-absolute x of selected target (or last known)
            centroid_y  — frame-absolute y of selected target (or last known)
            error_norm  — horizontal error in [-1, 1]; positive = target right of center
                          (this is "error_norm_x" in HLDD 005's 2026-09-21 revision;
                          the dict key is kept as-is so existing callers are unaffected)
            error_norm_y — vertical error in [-1, 1]; positive = target below center.
                          New (HLDD 005 Two-Axis Rollout Phase 1) — sensing only, no
                          consumer presses a key from this yet.
            n_detections — raw contour count found in scan region (tall-bar
                          contours only — unaffected by red_mass_steering
                          below, so it stays a legible signal of what the
                          original filter found even while that pick is
                          being overridden for actuation)
            roi_rect    — the local-ROI rect actually scanned *this tick* to
                          produce the fields above, as (x, y, w, h) in frame
                          coords, or None when this tick scanned the wider
                          global acquisition region instead (see scan-basis
                          note below).

        When `red_mass_steering` is enabled, `centroid_x`/`centroid_y` (and
        therefore `error_norm`/`error_norm_y`) report the centroid of *all*
        red pixels in the scanned crop instead of the tall-bar contour pick
        — see `_red_mass_centroid`. `visible`/`mode` follow that override too
        (any red pixel counts), but `n_detections` does not.
        """
        if ts is None:
            ts = time.time()
        h, w = frame.shape[:2]

        if self._mode == TrackMode.SEARCHING:
            self._mode = TrackMode.ACQUIRING

        # LOST_GRACE keeps scanning the (progressively expanded, see _handle_miss)
        # local ROI around the last-known position instead of falling back to a
        # full acquisition-region scan — that expansion has no effect otherwise.
        use_roi = (
            self._local_roi_enabled
            and self._mode in (TrackMode.TRACKING, TrackMode.LOST_GRACE)
            and self._roi_rect is not None
        )
        if use_roi:
            rx, ry, rw, rh = self._roi_rect
            crop = frame[ry:ry + rh, rx:rx + rw]
            ox, oy = rx, ry
            scanned_rect = (rx, ry, rw, rh)
        else:
            ax1 = int(w * self._acq_x1)
            ay1 = int(h * self._acq_y1)
            ax2 = int(w * self._acq_x2)
            ay2 = int(h * self._acq_y2)
            crop = frame[ay1:ay2, ax1:ax2]
            ox, oy = ax1, ay1
            scanned_rect = None

        local_hits, local_red_hits, local_green_hits, red_won = self._detect_targets(crop)
        logger.debug(
            "TargetTracker: scanned %s rect=%s hits=%d",
            "roi" if use_roi else "acq", scanned_rect or (ox, oy, crop.shape[1], crop.shape[0]),
            len(local_hits),
        )
        abs_hits = [(ox + lx, oy + ly, a) for lx, ly, a in local_hits]
        selected = self._select_target(abs_hits, w)
        ref = self._last_x if self._last_x is not None else w / 2.0

        local_discarded_green = local_green_hits if red_won else []
        if local_discarded_green:
            self._log_selection_rationale(ox, oy, local_discarded_green, selected, ref)

        # Selection Hardening Phase 2 (2026-09-21): shadow the ranked-pool
        # fix, still without acting on it. Computed every tick regardless of
        # red_won, since the point is to see whenever the two rules would
        # disagree — never only on the ticks the old rule already flagged as
        # interesting. See _select_target_ranked's own docstring for the
        # rule; see _log_ranked_priority_shadow for why only disagreements
        # are logged.
        abs_red_hits = [(ox + lx, oy + ly, a) for lx, ly, a in local_red_hits]
        abs_green_hits = [(ox + lx, oy + ly, a) for lx, ly, a in local_green_hits]
        ranked_selected = self._select_target_ranked(abs_red_hits, abs_green_hits, ref)
        if not self._ranked_lock_priority:
            self._log_ranked_priority_shadow(selected, ranked_selected, ref)
        # Phase 3 (not built here) is what would branch on
        # self._ranked_lock_priority being True and actually use
        # ranked_selected — see HLDD 005 Selection Hardening.

        # Direct operator instruction (2026-09-23): override the tall-bar
        # pick with the centroid of all red pixels in the scanned crop,
        # using the same red HSV range _detect_targets already uses (not a
        # new color sample) — after, not before, the shadow logging above,
        # so Selection Hardening's own before/after comparison keeps
        # reflecting the tall-bar algorithm's own decision either way.
        tall_pick = selected
        pick_path = "tallbar" if selected is not None else "none"
        rm_probe: "dict | None" = None
        if self._red_mass_steering:
            # Cluster mode steers on the nameplate nearest the previous lock, or
            # the screen centre when there is none (`ref=None`).
            ref = ((self._last_x, self._last_y)
                   if self._mode in (TrackMode.TRACKING, TrackMode.LOST_GRACE)
                   and self._last_x is not None and self._last_y is not None
                   else None)
            rm_probe = self._red_mass_probe(crop, ox, oy, w, h, ref=ref)
            red_mass_local = rm_probe["centroid"]
            if red_mass_local is not None:
                selected = (ox + red_mass_local[0], oy + red_mass_local[1])
                pick_path = "redmass"
            elif rm_probe["gate"] == "reject" and not self._red_mass_tallbar_fallback:
                # Action item 001: the gate's rejection is final — the
                # tall-bar pick it used to fall back to was false about nine
                # times in ten (see __init__). `tall_pick` is kept only so
                # _log_pick_path can say what was vetoed.
                selected = None
                pick_path = "suppressed" if tall_pick is not None else "none"
        self._log_pick_path(pick_path, selected, tall_pick, rm_probe,
                            abs_red_hits, red_won, len(local_hits))

        if selected is not None:
            abs_x, abs_y = selected
            self._last_x = abs_x
            self._last_y = abs_y
            self._last_seen_ts = ts
            self._roi_miss_count = 0
            self._roi_centre_hint = None
            if self._mode in (TrackMode.ACQUIRING, TrackMode.LOST_GRACE):
                self._mode = TrackMode.TRACKING
                self._current_roi_scale = self._roi_scale
                logger.debug("TargetTracker: acquired target at (%.0f, %.0f)", abs_x, abs_y)
            self._roi_rect = self._compute_roi(abs_x, abs_y, w, h)
        else:
            self._handle_miss(ts, w, h)
            self._follow_clipped_nameplate(use_roi, rm_probe, ox, oy, w, h)

        error_norm: "float | None" = None
        error_norm_y: "float | None" = None
        if self._last_x is not None and self._mode != TrackMode.SEARCHING:
            error_norm = float(np.clip((self._last_x - w / 2.0) / (w / 2.0), -1.0, 1.0))
            error_norm_y = float(np.clip((self._last_y - h / 2.0) / (h / 2.0), -1.0, 1.0))

        return {
            "mode": self._mode.name,
            "visible": selected is not None,
            "centroid_x": self._last_x,
            "centroid_y": self._last_y,
            "error_norm": error_norm,
            "error_norm_y": error_norm_y,
            "n_detections": len(local_hits),
            "roi_rect": scanned_rect,
        }

    def detect_padlock_off(self, frame: np.ndarray) -> bool:
        """ADR 136: True if the padlock-off indicator — a dashed green ring
        centered on screen, made of many short dash segments rather than
        one filled shape — is visible this frame.

        Counts contours sized like a single dash (min AND max area bounds,
        no aspect-ratio filter since dashes are short curved strokes, not
        tall bars) and requires at least min_dashes of them, rather than
        treating any one green blob as confirmation — a single stray green
        pixel elsewhere in the region would otherwise false-positive.
        """
        if frame is None or frame.size == 0:
            return False
        h, w = frame.shape[:2]
        x1 = int(w * self._padlock_region_pct[0])
        y1 = int(h * self._padlock_region_pct[1])
        x2 = int(w * self._padlock_region_pct[2])
        y2 = int(h * self._padlock_region_pct[3])
        crop = frame[y1:y2, x1:x2]
        if crop.size == 0:
            return False
        try:
            hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        except Exception:
            return False
        mask = cv2.inRange(hsv, self._padlock_green_lower, self._padlock_green_upper)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        dash_count = sum(
            1 for c in contours
            if self._padlock_min_area <= cv2.contourArea(c) <= self._padlock_max_area
        )
        return dash_count >= self._padlock_min_dashes

    def detect_padlock_center_dot(self, frame: np.ndarray) -> bool:
        """ADR 140: True if the fixed-screen-center padlock-off dot is
        visible this frame.

        Deliberately a tight, fixed crop rather than tracking the moving
        ring `detect_padlock_off` reads (ADR 136 D4 found that ring drifts
        with flight attitude) — this detector will simply fail to find the
        dot, not misidentify something else, whenever it isn't at true
        screen center. That is the safe failure mode for a signal only
        ever consumed as one leg of a multi-tick, multi-signal fusion
        (ADR 140 D4), not as a per-frame source of truth on its own.
        """
        if frame is None or frame.size == 0:
            return False
        h, w = frame.shape[:2]
        x1 = int(w * self._padlock_center_region_pct[0])
        y1 = int(h * self._padlock_center_region_pct[1])
        x2 = int(w * self._padlock_center_region_pct[2])
        y2 = int(h * self._padlock_center_region_pct[3])
        crop = frame[y1:y2, x1:x2]
        if crop.size == 0:
            return False
        try:
            hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        except Exception:
            return False
        mask = cv2.inRange(hsv, self._padlock_center_green_lower,
                           self._padlock_center_green_upper)
        return int(np.count_nonzero(mask)) >= self._padlock_center_min_pixels

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _handle_miss(self, ts: float, w: int, h: int) -> None:
        if self._mode == TrackMode.TRACKING:
            self._mode = TrackMode.LOST_GRACE
            self._roi_miss_count = 1
        elif self._mode == TrackMode.LOST_GRACE:
            self._roi_miss_count += 1
            elapsed = ts - self._last_seen_ts
            if elapsed >= self._lost_timeout:
                logger.debug("TargetTracker: grace timeout after %.2fs — ACQUIRING", elapsed)
                self._mode = TrackMode.ACQUIRING
                self._last_x = None
                self._last_y = None
                self._roi_rect = None
                self._roi_miss_count = 0
                self._roi_centre_hint = None
            elif self._roi_miss_count >= self._roi_reacquire_cycles:
                new_scale = min(self._current_roi_scale * self._roi_expand, self._roi_max_scale)
                self._current_roi_scale = new_scale
                self._roi_miss_count = 0
                centre = self._roi_centre_hint
                if centre is None and self._last_x is not None and self._last_y is not None:
                    centre = (self._last_x, self._last_y)
                if centre is not None:
                    self._roi_rect = self._compute_roi(centre[0], centre[1], w, h)
                if new_scale >= self._roi_max_scale:
                    logger.debug("TargetTracker: ROI at max scale — falling back to ACQUIRING")
                    self._mode = TrackMode.ACQUIRING

    def _follow_clipped_nameplate(
        self, use_roi: bool, rm_probe: "dict | None",
        ox: int, oy: int, fw: int, fh: int,
    ) -> None:
        """Action item 001, Cycle 5: after a missed tick, move the local ROI
        toward a red mass the nameplate gate rejected because the crop edge
        cut it. Never called on a lock and never changes `selected`, the
        steering point, `_last_x/_last_y` or the gate's threshold — a lock
        still needs a full nameplate — so it cannot admit a false positive;
        it only makes the next scan look where the clipped label is.

        Requires, all together: the feature is on, this tick scanned the
        local ROI (not the wide acquisition region), tracking is still in
        LOST_GRACE with an ROI (the miss handler may just have ended it), the
        gate positively rejected the mask, at least `local_roi_follow_min_px`
        red pixels were present, and the red touches a crop edge — the
        signature of a cut label rather than of debris or an absent
        nameplate. The follow is bounded by `lost_timeout_sec`, the same
        clock that ends every LOST_GRACE ROI.
        """
        if not (self._roi_follow_enabled and use_roi and rm_probe is not None):
            return
        if self._mode != TrackMode.LOST_GRACE or self._roi_rect is None:
            return
        mass = rm_probe.get("mass_centroid")
        edges = rm_probe.get("clipped_edges")
        if (rm_probe["gate"] != "reject" or mass is None or not edges
                or rm_probe["px"] < self._roi_follow_min_px):
            return
        cx, cy = ox + mass[0], oy + mass[1]
        old = self._roi_rect
        new = self._compute_roi(cx, cy, fw, fh)
        if new == old:
            return  # already pinned against the frame edge; nothing to move
        self._roi_rect = new
        self._roi_centre_hint = (cx, cy)
        self._roi_follow_count += 1
        n = self._roi_follow_count
        logger.debug(
            "ROIFOLLOW: gate rejected a red mass cut by the %s crop edge "
            "(glyphs=%s rm_px=%d) — ROI %s -> %s",
            "+".join(edges), rm_probe["glyphs"], rm_probe["px"], old, new,
        )
        if n in (1, 10, 100) or n % 500 == 0:
            logger.info("ROIFOLLOW: moved the ROI toward a clipped nameplate "
                        "(%d so far)", n)

    def _contours_to_hits(self, mask: np.ndarray) -> "list[tuple[float, float, float]]":
        """Return (cx, cy, area) in crop-local coords for each valid target bar in `mask`."""
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        results: "list[tuple[float, float, float]]" = []
        for c in contours:
            area = float(cv2.contourArea(c))
            if area < self._min_area:
                continue
            x, y, cw, ch = cv2.boundingRect(c)
            if cw == 0 or ch / cw < self._min_aspect:
                continue
            results.append((float(x + cw / 2), float(y + ch / 2), area))
        return results

    def _log_pick_path(
        self,
        pick_path: str,
        selected: "tuple[float, float] | None",
        tall_pick: "tuple[float, float] | None",
        rm_probe: "dict | None",
        abs_red_hits: "list[tuple[float, float, float]]",
        red_won: bool,
        n_tall: int,
    ) -> None:
        """Action item 001, Open Question 1: one greppable line per tick
        saying which path supplied the lock this tick — `redmass`, `tallbar`,
        `suppressed` (the gate vetoed a tall-bar pick) or `none` — plus what
        the gate saw. Turns "did the gate fire" from a forensic
        reconstruction into `grep TRACKPICK`. Pure logging; never changes
        `selected`. The DEBUG line follows the existing per-tick "scanned"
        line's cadence; a suppressed pick is also surfaced at INFO,
        rate-limited like every other shadow counter here, since DEBUG lines
        do not survive into a normal session log.
        """
        if rm_probe is not None:
            gate = rm_probe["gate"]
            glyphs = rm_probe["glyphs"] if rm_probe["glyphs"] is not None else "-"
            rm_px = rm_probe["px"]
            edges = "+".join(rm_probe["clipped_edges"]) or "-"
            blob = rm_probe.get("blob")
            blob_desc = "(%d,%d,a%d,%dx%d)" % blob if blob is not None else "-"
            clu_desc = rm_probe["clusters"] if rm_probe.get("clusters") is not None else "-"
        else:
            gate, glyphs, rm_px, edges, blob_desc, clu_desc = "off", "-", 0, "-", "-", "-"
        if tall_pick is not None:
            color = "R" if any(
                abs(rx - tall_pick[0]) < 1e-6 and abs(ry - tall_pick[1]) < 1e-6
                for rx, ry, _a in abs_red_hits
            ) else "G"
            tall_desc = f"({tall_pick[0]:.0f},{tall_pick[1]:.0f},{color})"
        else:
            tall_desc = "-"
        sel_desc = f"({selected[0]:.0f},{selected[1]:.0f})" if selected is not None else "-"
        logger.debug(
            "TRACKPICK: path=%s sel=%s tall=%s n_tall=%d red_won=%s "
            "gate=%s glyphs=%s rm_px=%d edges=%s blob=%s clu=%s",
            pick_path, sel_desc, tall_desc, n_tall, red_won, gate, glyphs, rm_px,
            edges, blob_desc, clu_desc,
        )
        if pick_path == "suppressed":
            self._suppressed_pick_count += 1
            n = self._suppressed_pick_count
            if n in (1, 10, 100) or n % 500 == 0:
                logger.info(
                    "TRACKPICK: tall-bar pick %s vetoed — nameplate gate "
                    "rejected (glyphs=%s < %d); %d so far",
                    tall_desc, glyphs, self._red_mass_nameplate_min_glyphs, n,
                )

    def _red_mass_centroid(
        self, crop: np.ndarray, ox: int, oy: int, frame_w: int, frame_h: int
    ) -> "tuple[float, float] | None":
        """Crop-local centroid of every red-mass pixel, or None — see
        `_red_mass_probe`, which does the work and also reports the gate's
        numbers. Kept as its own entry point so callers and tests that only
        want the point are unchanged."""
        return self._red_mass_probe(crop, ox, oy, frame_w, frame_h)["centroid"]

    def _red_mass_probe(
        self, crop: np.ndarray, ox: int, oy: int, frame_w: int, frame_h: int,
        ref: "tuple[float, float] | None" = None,
    ) -> dict:
        """Direct operator instruction (2026-09-23): crop-local centroid of
        every pixel matching the same red range _detect_targets uses (main
        hue band, upper end optionally narrowed by red_mass_hue_max, lower
        value bound optionally raised by red_mass_value_min, plus the
        [170,180] wrap-around) — no contour, no area or aspect-ratio gate,
        so this can and will fire on shapes the tall-bar filter exists
        specifically to reject (muzzle flash, damage decals, HUD chrome).
        That tradeoff was raised and set aside by the operator in favor of
        this simpler rule; see the 2026-09-23 conversation for the
        false-positive evidence this overrides.

        When `red_mass_nameplate_gate_enabled` is set, a candidate must also
        clear `_count_nameplate_glyphs`'s threshold or the returned
        "centroid" is None — see that method's docstring. Off by default;
        no effect on the behavior described above until enabled.

        Returns a dict (action item 001) rather than the bare point so the
        caller can tell *why* there is no centroid and log what the gate saw:
        `centroid` — crop-local (x, y) or None; `gate` — "off" (gate not
        enabled), "pass" or "reject"; `glyphs` — the count the gate compared
        (None when the gate is off); `px` — pixels in the final red mask.
        A "reject" is a positive statement that no nameplate was found;
        "off" with a None centroid only means no red pixels matched.

        `ref` (absolute frame coordinates, default the frame centre) is only used
        when `red_mass_cluster_select` is on, to choose among several nameplate
        clusters: the one nearest `ref` wins.

        `ox`/`oy` (crop's absolute top-left in the full frame) and
        `frame_w`/`frame_h` are needed only to translate
        `red_mass_exclude_pct` (defined in full-frame fractions, since the
        HUD element it targets is fixed relative to the screen, not to
        whichever crop happens to be active this tick) into this crop's
        local coordinates.
        """
        probe: dict = {"centroid": None, "gate": "off", "glyphs": None, "px": 0,
                       "mass_centroid": None, "clipped_edges": (), "blob": None,
                       "clusters": None}
        if crop is None or crop.size == 0:
            return probe
        try:
            hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        except Exception:
            return probe
        value_min = self._red_mass_value_min if self._red_mass_value_min is not None else int(self._red_lower[2])
        red_lower = np.array([int(self._red_lower[0]), int(self._red_lower[1]), value_min], dtype=np.uint8)
        red_wrap_lower = np.array([170, int(self._red_lower[1]), value_min], dtype=np.uint8)
        red_wrap_upper = np.array(
            [180, int(self._red_upper[1]), int(self._red_upper[2])], dtype=np.uint8
        )
        hue_max = self._red_mass_hue_max if self._red_mass_hue_max is not None else int(self._red_upper[0])
        red_upper = np.array([hue_max, int(self._red_upper[1]), int(self._red_upper[2])], dtype=np.uint8)
        mask = (
            cv2.inRange(hsv, red_lower, red_upper)
            | cv2.inRange(hsv, red_wrap_lower, red_wrap_upper)
        )
        exclusions = list(self._red_mass_exclude_zones)
        if self._red_mass_exclude_pct is not None:
            exclusions.append(self._red_mass_exclude_pct)
        for ex1, ey1, ex2, ey2 in exclusions:
            cx1 = max(0, int(frame_w * ex1) - ox)
            cy1 = max(0, int(frame_h * ey1) - oy)
            cx2 = min(crop.shape[1], int(frame_w * ex2) - ox)
            cy2 = min(crop.shape[0], int(frame_h * ey2) - oy)
            if cx1 < cx2 and cy1 < cy2:
                mask[cy1:cy2, cx1:cx2] = 0
        probe["px"] = int(np.count_nonzero(mask))
        ys, xs = np.nonzero(mask)
        if xs.size:
            # Reported whether or not the gate passes: a rejected mask that
            # touches the crop edge is how `_follow_clipped_nameplate` tells a
            # cut-off label from an absent one. `centroid` stays gate-owned.
            probe["mass_centroid"] = (float(xs.mean()), float(ys.mean()))
            crop_h, crop_w = mask.shape[:2]
            m = self._CLIP_EDGE_MARGIN_PX
            probe["clipped_edges"] = tuple(
                name for name, hit in (
                    ("left", xs.min() <= m), ("right", xs.max() >= crop_w - 1 - m),
                    ("top", ys.min() <= m), ("bottom", ys.max() >= crop_h - 1 - m),
                ) if hit
            )
        if xs.size and logger.isEnabledFor(logging.DEBUG):
            probe["blob"] = self._largest_blob(mask, ox, oy)
        if self._red_mass_nameplate_gate_enabled and self._cluster_select:
            return self._probe_by_cluster(mask, probe, ox, oy, frame_w, frame_h, ref)
        if self._red_mass_nameplate_gate_enabled:
            glyphs = self._count_nameplate_glyphs(mask)
            probe["glyphs"] = glyphs
            if glyphs < self._red_mass_nameplate_min_glyphs:
                probe["gate"] = "reject"
                return probe
            probe["gate"] = "pass"
        if xs.size == 0:
            return probe
        probe["centroid"] = probe["mass_centroid"]
        return probe

    def _probe_by_cluster(
        self, mask: np.ndarray, probe: dict, ox: int, oy: int,
        frame_w: int, frame_h: int, ref: "tuple[float, float] | None",
    ) -> dict:
        """The gate and steering point of `_red_mass_probe` in cluster mode: the
        gate counts glyphs per nameplate cluster (the best cluster is what
        `glyphs` reports, so a rejected tick still says how close it came), and
        the steering point is the mean of the red pixels around the chosen
        cluster, not of the whole crop. One eligible cluster in a crop small
        enough to sit inside the pixel window (the local ROI) gives the same
        point as the whole-crop mean."""
        clusters = self._nameplate_clusters(mask)
        best = max((c[2] for c in clusters), default=0)
        eligible = [c for c in clusters if c[2] >= self._red_mass_nameplate_min_glyphs]
        probe["glyphs"] = best
        probe["clusters"] = len(eligible)
        if not eligible:
            probe["gate"] = "reject"
            return probe
        rx, ry = ref if ref is not None else (frame_w / 2.0, frame_h / 2.0)
        rx, ry = rx - ox, ry - oy
        cx, cy, n = min(eligible, key=lambda c: (c[0] - rx) ** 2 + (c[1] - ry) ** 2)
        half_w, up, down = self._cluster_pixel_window
        h, w = mask.shape[:2]
        x0, x1 = max(0, int(cx - half_w)), min(w, int(cx + half_w) + 1)
        y0, y1 = max(0, int(cy - up)), min(h, int(cy + down) + 1)
        ys, xs = np.nonzero(mask[y0:y1, x0:x1])
        if xs.size == 0:
            probe["gate"] = "reject"
            return probe
        probe["gate"] = "pass"
        probe["glyphs"] = n
        probe["centroid"] = (float(xs.mean() + x0), float(ys.mean() + y0))
        return probe

    def _nameplate_clusters(self, mask: np.ndarray, max_clusters: int = 8
                            ) -> "list[tuple[float, float, int]]":
        """Groups of glyph-sized red components that sit close together, as
        (centre x, centre y, glyph count) in crop coordinates, densest first.
        A nameplate is dozens of small letters and digits in a block of about
        200 x 110 px; two labels on screen are two blocks. Greedy: take the
        window (`red_mass_cluster_glyph_window_px`) holding the most unclaimed
        glyphs, claim them, repeat. Glyph shape bounds are the gate's own."""
        n, _labels, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
        area_min, area_max = self._red_mass_nameplate_glyph_area
        max_dim = self._red_mass_nameplate_glyph_max_dim
        idx = [i for i in range(1, n)
               if area_min <= stats[i][cv2.CC_STAT_AREA] <= area_max
               and stats[i][cv2.CC_STAT_WIDTH] <= max_dim
               and stats[i][cv2.CC_STAT_HEIGHT] <= max_dim]
        if not idx:
            return []
        pts = centroids[idx][:2000]
        half_w, half_h = self._cluster_glyph_window[0] / 2.0, self._cluster_glyph_window[1] / 2.0
        near = ((np.abs(pts[:, 0][:, None] - pts[:, 0][None, :]) <= half_w)
                & (np.abs(pts[:, 1][:, None] - pts[:, 1][None, :]) <= half_h))
        used = np.zeros(len(pts), dtype=bool)
        clusters: "list[tuple[float, float, int]]" = []
        while len(clusters) < max_clusters:
            counts = (near & ~used[None, :]).sum(axis=1)
            counts[used] = 0
            i = int(np.argmax(counts))
            if counts[i] < 1:
                break
            members = near[i] & ~used
            cx, cy = pts[members].mean(axis=0)
            clusters.append((float(cx), float(cy), int(counts[i])))
            used |= members
        return clusters

    @staticmethod
    def _largest_blob(mask: np.ndarray, ox: int, oy: int) -> "tuple[int, int, int, int, int] | None":
        """Action item 001, Cycle 6 (2026-09-24), logging only: the largest
        connected red component in `mask` as (x, y, area, w, h), with (x, y)
        its centre in absolute frame coordinates. Feeds `TRACKPICK`'s `blob=`
        field so a gate-rejected tick still says *where* the red is and how
        big it is — a rejected tick has no `sel`. Measured on the archived
        raw crops of the 09:26-10:13 session: 117 of 129 label-less red
        crops held exactly one compact component (median 433 px, about
        39 x 35 px bounding box), the size of a red enemy-aircraft icon, and
        icon-like masses sat on 76% of non-locked pursuit ticks; where those
        icons are, and whether a labelled lock later appears there, could not
        be read from the log. Never changes a pick or a gate decision. Only
        called while DEBUG logging is on, since nothing else reads it.
        """
        n, _labels, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
        if n <= 1:
            return None
        i = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        _x, _y, w, h, area = (int(v) for v in stats[i])
        return (int(round(ox + centroids[i][0])), int(round(oy + centroids[i][1])), area, w, h)

    def _count_nameplate_glyphs(self, mask: np.ndarray) -> int:
        """HLDD 005 nameplate gate (2026-09-23): count small, glyph-shaped
        connected components in the same red mask `_red_mass_centroid`
        already computed for this tick — no OCR, no new color sample.

        A real enemy contact's HUD nameplate (name/distance/type) renders as
        many small components, one per letter or digit — measured 33 in
        pursuit_mode_20260923_195448_81.png, the one frame of the three
        where the lock was actually on the real target. A flare effect or
        the player's own engine exhaust produced 8 and 9 respectively in the
        other two (pursuit_mode_20260923_195450_83.png,
        pursuit_mode_20260923_195453_85.png) — no nameplate renders near
        either, so only stray fragments of the blob itself happen to fall in
        the glyph-size range. `red_mass_nameplate_glyph_area`/
        `_glyph_max_dim` bound what counts as glyph-shaped.
        """
        n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        area_min, area_max = self._red_mass_nameplate_glyph_area
        max_dim = self._red_mass_nameplate_glyph_max_dim
        count = 0
        for i in range(1, n):
            _x, _y, w, h, area = stats[i]
            if area_min <= area <= area_max and w <= max_dim and h <= max_dim:
                count += 1
        return count

    def _detect_targets(
        self, crop: np.ndarray
    ) -> "tuple[list[tuple[float, float, float]], list[tuple[float, float, float]], list[tuple[float, float, float]], bool]":
        """Return (hits, red_hits, green_hits, red_won) in crop-local coords.

        `hits` is exactly what this method always returned — the winning
        color class's valid target bars, unchanged by either HLDD 005
        Selection Hardening Phase 1 (rationale logging) or Phase 2 (ranked-
        pool shadow) below. `red_hits`/`green_hits` are the full, ungated
        candidate pools for both colors, always computed (Phase 2 needs both
        regardless of which one the existing rule would have picked, to know
        whether its ranked alternative would ever disagree) — a small,
        deliberate extra cost over the original single-mask contour pass,
        accepted the same way Phase 1's conditional extra pass already was.
        `red_won` is rule 1's own color-exclusion decision (`_prefer_red` and
        any red pixel present, *before* area/aspect filtering) — kept
        explicit because it can diverge from "`red_hits` is non-empty": red
        can win the mask-level exclusion and still filter down to zero valid
        bars, discarding every green candidate for nothing (observed live,
        2026-09-21: `SELECT[shadow]` logged "selected none" against a
        discarded green candidate in exactly this situation).
        """
        if crop is None or crop.size == 0:
            return [], [], [], False
        try:
            hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        except Exception:
            return [], [], [], False

        # Red: locked target — hue wraps; check primary + wrap-around range
        red_wrap_lower = np.array(
            [170, int(self._red_lower[1]), int(self._red_lower[2])], dtype=np.uint8
        )
        red_wrap_upper = np.array(
            [180, int(self._red_upper[1]), int(self._red_upper[2])], dtype=np.uint8
        )
        mask_red = (
            cv2.inRange(hsv, self._red_lower, self._red_upper)
            | cv2.inRange(hsv, red_wrap_lower, red_wrap_upper)
        )
        mask_green = cv2.inRange(hsv, self._green_lower, self._green_upper)

        red_won = self._prefer_red and bool(np.any(mask_red))

        red_hits = self._contours_to_hits(mask_red)
        green_hits = self._contours_to_hits(mask_green)
        hits = red_hits if red_won else green_hits
        return hits, red_hits, green_hits, red_won

    def _log_selection_rationale(
        self,
        ox: int,
        oy: int,
        local_discarded_green: "list[tuple[float, float, float]]",
        selected: "tuple[float, float] | None",
        ref: float,
    ) -> None:
        """HLDD 005 Selection Hardening Phase 1: rate-limited rationale log.

        Pure logging — never changes `selected`. Fires only on the tick
        `_detect_targets` found at least one green candidate that rule 1
        (color priority) discarded in favor of red. Reports how far the
        discarded candidate(s) sat from `ref` compared to the target that
        was actually selected, which is exactly the evidence Selection
        Hardening's Phase 1 needs before Phase 2's ranked-pool change is
        worth writing at all.
        """
        self._selection_shadow_count += 1
        n = self._selection_shadow_count
        if n not in (1, 10, 100) and n % 500 != 0:
            return
        discarded_abs = [(ox + lx, oy + ly) for lx, ly, _area in local_discarded_green]
        discarded_desc = ", ".join(
            f"(x={dx:.0f} dist={abs(dx - ref):.0f})" for dx, _dy in discarded_abs
        )
        if selected is not None:
            selected_desc = f"x={selected[0]:.0f} dist={abs(selected[0] - ref):.0f}"
        else:
            selected_desc = "none"
        logger.info(
            "SELECT[shadow]: red won, discarded %d green candidate(s) [%s]; "
            "selected %s (%d so far)",
            len(discarded_abs), discarded_desc, selected_desc, n,
        )

    def _select_target(
        self,
        abs_hits: "list[tuple[float, float, float]]",
        frame_w: int,
    ) -> "tuple[float, float] | None":
        if not abs_hits:
            return None
        ref = self._last_x if self._last_x is not None else frame_w / 2.0
        best = min(abs_hits, key=lambda d: abs(d[0] - ref))
        return (best[0], best[1])

    def _select_target_ranked(
        self,
        red_abs_hits: "list[tuple[float, float, float]]",
        green_abs_hits: "list[tuple[float, float, float]]",
        ref: float,
    ) -> "tuple[float, float] | None":
        """HLDD 005 Selection Hardening Phase 2 (2026-09-21): shadow-only
        ranked candidate pool. Never called by the real selection path
        (_select_target) — only compared against it, gated by
        self._ranked_lock_priority, to log where the two would disagree.

        Merges red and green candidates into one pool instead of excluding
        green outright whenever any red pixel is present. Red keeps a
        bounded preference — it wins ties within
        self._ranked_priority_tolerance_px — but a green candidate
        meaningfully closer to `ref` wins outright instead of being
        discarded regardless of position, which is today's rule (see
        _detect_targets's red_won).
        """
        best_red = min(red_abs_hits, key=lambda d: abs(d[0] - ref)) if red_abs_hits else None
        best_green = min(green_abs_hits, key=lambda d: abs(d[0] - ref)) if green_abs_hits else None
        if best_red is None:
            return (best_green[0], best_green[1]) if best_green is not None else None
        if best_green is None:
            return (best_red[0], best_red[1])
        red_dist = abs(best_red[0] - ref)
        green_dist = abs(best_green[0] - ref)
        if green_dist < red_dist - self._ranked_priority_tolerance_px:
            return (best_green[0], best_green[1])
        return (best_red[0], best_red[1])

    def _log_ranked_priority_shadow(
        self,
        selected: "tuple[float, float] | None",
        ranked_selected: "tuple[float, float] | None",
        ref: float,
    ) -> None:
        """Log only on disagreement between the real selection and Phase 2's
        ranked-pool alternative, rate-limited the same way every other
        shadow counter in this codebase is (1st/10th/100th, then every
        500th) — logging every tick the two happen to differ could log
        continuously for as long as a disagreement persists.
        """
        old_x = selected[0] if selected is not None else None
        new_x = ranked_selected[0] if ranked_selected is not None else None
        if old_x == new_x:
            return
        self._ranked_shadow_count += 1
        n = self._ranked_shadow_count
        if n not in (1, 10, 100) and n % 500 != 0:
            return
        old_desc = f"x={old_x:.0f} dist={abs(old_x - ref):.0f}" if old_x is not None else "none"
        new_desc = f"x={new_x:.0f} dist={abs(new_x - ref):.0f}" if new_x is not None else "none"
        logger.info(
            "SELECT[shadow]: old=%s new=%s would change (%d so far)",
            old_desc, new_desc, n,
        )

    def _compute_roi(self, cx: float, cy: float, fw: int, fh: int) -> "tuple[int, int, int, int]":
        rw = max(self._roi_min_w, int(fw * self._current_roi_scale))
        rh = max(self._roi_min_h, int(fh * self._current_roi_scale))
        x = int(max(0, min(cx - rw / 2, fw - rw)))
        y = int(max(0, min(cy - rh / 2, fh - rh)))
        return (x, y, rw, rh)
