import glob
import logging
import os
import re
import subprocess
import sys
import time

import numpy as np
from mss import mss

logger = logging.getLogger(__name__)


class _MssBackend:
    """Screen capture via mss — Windows and Linux X11."""

    def __init__(self, region, monitor_index=1):
        self.region = region
        self.monitor_index = monitor_index
        self._sct = mss()

    def get_monitor_rect(self):
        monitors = self._sct.monitors
        if self.monitor_index < 1 or self.monitor_index >= len(monitors):
            raise ValueError(
                f"Monitor index {self.monitor_index} out of range. "
                f"Found {len(monitors) - 1} monitors."
            )
        mon = monitors[self.monitor_index]
        return {
            "left": mon["left"] + self.region[0],
            "top": mon["top"] + self.region[1],
            "width": self.region[2],
            "height": self.region[3],
        }

    def get_frame(self):
        try:
            s = self._sct.grab(self.get_monitor_rect())
            return np.array(s)[:, :, :3]
        except Exception:
            return None

    def grab_from_thread(self):
        """Create a fresh mss context per call (mss uses thread-local storage)."""
        try:
            with mss() as sct:
                return np.array(sct.grab(self.get_monitor_rect()))[:, :, :3]
        except Exception:
            return None


class _PipeWireBackend:
    """Screen capture via PipeWire XDG Desktop Portal — GNOME Wayland.

    Captures the full monitor (types=1). On first use shows the GNOME Share Screen
    dialog; subsequent starts use a saved restore token and skip the dialog.

    Because DXVK/Wine renders as a Wayland surface with no X11 presence, the game
    window position is detected visually: on the first frame whose dimensions differ
    from the configured game size, we scan for where non-desktop-background content
    starts and cache that offset. Crop regions in the analyzer use fractional coords
    so they work at any game resolution.

    The portal session (and PipeWire node) stays alive as long as this object is alive.
    """

    # Re-check window position if this many consecutive frames had mismatched size
    _REDETECT_AFTER = 30

    def __init__(self, region, game_window_offset=None):
        self.region = region          # (left, top, game_width, game_height) from config
        self.monitor_index = 1
        self._bus = None              # keeps portal session alive
        self._pipeline = None
        self._appsink = None
        # Explicit offset from config (x, y); None means attempt visual detection
        self._configured_offset = game_window_offset
        self._game_offset = game_window_offset
        self._miss_count = 0
        self._setup()

    def _setup(self):
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        if not Gst.is_initialized():
            Gst.init(None)

        from wingman.portal import acquire_screencast_node
        node_id, self._bus = acquire_screencast_node()
        logger.info("PipeWireBackend: node_id=%d, starting pipeline", node_id)

        pipeline_str = (
            f"pipewiresrc path={node_id} keepalive-time=5000 "
            f"! videoconvert "
            f"! video/x-raw,format=BGR "
            f"! appsink name=sink max-buffers=2 drop=true sync=false"
        )
        self._pipeline = Gst.parse_launch(pipeline_str)
        self._appsink = self._pipeline.get_by_name("sink")
        ret = self._pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("PipeWireBackend: GStreamer pipeline failed to start")
        logger.info("PipeWireBackend: pipeline running")

    def _looks_like_game(self, frame, ox, oy):
        """Return True if the crop at (ox,oy)+game_size looks like MetalStorm.

        MetalStorm has a dark HUD bar occupying the bottom ~12% of the frame
        (mean brightness < 35) and a very dark top-right radar/status region.
        Browser videos and VS Code don't satisfy both conditions simultaneously.
        """
        _, _, gw, gh = self.region
        fh, fw = frame.shape[:2]
        if ox + gw > fw or oy + gh > fh:
            return False

        # Bottom HUD bar (health/ammo strip)
        hud_y1 = oy + int(gh * 0.88)
        hud_y2 = oy + int(gh * 0.97)
        hud = frame[hud_y1:hud_y2, ox:ox + gw]
        if hud.size == 0:
            return False
        hud_brightness = float(np.mean(hud))

        # Top-right corner (radar / good-luck text area) — always dark in MetalStorm
        tr_x1 = ox + int(gw * 0.85)
        tr_y1 = oy
        tr_y2 = oy + int(gh * 0.08)
        tr = frame[tr_y1:tr_y2, tr_x1:ox + gw]
        if tr.size == 0:
            return False
        tr_brightness = float(np.mean(tr))

        ok = hud_brightness < 45 and tr_brightness < 45
        logger.info(
            "PipeWireBackend: HUD check at (%d,%d): bottom=%.1f top-right=%.1f → %s",
            ox, oy, hud_brightness, tr_brightness, "GAME" if ok else "not-game",
        )
        return ok

    def _detect_game_offset(self, frame1):
        """Detect game window position by frame differencing + HUD verification.

        Accumulates motion across multiple frame pairs, finds the top motion blobs,
        and picks the first one whose game-sized crop passes the MetalStorm HUD check.
        This rejects browser video / YouTube which also produces large motion blobs.

        Returns (ox, oy) top-left of the game in the monitor frame, or None.
        """
        import cv2
        _, _, gw, gh = self.region
        fh, fw = frame1.shape[:2]

        # Accumulate motion over several frame pairs to handle static lobby menus
        accumulated = np.zeros((fh, fw), dtype=np.float32)
        prev = frame1
        for gap_ms in (200, 300, 500):
            time.sleep(gap_ms / 1000.0)
            curr = self._pull_raw_frame()
            if curr is None:
                continue
            diff = np.abs(curr.astype(np.int16) - prev.astype(np.int16)).max(axis=2).astype(np.float32)
            accumulated = np.maximum(accumulated, diff)
            prev = curr

        motion = (accumulated > 6).astype(np.uint8)

        kernel = np.ones((40, 40), np.uint8)
        dilated = cv2.dilate(motion, kernel)

        contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            logger.info(
                "PipeWireBackend: no motion blobs found in %dx%d frame "
                "(game may be static/loading or behind another window)",
                fw, fh,
            )
            return None

        # Check top blobs largest-first; pick the first that passes HUD verification
        min_area = gw * gh * 0.03
        large_blobs = [
            c for c in sorted(contours, key=cv2.contourArea, reverse=True)
            if cv2.contourArea(c) >= min_area
        ]
        logger.info(
            "PipeWireBackend: %d motion blob(s) above %.0f px² in %dx%d frame",
            len(large_blobs), min_area, fw, fh,
        )
        for contour in large_blobs:
            area = cv2.contourArea(contour)
            x, y, cw, ch = cv2.boundingRect(contour)
            # Centre the game window on the motion blob centre
            cx = x + cw // 2
            cy = y + ch // 2
            ox = max(0, min(cx - gw // 2, fw - gw))
            oy = max(0, min(cy - gh // 2, fh - gh))

            if self._looks_like_game(prev, ox, oy):
                logger.info(
                    "PipeWireBackend: game detected at (%d, %d) in %dx%d monitor "
                    "(motion blob centre %d,%d area=%.0f)",
                    ox, oy, fw, fh, cx, cy, area,
                )
                return (ox, oy)
            logger.info(
                "PipeWireBackend: blob at (%d,%d) %dx%d area=%.0f rejected by HUD check",
                x, y, cw, ch, area,
            )

        return None

    def get_monitor_rect(self):
        return None

    def _pull_raw_frame(self):
        from gi.repository import Gst
        sample = self._appsink.emit("try-pull-sample", 5 * Gst.SECOND)
        if sample is None:
            logger.warning("PipeWireBackend: no frame within 5 s — pipeline stalled?")
            return None
        buf = sample.get_buffer()
        caps = sample.get_caps()
        struct = caps.get_structure(0)
        w = struct.get_int("width")[1]
        h = struct.get_int("height")[1]
        ok, map_info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return None
        arr = np.frombuffer(map_info.data, dtype=np.uint8).reshape(h, w, 3).copy()
        buf.unmap(map_info)
        return arr

    def get_frame(self):
        if self._appsink is None:
            return None
        arr = self._pull_raw_frame()
        if arr is None:
            return None

        fh, fw = arr.shape[:2]
        _, _, gw, gh = self.region

        # Stream is already game-sized (e.g. window capture or same-res monitor)
        if fw == gw and fh == gh:
            return arr

        # Monitor frame: use configured offset if available
        if self._configured_offset is not None:
            ox, oy = self._configured_offset
        else:
            if self._game_offset is None or self._miss_count >= self._REDETECT_AFTER:
                self._miss_count = 0
                self._game_offset = self._detect_game_offset(arr)
                if self._game_offset is None:
                    self._miss_count += 1
                    if self._miss_count == 1:
                        logger.warning(
                            "PipeWireBackend: game window not found in %dx%d frame — "
                            "make sure MetalStorm is visible on screen (not minimised). "
                            "Saving debug frame to /tmp/wingman_detect_fail.png. "
                            "If this persists, run 'make find-game' and set "
                            "game_window_offset in config.yaml.",
                            fw, fh,
                        )
                        try:
                            import cv2 as _cv2
                            _half = _cv2.resize(arr, (fw // 2, fh // 2))
                            _cv2.imwrite("/tmp/wingman_detect_fail.png", _half)
                        except Exception:
                            pass
                    return None
            ox, oy = self._game_offset

        cropped = arr[oy:oy + gh, ox:ox + gw]
        if cropped.shape[:2] != (gh, gw):
            logger.warning(
                "PipeWireBackend: crop at (%d,%d)+%dx%d out of bounds for %dx%d frame",
                ox, oy, gw, gh, fw, fh,
            )
            self._game_offset = None
            return None
        return cropped

    def grab_from_thread(self):
        return self.get_frame()

    def cleanup(self):
        if self._pipeline:
            self._pipeline.set_state(__import__("gi.repository.Gst", fromlist=["Gst"]).Gst.State.NULL)


class _GstBackend:
    """Screen capture via GStreamer ximagesrc — Linux Wayland with XWayland Wine window.

    Uses gst-launch-1.0 ximagesrc (XShm-based) to capture the screen region occupied
    by the MetalStorm Wine virtual desktop window. xwd is not used because Mutter blocks
    X_GetImage on XWayland windows (BadMatch); XShm via ximagesrc bypasses this.

    Kept as a fallback but superseded by _PipeWireBackend on GNOME Wayland — DXVK games
    render as Wayland surfaces (not X11 windows), so ximagesrc captures only black frames.
    """

    def __init__(self, region):
        self.region = region          # (left, top, width, height) relative to game window
        self.monitor_index = 1        # kept for controller logging compat
        self._xauth = self._find_xauthority()
        self._win_pos = None          # cached (abs_x, abs_y) of game window on screen
        self._last_discovery_ts = 0.0 # throttle xwininfo when game is not running
        self._discovery_interval = 2.0

    def _find_xauthority(self):
        xauth = os.environ.get("XAUTHORITY", "")
        if xauth and os.path.exists(xauth):
            return xauth
        for path in glob.glob(f"/run/user/{os.getuid()}/.mutter-Xwaylandauth.*"):
            return path
        return None

    def _env(self):
        env = dict(os.environ)
        if self._xauth:
            env["XAUTHORITY"] = self._xauth
        return env

    def _find_game_window_position(self):
        """Return (abs_x, abs_y) of the Wine/Metalstorm window's top-left on screen."""
        try:
            tree = subprocess.run(
                ["xwininfo", "-root", "-tree"],
                capture_output=True, text=True, env=self._env(), timeout=5.0,
            )
        except Exception:
            return None

        w, h = self.region[2], self.region[3]
        geom_tag = f"{w}x{h}"
        wid = None
        skip_classes = {"code", "Code", "mutter-x11-frames", "ibus-x11", "gsd-xsettings"}
        for line in tree.stdout.splitlines():
            m = re.search(r'(0x[0-9a-fA-F]+)\s+"([^"]*)"(?:\s*:\s*\("([^"]*)"\s*"([^"]*)"\))?', line)
            if not m:
                continue
            line_wid, title = m.group(1), m.group(2)
            wm_class = m.group(3) or ""
            # skip known non-game windows
            if wm_class in skip_classes or "Visual Studio" in title:
                continue
            if title.lower() in ("metalstorm", "wine desktop", "metastorm"):
                logger.debug("GstBackend: found game window by title %r wid=%s", title, line_wid)
                wid = line_wid
                break
            if geom_tag in line and wid is None:
                logger.debug("GstBackend: found candidate by geometry %s wid=%s title=%r", geom_tag, line_wid, title)
                wid = line_wid

        if not wid:
            logger.debug("GstBackend: no game window found (xwininfo saw %d lines)", len(tree.stdout.splitlines()))
            return None

        try:
            stats = subprocess.run(
                ["xwininfo", "-id", wid, "-stats"],
                capture_output=True, text=True, env=self._env(), timeout=5.0,
            )
        except Exception:
            return None

        abs_x = abs_y = None
        for line in stats.stdout.splitlines():
            if "Absolute upper-left X:" in line:
                abs_x = int(line.split(":")[-1].strip())
            elif "Absolute upper-left Y:" in line:
                abs_y = int(line.split(":")[-1].strip())

        if abs_x is not None and abs_y is not None:
            return (abs_x, abs_y)
        return None

    def get_monitor_rect(self):
        # GStreamer backend captures by screen coordinates; no mss monitor rect.
        return None

    def get_frame(self):
        if self._win_pos is None:
            now = time.time()
            if now - self._last_discovery_ts >= self._discovery_interval:
                self._last_discovery_ts = now
                self._win_pos = self._find_game_window_position()
        if self._win_pos is None:
            return None

        win_x, win_y = self._win_pos
        rx, ry, rw, rh = self.region
        startx = win_x + rx
        starty = win_y + ry
        endx = startx + rw - 1
        endy = starty + rh - 1

        try:
            result = subprocess.run(
                [
                    "gst-launch-1.0", "-q",
                    "ximagesrc",
                    f"startx={startx}", f"starty={starty}",
                    f"endx={endx}", f"endy={endy}",
                    "num-buffers=1",
                    "!", "videoconvert",
                    "!", f"video/x-raw,format=BGR,width={rw},height={rh}",
                    "!", "fdsink", "fd=1",
                ],
                capture_output=True, env=self._env(), timeout=5.0,
            )
        except Exception:
            self._win_pos = None
            return None

        expected = rw * rh * 3
        if result.returncode != 0 or len(result.stdout) < expected:
            self._win_pos = None
            return None

        return np.frombuffer(result.stdout, dtype=np.uint8)[:expected].reshape(rh, rw, 3).copy()

    def grab_from_thread(self):
        """GStreamer subprocess is inherently thread-safe; delegates to get_frame()."""
        return self.get_frame()


def _is_wayland():
    return (
        sys.platform != "win32"
        and os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
    )


class Capture:
    """Platform-dispatched screen capture.

    Linux Wayland: _PipeWireBackend (XDG Desktop Portal + GStreamer pipewiresrc).
      DXVK/Vulkan games render as Wayland surfaces and are invisible to X11 XShm;
      PipeWire is the only reliable capture path on GNOME Wayland.
    Windows / Linux X11: _MssBackend (mss + XGetImage).
    """

    def __init__(self, region, monitor_index=1, game_window_offset=None):
        self.region = region
        self.monitor_index = monitor_index
        if _is_wayland():
            self._backend = _PipeWireBackend(region, game_window_offset=game_window_offset)
        else:
            self._backend = _MssBackend(region, monitor_index)

    def get_monitor_rect(self):
        """Returns monitor rect dict for mss callers; None on Wayland/GStreamer backend."""
        return self._backend.get_monitor_rect()

    def get_frame(self):
        """Return a BGR frame. Call from the thread that constructed this Capture."""
        return self._backend.get_frame()

    def grab_from_thread(self):
        """Thread-safe frame grab for daemon threads."""
        return self._backend.grab_from_thread()
