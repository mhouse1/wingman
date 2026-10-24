"""Periodic annotated screenshot HUD for tracking and game-state telemetry.

Writes to a temp file then atomically replaces the configured output path so
image viewers (VS Code, eog) never see a partially-written frame.

Usage:
    renderer = HudRenderer.from_config(cfg)
    # in main loop:
    renderer.maybe_render(frame, tracking_obs, state_name, health, missiles, flares)
"""

import atexit
import collections
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

import cv2
import numpy as np

from . import capture_budget
from .config_schema import schema_default
from .depth_view import MID_BOX, VIEW_BOX
from .terrain_loom import fmt_tau

logger = logging.getLogger(__name__)

_FONT = cv2.FONT_HERSHEY_SIMPLEX
_GREEN = (0, 220, 0)
_RED = (0, 60, 255)
_YELLOW = (0, 210, 255)
_CYAN = (220, 210, 0)
_WHITE = (240, 240, 240)
_DARK = (10, 10, 10)
_GREY = (140, 140, 140)
# Reserved exclusively for the pursued-target highlight below — must not
# collide with a color already meaningful in-frame: the game's own HUD uses
# green (unlocked enemy marker), red/orange (locked marker), yellow
# (proximity dot), and blue (edge indicator), and this overlay already uses
# green/yellow/cyan/grey for other elements above. Magenta is the one color
# neither palette claims.
_PURSUIT = (255, 60, 220)

# game_state_name values that mean "target tracking is running during a
# secondary-missile encounter" — the archive below exists specifically for
# this window. GAME_BATTLE_EJECT is ADR 136's live heatdive loop; PURSUIT_MODE
# is HLDD 015's not-yet-built alternative, named here so the archive needs no
# further change if that ever ships.
_TARGET_TRACKING_ARCHIVE_STATES = frozenset({"GAME_BATTLE_EJECT", "PURSUIT_MODE"})

# HLDD 001 Phase 1 overlay: a terrain reading older than this is drawn grey.
# Two main-loop ticks (1.5 s each) plus slack — the pursuit loops render far
# more often than the tick that takes the reading.
_TERRAIN_STALE_S = 4.0
# Brightness left on pixels the looming colour mask excluded as HUD.
_LOOM_MASKED_DIM = 0.3
# Tint strength for the pixels the sky test accepted.
_TERRAIN_TINT_ALPHA = 0.35

# Text is drawn after the frame is halved for live_hud.png (Code Review 019,
# CR-019-09): drawn before, the status lines came out about 6 px tall. Scales
# are for the halved picture of a frame this wide; positions are full-frame
# pixels. The status strip's rows, in full-frame pixels:
_TEXT_REF_WIDTH = 1920
_ROW0_PX = 32
_ROW_PX = 36
# The clear-path layer (Code Review 019 step 3a): brightness left outside the
# analysed view, tint strength on the free air inside it, and the colours of
# outlines by time to contact.
_PATH_OUTSIDE_DIM = 0.6
_PATH_TINT_ALPHA = 0.22
_PATH_CLOSE_S = 4.0
_AMBER = (0, 170, 255)
# An outline bigger than this share of the analysed view gets no figure of its
# own (the ground filling the view); the status line gives the nearest figure.
_PATH_LABEL_MAX_FRAC = 0.4
# Right edge of the status strip's text, full-frame pixels: a figure that would
# land under the strip is moved below it.
_PATH_STRIP_RIGHT = 760
# How much a status line's backing panel darkens the picture under it.
_PANEL_ALPHA = 0.55
# The planner's banner, by mode (Code Review 019 step 3d).
_PLAN_COLOURS = {"DIRECT": _GREEN, "DETOUR": _AMBER, "ESCAPE": _RED, "BLIND": _GREY}


def _row(i: int) -> int:
    """Baseline of status-strip row `i`, in full-frame pixels."""
    return _ROW0_PX + _ROW_PX * i


def _txt(canvas: np.ndarray, text: str, x: int, y: int,
         color=_WHITE, scale: float = 0.52, thick: int = 1) -> None:
    cv2.putText(canvas, text, (x, y), _FONT, scale, _DARK, thick + 2, cv2.LINE_AA)
    cv2.putText(canvas, text, (x, y), _FONT, scale, color, thick, cv2.LINE_AA)


class HudRenderer:
    """Render and atomic-write an annotated game-frame snapshot on a cadence."""

    def __init__(self, output_path: str, interval_sec: float = 1.0,
                 feh_geometry: str = "",
                 acquisition_region_pct: "tuple[float, float, float, float]" = (0.20, 0.18, 0.80, 0.68),
                 archive_enabled: bool = False,
                 archive_dir: str = "tests/test-output/target_tracking",
                 archive_max_files: int = 200,
                 archive_save_raw_scan: bool = False,
                 archive_min_interval_s: float = 0.0,
                 archive_max_per_encounter: int = 0,
                 black_box_enabled: bool = False,
                 black_box_dir: str = "test_screenshots/path_blackbox",
                 black_box_seconds: float = 18.0,
                 black_box_rate_hz: float = 3.0,
                 black_box_alarm_max: int = 0,
                 black_box_alarm_gap_s: float = 30.0,
                 black_box_alarm_seconds: float = 4.0,
                 black_box_raw: bool = False) -> None:
        self._output = Path(output_path)
        self._interval = float(interval_sec)
        self._last_ts: float = 0.0
        self._acq_pct = tuple(float(v) for v in acquisition_region_pct)
        self._render_lock = threading.Lock()
        # Timestamped archive of the annotated frame while target tracking
        # runs during a secondary-missile encounter (see
        # _TARGET_TRACKING_ARCHIVE_STATES) — live_hud.png itself is
        # overwritten every render, so this is what actually lets a saved
        # frame be lined up against a wingman.log timestamp for debugging.
        # Capped per session, same shape as AmmoEventsHandler
        # ._capture_crash_frame's max_per_session (ADR 137 D5).
        self._archive_enabled = bool(archive_enabled)
        self._archive_dir = Path(archive_dir)
        self._archive_max = int(archive_max_files)
        self._archive_count = 0
        # Action item 001: also save the exact, unannotated crop the tracker
        # scanned beside each archived frame. The annotated PNG cannot stand
        # in for it — the PURSUING marker is drawn on the very pixels that
        # produced the lock — and replaying the real detector against
        # overwritten pixels is what made earlier static reconstructions
        # contradict the live log. Off unless configured.
        self._archive_save_raw_scan = bool(archive_save_raw_scan)
        # 2026-09-24: archiving every render spent the whole session cap in
        # the first 6-10 minutes. Throttle, and cap each contiguous
        # eject/pursuit encounter (0 = uncapped) so later encounters still
        # get frames. The encounter counter resets on the first render
        # outside _TARGET_TRACKING_ARCHIVE_STATES.
        self._archive_min_interval = float(archive_min_interval_s)
        self._archive_max_per_encounter = int(archive_max_per_encounter)
        self._archive_encounter_count = 0
        self._archive_last_ts: "float | None" = None
        # Handle to the feh child process below, so close() has something to
        # terminate on shutdown (2026-09-23: previously discarded right after
        # Popen() returned, which is why the window used to survive wingman
        # exiting — nothing ever held a reference to kill it).
        self._feh_process: "subprocess.Popen | None" = None
        # HLDD 001 Phase 1 overlay. Off until set_terrain_source() wires it.
        # _terrain_reading is replaced whole by the main loop and read whole
        # by the render thread, so it needs no lock.
        self._terrain_mask_fn = None
        self._terrain_crop = (0.0, 0.0, 1.0, 1.0)
        self._terrain_sky_min_frac = 0.0
        self._terrain_shadow = True
        self._terrain_reading: "tuple[float | None, bool, float] | None" = None
        # HLDD 001 Phase 2 overlay (shadow). Off until set_loom_source().
        self._loom_path_box: "tuple[float, ...] | None" = None
        self._loom_tau_warn_s = 0.0
        self._loom_open_below = False
        self._loom_open_sides = False
        self._loom_reading = None    # (LoomReading | None, warn, ts), replaced whole
        self._shape_reading = None   # (ShapeReading | None, warn, ts), replaced whole
        # The render's text, drawn after the downscale (_flush_text). Only the
        # render thread touches it, one render at a time (_render_lock).
        self._texts: "list[tuple]" = []
        # Code Review 019 (CR-019-09): the last seconds of annotated frames, kept
        # in memory as JPEG and written out at a death (dump_black_box), so a
        # crash can be watched as wingman saw it. live_hud.png alone is
        # overwritten every second and keeps nothing. Rendered more often than
        # live_hud.png is written, from the same calls.
        self._bb_enabled = bool(black_box_enabled)
        self._bb_dir = Path(black_box_dir)
        self._bb_seconds = max(1.0, float(black_box_seconds))
        self._bb_interval = 1.0 / max(0.2, float(black_box_rate_hz))
        self._bb_last_ts = 0.0
        self._bb_frames: "collections.deque[tuple[float, bytes]]" = collections.deque()
        # Research 017 step 3: the game's own picture beside each annotated
        # frame, at the same size and moment, written to <dir>/raw under the
        # same names. The annotated frames carry the planner's overlay across
        # the analysed box, so they cannot be used to study the picture itself.
        self._bb_raw_enabled = bool(black_box_raw)
        self._bb_raw: "collections.deque[tuple[float, bytes]]" = collections.deque()
        self._bb_lock = threading.Lock()
        # The planner's alarms (Code Review 019, cycle 5): a short clip round
        # each, in a folder of its own so it cannot use up the deaths' budget.
        self._bb_alarm_dir = self._bb_dir / "alarms"
        self._bb_alarm_max = max(0, int(black_box_alarm_max))
        self._bb_alarm_gap_s = max(0.0, float(black_box_alarm_gap_s))
        self._bb_alarm_seconds = max(0.5, float(black_box_alarm_seconds))
        self._bb_alarm_count = 0
        self._bb_alarm_last = -1e9
        if feh_geometry:
            self._launch_feh(feh_geometry)

    def _text(self, text: str, x: int, y: int, color=_WHITE,
              scale: float = 0.45, thick: int = 1, panel: bool = False) -> None:
        """Queue a line of text: `x`, `y` in full-frame pixels, `scale` for the
        halved picture (module comment above). `panel` puts a translucent dark
        backing under it, for the status strip, which lies over the picture."""
        self._texts.append((text, x, y, color, scale, thick, panel))

    def _flush_text(self, img: np.ndarray, factor: float, frame_width: int) -> None:
        """Draw the queued text on `img`, which is the frame scaled by `factor`."""
        size = 2.0 * factor * min(2.0, max(0.25, frame_width / _TEXT_REF_WIDTH))
        for text, x, y, color, scale, thick, panel in self._texts:
            px, py = int(x * factor), int(y * factor)
            font_scale = scale * size
            weight = max(1, int(round(thick * 2.0 * factor)))
            if panel:
                (tw, th), base = cv2.getTextSize(text, _FONT, font_scale, weight + 2)
                x1, y1 = max(0, px - 3), max(0, py - th - 3)
                x2, y2 = min(img.shape[1], px + tw + 3), min(img.shape[0], py + base + 1)
                if x2 > x1 and y2 > y1:
                    roi = img[y1:y2, x1:x2]
                    img[y1:y2, x1:x2] = (roi * (1.0 - _PANEL_ALPHA)).astype(np.uint8)
            _txt(img, text, px, py, color, font_scale, weight)

    def _launch_feh(self, geometry: str) -> None:
        try:
            self._feh_process = subprocess.Popen(
                ["feh", "--reload", "1", "--zoom", "fill", "--geometry", geometry, str(self._output)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info("HudRenderer: feh launched (%s)", geometry)
            # Backstop for every exit that skips the shutdown sequence's
            # close(). main() builds the renderer well before its main-loop
            # try/finally, so an exception in between orphaned the window
            # (2026-10-02: three feh windows left open by crashed replay
            # tests). close() is idempotent and unregisters this itself.
            atexit.register(self.close)
        except FileNotFoundError:
            logger.warning("HudRenderer: feh not found — install with: sudo apt install feh")

    def close(self) -> None:
        """Terminate the feh window this renderer launched, if any.

        Call once from the main shutdown sequence — never automatically,
        since a renderer with no feh_geometry (tracking disabled) never
        launches one and this is then just a no-op. SIGTERM first, same
        grace-then-force shape close_game() uses for the game process
        itself, since feh does not always exit promptly on terminate()
        alone under Xwayland (observed live, 2026-09-23).
        """
        proc = self._feh_process
        if proc is not None:
            atexit.unregister(self.close)
        if proc is None or proc.poll() is not None:
            return  # never launched, or already exited on its own
        try:
            proc.terminate()
            proc.wait(timeout=2.0)
            logger.info("HudRenderer: feh closed")
        except subprocess.TimeoutExpired:
            logger.warning("HudRenderer: feh did not exit within 2.0s — killing")
            proc.kill()
            proc.wait(timeout=2.0)
        except Exception as e:
            logger.warning("HudRenderer: feh close failed (%s: %s)", type(e).__name__, e)

    def set_terrain_source(self, mask_fn, crop, sky_min_frac: float,
                           shadow: bool) -> None:
        """Turn on the terrain overlay (HLDD 001 Phase 1).

        `mask_fn(frame)` is the analyzer's own `terrain_sky_mask`, so the tint
        shows the pixels the detector counted and not a second implementation
        of the sky test. `crop` is the TERRAIN_FORWARD crop the mask covers
        (fractions, x before y). `sky_min_frac` and `shadow` are the trigger's
        settings, for the status line.
        """
        self._terrain_mask_fn = mask_fn
        self._terrain_crop = tuple(float(v) for v in crop[:4])
        self._terrain_sky_min_frac = float(sky_min_frac)
        self._terrain_shadow = bool(shadow)

    def set_terrain_reading(self, sky_frac: "float | None", ahead: bool,
                            ts: float) -> None:
        """Record this tick's terrain reading for whichever caller renders next.

        `sky_frac` is None when the tick took no reading (padlock camera not
        confirmed forward, or a perception gap); the overlay says so instead
        of repeating an old number.
        """
        self._terrain_reading = (sky_frac, bool(ahead), float(ts))

    def set_loom_source(self, path_box_pct, tau_warn_s: float,
                        open_below: bool = False, open_sides: bool = False) -> None:
        """Turn on the looming overlay (HLDD 001 Phase 2, shadow)."""
        self._loom_path_box = tuple(float(v) for v in path_box_pct[:4])
        self._loom_tau_warn_s = float(tau_warn_s)
        self._loom_open_below = bool(open_below)
        self._loom_open_sides = bool(open_sides)

    def set_loom_reading(self, reading, warn: bool, ts: float) -> None:
        """Record this tick's looming reading; None when the tick took none."""
        self._loom_reading = (reading, bool(warn), float(ts))

    def set_shape_reading(self, reading, warn: bool, ts: float) -> None:
        """Record this tick's shapes; None when the tick took none."""
        self._shape_reading = (reading, bool(warn), float(ts))

    def _draw_shapes(self, canvas: np.ndarray, ts: float) -> None:
        """The outline around each group of dots, the threat's in colour, and
        an arrow for the way out.

        Like the dots, the outlines belong to the frames that were measured,
        so a stale reading draws only its status line.
        """
        entry = self._shape_reading
        if entry is None:
            return
        reading, warn, shape_ts = entry
        h, w = canvas.shape[:2]
        age = ts - shape_ts
        if age > _TERRAIN_STALE_S:
            self._text(f"Shape[SHADOW]: stale {age:.0f}s", 8, _row(5), _GREY, panel=True)
            return
        if reading is None or not reading.readable:
            why = "no reading" if reading is None else f"no reading ({reading.status})"
            self._text(f"Shape[SHADOW]: {why}", 8, _row(5), _GREY, panel=True)
            return
        threat = reading.threat
        for shape in reading.shapes:
            if shape is threat:
                continue
            pts = shape.outline.astype(np.int32).reshape(-1, 1, 2)
            cv2.polylines(canvas, [pts], True, _DARK, 3, cv2.LINE_AA)
            cv2.polylines(canvas, [pts], True, _WHITE, 1, cv2.LINE_AA)
        if threat is None:
            if reading.blind:
                self._text(f"Shape[SHADOW]: {len(reading.shapes)} shapes, the middle is "
                           f"covered and its growth is unknown", 8, _row(5), _GREY,
                           panel=True)
            else:
                self._text(f"Shape[SHADOW]: {len(reading.shapes)} shapes, none growing "
                           f"in the middle", 8, _row(5), _GREEN, panel=True)
            return
        color = _RED if warn else _YELLOW
        pts = threat.outline.astype(np.int32).reshape(-1, 1, 2)
        cv2.polylines(canvas, [pts], True, _DARK, 6, cv2.LINE_AA)
        cv2.polylines(canvas, [pts], True, color, 3, cv2.LINE_AA)
        cx, cy = w // 2, h // 2
        reach = h // 6
        tip = {"left": (cx - reach, cy), "right": (cx + reach, cy),
               "up": (cx, cy - reach)}[reading.avoid]
        cv2.arrowedLine(canvas, (cx, cy), tip, _DARK, 8, cv2.LINE_AA, tipLength=0.35)
        cv2.arrowedLine(canvas, (cx, cy), tip, color, 4, cv2.LINE_AA, tipLength=0.35)
        self._text(f"Shape[SHADOW]: {len(reading.shapes)} shapes, contact "
                   f"{threat.tau:.1f}s, {threat.dots} dots, way out "
                   f"{reading.avoid.upper()}{'  CONFIRMED' if warn else ''}",
                   8, _row(5), color, panel=True)

    def _draw_loom(self, canvas: np.ndarray, ts: float) -> None:
        """Flight-path box, expansion point and time to contact.

        The tracked points are not drawn (operator, 2026-10-15: they drew white
        dots nothing used). `LoomReading.points` stays: the tests use it to
        check that the colour mask keeps HUD strokes out of the tracking.
        """
        if self._loom_path_box is None:
            return
        h, w = canvas.shape[:2]
        entry = self._loom_reading
        age = None if entry is None else ts - entry[2]
        reading = None if entry is None else entry[0]
        if entry is None or age > _TERRAIN_STALE_S:
            color = _GREY
            text = "no reading yet" if entry is None else f"stale {age:.0f}s"
            reading = None
        elif reading is None:
            color, text = _GREY, "no reading"
        elif not reading.readable:
            color, text = _GREY, f"no reading ({reading.status})"
        elif reading.tau is None:
            color, text = _GREEN, f"not closing  pts {reading.inliers}/{reading.tracked}"
        else:
            closing = reading.tau < self._loom_tau_warn_s and reading.on_course
            color = _RED if entry[1] else (_YELLOW if closing else _GREEN)
            text = (f"contact {reading.tau:.1f}s  "
                    f"{'on course' if reading.on_course else 'passing'}  "
                    f"pts {reading.inliers}/{reading.tracked}"
                    f"{'  TERRAIN CLOSING' if entry[1] else ''}")
        if reading is not None and reading.hud_mask is not None:
            # What the colour mask left out of the tracking, darkened: the HUD
            # strokes it took for HUD, and any terrain it took by mistake.
            ox, oy = reading.hud_origin
            mh = int(round(reading.hud_mask.shape[0] / reading.hud_scale))
            mw = int(round(reading.hud_mask.shape[1] / reading.hud_scale))
            roi = canvas[oy:oy + mh, ox:ox + mw]
            masked = cv2.resize(reading.hud_mask, (roi.shape[1], roi.shape[0]),
                                interpolation=cv2.INTER_NEAREST) > 0
            roi[masked] = (roi[masked] * _LOOM_MASKED_DIM).astype(np.uint8)
            text += f"  hud {100.0 * reading.hud_frac:.1f}%"
        self._text(f"Loom[SHADOW]: {text}", 8, _row(4), color, panel=True)
        x1, y1, x2, y2 = self._loom_path_box
        left, top, right, bottom = int(w * x1), int(h * y1), int(w * x2), int(h * y2)
        if self._loom_open_below and self._loom_open_sides:
            # Only the top edge is left: a line across the frame, with the old
            # box's corners marked on it. Below the line is on course.
            cv2.line(canvas, (0, top), (w - 1, top), color, 2)
            cv2.line(canvas, (left, top), (left, top + 12), color, 2)
            cv2.line(canvas, (right, top), (right, top + 12), color, 2)
        elif self._loom_open_below:
            # No bottom edge: the sides run to the foot of the frame, with a
            # tick where the closed box used to end.
            cv2.line(canvas, (left, top), (right, top), color, 2)
            cv2.line(canvas, (left, top), (left, h - 1), color, 2)
            cv2.line(canvas, (right, top), (right, h - 1), color, 2)
            cv2.line(canvas, (left, bottom), (left + 12, bottom), color, 2)
            cv2.line(canvas, (right - 12, bottom), (right, bottom), color, 2)
        else:
            cv2.rectangle(canvas, (left, top), (right, bottom), color, 2)
        self._text("path", left + 4, bottom - 6, color, scale=0.34)
        if reading is None or not reading.readable:
            return
        if reading.fixed is not None and reading.tau is not None:
            fx, fy = int(reading.fixed[0]), int(reading.fixed[1])
            if 0 <= fx < w and 0 <= fy < h:
                cv2.circle(canvas, (fx, fy), 14, _DARK, 4, cv2.LINE_AA)
                cv2.circle(canvas, (fx, fy), 14, color, 2, cv2.LINE_AA)
                cv2.drawMarker(canvas, (fx, fy), color, cv2.MARKER_TILTED_CROSS,
                               14, 2, cv2.LINE_AA)

    def _draw_terrain(self, canvas: np.ndarray, frame: np.ndarray, ts: float) -> None:
        """Box, sky tint and status line for the forward sky-occlusion detector.

        Two numbers, kept apart on purpose: `sky` is what the detector read
        on its last tick, `view` is the same test on the frame drawn here.
        They differ when the reading is a tick old or was skipped.
        """
        if self._terrain_mask_fn is None:
            return
        mask = self._terrain_mask_fn(frame)
        if mask is None:
            return
        h, w = canvas.shape[:2]
        mh, mw = mask.shape[:2]
        # Same truncation as crop_region.get_crop, which cut the mask.
        x1, y1 = int(w * self._terrain_crop[0]), int(h * self._terrain_crop[1])
        roi = canvas[y1:y1 + mh, x1:x1 + mw]
        sky = mask > 0
        tint = np.empty_like(roi)
        tint[:] = _GREEN
        blended = cv2.addWeighted(roi, 1.0 - _TERRAIN_TINT_ALPHA, tint,
                                  _TERRAIN_TINT_ALPHA, 0.0)
        roi[sky] = blended[sky]
        view = float(np.count_nonzero(mask)) / float(mask.size)

        reading = self._terrain_reading
        thr = self._terrain_sky_min_frac
        mode = "SHADOW" if self._terrain_shadow else "ACTIVE"
        age = None if reading is None else ts - reading[2]
        if reading is None or age > _TERRAIN_STALE_S:
            color = _GREY
            text = "no reading yet" if reading is None else f"stale {age:.0f}s"
        elif reading[0] is None:
            color = _GREY
            text = "no reading"
        else:
            sky_frac, ahead, _ = reading
            if ahead:
                color, text = _RED, f"sky {sky_frac:.2f}  TERRAIN AHEAD"
            elif sky_frac < thr:
                color, text = _YELLOW, f"sky {sky_frac:.2f}  low"
            else:
                color, text = _GREEN, f"sky {sky_frac:.2f}  clear"
        self._text(f"Terrain[{mode}]: {text}  view {view:.2f}  min {thr:.2f}",
                   8, _row(3), color, panel=True)
        cv2.rectangle(canvas, (x1, y1), (x1 + mw, y1 + mh), _DARK, 4)
        cv2.rectangle(canvas, (x1, y1), (x1 + mw, y1 + mh), color, 2)
        self._text("terrain", x1 + 4, y1 + mh - 6, color, scale=0.34)

    def _draw_plan(self, canvas: np.ndarray, plan, warn_s: float = 8.0) -> None:
        """The planner's corridor (Code Review 019 step 3d): a translucent band
        from where the aircraft is going to where it would aim, the aim marked,
        the goal ringed, and the straight line to the goal in red when the
        plan does not take it. Beside the ring, how soon the surface behind
        the goal is closing, when it is."""
        ox, oy = (int(v) for v in plan.origin)
        ax, ay = (int(v) for v in plan.aim)
        gx, gy = int(plan.goal.x), int(plan.goal.y)
        if plan.mode in ("DETOUR", "ESCAPE"):
            cv2.line(canvas, (ox, oy), (gx, gy), _DARK, 5, cv2.LINE_AA)
            cv2.line(canvas, (ox, oy), (gx, gy), _RED, 2, cv2.LINE_AA)
        band = canvas.copy()
        cv2.line(band, (ox, oy), (ax, ay), _PURSUIT, 26, cv2.LINE_AA)
        cv2.addWeighted(band, 0.35, canvas, 0.65, 0.0, dst=canvas)
        cv2.line(canvas, (ox, oy), (ax, ay), _PURSUIT, 2, cv2.LINE_AA)
        cv2.drawMarker(canvas, (ax, ay), _DARK, cv2.MARKER_CROSS, 36, 7, cv2.LINE_AA)
        cv2.drawMarker(canvas, (ax, ay), _PURSUIT, cv2.MARKER_CROSS, 36, 3, cv2.LINE_AA)
        cv2.circle(canvas, (gx, gy), 24, _DARK, 5, cv2.LINE_AA)
        cv2.circle(canvas, (gx, gy), 24, _YELLOW, 2, cv2.LINE_AA)
        if plan.mode != "DIRECT":
            self._text("aim", ax + 22, ay - 12, _PURSUIT, scale=0.4, thick=2)
        wall = getattr(plan, "wall_tau", None)
        local = getattr(plan, "wall_local_tau", None)
        if local is not None and local >= 2.0 * warn_s:
            local = None
        parts = ([] if wall is None else [f"wall {wall:.1f}s"]) + (
            [] if local is None else [f"local {local:.1f}s"])
        if parts:
            nearest = min(v for v in (wall, local) if v is not None)
            self._text("  ".join(parts), gx + 30, gy + 22,
                       self._tau_colour(nearest, warn_s, True), scale=0.45, thick=2)

    @staticmethod
    def _tau_colour(tau: "float | None", warn_s: float, measured: bool):
        """Red under `_PATH_CLOSE_S`, amber under `warn_s`, white when measured
        and not closing (or far), grey when there was nothing to measure."""
        if tau is not None and tau < _PATH_CLOSE_S:
            return _RED
        if tau is not None and tau < warn_s:
            return _AMBER
        return _WHITE if measured else _GREY

    def _draw_path_view(self, canvas: np.ndarray, view) -> None:
        """What the pursuit's perception saw on this very picture (Code Review
        019 step 3a): free air tinted inside the analysed view, everything
        outside it dimmed, each outline coloured by its time to contact, the
        point the picture expands from, and one status line.

        `view` is a `clear_path.PathView` taken on the frame being drawn, so
        unlike the main tick's overlays nothing here can be from another
        picture. Shadow: it says what was seen, not what was done.
        """
        h, w = canvas.shape[:2]
        shape = view.shape
        if shape is not None and shape.texture is not None:
            ox, oy = shape.view_origin
            s = shape.view_scale or 1.0
            th, tw = shape.texture.shape[:2]
            vw = max(1, min(int(round(tw / s)), w - ox))
            vh = max(1, min(int(round(th / s)), h - oy))
            inside = np.zeros((h, w), bool)
            inside[oy:oy + vh, ox:ox + vw] = True
            canvas[~inside] = (canvas[~inside] * _PATH_OUTSIDE_DIM).astype(np.uint8)
            textured = cv2.resize(shape.texture, (vw, vh), interpolation=cv2.INTER_NEAREST)
            roi = canvas[oy:oy + vh, ox:ox + vw]
            free = textured == 0
            tint = np.empty_like(roi)
            tint[:] = _GREEN
            blended = cv2.addWeighted(roi, 1.0 - _PATH_TINT_ALPHA, tint, _PATH_TINT_ALPHA, 0.0)
            roi[free] = blended[free]
            cv2.rectangle(canvas, (ox, oy), (ox + vw - 1, oy + vh - 1), _GREY, 1)
        warn_s = float(getattr(view, "tau_warn_s", 8.0))
        view_area = None
        if shape is not None and shape.texture is not None:
            view_area = float(shape.texture.size) / max(shape.view_scale, 1e-6) ** 2
        for sh in (() if shape is None else shape.shapes):
            color = self._tau_colour(sh.tau, warn_s, sh.measured)
            pts = sh.outline.astype(np.int32).reshape(-1, 1, 2)
            thick = 3 if sh.centred else 2
            cv2.polylines(canvas, [pts], True, _DARK, thick + 3, cv2.LINE_AA)
            cv2.polylines(canvas, [pts], True, color, thick, cv2.LINE_AA)
            # The figure goes on the object itself. Not on one that fills most
            # of the view: the status line already gives the nearest figure.
            big = view_area is not None and sh.area_last > _PATH_LABEL_MAX_FRAC * view_area
            if len(pts) >= 3 and not big and (sh.tau is not None and sh.tau < 2.0 * warn_s
                                              or (sh.centred and not sh.measured)):
                m = cv2.moments(pts)
                if m["m00"] > 0:
                    lx, ly = int(m["m10"] / m["m00"]), int(m["m01"] / m["m00"])
                    if abs(lx - w // 2) < 60 and abs(ly - h // 2) < 60:
                        ly += 70           # off the bore sight's crosshair
                    if lx < _PATH_STRIP_RIGHT and ly < _row(5) + 24:
                        ly = _row(5) + 60  # below the status strip
                    label = f"{sh.tau:.1f}s" if sh.tau is not None else "?"
                    self._text(label, lx - 30, ly, color, scale=0.45, thick=2)
        plan = getattr(view, "plan", None)
        if plan is not None:
            self._draw_plan(canvas, plan, warn_s)
        point = view.heading_point
        if point is not None and 0 <= point[0] < w and 0 <= point[1] < h:
            px, py = int(point[0]), int(point[1])
            for col, th_ in ((_DARK, 5), (_CYAN, 2)):
                cv2.circle(canvas, (px, py), 13, col, th_, cv2.LINE_AA)
                cv2.line(canvas, (px - 36, py), (px - 13, py), col, th_, cv2.LINE_AA)
                cv2.line(canvas, (px + 13, py), (px + 36, py), col, th_, cv2.LINE_AA)
                cv2.line(canvas, (px, py - 13), (px, py - 28), col, th_, cv2.LINE_AA)
            self._text("heading", px + 40, py + 6, _CYAN, scale=0.34)
        verdict = view.verdict()
        if plan is not None:
            color = _PLAN_COLOURS.get(plan.mode, _GREY)
            blocker = ("" if plan.blocker_tau is None
                       else f"  blocker {plan.blocker_tau:.1f}s")
            banner = f"PATH[SHADOW]: {plan.label} -> {plan.goal.kind.upper()}{blocker}"
        else:
            if verdict.startswith("closing"):
                near = view.nearest
                color = self._tau_colour(None if near is None else near.tau, warn_s, True)
            elif verdict == "clear":
                color = _GREEN
            else:
                color = _GREY
            banner = f"PATH[SHADOW]: {verdict}"
        self._text(banner, 8, _row(3), color, scale=0.55, thick=2, panel=True)
        free = None if shape is None else shape.free_frac
        details = [f"{0 if shape is None else len(shape.shapes)} outlines"]
        if plan is not None:
            details.insert(0, f"view {verdict}")
        if free is not None:
            details.append(f"free air {free:.0%}")
        if view.pair is not None:
            details.append(f"pair {fmt_tau(view.pair)}")
        details.append(f"dt {view.dt:.2f}s  {view.cost_ms:.0f} ms")
        self._text("   ".join(details), 8, _row(4), _WHITE, scale=0.42, panel=True)
        self._draw_depth_inset(canvas, getattr(view, "depth", None), view.picture_ts)

    def _draw_depth_inset(self, canvas: np.ndarray, reading, picture_ts: float) -> None:
        """Code Review 019 cycle 11: what the depth model saw of the analysed
        view, as a small map left of it: near bright, far dark, the middle box
        and the nearest point marked. Grey when the reading is more than 1 s
        from this picture. Shadow."""
        if reading is None or getattr(reading, "status", "") != "ok" or reading.near is None:
            return
        h, w = canvas.shape[:2]
        x1, y1 = int(0.008 * w), int(0.21 * h)
        iw = int(0.19 * w)
        ih = max(1, int(iw * ((VIEW_BOX[3] - VIEW_BOX[1]) * h) / ((VIEW_BOX[2] - VIEW_BOX[0]) * w)))
        stale = abs(reading.picture_ts - picture_ts) > 1.0
        gray = cv2.resize((np.clip(reading.near, 0.0, 1.0) * 255).astype(np.uint8), (iw, ih),
                          interpolation=cv2.INTER_NEAREST)
        inset = (cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR) if stale
                 else cv2.applyColorMap(gray, cv2.COLORMAP_INFERNO))
        canvas[y1:y1 + ih, x1:x1 + iw] = inset

        def to_inset(fx, fy):
            return (x1 + int((fx - VIEW_BOX[0]) / (VIEW_BOX[2] - VIEW_BOX[0]) * iw),
                    y1 + int((fy - VIEW_BOX[1]) / (VIEW_BOX[3] - VIEW_BOX[1]) * ih))

        cv2.rectangle(canvas, to_inset(MID_BOX[0], MID_BOX[1]), to_inset(MID_BOX[2], MID_BOX[3]),
                      _CYAN, 2)
        if reading.nearest is not None:
            cv2.circle(canvas, to_inset(*reading.nearest), 9, _WHITE, 2, cv2.LINE_AA)
        cv2.rectangle(canvas, (x1, y1), (x1 + iw - 1, y1 + ih - 1), _GREY, 1)
        self._text("DEPTH[SHADOW] mid %.2f  %.0f ms%s" % (
            reading.mid_rank, reading.cost_ms, "  (old)" if stale else ""),
            x1, y1 - 10, _GREY if stale else _WHITE, scale=0.4, panel=True)

    @classmethod
    def from_config(cls, config: dict, live: bool = True) -> "HudRenderer | None":
        """Return a HudRenderer unless hud.enabled is explicitly false.

        `live` False (the replay and capture lanes) keeps the black box off,
        so a gate run writes no frames into test_screenshots/.

        hud.enabled is the sole authority (2026-09-21) — tracking.enabled no
        longer implies a renderer gets built on its own. This decouples
        rendering from sensing: a long unattended session can keep
        tracking.enabled: true (needed for error_norm_y/SELECT[shadow]/
        PITCH[shadow] logging, which comes from TargetTracker.update()
        directly, not from this renderer) while paying zero per-tick
        render/disk-write cost for a debug view nobody is watching. Before
        this, tracking.enabled alone was enough to build a renderer even
        with hud.enabled: false — exactly the coupling this removes.
        """
        hud_cfg = config.get("hud", {})
        tracking_enabled = bool(config.get("tracking", {}).get("enabled", False))
        hud_enabled = bool(hud_cfg.get("enabled", True))
        if not hud_enabled:
            return None
        region = config.get("region", {})
        r_left = int(region.get("left", 0))
        r_top = int(region.get("top", 0))
        r_w = int(region.get("width", 1920))
        r_h = int(region.get("height", 1200))
        feh_geometry = f"{r_w // 2}x{r_h // 2}+{r_left + r_w}+{r_top}" if tracking_enabled else ""
        acq_pct = config.get("tracking", {}).get("acquisition_region_pct", (0.20, 0.18, 0.80, 0.68))
        archive_cfg = hud_cfg.get("target_tracking_archive", {}) or {}
        box_cfg = hud_cfg.get("black_box", {}) or {}
        return cls(
            output_path=hud_cfg.get("output_path", "tests/test-output/live_hud.png"),
            interval_sec=float(hud_cfg.get("interval_sec", 1.0)),
            feh_geometry=feh_geometry,
            acquisition_region_pct=acq_pct,
            archive_enabled=bool(archive_cfg.get("enabled", False)),
            archive_dir=str(archive_cfg.get("dir", "tests/test-output/target_tracking")),
            archive_max_files=int(archive_cfg.get("max_files", 200)),
            archive_save_raw_scan=bool(archive_cfg.get("save_raw_scan", False)),
            archive_min_interval_s=float(archive_cfg.get("min_interval_s", 0.0)),
            archive_max_per_encounter=int(archive_cfg.get("max_per_encounter", 0)),
            black_box_enabled=live and bool(
                box_cfg.get("enabled", schema_default("hud.black_box.enabled"))),
            black_box_dir=str(box_cfg.get("dir", schema_default("hud.black_box.dir"))),
            black_box_seconds=float(
                box_cfg.get("seconds", schema_default("hud.black_box.seconds"))),
            black_box_rate_hz=float(
                box_cfg.get("rate_hz", schema_default("hud.black_box.rate_hz"))),
            black_box_alarm_max=int(
                box_cfg.get("alarm_max", schema_default("hud.black_box.alarm_max"))),
            black_box_alarm_gap_s=float(
                box_cfg.get("alarm_gap_s", schema_default("hud.black_box.alarm_gap_s"))),
            black_box_alarm_seconds=float(
                box_cfg.get("alarm_seconds", schema_default("hud.black_box.alarm_seconds"))),
            black_box_raw=bool(box_cfg.get("raw", schema_default("hud.black_box.raw"))),
        )

    def maybe_render(
        self,
        frame: np.ndarray,
        tracking_obs: "dict | None",
        game_state_name: str,
        health: "int | None",
        missiles: "int | None",
        flares: "int | None",
        *,
        steering_target: "tuple[float, float] | None" = None,
        steering_label: "str | None" = None,
        steering_stale: bool = False,
        path_view=None,
    ) -> "threading.Thread | None":
        """Render and write on a background thread if the cadence interval has elapsed.

        Render/encode/disk-write is offloaded so the main tick loop is never blocked
        on it; a non-blocking lock skips a new render while one is still in flight
        instead of queuing up work. Returns the background thread (mainly so callers
        such as tests can join() it); production callers can ignore the return value.

        `path_view`: a `clear_path.PathView` taken on this very `frame` (Code
        Review 019 step 3a). When given, it is drawn in place of the main
        tick's terrain overlays, which belong to other pictures.
        """
        now = time.time()
        live_due = now - self._last_ts >= self._interval
        box_due = self._bb_enabled and now - self._bb_last_ts >= self._bb_interval
        if not live_due and not box_due:
            return None
        if not self._render_lock.acquire(blocking=False):
            logger.debug("HudRenderer: previous render still in flight — skipping")
            return None
        if live_due:
            self._last_ts = now
        if box_due:
            self._bb_last_ts = now
        thread = threading.Thread(
            target=self._render_async,
            args=(frame, tracking_obs, game_state_name, health, missiles, flares,
                  now, steering_target, steering_label, steering_stale, path_view,
                  live_due, box_due),
            daemon=True,
            name="HudRenderer-write",
        )
        thread.start()
        return thread

    def _render_async(
        self,
        frame: np.ndarray,
        obs: "dict | None",
        state: str,
        health: "int | None",
        missiles: "int | None",
        flares: "int | None",
        ts: float,
        steering_target: "tuple[float, float] | None",
        steering_label: "str | None",
        steering_stale: bool,
        path_view=None,
        write_live: bool = True,
        to_black_box: bool = False,
    ) -> None:
        try:
            self._render(frame, obs, state, health, missiles, flares, ts,
                         steering_target, steering_label, steering_stale, path_view,
                         write_live, to_black_box)
        except Exception as exc:
            logger.debug("HudRenderer: render error: %s", exc)
        finally:
            if self._render_lock.locked():
                self._render_lock.release()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _render(
        self,
        frame: np.ndarray,
        obs: "dict | None",
        state: str,
        health: "int | None",
        missiles: "int | None",
        flares: "int | None",
        ts: float,
        steering_target: "tuple[float, float] | None" = None,
        steering_label: "str | None" = None,
        steering_stale: bool = False,
        path_view=None,
        write_live: bool = True,
        to_black_box: bool = False,
    ) -> None:
        canvas = frame.copy()
        h, w = canvas.shape[:2]
        scx, scy = w // 2, h // 2
        self._texts = []

        # ── Terrain overlay — first, so every marker below is drawn over the
        # tint. The pursuit's own view of this picture when it has one (Code
        # Review 019 step 3a); otherwise the main tick's HLDD 001 readings.
        # Its own failure must not cost the rest of the HUD. ───────────────
        if path_view is not None:
            try:
                self._draw_path_view(canvas, path_view)
            except Exception as exc:
                logger.debug("HudRenderer: clear-path overlay error: %s", exc)
        else:
            try:
                self._draw_terrain(canvas, frame, ts)
            except Exception as exc:
                logger.debug("HudRenderer: terrain overlay error: %s", exc)
            try:
                self._draw_loom(canvas, ts)
            except Exception as exc:
                logger.debug("HudRenderer: loom overlay error: %s", exc)
            try:
                self._draw_shapes(canvas, ts)
            except Exception as exc:
                logger.debug("HudRenderer: shape overlay error: %s", exc)

        # ── Status strip (top-left) ──────────────────────────────────────
        ts_str = time.strftime("%H:%M:%S", time.localtime(ts))
        self._text(f"{ts_str}  [{state}]", 8, _row(0), _CYAN, scale=0.52, panel=True)
        hp_str = str(health) if health is not None else "?"
        mis_str = str(missiles) if missiles is not None else "?"
        fla_str = str(flares) if flares is not None else "?"
        self._text(f"HP:{hp_str}  Mis:{mis_str}  Fla:{fla_str}", 8, _row(1), _WHITE,
                   panel=True)

        default_steering_target = None
        default_steering_label = None
        default_steering_stale = False

        # ── Tracking overlay ─────────────────────────────────────────────
        if obs is not None:
            mode = obs.get("mode", "?")
            visible = bool(obs.get("visible", False))
            cx = obs.get("centroid_x")
            cy_ = obs.get("centroid_y")
            err = obs.get("error_norm")

            track_color = _GREEN if visible else _YELLOW
            vis_tag = "VIS" if visible else "---"
            err_tag = f"{err:+.3f}" if err is not None else "  n/a"
            self._text(f"Track:{mode}  {vis_tag}  err={err_tag}", 8, _row(2), track_color,
                       panel=True)

            # Pursued-target highlight — the one target this loop is actually
            # rolling/pitching toward, called out in a color reserved for
            # exactly this (see _PURSUIT above) so it reads unambiguously
            # against the game's own same-frame enemy markers and this HUD's
            # other overlay elements, especially when several contacts are
            # visible at once (this is the exact ambiguity the ALTITUDE_SPEED
            # reference frame in HLDD 005's Selection Hardening raised).
            # Black outline drawn first so the ring/cross reads against any
            # background brightness. Solid when confirmed this frame; a
            # thinner ring plus a "(lost)" label when coasting on the last
            # known position during LOST_GRACE, so the HUD doesn't imply a
            # fresh detection that didn't happen.
            if cx is not None and cy_ is not None:
                default_steering_target = (cx, cy_)
                default_steering_label = "PURSUING"
                default_steering_stale = not visible

            # Horizontal error bar at bottom of frame
            if err is not None:
                bar_y = h - 16
                bar_cx = w // 2
                bar_px = int(bar_cx + err * (w // 2))
                cv2.line(canvas, (bar_cx - 1, bar_y - 10), (bar_cx - 1, bar_y + 10), _GREY, 1)
                dot_color = _GREEN if abs(err) <= 0.05 else _RED
                cv2.circle(canvas, (bar_px, bar_y), 6, dot_color, -1, cv2.LINE_AA)
                self._text("L", 4, bar_y + 5, _GREY, scale=0.36)
                self._text("R", w - 24, bar_y + 5, _GREY, scale=0.36)

        selected_target = steering_target or default_steering_target
        selected_label = steering_label or default_steering_label
        selected_stale = steering_stale if steering_target is not None else default_steering_stale
        if selected_target is not None and selected_label is not None:
            px, py = map(int, selected_target)
            cv2.line(canvas, (scx, scy), (px, py), _PURSUIT, 1, cv2.LINE_AA)
            thick = 1 if selected_stale else 2
            cv2.circle(canvas, (px, py), 16, _DARK, thick + 2, cv2.LINE_AA)
            cv2.circle(canvas, (px, py), 16, _PURSUIT, thick, cv2.LINE_AA)
            cv2.drawMarker(canvas, (px, py), _PURSUIT, cv2.MARKER_CROSS, 26, thick, cv2.LINE_AA)
            label = f"{selected_label} (lost)" if selected_stale else selected_label
            self._text(label, px + 20, py - 14, _PURSUIT, scale=0.4)

        # ── Acquisition region outline ───────────────────────────────────
        acq_x1, acq_y1, acq_x2, acq_y2 = self._acq_pct
        ax1 = int(w * acq_x1)
        ay1 = int(h * acq_y1)
        ax2 = int(w * acq_x2)
        ay2 = int(h * acq_y2)
        cv2.rectangle(canvas, (ax1, ay1), (ax2, ay2), _CYAN, 1)
        if path_view is None:
            # With the pursuit's view drawn, the label would sit on its status line.
            self._text("acq", ax1 + 4, ay1 + 24, _CYAN, scale=0.34)

        # ── Screen center crosshair ──────────────────────────────────────
        cv2.line(canvas, (scx - 18, scy), (scx + 18, scy), _GREY, 1)
        cv2.line(canvas, (scx, scy - 18), (scx, scy + 18), _GREY, 1)

        # ── Target-tracking archive (full resolution, before the live_hud
        # resize below) ───────────────────────────────────────────────────
        if not write_live:
            pass        # a black-box frame only: the archive follows live_hud.png
        elif state in _TARGET_TRACKING_ARCHIVE_STATES:
            # `frame` is still the raw capture here — every overlay above was
            # drawn on `canvas`, a copy.
            raw_scan = self._scanned_crop(frame) if self._archive_save_raw_scan else None
            if self._archive_enabled:
                archived = canvas.copy()
                self._flush_text(archived, 1.0, w)
                self._archive_frame(archived, state, ts, raw_scan)
        else:
            self._archive_encounter_count = 0

        # ── Atomic write ─────────────────────────────────────────────────
        canvas = cv2.resize(canvas, (w // 2, h // 2), interpolation=cv2.INTER_AREA)
        # Text after the downscale, so it reads at the size the file is watched.
        self._flush_text(canvas, 0.5, w)
        if to_black_box:
            self._keep_for_black_box(canvas, ts)
            if self._bb_raw_enabled:
                # `frame` is still the raw capture: every overlay went on `canvas`.
                self._keep_for_black_box(
                    cv2.resize(frame, (w // 2, h // 2), interpolation=cv2.INTER_AREA),
                    ts, raw=True)
        if not write_live:
            return
        self._output.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._output.with_suffix(".tmp.png")
        cv2.imwrite(str(tmp), canvas)
        os.replace(str(tmp), str(self._output))
        logger.debug("HudRenderer: wrote %s", self._output)

    def _keep_for_black_box(self, image: np.ndarray, ts: float, raw: bool = False) -> None:
        """Hold one frame, as JPEG, and drop those older than the box's span:
        an annotated frame, or with `raw` the game's own picture."""
        ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ok:
            return
        held = self._bb_raw if raw else self._bb_frames
        with self._bb_lock:
            held.append((ts, buf.tobytes()))
            while held and ts - held[0][0] > self._bb_seconds:
                held.popleft()

    def dump_black_box(self, reason: str, last_s: "float | None" = None,
                       clear: bool = True, directory: "Path | None" = None
                       ) -> "threading.Thread | None":
        """Write the frames held in memory, oldest first, and start again.

        Called at a death (main.py, on the respawn event). The respawn screen is
        read several seconds after the impact, so the box holds more than the
        seconds before the death. `last_s` keeps only the newest seconds and
        `clear=False` leaves the box as it was (a planner alarm's clip, which a
        death a moment later must still find whole). Writes on a thread of its
        own; returns it, or None when there is nothing to write.
        """
        if not self._bb_enabled:
            return None
        with self._bb_lock:
            frames = list(self._bb_frames)
            raw = list(self._bb_raw)
            if clear:
                self._bb_frames.clear()
                self._bb_raw.clear()
        if last_s is not None and frames:
            frames = [f for f in frames if frames[-1][0] - f[0] <= last_s]
            raw = [f for f in raw if frames and f[0] >= frames[0][0]]
        if not frames:
            return None
        stamp = time.strftime("%Y%m%d_%H%M%S")
        thread = threading.Thread(target=self._write_black_box,
                                  args=(frames, stamp, reason, directory or self._bb_dir, raw),
                                  daemon=True, name="HudRenderer-blackbox")
        thread.start()
        return thread

    def note_alarm(self, reason: str, delay_s: float = 1.5) -> "threading.Timer | None":
        """Keep the frames round a planner alarm (Code Review 019, cycle 5).

        `delay_s` after it, the last `alarm_seconds` of the box go to the alarms
        folder, the box itself untouched. At most one alarm every `alarm_gap_s`
        and `alarm_max` a session; off at 0. Returns the timer, or None.
        """
        if (not self._bb_enabled or self._bb_alarm_max <= 0
                or self._bb_alarm_count >= self._bb_alarm_max):
            return None
        now = time.monotonic()
        if now - self._bb_alarm_last < self._bb_alarm_gap_s:
            return None
        self._bb_alarm_last = now
        self._bb_alarm_count += 1
        timer = threading.Timer(delay_s, self.dump_black_box, args=(reason,),
                                kwargs={"last_s": self._bb_alarm_seconds, "clear": False,
                                        "directory": self._bb_alarm_dir})
        timer.daemon = True
        timer.start()
        return timer

    def _write_black_box(self, frames: "list[tuple[float, bytes]]", stamp: str,
                         reason: str, directory: Path,
                         raw: "list[tuple[float, bytes]] | None" = None) -> None:
        written = self._write_frames(frames, stamp, reason, directory)
        raw_written = self._write_frames(raw, stamp, reason, directory / "raw") if raw else 0
        logger.info("BLACK BOX: %d frame(s) covering %.1f s before the %s, written to %s/%s_%s_*.jpg%s",
                    written, frames[-1][0] - frames[0][0], reason, directory, stamp, reason,
                    f" (and {raw_written} raw to {directory / 'raw'})" if raw else "")

    @staticmethod
    def _write_frames(frames: "list[tuple[float, bytes]]", stamp: str, reason: str,
                      directory: Path) -> int:
        """Write frames oldest first, named for their moment; the count written."""
        written = 0
        try:
            directory.mkdir(parents=True, exist_ok=True)
            for k, (ts, data) in enumerate(frames):
                if not capture_budget.admit(directory, "HUD black box"):
                    break
                at = time.strftime("%H%M%S", time.localtime(ts)) + f".{int(ts * 1000) % 1000:03d}"
                (directory / f"{stamp}_{reason}_{k:02d}_{at}.jpg").write_bytes(data)
                written += 1
        except Exception as e:
            logger.warning("HUD black box: write failed: %s: %s", type(e).__name__, e)
        return written

    def _scanned_crop(self, frame: np.ndarray) -> "tuple[np.ndarray, int, int] | None":
        """The exact region the tracker scanned this tick, as (crop, ox, oy):
        the acquisition region, which every tick scans, reproduced with the
        tracker's own integer math (`int(w * pct)`) so the crop is
        byte-for-byte what `TargetTracker.update` handed its detectors.
        """
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = self._acq_pct
        ax1, ay1 = int(w * x1), int(h * y1)
        return frame[ay1:int(h * y2), ax1:int(w * x2)], ax1, ay1

    def _archive_frame(
        self, canvas: np.ndarray, state: str, ts: float,
        raw_scan: "tuple[np.ndarray, int, int] | None" = None,
    ) -> None:
        """Save a timestamped copy of the annotated frame while target
        tracking is active during a secondary-missile encounter.

        `live_hud.png` is overwritten every render — there is no history to
        look back at once a session has moved on. This gives a session a
        persistent trail of frames, named so they can be lined up directly
        against a `wingman.log` timestamp for debugging (same
        `time.strftime` precision the log itself uses). Capped per session
        (`_archive_max`), same shape as `AmmoEventsHandler
        ._capture_crash_frame` (ADR 137 D5): a silent cap-out would look
        identical to "nothing else happened," so it is logged explicitly
        either way, not swallowed.
        """
        if not self._archive_enabled:
            return
        if self._archive_count >= self._archive_max:
            logger.debug("HudRenderer archive: session cap (%d) reached — not saving",
                         self._archive_max)
            return
        if (self._archive_last_ts is not None
                and ts - self._archive_last_ts < self._archive_min_interval):
            return
        if (self._archive_max_per_encounter > 0
                and self._archive_encounter_count >= self._archive_max_per_encounter):
            logger.debug("HudRenderer archive: encounter cap (%d) reached — not saving",
                         self._archive_max_per_encounter)
            return
        if not capture_budget.admit(self._archive_dir, "HUD target-tracking archive"):
            return
        try:
            self._archive_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(ts))
            # Sequence suffix, not just the timestamp: two renders landing in
            # the same wall-clock second would otherwise silently overwrite
            # one PNG with the other while the counter still claims both
            # were saved (same reasoning _capture_crash_frame documents).
            path = self._archive_dir / f"{state.lower()}_{stamp}_{self._archive_count}.png"
            if not cv2.imwrite(str(path), canvas):
                logger.warning("HudRenderer archive: write failed: %s", path)
                return
            self._archive_count += 1
            self._archive_encounter_count += 1
            self._archive_last_ts = ts
            logger.info("HudRenderer archive: saved %s (%d/%d this session)",
                        path, self._archive_count, self._archive_max)
            if raw_scan is not None:
                crop, ox, oy = raw_scan
                # Origin and full-frame size in the name: everything a replay
                # needs to rebuild the tracker's (ox, oy, frame_w, frame_h)
                # call, with no sidecar file to lose track of.
                fh, fw = canvas.shape[:2]
                raw_path = path.with_name(
                    f"{path.stem}_raw_ox{ox}_oy{oy}_fw{fw}_fh{fh}.png")
                if not cv2.imwrite(str(raw_path), crop):
                    logger.warning("HudRenderer archive: raw scan write failed: %s",
                                   raw_path)
        except Exception as e:
            logger.warning("HudRenderer archive: failed to save frame: %s: %s",
                           type(e).__name__, e)
