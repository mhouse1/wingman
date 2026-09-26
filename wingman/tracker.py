"""Screen-space target tracking for MetalStorm combat.

Finds enemy nameplates (the red name / range / type label the game draws over
every enemy contact) as clusters of glyph-sized components in a red HSV mask,
steers on the aircraft marker a fixed offset below the chosen label, maintains
a scan-mode state machine (ACQUIRING → TRACKING → LOST_GRACE), and returns a
per-frame observation dict. Review 018 CR-018-04 (2026-09-25) removed the
tall-bar contour detector, its Selection Hardening shadow logs and the flags
that kept it reachable: with the shipped config it never steered.

Call update(frame) once per main-loop tick while in GAME_BATTLE.
Call reset() when leaving GAME_BATTLE.
Pass observation["error_norm"] to Controller.orient_nose_to_target().
"""

import logging
import threading
import time
from enum import Enum, auto

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# tracking.red_mass_exclude_zones_pct's shipped value (action item 001, Cycle
# 12), the code default since CR-018-04: scoreboard strip, top-right roster,
# bottom-right weapons panel, bottom-left squad logo.
_SHIPPED_EXCLUDE_ZONES = [
    [0.0, 0.0, 1.0, 0.0917],
    [0.828, 0.0, 1.0, 0.275],
    [0.75, 0.883, 1.0, 1.0],
    [0.0, 0.9, 0.172, 1.0],
]


class TrackMode(Enum):
    SEARCHING = auto()    # not in battle / between missions
    ACQUIRING = auto()    # scanning global acquisition region for first lock
    TRACKING = auto()     # locked; the target was seen this tick
    LOST_GRACE = auto()   # target lost; brief hold before reacquire or fallback


class TargetTracker:
    """Find the enemy nameplate to steer on and report the steering error.

    One path (CR-018-04): the acquisition region's red mask, minus the fixed
    HUD zones, is grouped into nameplate clusters; a lock is kept on a looser
    glyph test near the previous one, otherwise the strict gate picks the
    eligible cluster nearest the screen centre (see `_probe_by_cluster`).

    Thread-safety (CR-018-05): `update()` and `reset()` are serialised by an
    internal lock. Three threads drive this object — the main tick
    (TrackingHandler, GAME_BATTLE and GAME_BATTLE_MANUAL), the pursuit loop
    and the eject heatdive loop (both GAME_BATTLE_EJECT). Game state keeps
    them apart except at transitions; the lock covers those. A caller that
    cannot take it within `_LOCK_TIMEOUT_S` gets the last observation back
    instead of blocking, so the main loop never waits on a pursuit tick.
    """

    # A scan measured 3.7 to 9.5 ms (review 018); this is many scans' worth.
    _LOCK_TIMEOUT_S = 0.25

    # A red mask pixel this close to a crop border counts as touching it
    # (anti-aliased glyph edges rarely land exactly on the border column).
    _CLIP_EDGE_MARGIN_PX = 2

    def __init__(self, config: dict) -> None:
        cfg = config.get("tracking", {})
        self._enabled = bool(cfg.get("enabled", False))

        acq = cfg.get("acquisition_region_pct", [0.0, 0.09, 1.0, 0.95])
        self._acq_x1 = float(acq[0])
        self._acq_y1 = float(acq[1])
        self._acq_x2 = float(acq[2])
        self._acq_y2 = float(acq[3])

        # How long LOST_GRACE remembers a lost target (2026-09-24): the same
        # delay Controller.roll_on_miss holds the roll axis neutral for before
        # it resumes the search — pursuit_mode.search_resume_delay_s, or
        # search_resume_centre_delay_s when the last visible horizontal error
        # was within search_resume_centre_err — read from the same keys with
        # the Controller's own defaults so the two cannot drift apart. It
        # replaces lost_timeout_sec (0.4 s), which forgot the target, and with
        # it the anchor the keep test below needs, while the actuation layer
        # was still waiting 2-6 s for that same target to come back.
        pm = config.get("pursuit_mode", {}) or {}
        self._memory_s = float(pm.get("search_resume_delay_s", 2.0))
        self._memory_centre_err = float(pm.get("search_resume_centre_err", 0.15))
        self._memory_centre_s = float(pm.get("search_resume_centre_delay_s", 6.0))
        # Fixed-position HUD chrome exclusion for the above (2026-09-23):
        # the "NO LOCK" / lock-status text is bright red and boresight-
        # relative, not world content — measured at the identical fractional
        # bbox, (0.474-0.526, 0.762-0.777), across 4 frames from different
        # sessions, camera zoom, and terrain (game_battle_eject_20260923_
        # 094830_187.png and 3 others). Because it is always present and
        # always inside the acq/ROI crop, red_mass_steering would otherwise
        # lock onto it and never escape: the local ROI narrows around it,
        # which finds it again next tick, indefinitely. [x1,y1,x2,y2],
        # fractional, with margin over the measured bbox. An empty value
        # disables it; the default is the shipped rectangle (CR-018-04).
        red_mass_exclude = cfg.get("red_mass_exclude_pct", [0.46, 0.75, 0.54, 0.79])
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
        # against 134. The default is the shipped list (CR-018-04); an empty
        # list turns it off.
        self._red_mass_exclude_zones = [
            [float(v) for v in z]
            for z in (cfg.get("red_mass_exclude_zones_pct", _SHIPPED_EXCLUDE_ZONES) or [])
        ]
        # Same date: steer on ONE nameplate cluster, not on the mean of every red
        # pixel in the crop. With a wide region two nameplates are often on
        # screen at once (two in the 16:48:21 frame the operator flagged) and
        # their mean is a point between them where nothing is. The glyph gate is
        # then counted per cluster (a label's glyphs inside `glyph_window_px`,
        # width x height). The chosen cluster is the one nearest the previous
        # lock, or the screen centre when there is none. The only rule since
        # CR-018-04.
        _gw = cfg.get("red_mass_cluster_glyph_window_px", [300, 170])
        self._cluster_glyph_window = (int(_gw[0]), int(_gw[1]))
        # 2026-09-24 (corrected the same day): in cluster mode the steering point is the
        # aircraft, not its label. The game draws the aircraft's own marker (a hollow
        # diamond bracket, or a small red jet icon on a far contact) BELOW the nameplate,
        # at a fixed screen offset. Measured on 8 archived frames with a ruler on the
        # frame (targets from 0.36 to 6.8 km): the marker centre sits 90 to 110 px below
        # the label's glyph-cluster centre, mean 100.5, at every range. The marker is
        # darker than the strict red mask admits (hue 0-2, value 217-230 against 245), so
        # it cannot be found in the mask, and the steering point is the cluster centre
        # moved down by `red_mass_aim_offset_px`. The rule this replaced looked for a
        # marker 162-287 px ABOVE the label and otherwise moved the label up 225 px,
        # which put the point about 325 px above the aircraft (no archived frame has a
        # marker there); the mean-of-red rule before it landed about 85 px above.
        self._aim_offset = int(cfg.get("red_mass_aim_offset_px", 100))
        # Same date: the strict gate (red_mass_nameplate_min_glyphs) decides
        # acquisition only. A lock is kept on a looser test near where it was:
        # a cluster of at least `keep_min_glyphs` glyphs within `keep_box_pct`
        # (half width, half height, frame fractions) of the previous lock's
        # cluster. Measured (07:34-08:56 logs and after): 11 of 18 acquisitions
        # were lost on the very next scan, lock ticks sat at a median 25 glyphs
        # with 46% within 4 of the threshold, and cut or partly drawn real
        # labels read 6-19 glyphs while junk never exceeded 7 (8-9 on two
        # frames). The box is the reach the 0.22-scale local ROI gave around a
        # lock (+-211 x +-132 px at 1920x1200), without an edge to cut the label.
        self._keep_min_glyphs = int(cfg.get("red_mass_keep_min_glyphs", 8))
        _kb = cfg.get("red_mass_keep_box_pct", [0.11, 0.11])
        self._keep_box_pct = (float(_kb[0]), float(_kb[1]))
        # Own-afterburner exclusion for the above (2026-09-23): unlike "NO
        # LOCK" above, the afterburner glow is not fixed screen position (it
        # moves with the airframe's on-screen attitude), so a region-based
        # exclusion doesn't work here — but its color reliably does. Measured
        # directly (game_battle_eject_20260923_123703-05_35/36/37.png):
        # afterburner glow sits at hue 7-10 (orange, bordering true orange),
        # the real target icon at hue 3-5 (pure red) — non-overlapping in
        # bulk, unlike per-blob pixel area, which was checked first and
        # rejected (270 vs 271, 222 vs 309 px in these same frames — no
        # reliable size threshold exists). Narrows the nameplate mask's upper
        # hue bound. Default 6, the shipped value (CR-018-04);
        # null falls back to tracking_hsv.red_upper's hue.
        red_mass_hue_max = cfg.get("red_mass_hue_max", 6)
        self._red_mass_hue_max = int(red_mass_hue_max) if red_mass_hue_max is not None else None
        # Afterburner-rim exclusion for the above (2026-09-23): the hue cutoff
        # above targets the flame's orange bulk, but its outer rim fades
        # through the same red hue band the real target uses (measured:
        # game_battle_eject_20260923_171137-39_0/1/2.png) — hue alone can't
        # separate a gradient's edge from a flat color. Brightness can: the
        # real target is a flat-shaded icon (value ~255 on ~86% of its
        # qualifying pixels); the flame's fading rim only reaches value
        # >=245 on 9-11% of its qualifying pixels, the rest being visibly
        # dimmer as it fades to background. Raises the nameplate mask's value
        # floor. Default 245, the shipped value (CR-018-04); null falls back to
        # tracking_hsv.red_lower's value (150).
        red_mass_value_min = cfg.get("red_mass_value_min", 245)
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
        # small glyph-shaped components in the same red mask
        # (no OCR, no new color sample) measured 33 such
        # components for the real target vs 8-9 for each false positive in
        # the three frames above — a roughly 4x gap, but from 3 frames in one
        # engagement. Always on since CR-018-04, counted per cluster.
        self._red_mass_nameplate_min_glyphs = int(
            cfg.get("red_mass_nameplate_min_glyphs", 20))
        glyph_area = cfg.get("red_mass_nameplate_glyph_area", [10, 200])
        self._red_mass_nameplate_glyph_area = (int(glyph_area[0]), int(glyph_area[1]))
        self._red_mass_nameplate_glyph_max_dim = int(
            cfg.get("red_mass_nameplate_glyph_max_dim", 25))
        hsv = config.get("tracking_hsv", {})
        self._red_lower = np.array(hsv.get("red_lower", [0, 150, 150]), dtype=np.uint8)
        self._red_upper = np.array(hsv.get("red_upper", [10, 255, 255]), dtype=np.uint8)

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
        # Cluster mode only: the locked nameplate cluster's centre (absolute
        # frame coords), the anchor for keeping and re-choosing the same label.
        # Kept apart from _last_x/_last_y, which are the point steered on.
        self._last_cluster: "tuple[float, float] | None" = None
        self._last_seen_ts: float = 0.0
        # CR-018-05: serialises update()/reset() across the three callers.
        self._lock = threading.Lock()
        self._last_obs: "dict | None" = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def mode(self) -> TrackMode:
        return self._mode

    def reset(self) -> None:
        """Clear all tracking state. Call when leaving GAME_BATTLE."""
        if not self._lock.acquire(timeout=self._LOCK_TIMEOUT_S):
            logger.warning("TargetTracker: reset skipped — lock busy for %.2fs",
                           self._LOCK_TIMEOUT_S)
            return
        try:
            self._reset_locked()
        finally:
            if self._lock.locked():
                self._lock.release()

    def _reset_locked(self) -> None:
        self._last_obs = None
        self._mode = TrackMode.SEARCHING
        self._last_x = None
        self._last_y = None
        self._last_cluster = None
        self._last_seen_ts = 0.0
        logger.debug("TargetTracker: reset to SEARCHING")

    def update(self, frame: np.ndarray, ts: "float | None" = None) -> dict:
        """Process one frame under the tracker lock; see `_update_locked`.

        On lock timeout (another thread mid-scan) returns a copy of the last
        observation, or a not-visible one if there is none, and logs a warning.
        """
        if not self._lock.acquire(timeout=self._LOCK_TIMEOUT_S):
            logger.warning("TargetTracker: update skipped — lock busy for %.2fs, "
                           "returning the last observation", self._LOCK_TIMEOUT_S)
            if self._last_obs is not None:
                return dict(self._last_obs)
            return {"mode": self._mode.name, "visible": False,
                    "centroid_x": None, "centroid_y": None,
                    "error_norm": None, "error_norm_y": None}
        try:
            obs = self._update_locked(frame, ts)
            self._last_obs = obs
            return dict(obs)
        finally:
            if self._lock.locked():
                self._lock.release()

    def _update_locked(self, frame: np.ndarray, ts: "float | None" = None) -> dict:
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

        `centroid_x`/`centroid_y` (and so the errors) are the aircraft marker
        below the chosen nameplate (see `_probe_by_cluster`).

        Every tick scans the whole acquisition region. A missed target stays in
        LOST_GRACE, its last position reported, for as long as
        Controller.roll_on_miss waits for it (see `_memory_s_for`).
        """
        if ts is None:
            ts = time.time()
        h, w = frame.shape[:2]

        if self._mode == TrackMode.SEARCHING:
            self._mode = TrackMode.ACQUIRING

        # Every tick scans the whole acquisition region (2026-09-24). Once
        # locked, a 422 x 264 local ROI around the last lock used to be scanned
        # instead, but that is smaller than a nameplate plus the marker below
        # it, so a target drifting between scans had its label cut by the ROI
        # edge and failed the gate: 125 of 182 measured lock drops had red
        # present and a gate rejection, and every archived drop crop replayed
        # showed a cut label. Continuity comes from choosing, and keeping, the
        # cluster nearest the previous lock instead; the wide scan costs about
        # 9.5 ms against the 0.33 s pursuit loop.
        ax1 = int(w * self._acq_x1)
        ay1 = int(h * self._acq_y1)
        ax2 = int(w * self._acq_x2)
        ay2 = int(h * self._acq_y2)
        crop = frame[ay1:ay2, ax1:ax2]
        ox, oy = ax1, ay1

        # Keep, or re-choose, the nameplate nearest the previous lock's
        # cluster while the target is remembered; otherwise pick the one
        # nearest the screen centre (`ref=None`). A lock only the looser keep
        # test allowed is logged as path=keep, so path=redmass still means the
        # strict gate passed.
        ref = (self._last_cluster
               if self._mode in (TrackMode.TRACKING, TrackMode.LOST_GRACE)
               else None)
        rm_probe = self._red_mass_probe(crop, ox, oy, w, h, ref=ref)
        selected = None
        pick_path = "none"
        if rm_probe["centroid"] is not None:
            selected = (ox + rm_probe["centroid"][0], oy + rm_probe["centroid"][1])
            pick_path = "keep" if rm_probe["kept"] else "redmass"
        self._log_pick_path(pick_path, selected, rm_probe)

        if selected is not None:
            abs_x, abs_y = selected
            self._last_x = abs_x
            self._last_y = abs_y
            self._last_cluster = rm_probe["cluster"]
            self._last_seen_ts = ts
            if self._mode in (TrackMode.ACQUIRING, TrackMode.LOST_GRACE):
                self._mode = TrackMode.TRACKING
                logger.debug("TargetTracker: acquired target at (%.0f, %.0f)", abs_x, abs_y)
        else:
            self._handle_miss(ts, w)

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

    def _handle_miss(self, ts: float, w: int) -> None:
        if self._mode == TrackMode.TRACKING:
            self._mode = TrackMode.LOST_GRACE
        elif self._mode == TrackMode.LOST_GRACE:
            elapsed = ts - self._last_seen_ts
            if elapsed >= self._memory_s_for(w):
                logger.debug("TargetTracker: grace timeout after %.2fs — ACQUIRING", elapsed)
                self._mode = TrackMode.ACQUIRING
                self._last_x = None
                self._last_y = None
                self._last_cluster = None

    def _memory_s_for(self, w: int) -> float:
        """How long LOST_GRACE remembers the target: Controller.roll_on_miss's
        own rule, on its own config — the centre delay (when longer) if the
        last visible horizontal error was within the centre band, else the
        resume delay."""
        if self._last_x is not None:
            last_err = abs((self._last_x - w / 2.0) / (w / 2.0))
            if last_err <= self._memory_centre_err:
                return max(self._memory_s, self._memory_centre_s)
        return self._memory_s

    def _log_pick_path(
        self,
        pick_path: str,
        selected: "tuple[float, float] | None",
        rm_probe: dict,
    ) -> None:
        """Action item 001, Open Question 1: one greppable DEBUG line per tick
        saying which path supplied the lock — `redmass`, `keep` (a lock held on
        the looser keep test only) or `none` — plus what the gate saw. Pure
        logging. CR-018-04 dropped the tall-bar fields (`tall=`, `n_tall=`,
        `red_won=`) and the `suppressed` path with the detector they described;
        `scripts/session-report.py` reads old and new lines alike.
        """
        glyphs = rm_probe["glyphs"] if rm_probe["glyphs"] is not None else "-"
        edges = "+".join(rm_probe["clipped_edges"]) or "-"
        blob = rm_probe.get("blob")
        blob_desc = "(%d,%d,a%d,%dx%d)" % blob if blob is not None else "-"
        clu_desc = rm_probe["clusters"] if rm_probe.get("clusters") is not None else "-"
        sel_desc = f"({selected[0]:.0f},{selected[1]:.0f})" if selected is not None else "-"
        logger.debug(
            "TRACKPICK: path=%s sel=%s gate=%s glyphs=%s rm_px=%d edges=%s blob=%s clu=%s",
            pick_path, sel_desc, rm_probe["gate"], glyphs, rm_probe["px"],
            edges, blob_desc, clu_desc,
        )

    def _red_mass_probe(
        self, crop: np.ndarray, ox: int, oy: int, frame_w: int, frame_h: int,
        ref: "tuple[float, float] | None" = None,
    ) -> dict:
        """Build the red mask for this crop and hand it to `_probe_by_cluster`.

        The mask is the red HSV range (tracking_hsv.red_lower/red_upper, the
        upper hue narrowed by `red_mass_hue_max` and the value floor raised by
        `red_mass_value_min`, plus the [170, 180] wrap-around) minus the fixed
        HUD zones (`red_mass_exclude_pct`, `red_mass_exclude_zones_pct`, in
        full-frame fractions, hence `ox`/`oy`/`frame_w`/`frame_h`).

        Returns a dict (action item 001) so the caller can log what the gate
        saw: `centroid` — the crop-local steering point or None; `gate` —
        "pass", "keep" (only the looser keep test passed) or "reject" (no
        nameplate found); "off" only when the crop had no usable pixels;
        `glyphs` — the count compared; `px` — pixels in the final mask;
        `cluster` — the chosen nameplate's centre, absolute; `kept`; and the
        logging-only `mass_centroid`, `clipped_edges`, `blob`, `clusters`.
        `ref` is the previous lock's cluster centre, absolute, or None.
        """
        probe: dict = {"centroid": None, "gate": "off", "glyphs": None, "px": 0,
                       "mass_centroid": None, "clipped_edges": (), "blob": None,
                       "clusters": None, "cluster": None, "kept": False}
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
            # Reported whether or not the gate passes (TRACKPICK's `edges=`
            # field reads clipped_edges). `centroid` stays gate-owned.
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
        return self._probe_by_cluster(mask, probe, ox, oy, frame_w, frame_h, ref)

    def _probe_by_cluster(
        self, mask: np.ndarray, probe: dict, ox: int, oy: int,
        frame_w: int, frame_h: int, ref: "tuple[float, float] | None",
    ) -> dict:
        """The gate and steering point of `_red_mass_probe` in cluster mode.

        Acquire strictly, keep loosely: with a previous lock (`ref`, absolute),
        the cluster nearest it with at least `red_mass_keep_min_glyphs` glyphs
        inside the keep box holds the lock (gate "keep" when only that looser
        test passed, "pass" when the strict one did too). Otherwise the strict
        gate decides, per cluster, and the eligible cluster nearest `ref`, or
        the screen centre, is acquired. `glyphs` is the chosen cluster's count,
        or on a rejection the best cluster's, so a rejected tick still says how
        close it came. The steering point is the aircraft, `red_mass_aim_offset_px`
        below that nameplate's glyph centre (see `__init__`), not the label."""
        clusters = self._nameplate_clusters(mask)
        best = max((c[2] for c in clusters), default=0)
        eligible = [c for c in clusters if c[2] >= self._red_mass_nameplate_min_glyphs]
        probe["glyphs"] = best
        probe["clusters"] = len(eligible)
        chosen = None
        if ref is not None:
            rx, ry = ref[0] - ox, ref[1] - oy
            box_x = self._keep_box_pct[0] * frame_w
            box_y = self._keep_box_pct[1] * frame_h
            near = [c for c in clusters
                    if c[2] >= self._keep_min_glyphs
                    and abs(c[0] - rx) <= box_x and abs(c[1] - ry) <= box_y]
            if near:
                chosen = min(near, key=lambda c: (c[0] - rx) ** 2 + (c[1] - ry) ** 2)
        if chosen is None:
            if not eligible:
                probe["gate"] = "reject"
                return probe
            rx, ry = ref if ref is not None else (frame_w / 2.0, frame_h / 2.0)
            rx, ry = rx - ox, ry - oy
            chosen = min(eligible, key=lambda c: (c[0] - rx) ** 2 + (c[1] - ry) ** 2)
        cx, cy, n = chosen
        kept = n < self._red_mass_nameplate_min_glyphs
        probe["gate"] = "keep" if kept else "pass"
        probe["kept"] = kept
        probe["glyphs"] = n
        probe["cluster"] = (cx + ox, cy + oy)
        probe["centroid"] = (float(cx), float(cy + self._aim_offset))
        return probe

    def _nameplate_clusters(self, mask: np.ndarray, max_clusters: int = 8,
                            components=None) -> "list[tuple[float, float, int]]":
        """Groups of glyph-sized red components that sit close together, as
        (centre x, centre y, glyph count) in crop coordinates, densest first.
        A nameplate is dozens of small letters and digits in a block of about
        200 x 110 px; two labels on screen are two blocks. Greedy: take the
        window (`red_mass_cluster_glyph_window_px`) holding the most unclaimed
        glyphs, claim them, repeat. Glyph shape bounds are the gate's own.
        `components` is `mask`'s cv2.connectedComponentsWithStats result when
        the caller already has it."""
        n, _labels, stats, centroids = (
            components if components is not None
            else cv2.connectedComponentsWithStats(mask, connectivity=8))
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
