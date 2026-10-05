"""Keep the game drawing at full rate while its window on the desktop is not shown.

ADR 099 V3. The nested display is Xwayland running as a window on the
operator's desktop. The game hands each finished frame to Xwayland, and
Xwayland waits for the desktop to say the last one was shown. When the desktop
is not drawing that window (covered, minimised, another workspace), no such
word comes and Xwayland falls back to a timer:

    a frame delivered by swapping the whole buffer   -> one a second
    a frame delivered by copying                      -> sixty a second

The game's window fills the nested display exactly, so its frames are swapped
and it gets the slow timer. Measured 2026-10-04 11:27 to 11:34: covered by a
browser, the game's own counter fell to FPS 1, the aircraft stopped answering
the controls and died three times.

A window that another window overlaps cannot have its buffer swapped. This
guard keeps one pixel of its own on top of the game in a corner of the nested
display, which puts every frame on the copy path whatever the desktop is doing.

The reading of Xwayland above is from its source as remembered, not checked
against the installed version, which is why this ships switched off
(`nested.block_page_flips`) until a covered-window flight has confirmed it.

Every X call is made on the guard's own thread: an Xlib connection is not safe
to share between threads.
"""

import logging
import threading

logger = logging.getLogger(__name__)

_SHAPE_INPUT = 2        # the X Shape extension's input region (python-xlib 0.15 names only 0 and 1)
_SHAPE_SET = 0


def _open_display(name: str):
    from Xlib import display as xdisplay
    return xdisplay.Display(name)


class PresentCopyGuard:
    """One pixel kept on top of the game on `display_name`, for as long as it runs."""

    def __init__(self, display_name: str, x: int = 0, y: int = 0, raise_every_s: float = 5.0,
                 open_display=_open_display) -> None:
        self._display_name = display_name
        self._x = int(x)
        self._y = int(y)
        self._raise_every_s = float(raise_every_s)
        self._open_display = open_display
        self._stop = threading.Event()
        self._thread: "threading.Thread | None" = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="present-copy-guard")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)

    def _run(self) -> None:
        from Xlib import X
        display = None
        window = None
        try:
            display = self._open_display(self._display_name)
            screen = display.screen()
            # Override-redirect: the window manager leaves it alone, so it takes
            # no focus and gets no frame. Black, in the corner, one pixel.
            window = screen.root.create_window(
                self._x, self._y, 1, 1, 0, screen.root_depth, X.InputOutput, X.CopyFromParent,
                background_pixel=screen.black_pixel, override_redirect=True, event_mask=0)
            try:
                # No input region: a click on that pixel goes to the game under it.
                window.shape_rectangles(_SHAPE_SET, _SHAPE_INPUT, 0, 0, 0, [])
            except Exception as exc:
                logger.debug("PresentCopyGuard: no input shape on %s (%s); the pixel will "
                             "take clicks made exactly on it", self._display_name, exc)
            window.map()
            window.configure(stack_mode=X.Above)
            display.sync()
            logger.info("ADR 099 V3: one pixel held on top of the game at (%d,%d) on %s, so its "
                        "frames are copied and it keeps drawing while its window is not shown",
                        self._x, self._y, self._display_name)
            # The game can restack itself, and a restarted game maps a new
            # window on top. Put the pixel back above every few seconds.
            while not self._stop.wait(timeout=self._raise_every_s):
                window.configure(stack_mode=X.Above)
                display.flush()
        except Exception as exc:
            logger.warning("PresentCopyGuard: stopped on %s: %s. The game may fall to one frame "
                           "a second while its window is not shown.", self._display_name, exc)
        finally:
            try:
                if window is not None:
                    window.destroy()
                if display is not None:
                    display.sync()
                    display.close()
            except Exception as exc:
                logger.debug("PresentCopyGuard: close on %s: %s", self._display_name, exc)
