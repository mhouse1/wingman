"""Linux X11 input injection and hotkey observation.

Extracted from `controller.py` (Future 002 finding A-01). This subsystem is the
one that produced three distinct production incidents — stuck keys, delayed-echo
false takeovers, and XTest release latency under load — so it is isolated here
with its own tests and ownership rather than buried in the Controller god object.

Two responsibilities, deliberately kept together because they share the
XAUTHORITY bootstrap and the keysym alias table:

- **Injection** (`_linux_click`, `_linux_key_event`): XTest fake_input over a
  throwaway Display per call. No root required; works against XWayland windows.
- **Observation** (`_LinuxXTestKeyboard`): XRecord passive observation of real
  keystrokes, chosen over XGrabKey because a grab prevents the key from reaching
  the game window.

Two invariants this file must keep:

1. **XTest key state lives in the X server, not in this process.** A key pressed
   here and not released stays held for the rest of the X session, surviving
   process death. Every press path must have a release path that runs even on
   error.

2. **This module must stay importable on Windows.** Wingman is platform-agnostic
   (Windows and Linux), and `controller.py` imports this module *unconditionally*
   so it can re-export the symbols `conftest.py`, `move_game_window.py` and the
   tests reach for. That works only because every import at module scope is
   stdlib: `Xlib` is imported lazily inside the functions that use it, and
   `_LinuxXTestKeyboard` is only instantiated behind the platform check in
   `maybe_install_linux_keyboard`. Hoisting any `from Xlib import ...` to the top
   of this file would break Windows startup and nothing on Linux would notice —
   `test_input_linux.py::test_module_scope_imports_are_stdlib_only` fails the
   build instead.
"""

import logging
import os
import sys
import threading
import time

logger = logging.getLogger(__name__)


_WINGMAN_XAUTH = "/tmp/wingman_click_auth.db"


def _ensure_xauthority() -> None:
    """Ensure XAUTHORITY points to an xauth file with an explicit :0 display entry.

    The mutter XWayland auth file uses an empty display number (wildcard) that
    libX11 accepts but python-xlib does not match. We copy the cookie into a new
    file with an explicit ':0' entry so python-xlib can connect.
    """
    import glob
    import subprocess

    if os.environ.get("XAUTHORITY") == _WINGMAN_XAUTH and os.path.exists(_WINGMAN_XAUTH):
        return

    # Locate the mutter XWayland auth file
    uid = os.getuid() if hasattr(os, "getuid") else 0
    src = None
    for path in glob.glob(f"/run/user/{uid}/.mutter-Xwaylandauth.*"):
        src = path
        break
    if src is None:
        src = os.environ.get("XAUTHORITY", "")
    if not src or not os.path.exists(src):
        logger.warning("Controller: no XWayland auth file found — click may fail")
        return

    # Extract the cookie and write a new db with explicit ':0' display number
    try:
        r = subprocess.run(
            ["xauth", "-f", src, "list"],
            capture_output=True, text=True, timeout=5,
        )
        cookie = None
        for line in r.stdout.splitlines():
            if "MIT-MAGIC-COOKIE-1" in line:
                cookie = line.split()[-1]
                break
        if not cookie:
            logger.warning("Controller: could not extract MIT-MAGIC-COOKIE-1 from %s", src)
            return
        subprocess.run(
            ["xauth", "-f", _WINGMAN_XAUTH, "add", ":0", "MIT-MAGIC-COOKIE-1", cookie],
            check=True, timeout=5,
        )
        os.environ["XAUTHORITY"] = _WINGMAN_XAUTH
        logger.debug("Controller: XAUTHORITY set to %s (explicit :0 entry)", _WINGMAN_XAUTH)
    except Exception as e:
        logger.warning("Controller: failed to create xauth db: %s", e)


def _linux_click(x: int, y: int, count: int = 1) -> None:
    """Left-click at absolute screen coordinates via python-xlib XTest.

    Works for XWayland windows (Wine/DXVK games) without root.
    XAUTHORITY is resolved from the mutter socket if not set in the environment.
    """
    _ensure_xauthority()
    try:
        from Xlib import display as _xdisplay, X as _X
        from Xlib.ext import xtest as _xtest
        display_name = os.environ.get("DISPLAY", ":0").strip()
        d = _xdisplay.Display(display_name)
        _xtest.fake_input(d, _X.MotionNotify, x=x, y=y)
        d.sync()
        time.sleep(0.05)
        for i in range(count):
            _xtest.fake_input(d, _X.ButtonPress, detail=1)
            d.sync()
            time.sleep(0.05)
            _xtest.fake_input(d, _X.ButtonRelease, detail=1)
            d.sync()
            if i < count - 1:
                time.sleep(0.5)
        d.close()
    except Exception as e:
        logger.error("Linux click at (%d, %d) failed: %s", x, y, e)


# XK name overrides for key names that differ from python-xlib's XK strings
_XKEY_ALIASES = {
    "space": "space",
    "backspace": "BackSpace",
    "enter": "Return",
    "escape": "Escape",
    "esc": "Escape",
    "tab": "Tab",
    "shift": "Shift_L",
    "ctrl": "Control_L",
    "alt": "Alt_L",
    "end": "End",
    "home": "Home",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    # Punctuation must use its X11 keysym NAME — string_to_keysym(';') returns
    # 0 (observed 2026-08-11 07:27:22: "unknown keysym for ';'" from cleanup's
    # YAW_LEFT release; ADR 070 V1). Letters and digits resolve as themselves.
    ";": "semicolon",
    "'": "apostrophe",
    ",": "comma",
    ".": "period",
    "/": "slash",
    "\\": "backslash",
    "[": "bracketleft",
    "]": "bracketright",
    "-": "minus",
    "=": "equal",
    "`": "grave",
}


def _linux_key_event(key: str, event_type) -> None:
    """Inject a single KeyPress or KeyRelease event via XTest.

    Retries once on transient failure: each call opens a throwaway Display, so
    a single failed connection between a press and its release would otherwise
    leave the key logically held in the X server for the rest of the session
    (XTest key state is server-side and does not die with this client).
    """
    _ensure_xauthority()
    from Xlib import display as _xdisplay, XK as _XK
    from Xlib.ext import xtest as _xtest
    xk_name = _XKEY_ALIASES.get(key.lower(), key.lower())
    keysym = _XK.string_to_keysym(xk_name)
    if keysym == 0:
        logger.warning("Linux key: unknown keysym for %r", key)
        return
    display_name = os.environ.get("DISPLAY", ":0").strip()
    last_err = None
    for attempt in (1, 2):
        try:
            d = _xdisplay.Display(display_name)
            try:
                keycode = d.keysym_to_keycode(keysym)
                if keycode == 0:
                    logger.warning("Linux key: no keycode for keysym %d (%r)", keysym, key)
                    return
                _xtest.fake_input(d, event_type, keycode)
                d.sync()
                return
            finally:
                d.close()
        except Exception as e:
            last_err = e
            if attempt == 1:
                time.sleep(0.05)
    logger.error("Linux key event for %r failed after retry: %s", key, last_err)


class _XKeyEvent:
    """Minimal keyboard event passed to hotkey callbacks, mirroring keyboard.KeyboardEvent."""
    __slots__ = ("name", "is_injected", "event_type")

    def __init__(self, name: str, is_injected: bool) -> None:
        self.name = name
        self.is_injected = is_injected
        self.event_type = "down"


class _LinuxXTestKeyboard:
    """Drop-in shim for the `keyboard` module on Linux.

    - press / release / press_and_release: XTest injection, no root required.
    - on_press_key / add_hotkey: XGrabKey passive grab on the root window,
      no root required. Works for XWayland windows (including Wine/DXVK games).
      Keys are caught when any XWayland window has focus; native-Wayland windows
      (e.g. VS Code) will not trigger the grab.
    - Callbacks receive an _XKeyEvent with .name and .is_injected matching the
      keyboard.KeyboardEvent interface. XTest-injected events have is_injected=True
      (X11 send_event bit), so maneuver-key takeover logic ignores them correctly.
    """

    def __init__(self) -> None:
        self._pending: dict[str, object] = {}        # key_name -> callback, not yet grabbed
        self._grabbed: dict[int, tuple] = {}          # keycode -> (key_name, callback)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ctrl_display = None   # used by unhook_all to disable the record context
        self._record_ctx = None

    # --- Key injection (transient Display, no shared state) ---

    def press(self, key: str) -> None:
        from Xlib import X as _X
        _linux_key_event(key, _X.KeyPress)

    def release(self, key: str) -> None:
        from Xlib import X as _X
        _linux_key_event(key, _X.KeyRelease)

    def press_and_release(self, key: str) -> None:
        from Xlib import X as _X
        _linux_key_event(key, _X.KeyPress)
        time.sleep(0.05)
        _linux_key_event(key, _X.KeyRelease)

    # --- Hotkey registration ---

    def on_press_key(self, key: str, callback, suppress=False) -> None:  # noqa: ARG002 — `suppress` matches the keyboard module's signature and is passed by keyword
        with self._lock:
            self._pending[key.lower()] = callback
        self._ensure_listener()

    def add_hotkey(self, key: str, callback, *args, **kwargs) -> None:
        self.on_press_key(key, callback)

    def unhook_all(self) -> None:
        self._stop.set()
        if self._ctrl_display is not None and self._record_ctx is not None:
            try:
                self._ctrl_display.record_disable_context(self._record_ctx)
                self._ctrl_display.flush()
            except Exception as e:
                # Shutdown path: the listener thread is a daemon and exits with
                # the process either way, so this is benign — but it is the only
                # evidence that unhook_all did not stop the context cleanly.
                logger.debug("XKey: unhook_all could not disable the record context: %s", e)

    # --- Listener thread ---

    def _ensure_listener(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._listener_loop, daemon=True, name="XKeyListener"
        )
        self._thread.start()

    def _listener_loop(self) -> None:
        """Observe keyboard events via XRecord without consuming them.

        XGrabKey was ruled out because it prevents grabbed keys from reaching the
        game window. XRecord delivers events to our handler non-destructively —
        the game still receives every keystroke.

        XRecord requires two display connections:
          d_rec  — creates the context + calls record_enable_context (blocks)
          d_ctrl — calls record_disable_context to stop d_rec (stored for unhook_all)

        The outer loop retries on transient display errors (e.g. XWayland dropping
        the connection). It exits only when _stop is set (via unhook_all).
        """
        reconnect_attempts = 0
        while not self._stop.is_set():
            _ensure_xauthority()
            d_rec = None
            d_ctrl = None
            iter_done = threading.Event()  # wakes _stop_watcher when this iteration ends, so it can't outlive its Display connections
            try:
                from Xlib import display as _xdisplay, X as _X, XK as _XK
                from Xlib.ext import record as _record
                from Xlib.protocol import rq as _rq

                display_name = os.environ.get("DISPLAY", ":0").strip()

                # Resolve keycodes for pending registrations. On reconnect, _pending is
                # empty but _grabbed still holds previously resolved keycodes, which are
                # stable across reconnects to the same X server.
                d_setup = _xdisplay.Display(display_name)
                with self._lock:
                    snapshot = dict(self._pending)
                    self._pending.clear()
                for key_name, callback in snapshot.items():
                    xk_name = _XKEY_ALIASES.get(key_name, key_name)
                    keysym = _XK.string_to_keysym(xk_name)
                    if not keysym:
                        logger.warning("XKey: unknown keysym for %r", key_name)
                        continue
                    keycode = d_setup.keysym_to_keycode(keysym)
                    if not keycode:
                        logger.warning("XKey: no keycode for %r", key_name)
                        continue
                    self._grabbed[keycode] = (key_name, callback)
                    logger.debug("XKey: registered %r (keycode=%d)", key_name, keycode)
                d_setup.close()

                # d_rec: owns the recording context (create + enable, blocks)
                # d_ctrl: used only to disable the context (stored for unhook_all)
                d_rec = _xdisplay.Display(display_name)
                d_ctrl = _xdisplay.Display(display_name)

                ctx = d_rec.record_create_context(
                    0,
                    [_record.AllClients],
                    [{
                        "core_requests": (0, 0),
                        "core_replies": (0, 0),
                        "ext_requests": (0, 0, 0, 0),
                        "ext_replies": (0, 0, 0, 0),
                        "delivered_events": (0, 0),
                        "device_events": (_X.KeyPress, _X.KeyPress),
                        "errors": (0, 0),
                        "client_started": False,
                        "client_died": False,
                    }],
                )

                self._ctrl_display = d_ctrl
                self._record_ctx = ctx

                # Watcher: unblocks record_enable_context when _stop is set (e.g. on
                # abnormal exit where cleanup() never runs), or exits without acting
                # once this iteration has already ended (e.g. a reconnect after a
                # transient error) so it doesn't linger holding a stale Display.
                # Loop variables are bound as defaults, not captured. This thread
                # can outlive its iteration (the reconnect path sets iter_done
                # and sleeps 3 s before rebinding), and a late-bound closure
                # would then read the NEXT iteration's Event and Display: the
                # iter_done guard below would test a fresh, unset Event and the
                # watcher would go on to disable the *live* record context,
                # silently killing hotkeys — and with them the SAF-001 manual
                # takeover path. Found by ruff B023 (Research 006).
                def _stop_watcher(iter_done=iter_done, d_ctrl=d_ctrl, ctx=ctx):
                    while not self._stop.is_set() and not iter_done.is_set():
                        if self._stop.wait(timeout=0.5):
                            break
                    if iter_done.is_set() and not self._stop.is_set():
                        return
                    try:
                        d_ctrl.record_disable_context(ctx)
                        d_ctrl.flush()
                    except Exception as e:
                        logger.debug("XKey: stop-watcher could not disable the record context: %s", e)

                threading.Thread(target=_stop_watcher, daemon=True, name="XKeyListener-stop").start()

                _ef = _rq.EventField(None)

                # Same binding discipline as _stop_watcher. This one is consumed
                # synchronously by record_enable_context below, so it cannot
                # actually outlive the iteration — bound explicitly anyway so the
                # rule holds for every closure in this loop rather than relying on
                # a call-ordering argument that a later edit could invalidate.
                def _record_handler(reply, _ef=_ef, d_rec=d_rec, display_name=display_name):
                    if reply.category != _record.FromServer:
                        return
                    data = reply.data
                    while len(data) >= 32:
                        event, data = _ef.parse_binary_value(
                            data, d_rec.display, None, None
                        )
                        if event.type != _X.KeyPress:
                            continue
                        # Pick up any keys registered after the loop started
                        with self._lock:
                            new = dict(self._pending)
                            self._pending.clear()
                        if new:
                            d_tmp = _xdisplay.Display(display_name)
                            for kn, cb in new.items():
                                xkn = _XKEY_ALIASES.get(kn, kn)
                                ks = _XK.string_to_keysym(xkn)
                                kc = d_tmp.keysym_to_keycode(ks) if ks else 0
                                if kc:
                                    self._grabbed[kc] = (kn, cb)
                                    logger.debug("XKey: registered %r (keycode=%d)", kn, kc)
                            d_tmp.close()

                        entry = self._grabbed.get(event.detail)
                        if not entry:
                            continue
                        key_name, cb = entry
                        ev_obj = _XKeyEvent(name=key_name,
                                            is_injected=bool(event.send_event))
                        try:
                            cb(ev_obj)
                        except Exception as exc:
                            logger.error("XKey callback error for %r: %s", key_name, exc)

                if reconnect_attempts:
                    logger.info("XKey: display reconnected after %d attempt(s) — hotkeys active again", reconnect_attempts)
                    reconnect_attempts = 0

                # Blocks until record_disable_context is called (from unhook_all)
                d_rec.record_enable_context(ctx, _record_handler)
                d_rec.record_free_context(ctx)
                d_rec.close()
                d_ctrl.close()
                self._ctrl_display = None
                self._record_ctx = None
                break  # clean exit: _stop was set via unhook_all
            except Exception as e:
                logger.error("XKey listener thread died: %s", e)
                iter_done.set()  # let this iteration's _stop_watcher exit instead of leaking
                if d_rec is not None:
                    try:
                        d_rec.close()
                    except Exception as close_err:
                        logger.debug("XKey: d_rec.close() failed during reconnect: %s", close_err)
                if d_ctrl is not None:
                    try:
                        d_ctrl.close()
                    except Exception as close_err:
                        logger.debug("XKey: d_ctrl.close() failed during reconnect: %s", close_err)
                self._ctrl_display = None
                self._record_ctx = None
                if not self._stop.is_set():
                    reconnect_attempts += 1
                    logger.info("XKey: reconnecting display in 3s (attempt %d)", reconnect_attempts)
                    self._stop.wait(timeout=3.0)


def maybe_install_linux_keyboard(fallback):
    """Return the XTest shim on Linux, or `fallback` (the `keyboard` module) elsewhere.

    Called on **every** platform — on Windows it returns `fallback` untouched and
    never instantiates the shim, which is why importing this module there costs
    nothing (see invariant 2 in the module docstring).

    Callers keep the result in their own module-level `keyboard_module` name so
    tests can monkeypatch it per-module; this function only decides which
    implementation that name starts out holding.
    """
    if sys.platform != "win32":
        logger.debug("Controller: using XTest keyboard shim (no root required)")
        return _LinuxXTestKeyboard()
    return fallback
