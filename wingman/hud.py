"""Periodic annotated screenshot HUD for tracking and game-state telemetry.

Writes to a temp file then atomically replaces the configured output path so
image viewers (VS Code, eog) never see a partially-written frame.

Usage:
    renderer = HudRenderer.from_config(cfg)
    # in main loop:
    renderer.maybe_render(frame, tracking_obs, state_name, health, missiles, flares)
"""

import logging
import os
import subprocess
import threading
import time
from pathlib import Path

import cv2
import numpy as np

from . import capture_budget

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
                 archive_max_per_encounter: int = 0) -> None:
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
        if feh_geometry:
            self._launch_feh(feh_geometry)

    def _launch_feh(self, geometry: str) -> None:
        try:
            self._feh_process = subprocess.Popen(
                ["feh", "--reload", "1", "--zoom", "fill", "--geometry", geometry, str(self._output)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info("HudRenderer: feh launched (%s)", geometry)
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

    @classmethod
    def from_config(cls, config: dict) -> "HudRenderer | None":
        """Return a HudRenderer unless hud.enabled is explicitly false.

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
        )

    def maybe_render(
        self,
        frame: np.ndarray,
        tracking_obs: "dict | None",
        game_state_name: str,
        health: "int | None",
        missiles: "int | None",
        flares: "int | None",
    ) -> "threading.Thread | None":
        """Render and write on a background thread if the cadence interval has elapsed.

        Render/encode/disk-write is offloaded so the main tick loop is never blocked
        on it; a non-blocking lock skips a new render while one is still in flight
        instead of queuing up work. Returns the background thread (mainly so callers
        such as tests can join() it); production callers can ignore the return value.
        """
        now = time.time()
        if now - self._last_ts < self._interval:
            return None
        if not self._render_lock.acquire(blocking=False):
            logger.debug("HudRenderer: previous render still in flight — skipping")
            return None
        self._last_ts = now
        thread = threading.Thread(
            target=self._render_async,
            args=(frame, tracking_obs, game_state_name, health, missiles, flares, now),
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
    ) -> None:
        try:
            self._render(frame, obs, state, health, missiles, flares, ts)
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
    ) -> None:
        canvas = frame.copy()
        h, w = canvas.shape[:2]
        scx, scy = w // 2, h // 2

        # ── Status strip (top-left) ──────────────────────────────────────
        ts_str = time.strftime("%H:%M:%S", time.localtime(ts))
        _txt(canvas, f"{ts_str}  [{state}]", 8, 22, _CYAN, scale=0.58)
        hp_str = str(health) if health is not None else "?"
        mis_str = str(missiles) if missiles is not None else "?"
        fla_str = str(flares) if flares is not None else "?"
        _txt(canvas, f"HP:{hp_str}  Mis:{mis_str}  Fla:{fla_str}", 8, 44, _WHITE)

        # ── Tracking overlay ─────────────────────────────────────────────
        if obs is not None:
            mode = obs.get("mode", "?")
            visible = bool(obs.get("visible", False))
            cx = obs.get("centroid_x")
            cy_ = obs.get("centroid_y")
            err = obs.get("error_norm")
            n_det = obs.get("n_detections", 0)

            track_color = _GREEN if visible else _YELLOW
            vis_tag = "VIS" if visible else "---"
            err_tag = f"{err:+.3f}" if err is not None else "  n/a"
            _txt(canvas, f"Track:{mode}  {vis_tag}  err={err_tag}  det={n_det}", 8, 66, track_color)

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
                px, py = int(cx), int(cy_)
                # Steering vector (2026-09-23, direct instruction): center of
                # screen to the current steer target, whatever detection mode
                # produced it (tall-bar pick or, when tracking.red_mass_steering
                # is on, the red-mass centroid) — drawn first so the marker
                # below sits on top of it at the target end.
                cv2.line(canvas, (scx, scy), (px, py), _PURSUIT, 1, cv2.LINE_AA)
                thick = 2 if visible else 1
                cv2.circle(canvas, (px, py), 16, _DARK, thick + 2, cv2.LINE_AA)
                cv2.circle(canvas, (px, py), 16, _PURSUIT, thick, cv2.LINE_AA)
                cv2.drawMarker(canvas, (px, py), _PURSUIT, cv2.MARKER_CROSS, 26, thick, cv2.LINE_AA)
                label = "PURSUING" if visible else "PURSUING (lost)"
                _txt(canvas, label, px + 20, py - 14, _PURSUIT, scale=0.42)

            # Horizontal error bar at bottom of frame
            if err is not None:
                bar_y = h - 16
                bar_cx = w // 2
                bar_px = int(bar_cx + err * (w // 2))
                cv2.line(canvas, (bar_cx - 1, bar_y - 10), (bar_cx - 1, bar_y + 10), _GREY, 1)
                dot_color = _GREEN if abs(err) <= 0.05 else _RED
                cv2.circle(canvas, (bar_px, bar_y), 6, dot_color, -1, cv2.LINE_AA)
                _txt(canvas, "L", 4, bar_y + 5, _GREY, scale=0.4)
                _txt(canvas, "R", w - 14, bar_y + 5, _GREY, scale=0.4)

        # ── Acquisition region outline ───────────────────────────────────
        acq_x1, acq_y1, acq_x2, acq_y2 = self._acq_pct
        ax1 = int(w * acq_x1)
        ay1 = int(h * acq_y1)
        ax2 = int(w * acq_x2)
        ay2 = int(h * acq_y2)
        cv2.rectangle(canvas, (ax1, ay1), (ax2, ay2), _CYAN, 1)
        _txt(canvas, "acq", ax1 + 2, ay1 + 14, _CYAN, scale=0.38)

        # ── Screen center crosshair ──────────────────────────────────────
        cv2.line(canvas, (scx - 18, scy), (scx + 18, scy), _GREY, 1)
        cv2.line(canvas, (scx, scy - 18), (scx, scy + 18), _GREY, 1)

        # ── Target-tracking archive (full resolution, before the live_hud
        # resize below) ───────────────────────────────────────────────────
        if state in _TARGET_TRACKING_ARCHIVE_STATES:
            # `frame` is still the raw capture here — every overlay above was
            # drawn on `canvas`, a copy.
            raw_scan = self._scanned_crop(frame) if self._archive_save_raw_scan else None
            self._archive_frame(canvas, state, ts, raw_scan)
        else:
            self._archive_encounter_count = 0

        # ── Atomic write ─────────────────────────────────────────────────
        canvas = cv2.resize(canvas, (w // 2, h // 2), interpolation=cv2.INTER_AREA)
        self._output.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._output.with_suffix(".tmp.png")
        cv2.imwrite(str(tmp), canvas)
        os.replace(str(tmp), str(self._output))
        logger.debug("HudRenderer: wrote %s", self._output)

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
