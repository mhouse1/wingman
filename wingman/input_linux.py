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

import contextlib
import logging
import os
import sys
import threading
import time

logger = logging.getLogger(__name__)


_WINGMAN_XAUTH = "/tmp/wingman_click_auth.db"


def _ensure_xauthority() -> None:
    """Ensure XAUTHORITY points to an xauth file with an explicit display entry.

    The mutter XWayland auth file uses an empty display number (wildcard) that
    libX11 accepts but python-xlib does not match. We copy the cookie into a new
    file with an explicit entry for the current DISPLAY so python-xlib can
    connect.
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

    # Extract the cookie and write a new db with an explicit entry for the
    # actual display number (not necessarily ":0" — DISPLAY varies by session).
    display_entry = os.environ.get("DISPLAY", ":0").strip()
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
            ["xauth", "-f", _WINGMAN_XAUTH, "add", display_entry, "MIT-MAGIC-COOKIE-1", cookie],
            check=True, timeout=5,
        )
        os.environ["XAUTHORITY"] = _WINGMAN_XAUTH
        logger.debug(
            "Controller: XAUTHORITY set to %s (explicit %s entry)",
            _WINGMAN_XAUTH, display_entry,
        )
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
        display_name = _inject_display_name()
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


# --- Injection vs observation display (ADR 099) ------------------------------
#
# DISPLAY does three jobs in this module and they do NOT want the same value
# once the game runs on a nested display:
#
#   capture + injection  ->  the nested display, where the game lives
#   hotkey observation   ->  the OPERATOR's display, where their hands are
#
# XRecord in `_listener_loop` observes real keypresses so the operator can hit
# backspace to exit or i/j/k/l to take over. Point that at the nested display
# and those keys are only seen while the nested window has focus - i.e. exactly
# when the operator is NOT working elsewhere, which is the entire point of the
# lane. Manual takeover is a safety property, so the two displays are split:
# injection reads this override, observation keeps reading os.environ.
_injection_display = None
_injected_keys = frozenset()
# SAF-001: flight-control and arrow keys must ALWAYS reach the handler, even on
# the injection display where wingman presses them itself. Discriminating
# wingman's own echoes from the operator's hand is SAF-001.1's job and belongs
# to `_programmatic_key_counts` plus its post-release grace window, which is the
# mechanism that ran for a year before the nested lane existed — injection and
# observation shared one display then, so every injected maneuver key echoed
# back and was correctly ignored.
#
# Filtering them here instead silently removed manual takeover: with the game on
# its own display, pressing i/j/k/l at the game window did nothing at all.
_takeover_keys = frozenset()
# SAF-001: keys the operator uses to hand the aircraft BACK. Delivered on the
# injection display too, but only while the takeover is active — wingman
# injects nothing during manual except flares, so there is nothing they could
# be confused with. Outside manual they stay filtered, because wingman does
# press them itself ('u' starts the J20 mission).
_handback_keys = frozenset()
_manual_state_fn = None


def set_handback_keys(keys, manual_state_fn=None) -> None:
    """Declare the hand-back keys and how to ask whether manual is active."""
    global _handback_keys, _manual_state_fn
    _handback_keys = {str(k).lower() for k in (keys or ())}
    if manual_state_fn is not None:
        _manual_state_fn = manual_state_fn


def _manual_active() -> bool:
    try:
        return bool(_manual_state_fn and _manual_state_fn())
    except Exception:
        return False

# ADR 099: X modifier mask required for hotkeys observed on the OPERATOR's
# display while the nested lane is active. ControlMask (1<<2) | Mod1Mask (1<<3).
#
# Before the nested lane the game held focus on the operator's display, so bare
# single-letter hotkeys were unreachable by ordinary typing — the keys went to
# the game. Moving the game to its own display freed the operator's keyboard,
# which is the point, and simultaneously made every hotkey fire from ordinary
# typing: observed 2026-08-30, stray 'm' presses forced GAME_LOBBY three times
# and cancelled matchmaking so the session never reached battle. 'z' would have
# closed the game outright.
#
# On the NESTED display bare keys still work: the only way to type there is to
# focus the game window, which is an explicit act.
_OPERATOR_MOD_MASK = (1 << 2) | (1 << 3)


def set_injection_display(display_name) -> None:
    """Route key/mouse injection to `display_name` (None restores DISPLAY).

    Observation is deliberately unaffected - see the note above.
    """
    global _injection_display
    _injection_display = display_name.strip() if display_name else None
    if _injection_display:
        logger.info("ADR 099: injection routed to display %r; hotkeys observed "
                    "on %s", _injection_display,
                    ", ".join(repr(n) for n in _observe_display_names()))


def _inject_display_name() -> str:
    """Display for XTest injection: the override if set, else DISPLAY."""
    return _injection_display or os.environ.get("DISPLAY", ":0").strip()


def set_takeover_keys(keys) -> None:
    """Declare the keys that must never be filtered (SAF-001)."""
    global _takeover_keys
    _takeover_keys = {str(k).lower() for k in (keys or ())}


def set_injected_keys(keys) -> None:
    """Declare the keys wingman itself injects (ADR 099).

    On the INJECTION display these are wingman's own keystrokes, never the
    operator's, and must not fire operator hotkeys. Before the nested lane this
    could not happen — injection went to one display and observation to another —
    so adding the second listener introduced it.

    Observed 2026-08-30 08:27: wingman's own 'u', 'p' and 'm' injections fired
    the J20-mission, padlock and force-lobby hotkeys, which drove the FSM into a
    GAME_STARTING/GAME_LOBBY oscillation that never reached battle.

    Filtering by display is race-free, unlike counting presses and debiting them
    on observation: XRecord delivery is asynchronous, so a count can already
    have been decremented by the time its own event arrives. The maneuver path
    needs a post-release grace window for exactly that reason.
    """
    global _injected_keys
    _injected_keys = {str(k).lower() for k in (keys or ())}


def should_deliver_hotkey(display_name: str, key_name: str, state: int) -> bool:
    """Should an observed keypress fire its operator hotkey? ADR 099.

    Two filters, and they apply to different displays:

    - On the INJECTION display, a key wingman injects is wingman's own. Filtering
      by display is race-free, unlike counting presses and debiting them on
      observation, because XRecord delivery is asynchronous.
    - On the OPERATOR's display, while the game lives elsewhere, a bare keypress
      is ordinary typing and must not drive the aircraft.

    With no nested lane there is one display, the game holds focus on it, and
    both filters are inert — the on-screen lane behaves exactly as before.
    """
    if _injection_display is None:
        return True
    if display_name == _injection_display:
        # SAF-001: the hand-back key gets through while the operator holds the
        # aircraft. Wingman injects nothing during manual but flares, so this
        # cannot be one of its own presses; outside manual it stays filtered
        # because wingman does press it itself.
        if _manual_active() and key_name.lower() in _handback_keys:
            return True
        # SAF-001: takeover on this display uses only keys wingman NEVER injects
        # — ENTER and the arrow keys. On a shared display wingman's own presses
        # and the operator's are indistinguishable in content, and telling them
        # apart by timing was measured failing: echoes arrived 1.67-9.74 s after
        # release against a 1.0 s grace, producing four spurious takeovers in
        # 23 minutes on 2026-08-30. A key wingman never presses has nothing to
        # discriminate.
        # — the arrow keys, which the requirement names explicitly alongside
        # i/j/k/l. For i/j/k/l here the two sources are indistinguishable:
        #
        # Echo discrimination (SAF-001.1) assumes an injected key echoes back
        # promptly, and it did while injection and observation shared one
        # display. On the nested lane, under 13 OCR workers, echoes were
        # measured arriving 1.67-9.74 s after release against a 1.0 s grace
        # window — four spurious takeovers in 23 minutes on 2026-08-30, each
        # dropping the aircraft out of automation mid-round.
        #
        # Widening the grace is not a fix: it would suppress the operator's own
        # presses for the same seconds, against SAF-001's 2.0 s cessation bound.
        # Arrow keys carry no such ambiguity, so takeover on the injection
        # display is unconditional and race-free through them.
        return key_name.lower() not in _injected_keys
    return (state & _OPERATOR_MOD_MASK) == _OPERATOR_MOD_MASK


def _observe_display_names() -> "list[str]":
    """Displays the hotkey listener must watch. ADR 099.

    Keeping observation on the operator's DISPLAY is necessary but NOT
    sufficient. On a Wayland session that DISPLAY is a *rootless* Xwayland,
    which only receives key events while an X11 client holds focus. Before the
    nested lane the game was that client, so the operator's keys reached
    Xwayland and XRecord saw them. Moving the game to its own display removed
    the only X client that was ever focused, and every hotkey went dead —
    backspace and the SAF-001 manual takeover included.

    So the injection display is observed too. When the operator is looking at
    the nested window, their keys are delivered into that server and are only
    visible there. Observing it also means wingman sees its OWN injected keys,
    which is exactly the pre-nested topology that `_programmatic_key_counts`
    already exists to handle.

    Native-Wayland windows (e.g. VS Code) remain invisible to both — a
    pre-existing X11 limitation this does not change.
    """
    names = [os.environ.get("DISPLAY", ":0").strip()]
    inject = _inject_display_name()
    if inject and inject not in names:
        names.append(inject)
    return names


# --- Shared XTest display (ADR 091) -----------------------------------------
# Key injection used to open a throwaway Xlib Display per event. Every
# Display.__init__ rebuilds the resource classes at Xlib/display.py:121, and
# those survive both close() and gc.collect() — measured at ~16.2 KB retained
# per construction. Over a 1h46m session that was 1,277 MB from this one site,
# 96% of all post-warm-up heap growth (Performance 008).
#
# So: one connection, reused. Xlib Display objects are NOT safe for concurrent
# use, and injection comes from the main loop, the behaviour tree and hotkey
# callbacks — the per-call Displays were providing isolation for free, so the
# shared one has to take a lock instead.
_display_lock = threading.RLock()
_shared_display = None


def _shared_xtest_display(display_name: str):
    """Return the process-wide XTest display, opening it on first use.

    Callers must hold `_display_lock`.
    """
    global _shared_display
    if _shared_display is None:
        from Xlib import display as _xdisplay
        _shared_display = _xdisplay.Display(display_name)
        logger.debug("XTest: opened shared display %r", display_name)
    return _shared_display


def _drop_shared_display() -> None:
    """Close and forget the shared display so the next call reconnects.

    Called on any injection failure: a half-dead connection must not be reused
    for the release half of a press/release pair.
    """
    global _shared_display
    d, _shared_display = _shared_display, None
    if d is None:
        return
    # A connection dropped because it broke will usually fail to close cleanly;
    # that is the normal path here, not an error worth surfacing.
    with contextlib.suppress(Exception):
        d.close()


def _linux_key_event(key: str, event_type) -> None:
    """Inject a single KeyPress or KeyRelease event via XTest.

    Retries once on transient failure, dropping the shared connection in
    between so the retry reconnects: a single failed injection between a press
    and its release would otherwise leave the key logically held in the X
    server for the rest of the session (XTest key state is server-side and does
    not die with this client).
    """
    _ensure_xauthority()
    from Xlib import X as _X, XK as _XK
    from Xlib.ext import xtest as _xtest
    xk_name = _XKEY_ALIASES.get(key.lower(), key.lower())
    keysym = _XK.string_to_keysym(xk_name)
    if keysym == 0:
        logger.warning("Linux key: unknown keysym for %r", key)
        return
    display_name = _inject_display_name()
    last_err = None
    with _display_lock:
        for attempt in (1, 2):
            try:
                d = _shared_xtest_display(display_name)
                keycode = d.keysym_to_keycode(keysym)
                if keycode == 0:
                    logger.warning("Linux key: no keycode for keysym %d (%r)", keysym, key)
                    return
                _xtest.fake_input(d, event_type, keycode)
                d.sync()
                return
            except Exception as e:
                last_err = e
                _drop_shared_display()
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

        # ADR 099: one listener per observed display, so the maps are keyed by
        # display name. Used by unhook_all to disable every record context.
        self._contexts: dict = {}   # display name -> (d_ctrl, ctx)
        self._threads: dict = {}    # display name -> Thread

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
        for _disp, (_ctrl, _ctx) in list(self._contexts.items()):
            try:
                _ctrl.record_disable_context(_ctx)
                _ctrl.flush()
            except Exception as e:
                # Shutdown path: the listener thread is a daemon and exits with
                # the process either way, so this is benign — but it is the only
                # evidence that unhook_all did not stop the context cleanly.
                logger.debug("XKey: unhook_all could not disable the record context: %s", e)

    # --- Listener thread ---

    def _ensure_listener(self) -> None:
        names = _observe_display_names()
        if all(t is not None and t.is_alive()
               for t in (self._threads.get(n) for n in names)):
            return
        self._stop.clear()
        for name in names:
            t = self._threads.get(name)
            if t is not None and t.is_alive():
                continue
            t = threading.Thread(
                target=self._listener_loop, args=(name,), daemon=True,
                name=f"XKeyListener{name}",
            )
            self._threads[name] = t
            t.start()
            logger.info("XKey: observing hotkeys on display %r", name)

    def _listener_loop(self, display_name: str) -> None:
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

                # display_name is this listener's own display, passed in by
                # _ensure_listener - NOT read from the environment, which would
                # collapse every listener onto the operator's display.

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

                self._contexts[display_name] = (d_ctrl, ctx)

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
                        if not should_deliver_hotkey(
                                display_name, key_name,
                                getattr(event, "state", 0)):
                            continue
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
                self._contexts.pop(display_name, None)
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
                self._contexts.pop(display_name, None)
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
