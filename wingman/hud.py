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
import time
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger(__name__)

_FONT = cv2.FONT_HERSHEY_SIMPLEX
_GREEN = (0, 220, 0)
_RED = (0, 60, 255)
_YELLOW = (0, 210, 255)
_CYAN = (220, 210, 0)
_WHITE = (240, 240, 240)
_DARK = (10, 10, 10)
_GREY = (140, 140, 140)


def _txt(canvas: np.ndarray, text: str, x: int, y: int,
         color=_WHITE, scale: float = 0.52, thick: int = 1) -> None:
    cv2.putText(canvas, text, (x, y), _FONT, scale, _DARK, thick + 2, cv2.LINE_AA)
    cv2.putText(canvas, text, (x, y), _FONT, scale, color, thick, cv2.LINE_AA)


class HudRenderer:
    """Render and atomic-write an annotated game-frame snapshot on a cadence."""

    def __init__(self, output_path: str, interval_sec: float = 1.0,
                 feh_geometry: str = "") -> None:
        self._output = Path(output_path)
        self._interval = float(interval_sec)
        self._last_ts: float = 0.0
        if feh_geometry:
            self._launch_feh(feh_geometry)

    def _launch_feh(self, geometry: str) -> None:
        try:
            subprocess.Popen(
                ["feh", "--reload", "1", "--zoom", "fill", "--geometry", geometry, str(self._output)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info("HudRenderer: feh launched (%s)", geometry)
        except FileNotFoundError:
            logger.warning("HudRenderer: feh not found — install with: sudo apt install feh")

    @classmethod
    def from_config(cls, config: dict) -> "HudRenderer | None":
        """Return a HudRenderer only when tracking.enabled and hud.enabled are both true."""
        hud_cfg = config.get("hud", {})
        tracking_enabled = bool(config.get("tracking", {}).get("enabled", False))
        if not tracking_enabled or not bool(hud_cfg.get("enabled", True)):
            return None
        region = config.get("region", {})
        r_left = int(region.get("left", 0))
        r_top = int(region.get("top", 0))
        r_w = int(region.get("width", 1920))
        r_h = int(region.get("height", 1200))
        feh_geometry = f"{r_w // 2}x{r_h // 2}+{r_left + r_w}+{r_top}" if tracking_enabled else ""
        return cls(
            output_path=hud_cfg.get("output_path", "tests/test-output/live_hud.png"),
            interval_sec=float(hud_cfg.get("interval_sec", 1.0)),
            feh_geometry=feh_geometry,
        )

    def maybe_render(
        self,
        frame: np.ndarray,
        tracking_obs: "dict | None",
        game_state_name: str,
        health: "int | None",
        missiles: "int | None",
        flares: "int | None",
    ) -> None:
        """Render and write if the cadence interval has elapsed; otherwise no-op."""
        now = time.time()
        if now - self._last_ts < self._interval:
            return
        self._last_ts = now
        try:
            self._render(frame, tracking_obs, game_state_name, health, missiles, flares, now)
        except Exception as exc:
            logger.debug("HudRenderer: render error: %s", exc)

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
            roi = obs.get("roi_rect")

            track_color = _GREEN if visible else _YELLOW
            vis_tag = "VIS" if visible else "---"
            err_tag = f"{err:+.3f}" if err is not None else "  n/a"
            _txt(canvas, f"Track:{mode}  {vis_tag}  err={err_tag}  det={n_det}", 8, 66, track_color)

            # Centroid crosshair
            if cx is not None and cy_ is not None:
                px, py = int(cx), int(cy_)
                cv2.drawMarker(canvas, (px, py), _GREEN, cv2.MARKER_CROSS, 22, 2, cv2.LINE_AA)
                cv2.circle(canvas, (px, py), 10, _GREEN, 1, cv2.LINE_AA)

            # Local ROI rectangle
            if roi is not None:
                rx, ry, rw, rh = roi
                cv2.rectangle(canvas, (rx, ry), (rx + rw, ry + rh), _YELLOW, 1)
                _txt(canvas, "ROI", rx + 2, ry + 14, _YELLOW, scale=0.38)

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
        ax1 = int(w * 0.20)
        ay1 = int(h * 0.18)
        ax2 = int(w * 0.80)
        ay2 = int(h * 0.68)
        cv2.rectangle(canvas, (ax1, ay1), (ax2, ay2), _CYAN, 1)
        _txt(canvas, "acq", ax1 + 2, ay1 + 14, _CYAN, scale=0.38)

        # ── Screen center crosshair ──────────────────────────────────────
        scx, scy = w // 2, h // 2
        cv2.line(canvas, (scx - 18, scy), (scx + 18, scy), _GREY, 1)
        cv2.line(canvas, (scx, scy - 18), (scx, scy + 18), _GREY, 1)

        # ── Atomic write ─────────────────────────────────────────────────
        canvas = cv2.resize(canvas, (w // 2, h // 2), interpolation=cv2.INTER_AREA)
        self._output.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._output.with_suffix(".tmp.png")
        cv2.imwrite(str(tmp), canvas)
        os.replace(str(tmp), str(self._output))
        logger.debug("HudRenderer: wrote %s", self._output)
