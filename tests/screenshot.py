"""Snipping-tool style region screenshot for Wingman (`make screenshot`).

The whole desktop (every monitor) dims; drag a rectangle anywhere on it. The
dimming then goes away and a thin outline marks the region while you keep using
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

Captures go through mss, so they see what an X11 or Windows desktop shows. A
GNOME Wayland session hides native Wayland windows from it.
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
_OVERLAY_ALPHA = 0.3


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


# ---------------------------------------------------------------------------
# Capture and save
# ---------------------------------------------------------------------------

def _save(sct, region: dict) -> "Path | None":
    if not capture_budget.admit(_OUTPUT_DIR, "make screenshot", patterns="screenshot_*.png"):
        print("[SKIP] disk below the capture-budget floor - nothing saved.")
        return None
    frame = np.asarray(sct.grab(region))[:, :, :3]  # BGRA -> BGR
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
    def __init__(self, sct, keys):
        self._sct = sct
        self._screen = sct.monitors[0]  # the whole virtual desktop
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

    def _build_overlay(self) -> None:
        s = self._screen
        ov = tk.Toplevel(self._root)
        ov.overrideredirect(True)
        ov.geometry(_geometry(s["left"], s["top"], s["width"], s["height"]))
        ov.attributes("-topmost", True)
        ov.attributes("-alpha", _OVERLAY_ALPHA)
        canvas = tk.Canvas(ov, bg="black", highlightthickness=0, cursor="crosshair")
        canvas.pack(fill=tk.BOTH, expand=True)
        canvas.create_text(s["width"] // 2, 40, fill="white", font=("Segoe UI", 16),
                           text="Drag to select a region  -  Esc or Backspace to exit")
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
        path = _save(self._sct, self._region)
        if path is not None:
            self._flash()
            print(f"  Saved {self._region['width']}x{self._region['height']} -> {path}")

    def _exit(self) -> None:
        self._root.destroy()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main():
    if sys.platform != "win32" and os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
        print("[WARN] Wayland session: mss cannot see native Wayland windows, so captures "
              "may come out black. Run under an X11 session for reliable results.")

    keys = _keyboard_backend()
    if keys is None:
        print("[ERROR] no keyboard backend for global hotkeys (`keyboard` module missing).")
        sys.exit(1)

    _set_dpi_aware()
    print("Drag a region anywhere on screen. Then Enter captures it (from any window); "
          "Backspace exits.")
    try:
        with mss.mss() as sct:
            _Snipper(sct, keys).run()
    finally:
        with contextlib.suppress(Exception):
            keys.unhook_all()

    print("[DONE] Screenshot tool closed.")


if __name__ == "__main__":
    main()
