"""Put the nested display back to its configured size when its window shrinks it.

ADR 162 / Anomaly 015. ADR 104 took `-geometry` to fix the nested display's
screen for the life of the server. On Xwayland 24.1.10 it only sets the starting
size: started with `-decorate`, the server is an ordinary resizable window on
the operator's desktop, and it resizes its own screen to match whenever that
window changes size. On 2026-10-25 at 16:21:27 the window lost 3 pixels of
height, the screen became 1920x1197, and every grab of the configured 1920x1200
region failed for ten minutes while wingman pressed keys it could not see.

This guard reads the screen's size every second. When it is smaller than
`nested.size` in either direction it sets the RandR mode back with `xrandr`,
which is what restored that session by hand. A larger screen is left alone:
capture still fits inside it, and a `-fullscreen` window on a bigger monitor
would otherwise be fought every second.

Every X call is made on the guard's own thread: an Xlib connection is not safe
to share between threads.
"""

import logging
import re
import subprocess
import threading

logger = logging.getLogger(__name__)

# How long to wait after a restore that failed before trying again, so a
# missing xrandr or a refused mode is one warning every half minute, not a
# warning a second.
_RETRY_AFTER_FAILURE_S = 30.0
_CONNECTED_OUTPUT = re.compile(r"^(\S+) connected", re.MULTILINE)


def _open_display(name: str):
    from Xlib import display as xdisplay
    return xdisplay.Display(name)


def parse_size(size: str) -> "tuple[int, int]":
    m = re.match(r"^\s*(\d+)x(\d+)\s*$", str(size))
    if not m:
        raise ValueError(f"nested.size must look like 1920x1200, not {size!r}")
    return int(m.group(1)), int(m.group(2))


class NestedSizeGuard:
    """Keeps the screen of `display_name` at least `size`, for as long as it runs."""

    def __init__(self, display_name: str, size: str, check_every_s: float = 1.0,
                 open_display=_open_display, run=subprocess.run) -> None:
        self._display_name = display_name
        self._size = str(size).strip()
        self._width, self._height = parse_size(self._size)
        self._check_every_s = float(check_every_s)
        self._open_display = open_display
        self._run_cmd = run
        self._output: "str | None" = None
        self.restores = 0
        self._stop = threading.Event()
        self._thread: "threading.Thread | None" = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="nested-size-guard")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)

    def _run(self) -> None:
        display = None
        try:
            display = self._open_display(self._display_name)
            logger.info("ADR 162: keeping %s at %s or larger", self._display_name, self._size)
            wait_s = self._check_every_s
            while not self._stop.wait(timeout=wait_s):
                wait_s = (self._check_every_s if self.check_once(display)
                          else _RETRY_AFTER_FAILURE_S)
        except Exception as exc:
            if self._stop.is_set():
                logger.debug("NestedSizeGuard: %s closed during shutdown: %s",
                             self._display_name, exc)
            else:
                logger.warning("NestedSizeGuard: stopped on %s: %s. If its window is resized, "
                               "capture fails until the screen is %s again.",
                               self._display_name, exc, self._size)
        finally:
            try:
                if display is not None:
                    display.close()
            except Exception as exc:
                logger.debug("NestedSizeGuard: close on %s: %s", self._display_name, exc)

    def check_once(self, display) -> bool:
        """Restore the size if the screen is too small. False only when a restore failed."""
        geometry = display.screen().root.get_geometry()
        width, height = int(geometry.width), int(geometry.height)
        if width >= self._width and height >= self._height:
            return True
        try:
            output = self._output or self._find_output()
            result = self._xrandr("--output", output, "--mode", self._size)
        except (OSError, subprocess.SubprocessError, LookupError) as exc:
            logger.warning("ADR 162: %s is %dx%d, smaller than %s, and could not be restored: "
                           "%s. Every grab fails until it is.",
                           self._display_name, width, height, self._size, exc)
            return False
        if result.returncode != 0:
            logger.warning("ADR 162: %s is %dx%d, smaller than %s; xrandr refused the restore: %s",
                           self._display_name, width, height, self._size,
                           (result.stderr or "").strip() or f"exit {result.returncode}")
            return False
        self._output = output
        self.restores += 1
        logger.warning("ADR 162: %s shrank to %dx%d (its window was resized); set back to %s "
                       "(%d this session)", self._display_name, width, height, self._size,
                       self.restores)
        return True

    def _find_output(self) -> str:
        result = self._xrandr("--query")
        m = _CONNECTED_OUTPUT.search(result.stdout or "")
        if result.returncode != 0 or not m:
            raise LookupError(f"no connected output in `xrandr --query` "
                              f"(exit {result.returncode})")
        return m.group(1)

    def _xrandr(self, *args: str):
        return self._run_cmd(["xrandr", "--display", self._display_name, *args],
                             capture_output=True, text=True, timeout=5.0)
