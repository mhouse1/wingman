"""Snipping-tool style region screenshot for Wingman (`make screenshot`).

A dimmed picture of the whole desktop (every monitor) covers the screen; drag a
rectangle anywhere on it. The picture then goes away and a thin outline marks the region while you keep using
the computer normally — the outline sits just outside the region, so it never
blocks a click inside it or appears in the capture.

Usage:
    python tests/screenshot.py

Controls (global — they work whichever window has focus):
    Enter      — capture the outlined region (as often as you like)
    Backspace  — exit
    Esc        — exit while the selection overlay is up

Screenshots are written to tests/test-output/screenshot_<timestamp>.png, the
folder and name pattern the 'v' hotkey uses, under the same capture budget.

Windows and Linux X11 capture through mss. A Linux Wayland session captures
through the XDG ScreenCast portal (PipeWire), the path wingman itself uses there:
Xwayland on GNOME 50 refuses mss's XGetImage on the root window outright. The
first run there shows GNOME's share-screen dialog; the restore token wingman
saves skips it afterwards.
"""

import contextlib
import ctypes
import os
import queue
import sys
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path

import cv2
import mss
import numpy as np

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_OUTPUT_DIR = _ROOT / "tests" / "test-output"

sys.path.insert(0, str(_ROOT))
from wingman import capture_budget  # noqa: E402

_MIN_SIZE_PX = 4          # a click without a drag is not a selection
_BORDER_PX = 2            # outline thickness, drawn outside the region
_DEBOUNCE_S = 0.4         # key-repeat while Enter is held must not burst captures
_POLL_MS = 50             # how often the Tk loop drains the hotkey queue
_OVERLAY_DIM = 0.6        # brightness of the frozen desktop under the selection overlay
_OVERLAY_ALPHA = 0.3      # see-through black overlay, only when no desktop picture was taken


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _selection_to_region(ax: int, ay: int, bx: int, by: int,
                         screen: dict) -> "dict | None":
    """Map an overlay drag to an mss region in desktop coordinates.

    Overlay coordinates start at the virtual screen's top-left, which can be
    negative on a multi-monitor desktop. Corners may come in either order and
    are clamped to the screen. Returns None when the drag is too small to be a
    selection.
    """
    sl, st = screen["left"], screen["top"]
    sw, sh = screen["width"], screen["height"]
    x1 = max(0, min(sw, min(ax, bx)))
    y1 = max(0, min(sh, min(ay, by)))
    x2 = max(0, min(sw, max(ax, bx)))
    y2 = max(0, min(sh, max(ay, by)))
    if x2 - x1 < _MIN_SIZE_PX or y2 - y1 < _MIN_SIZE_PX:
        return None
    return {"left": sl + x1, "top": st + y1, "width": x2 - x1, "height": y2 - y1}


def _border_rects(region: dict, t: int = _BORDER_PX) -> "list[tuple[int, int, int, int]]":
    """Four (x, y, w, h) strips framing `region` from the outside, so none overlaps it."""
    x, y, w, h = region["left"], region["top"], region["width"], region["height"]
    return [
        (x - t, y - t, w + 2 * t, t),   # top
        (x - t, y + h, w + 2 * t, t),   # bottom
        (x - t, y, t, h),               # left
        (x + w, y, t, h),               # right
    ]


def _geometry(x: int, y: int, w: int, h: int) -> str:
    # Tk reads "-N" as "N from the right edge"; "+-N" is a negative position.
    return f"{w}x{h}+{x}+{y}"


def _region_in_stream(region: dict, origin: "tuple[int, int]",
                      scale: "tuple[float, float]",
                      frame_size: "tuple[int, int]") -> "tuple[int, int, int, int]":
    """Map a desktop region to (x, y, w, h) pixels in a portal stream's frame.

    The stream shows one monitor whose top-left sits at `origin` on the desktop;
    `scale` is frame pixels per desktop pixel. Raises ValueError when the region
    is not wholly on that monitor.
    """
    (ox, oy), (sx, sy), (fw, fh) = origin, scale, frame_size
    x = round((region["left"] - ox) * sx)
    y = round((region["top"] - oy) * sy)
    w = round(region["width"] * sx)
    h = round(region["height"] * sy)
    if x < 0 or y < 0 or x + w > fw or y + h > fh:
        raise ValueError("region is not on the monitor the screen-share stream shows")
    return x, y, w, h


def _place_on_desktop(frame: np.ndarray, origin: "tuple[int, int]",
                      scale: "tuple[float, float]", screen: dict) -> np.ndarray:
    """Paste one monitor's stream frame into a black picture of the whole desktop.

    The inverse of _region_in_stream: the frame is brought back to desktop pixels
    and placed at `origin`; whatever falls off the desktop is dropped.
    """
    sw, sh = screen["width"], screen["height"]
    desktop = np.zeros((sh, sw, 3), dtype=np.uint8)
    fh, fw = frame.shape[:2]
    w, h = round(fw / scale[0]), round(fh / scale[1])
    if (w, h) != (fw, fh):
        frame = cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)
    x, y = origin[0] - screen["left"], origin[1] - screen["top"]
    x1, y1 = max(0, x), max(0, y)
    x2, y2 = min(sw, x + w), min(sh, y + h)
    if x2 > x1 and y2 > y1:
        desktop[y1:y2, x1:x2] = frame[y1 - y:y2 - y, x1 - x:x2 - x]
    return desktop


# ---------------------------------------------------------------------------
# Capture and save
# ---------------------------------------------------------------------------

class _MssGrabber:
    """Windows and Linux X11: mss reads the desktop directly."""

    def __init__(self, sct):
        self._sct = sct

    def grab(self, region: dict):
        return self._sct.grab(region)

    def desktop(self, screen: dict) -> np.ndarray:
        return np.asarray(self._sct.grab(screen))[:, :, :3]

    def close(self) -> None:
        pass


class _PortalGrabber:
    """Linux Wayland: the newest frame of an XDG ScreenCast portal stream.

    PipeWire sends a frame only when the screen changes, so a pull that finds
    none means the last frame kept is still what the screen shows.
    """

    def __init__(self):
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        from wingman.portal import acquire_screencast_stream

        if not Gst.is_initialized():
            Gst.init(None)
        self._Gst = Gst
        # Hidden cursor: a capture of a crop region must not carry the pointer.
        node_id, props, self._bus = acquire_screencast_stream(cursor_mode=1)
        self._pipeline = Gst.parse_launch(
            f"pipewiresrc path={node_id} keepalive-time=5000 "
            f"! videoconvert ! video/x-raw,format=BGR "
            f"! appsink name=sink max-buffers=1 drop=true sync=false"
        )
        self._sink = self._pipeline.get_by_name("sink")
        if self._pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("GStreamer pipeline for the portal stream failed to start")
        self._frame = self._pull(5 * Gst.SECOND)
        if self._frame is None:
            self.close()
            raise RuntimeError("the portal stream sent no frame within 5 s")
        fh, fw = self._frame.shape[:2]
        self._origin = tuple(props.get("position", (0, 0)))
        sw, sh = props.get("size", (fw, fh))
        self._scale = (fw / sw, fh / sh)

    def _pull(self, timeout_ns: int) -> "np.ndarray | None":
        sample = self._sink.emit("try-pull-sample", timeout_ns)
        if sample is None:
            return None
        caps = sample.get_caps().get_structure(0)
        w, h = caps.get_int("width")[1], caps.get_int("height")[1]
        buf = sample.get_buffer()
        ok, info = buf.map(self._Gst.MapFlags.READ)
        if not ok:
            return None
        try:
            # Rows may be padded past w * 3 bytes.
            stride = info.size // h
            rows = np.frombuffer(info.data, dtype=np.uint8, count=stride * h).reshape(h, stride)
            return rows[:, :w * 3].reshape(h, w, 3).copy()
        finally:
            buf.unmap(info)

    def grab(self, region: dict) -> np.ndarray:
        newest = self._pull(0)
        if newest is not None:
            self._frame = newest
        fh, fw = self._frame.shape[:2]
        x, y, w, h = _region_in_stream(region, self._origin, self._scale, (fw, fh))
        return self._frame[y:y + h, x:x + w]

    def desktop(self, screen: dict) -> np.ndarray:
        newest = self._pull(0)
        if newest is not None:
            self._frame = newest
        return _place_on_desktop(self._frame, self._origin, self._scale, screen)

    def close(self) -> None:
        self._pipeline.set_state(self._Gst.State.NULL)


def _grabber(sct):
    """mss everywhere except Linux Wayland, which gets the portal stream."""
    if sys.platform == "win32" or os.environ.get("XDG_SESSION_TYPE", "").lower() != "wayland":
        return _MssGrabber(sct)
    try:
        return _PortalGrabber()
    except Exception as exc:
        print(f"[WARN] screen-share portal unavailable ({exc}); falling back to mss, "
              "which Xwayland may refuse.")
        return _MssGrabber(sct)


def _save(grabber, region: dict) -> "Path | None":
    if not capture_budget.admit(_OUTPUT_DIR, "make screenshot", patterns="screenshot_*.png"):
        print("[SKIP] disk below the capture-budget floor - nothing saved.")
        return None
    frame = np.ascontiguousarray(np.asarray(grabber.grab(region))[:, :, :3])  # BGRA -> BGR
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # Milliseconds, so two Enters in the same second do not overwrite each other.
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    path = _OUTPUT_DIR / f"screenshot_{timestamp}.png"
    if not cv2.imwrite(str(path), frame):
        print(f"[ERROR] could not write {path}")
        return None
    return path


def _keyboard_backend():
    """Global key hooks: the `keyboard` module on Windows, the no-root XRecord shim on Linux."""
    try:
        import keyboard
    except Exception:
        keyboard = None
    from wingman.input_linux import maybe_install_linux_keyboard
    return maybe_install_linux_keyboard(keyboard)


def _set_dpi_aware() -> None:
    """Make Tk and mss agree on physical pixels under Windows display scaling."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor aware
    except Exception:
        with contextlib.suppress(Exception):
            ctypes.windll.user32.SetProcessDPIAware()


# ---------------------------------------------------------------------------
# Snipping tool (tkinter)
# ---------------------------------------------------------------------------

class _Snipper:
    def __init__(self, screen: dict, grabber, keys):
        self._grabber = grabber
        self._screen = screen  # the whole virtual desktop
        self._events: "queue.Queue[str]" = queue.Queue()
        self._last_enter = 0.0
        self._region: "dict | None" = None
        self._borders: list = []
        self._anchor: "tuple[int, int] | None" = None
        self._rect_id = None

        self._root = tk.Tk()
        self._root.withdraw()  # hidden host; the overlay and borders are Toplevels
        self._build_overlay()

        # Hook callbacks run on the backend's thread; Tk is not thread-safe, so
        # they only enqueue and the Tk loop does the work.
        keys.on_press_key("enter", lambda _e=None: self._events.put("enter"), suppress=False)
        keys.on_press_key("backspace", lambda _e=None: self._events.put("exit"), suppress=False)

    def run(self) -> None:
        self._root.after(_POLL_MS, self._poll)
        self._root.mainloop()

    # --- selection overlay ---

    def _desktop_photo(self) -> "tk.PhotoImage | None":
        """A dimmed picture of the desktop for the overlay, or None if it cannot be taken.

        Taken before the overlay exists, so the overlay is not in it. Window
        transparency is not used: Xwayland under GNOME ignores it on an
        override-redirect window and shows plain black.
        """
        s = self._screen
        try:
            img = self._grabber.desktop(s)
        except Exception as exc:
            print(f"[WARN] could not picture the desktop for the overlay ({exc}).")
            return None
        if img.shape[:2] != (s["height"], s["width"]):
            img = cv2.resize(img, (s["width"], s["height"]), interpolation=cv2.INTER_AREA)
        img = cv2.convertScaleAbs(img, alpha=_OVERLAY_DIM)
        ok, ppm = cv2.imencode(".ppm", img)
        return tk.PhotoImage(master=self._root, data=ppm.tobytes()) if ok else None

    def _build_overlay(self) -> None:
        s = self._screen
        self._photo = self._desktop_photo()  # held here: Tk drops an unreferenced image
        ov = tk.Toplevel(self._root)
        ov.overrideredirect(True)
        ov.geometry(_geometry(s["left"], s["top"], s["width"], s["height"]))
        ov.attributes("-topmost", True)
        canvas = tk.Canvas(ov, bg="black", highlightthickness=0, cursor="crosshair")
        canvas.pack(fill=tk.BOTH, expand=True)
        if self._photo is not None:
            canvas.create_image(0, 0, image=self._photo, anchor=tk.NW)
        else:
            ov.attributes("-alpha", _OVERLAY_ALPHA)
        hint = canvas.create_text(s["width"] // 2, 40, fill="white", font=("Segoe UI", 16),
                                  text="Drag to select a region  -  Esc or Backspace to exit")
        x1, y1, x2, y2 = canvas.bbox(hint)
        canvas.tag_lower(canvas.create_rectangle(x1 - 10, y1 - 6, x2 + 10, y2 + 6,
                                                 fill="black", outline=""), hint)
        canvas.bind("<ButtonPress-1>", self._on_press)
        canvas.bind("<B1-Motion>", self._on_drag)
        canvas.bind("<ButtonRelease-1>", self._on_release)
        ov.bind("<Escape>", lambda _e: self._exit())
        ov.focus_force()
        self._overlay, self._canvas = ov, canvas

    def _on_press(self, event) -> None:
        self._anchor = (event.x, event.y)
        if self._rect_id is not None:
            self._canvas.delete(self._rect_id)
        self._rect_id = self._canvas.create_rectangle(event.x, event.y, event.x, event.y,
                                                      outline="#ff3030", width=2)

    def _on_drag(self, event) -> None:
        if self._anchor is not None:
            self._canvas.coords(self._rect_id, *self._anchor, event.x, event.y)

    def _on_release(self, event) -> None:
        if self._anchor is None:
            return
        region = _selection_to_region(*self._anchor, event.x, event.y, self._screen)
        self._anchor = None
        if region is None:
            return  # a click, not a drag — stay in the overlay
        self._region = region
        self._overlay.destroy()
        self._photo = None
        self._show_borders()
        print(f"Region {region['width']}x{region['height']} at ({region['left']},{region['top']}). "
              "Enter captures it, Backspace exits.")

    # --- outline while the operator works ---

    def _show_borders(self) -> None:
        for x, y, w, h in _border_rects(self._region):
            b = tk.Toplevel(self._root)
            b.overrideredirect(True)
            b.attributes("-topmost", True)
            b.geometry(_geometry(x, y, w, h))
            b.configure(bg="#ff3030")
            self._borders.append(b)

    def _flash(self) -> None:
        for b in self._borders:
            b.configure(bg="white")
        self._root.after(150, lambda: [b.configure(bg="#ff3030") for b in self._borders])

    # --- hotkeys ---

    def _poll(self) -> None:
        try:
            while True:
                event = self._events.get_nowait()
                if event == "exit":
                    self._exit()
                    return
                if event == "enter":
                    self._on_enter()
        except queue.Empty:
            pass
        self._root.after(_POLL_MS, self._poll)

    def _on_enter(self) -> None:
        now = time.monotonic()
        if now - self._last_enter < _DEBOUNCE_S:
            return
        self._last_enter = now
        if self._region is None:
            print("  No region yet - drag one on the overlay first.")
            return
        try:
            path = _save(self._grabber, self._region)
        except Exception as exc:
            print(f"  [ERROR] capture failed: {exc}")
            return
        if path is not None:
            self._flash()
            print(f"  Saved {self._region['width']}x{self._region['height']} -> {path}")

    def _exit(self) -> None:
        self._root.destroy()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main():
    keys = _keyboard_backend()
    if keys is None:
        print("[ERROR] no keyboard backend for global hotkeys (`keyboard` module missing).")
        sys.exit(1)

    _set_dpi_aware()
    print("Drag a region anywhere on screen. Then Enter captures it (from any window); "
          "Backspace exits.")
    try:
        with mss.mss() as sct:
            # mss's monitor list comes from XRandR, which still answers on Xwayland.
            grabber = _grabber(sct)
            try:
                _Snipper(sct.monitors[0], grabber, keys).run()
            finally:
                grabber.close()
    finally:
        with contextlib.suppress(Exception):
            keys.unhook_all()

    print("[DONE] Screenshot tool closed.")

    if sys.platform == "win32":
        # unhook_all() drops the callbacks but leaves the `keyboard` module's
        # low-level hook installed. A key event during interpreter shutdown —
        # the Backspace key-up that triggered the exit, typically — makes
        # Python end the hook thread inside the Windows callback, and the
        # process dies with 0xC000041D (STATUS_FATAL_USER_CALLBACK_EXCEPTION).
        # Everything is saved and closed by now, so skip the shutdown phase.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)


if __name__ == "__main__":
    main()
