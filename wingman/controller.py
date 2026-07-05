import os
import time
import logging
import threading
import ctypes
import sys
import cv2
import numpy as np
from datetime import datetime
from pathlib import Path
from mss import mss

from .crop_region import CropCoords, crop_centre, draw_crops
from .analyzer import GameState

try:
    import keyboard as keyboard_module
except Exception:
    keyboard_module = None

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
}


def _linux_key_event(key: str, event_type) -> None:
    """Inject a single KeyPress or KeyRelease event via XTest."""
    _ensure_xauthority()
    try:
        from Xlib import display as _xdisplay, X as _X, XK as _XK
        from Xlib.ext import xtest as _xtest
        xk_name = _XKEY_ALIASES.get(key.lower(), key.lower())
        keysym = _XK.string_to_keysym(xk_name)
        if keysym == 0:
            logger.warning("Linux key: unknown keysym for %r", key)
            return
        display_name = os.environ.get("DISPLAY", ":0").strip()
        d = _xdisplay.Display(display_name)
        keycode = d.keysym_to_keycode(keysym)
        if keycode == 0:
            logger.warning("Linux key: no keycode for keysym %d (%r)", keysym, key)
            d.close()
            return
        _xtest.fake_input(d, event_type, keycode)
        d.sync()
        d.close()
    except Exception as e:
        logger.error("Linux key event for %r failed: %s", key, e)


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

    def on_press_key(self, key: str, callback, suppress=False) -> None:
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
            except Exception:
                pass

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
        while not self._stop.is_set():
            _ensure_xauthority()
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
                # abnormal exit where cleanup() never runs).
                def _stop_watcher():
                    self._stop.wait()
                    try:
                        d_ctrl.record_disable_context(ctx)
                        d_ctrl.flush()
                    except Exception:
                        pass

                threading.Thread(target=_stop_watcher, daemon=True, name="XKeyListener-stop").start()

                _ef = _rq.EventField(None)

                def _record_handler(reply):
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
                self._ctrl_display = None
                self._record_ctx = None
                if not self._stop.is_set():
                    logger.info("XKey: reconnecting display in 3s")
                    self._stop.wait(timeout=3.0)


if sys.platform != "win32":
    keyboard_module = _LinuxXTestKeyboard()
    logger.debug("Controller: using XTest keyboard shim (no root required)")


# Key bindings
NOSE_UP_KEY = 'i'
NOSE_DOWN_KEY = 'k'
ROLL_LEFT_KEY = 'j'
ROLL_RIGHT_KEY = 'l'
AFTERBURNER_KEY = 'e'
AIRBRAKE_KEY = 'd'
DEPLOY_FLARES_KEY = 'space'
FIRE_MACHINE_GUN = 'a'
FIRE_ACTIVE_WEAPON = 'f'
WINGSWEEP_KEY = 'w'
SWITCH_WEAPON = 'g'
SPECIAL_ABILITY = 'q'
PADLOCK_CAMERA = 'p'
ALT_FLIGHT_KEYS = ('up', 'down', 'left', 'right')  # Arrow keys also trigger GAME_BATTLE_MANUAL
TOGGLE_WEAPON_LOOP_KEY = 'x'  # Press X to toggle weapon firing loop
MISSION_J20_KEY = 'u'  # Press U to start J20 mission
MISSION_LOITER_KEY = 'y'  # Press Y to start loiter mission
CANCEL_MISSION_KEY = 'end'   # Press End to cancel active mission
CAPTURE_SCREEN_SHOT = 'v'  # Press V to capture a screenshot (for testing/debugging)
AUTO_MISSION_KEY = 'm'  # Press M to start an automatic mission based on detected game state (not implemented yet)
SIMULATE_RESPAWN_KEY = 'b'  # Press B to inject a fake respawn OCR result (testing)

# Available Emotes in-game
# The list here are for future use when implementing hldd003's request for emote support, and are not currently used in the codebase.
"""
EMOTE1 # Moving to , bind to numpad 1
EMOTE2 # Help!
EMOTE3 # Defend
EMOTE4 # Attack, bind to T, use with HLDD003's target painting mode for marking targets to attack with the weapon loop
EMOTE5 # Goodluck , bind to 'u', the same key as J20 mission for easy access at the start of a match
EMOTE6 # Well Played  
EMOTE7 # Wow!
EMOTE8 # Thanks!
EMOTE9 # Good Game!
EMOTE10 # Oops!
"""

# Region name constants — used as log labels in click_grid_region and elsewhere.
# Defining them as constants means the string is written once; a rename is a
# single-line change here rather than a grep-and-replace across the codebase.
REGION_GOOD_LUCK         = "good_luck"
REGION_EVENT_REFRESH     = "event_refresh"
REGION_PLAY_BUTTON       = "PLAY"
REGION_CLICK_TO_CONTINUE = "click_to_continue"
REGION_REVEAL_ALL        = "REVEAL_ALL"
REGION_TAP_HERE          = "TAP_HERE_TO_CONTINUE"
REGION_UNLOCK_CLOSE      = "UNLOCK_CLOSE"
REGION_FINAL_CONTINUE    = "FINAL_CONTINUE"

class Controller:
    def __init__(self, region, fire_button="left", fire_hold_seconds: float = 0.0, exit_event=None, analyzer=None, weapon_loop_interval: float = None, capture=None, on_auto_mission_key=None, crops: "dict[str, CropCoords] | None" = None, target_painting_mode: bool = False, simulate_os_input: bool = False, disable_hotkeys: bool = False, capture_with_overlay: bool = True, starting_max_wait_s: float = 90.0):
        # region is (left, top, width, height)
        self.region = region
        self.fire_button = fire_button
        self.fire_hold_seconds = float(fire_hold_seconds or 0.0)
        self._firing_lock = threading.Lock()
        self._mission_lock = threading.Lock()
        self._mission_complete = threading.Event()
        self._mission_cancel = threading.Event()
        self._exit_event = exit_event  # Event to signal program exit
        self._last_mission = None
        self._last_mission_lock = threading.Lock()
        self._analyzer = analyzer
        self._capture = capture
        self._on_auto_mission_key = on_auto_mission_key
        self._crops: "dict[str, CropCoords]" = crops or {}
        self._auto_respawn_restart = True  # cleared by manual End press; restored when a mission starts
        self._game_battle_since = 0.0  # timestamp of last GAME_BATTLE entry; used by grace period guard
        self._ready_button_region = 0  # grid region number for the ready-button click; 0 = not configured
        self._popup_last_clicked: "dict[str, float]" = {}  # popup name → timestamp of last click

        # Padlock camera cooldown: set when the key is pressed manually
        self._padlock_cooldown_until = 0.0

        # Target tracking: timestamp of last orient_nose_to_target command
        self._last_orient_ts: float = 0.0

        # Weapon loop state (configurable via config or start_weapon_loop)
        self._weapon_loop_active = False
        self._weapon_loop_thread = None
        self._weapon_loop_stop = threading.Event()
        self._weapon_loop_interval = float(weapon_loop_interval or 0.5)  # Firing interval from config or default
        self._starting_max_wait_s = float(starting_max_wait_s)

        # Search-and-destroy loop state (padlock + weapon fire; used during disengage)
        self._sdl_stop: threading.Event | None = None
        self._sdl_padlock_thread: threading.Thread | None = None
        self._sdl_weapon_thread: threading.Thread | None = None
        self._target_painting_mode = target_painting_mode
        self._simulate_os_input = bool(simulate_os_input)
        self._disable_hotkeys = bool(disable_hotkeys)
        self._capture_with_overlay = bool(capture_with_overlay)
        self._action_intents: list[dict] = []
        self._action_intents_lock = threading.Lock()

        # Eject-and-dive cancellation: set by End key to abort the dive thread early
        self._eject_stop = threading.Event()
        # Set while eject_and_dive thread is running; cleared by the thread's finally block.
        self._ejecting = threading.Event()

        # Tracks how many programmatic key presses are in flight.
        # keyboard.KeyboardEvent has no is_injected attribute, so the getattr guard
        # in the maneuver hooks always falls back to False and cannot distinguish
        # machine-generated from human-generated key events.  Incrementing this
        # counter before keyboard.press() and decrementing after keyboard.release()
        # lets the hooks skip cancel logic for keys the mission pressed itself.
        self._programmatic_key_count = 0
        self._programmatic_key_lock = threading.Lock()

        # Optional callback fired immediately when Good Luck OCR succeeds, with the
        # captured frame.  Used by live capture mode to record the fixture at the
        # moment of detection rather than 13s later when the FSM trigger fires.
        self._on_good_luck_frame = None

        # Optional callback fired immediately when the player presses a maneuver key
        # to trigger manual takeover (GAME_BATTLE → GAME_BATTLE_MANUAL).  The frame
        # is captured BEFORE the FSM transition so the screenshot still shows the
        # GAME_BATTLE HUD — used by live capture mode for P2_020.
        self._on_manual_takeover_frame = None

        # Exit script hotkey (Backspace).
        # Honor disable_hotkeys so replay/capture automation is not interrupted by
        # ambient keyboard events from the host environment.
        # Probe keyboard access on the first registration; if ImportError (Linux not in
        # 'input' group), emit one warning and skip all remaining hotkeys.
        _kbd_ok = True
        if keyboard_module and not self._disable_hotkeys:
            try:
                def exit_script_hotkey(e):
                    logger.info("Controller: Backspace key pressed - exiting script")
                    if self._exit_event:
                        self._exit_event.set()
                keyboard_module.on_press_key('backspace', exit_script_hotkey, suppress=False)
                logger.info("Controller: registered hotkey 'backspace' to exit script")
            except ImportError as e:
                logger.warning(
                    "Controller: keyboard hotkeys disabled — %s  "
                    "(fix: sudo usermod -aG input $USER then log out and back in)",
                    e,
                )
                _kbd_ok = False
            except Exception:
                logger.exception("Controller: failed to register exit script hotkey")

        # Register hotkey for weapon loop toggle and other hotkeys
        if keyboard_module and not self._disable_hotkeys and _kbd_ok:
            # Cancel mission hotkey (End)
            try:
                self._last_cancel_key_ts = 0.0
                def cancel_mission_hotkey(e):
                    now = time.time()
                    if now - self._last_cancel_key_ts < 0.5:  # debounce: ignore key-repeat
                        return
                    self._last_cancel_key_ts = now
                    logger.info("Controller: '%s' key pressed - cancelling mission and disabling auto-respawn restart", CANCEL_MISSION_KEY)
                    self._auto_respawn_restart = False
                    self._eject_stop.set()
                    self.cancel_mission()
                keyboard_module.on_press_key(CANCEL_MISSION_KEY, cancel_mission_hotkey, suppress=False)
                logger.info("Controller: registered hotkey '%s' to cancel mission", CANCEL_MISSION_KEY)
            except Exception:
                logger.exception("Controller: failed to register cancel mission hotkey")

            # Maneuver keys cancel mission when pressed during GAME_BATTLE (manual takeover)
            try:
                def maneuver_key_pressed(e):
                    self._handle_maneuver_key_press(
                        key_name=getattr(e, 'name', str(e)),
                        is_injected=getattr(e, 'is_injected', False),
                    )
                for _key in (NOSE_UP_KEY, NOSE_DOWN_KEY, ROLL_LEFT_KEY, ROLL_RIGHT_KEY, *ALT_FLIGHT_KEYS):
                    keyboard_module.on_press_key(_key, maneuver_key_pressed, suppress=False)
                logger.info(
                    "Controller: registered maneuver keys (%s/%s/%s/%s) and arrow keys to cancel mission on manual press",
                    NOSE_UP_KEY, NOSE_DOWN_KEY, ROLL_LEFT_KEY, ROLL_RIGHT_KEY,
                )
            except Exception:
                logger.exception("Controller: failed to register maneuver key hotkeys")
            try:
                keyboard_module.add_hotkey(TOGGLE_WEAPON_LOOP_KEY, self.toggle_weapon_loop)
                logger.info("Controller: registered hotkey '%s' to toggle weapon loop", TOGGLE_WEAPON_LOOP_KEY)
            except Exception:
                logger.exception("Controller: failed to register weapon loop hotkey")

            try:
                def start_j20_mission(e):
                    self._auto_respawn_restart = True
                    current_state = self._analyzer.game_state if self._analyzer is not None else None
                    if current_state == GameState.GAME_BATTLE_MANUAL:
                        # Only force FSM back to GAME_BATTLE when resuming from manual takeover.
                        logger.info(
                            "Controller: '%s' key pressed — resuming auto mode from GAME_BATTLE_MANUAL",
                            MISSION_J20_KEY,
                        )
                        if not self._analyzer.trigger_event("manual_force_battle"):
                            logger.warning("Controller: unable to force GAME_BATTLE via FSM trigger")
                    elif current_state == GameState.GAME_STARTING:
                        # XRecord captures the XTest-injected 'u' from the game_starting loop.
                        # Ignore it — the loop itself will launch mission_j20 after Good Luck.
                        logger.debug(
                            "Controller: '%s' key during GAME_STARTING — XTest echo from game_starting loop, ignoring",
                            MISSION_J20_KEY,
                        )
                        return
                    else:
                        logger.info("Controller: '%s' key pressed - starting J20 mission (state=%s)",
                                    MISSION_J20_KEY,
                                    current_state.name if current_state is not None and hasattr(current_state, 'name') else current_state)
                    self._set_last_mission("j20")
                    threading.Thread(target=self.mission_j20, daemon=True).start()
                keyboard_module.on_press_key(MISSION_J20_KEY, start_j20_mission, suppress=False)
                logger.info("Controller: registered hotkey '%s' to start J20 mission", MISSION_J20_KEY)
            except Exception:
                logger.exception("Controller: failed to register J20 mission hotkey")

            try:
                def start_loiter_mission(e):
                    logger.info("Controller: '%s' key pressed - starting loiter mission", MISSION_LOITER_KEY)
                    self._set_last_mission("loiter")
                    threading.Thread(target=self.mission_loiter, daemon=True).start()
                keyboard_module.on_press_key(MISSION_LOITER_KEY, start_loiter_mission, suppress=False)
                logger.info("Controller: registered hotkey '%s' to start loiter mission", MISSION_LOITER_KEY)
            except Exception:
                logger.exception("Controller: failed to register loiter mission hotkey")

            # Register hotkey for simulating respawn detected (for testing)
            try:
                self._simulate_respawn_flag = threading.Event()
                self._last_b_press_time = 0.0
                def simulate_respawn(e):
                    now = time.time()
                    if now - self._last_b_press_time < 0.5:  # debounce: ignore key-repeat
                        return
                    self._last_b_press_time = now
                    logger.info("Controller: '%s' key pressed - simulating respawn detected (as if OCR detected 'RESPAWN')", SIMULATE_RESPAWN_KEY)
                    if self._analyzer is not None:
                        self._analyzer.inject_respawn_ocr_result(True, 1.0, "ocr")
                        logger.info("Controller: Injected fake OCR respawn result into analyzer cache.")
                    else:
                        logger.warning("Controller: No analyzer reference to inject fake OCR respawn result.")
                    self._simulate_respawn_flag.set()
                keyboard_module.on_press_key(SIMULATE_RESPAWN_KEY, simulate_respawn, suppress=False)
                logger.info("Controller: registered hotkey '%s' to simulate respawn detected", SIMULATE_RESPAWN_KEY)
            except Exception:
                logger.exception("Controller: failed to register simulate respawn hotkey")

            # Register hotkey for capturing screenshots (for testing/debugging)
            try:
                def capture_screenshot(e):
                    logger.info("Controller: '%s' key pressed - capturing screenshot", CAPTURE_SCREEN_SHOT)
                    if self._capture is not None and self._analyzer is not None:
                        try:
                            frame = self._capture.grab_from_thread()
                            
                            # Create output directory if it doesn't exist
                            output_dir = Path("tests/test-output")
                            output_dir.mkdir(parents=True, exist_ok=True)

                            # Generate timestamp filename
                            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                            filename = output_dir / f"screenshot_{timestamp}.png"

                            if self._capture_with_overlay:
                                # Draw only state-relevant crop overlays when enabled.
                                crops = self._analyzer.crops_for_state()
                                frame = draw_crops(frame, crops)
                                logger.info("Controller: Screenshot saved to %s with crop overlays", filename)
                            else:
                                logger.info("Controller: Screenshot saved to %s without overlays", filename)

                            cv2.imwrite(str(filename), frame)
                        except Exception as e:
                            logger.exception("Controller: Failed to capture screenshot: %s", e)
                    else:
                        logger.warning("Controller: No capture or analyzer reference to take screenshot.")
                keyboard_module.on_press_key(CAPTURE_SCREEN_SHOT, capture_screenshot, suppress=False)
                logger.info("Controller: registered hotkey '%s' to capture screenshot", CAPTURE_SCREEN_SHOT)
            except Exception:
                logger.exception("Controller: failed to register capture screenshot hotkey")

            # Padlock camera cooldown hotkey: when P is pressed manually, suppress
            # the padlock loop for 10 seconds so it doesn't immediately re-lock.
            try:
                def padlock_key_pressed(e):
                    cooldown = 10.0
                    self._padlock_cooldown_until = time.time() + cooldown
                    logger.info("Controller: '%s' key pressed manually - padlock loop cooldown set for %.0fs", PADLOCK_CAMERA, cooldown)
                keyboard_module.on_press_key(PADLOCK_CAMERA, padlock_key_pressed, suppress=False)
                logger.info("Controller: registered hotkey '%s' to set padlock loop cooldown", PADLOCK_CAMERA)
            except Exception:
                logger.exception("Controller: failed to register padlock camera cooldown hotkey")

            # Auto-mission hotkey: force GAME_LOBBY state, then click PLAY/READY
            try:
                self._last_auto_mission_key_ts = 0.0
                def auto_mission_key_pressed(_e):
                    now = time.time()
                    if now - self._last_auto_mission_key_ts < 0.5:  # debounce: ignore key-repeat
                        return
                    self._last_auto_mission_key_ts = now
                    if self._analyzer is None:
                        return
                    if self._analyzer.game_state != GameState.GAME_LOBBY:
                        current_state = self._analyzer.game_state
                        logger.info(
                            "Controller: '%s' key pressed — forcing GAME_LOBBY (was %s)",
                            AUTO_MISSION_KEY, current_state.name if hasattr(current_state, 'name') else current_state,
                        )
                        self._analyzer.trigger_event("manual_reset")
                    if self._on_auto_mission_key is not None:
                        self._on_auto_mission_key()
                    crop = next(
                        (c for c in ("PLAY", "READY") if c in self._crops),
                        None,
                    )
                    if crop is None:
                        logger.warning("Controller: '%s' pressed but no PLAY/READY crop configured", AUTO_MISSION_KEY)
                        return
                    logger.info("Controller: '%s' pressed in GAME_LOBBY - clicking %s (waiting for CANCEL)", AUTO_MISSION_KEY, crop)
                    self.click_crop(self._crops[crop], block=False, count=1, region_name=crop)
                keyboard_module.on_press_key(AUTO_MISSION_KEY, auto_mission_key_pressed, suppress=False)
                logger.info("Controller: registered hotkey '%s' to click PLAY/READY in GAME_LOBBY", AUTO_MISSION_KEY)
            except Exception:
                logger.exception("Controller: failed to register auto mission hotkey")

    def _record_action_intent(self, action_type: str, **payload):
        intent = {
            "timestamp": time.time(),
            "action_type": action_type,
            **payload,
        }
        with self._action_intents_lock:
            self._action_intents.append(intent)

    def get_action_intents(self) -> list[dict]:
        with self._action_intents_lock:
            return list(self._action_intents)

    def set_on_good_luck_frame(self, callback) -> None:
        """Register callback fired when Good Luck OCR is detected with frame payload."""
        self._on_good_luck_frame = callback

    def set_on_manual_takeover_frame(self, callback) -> None:
        """Register callback fired before manual takeover FSM transition with frame payload."""
        self._on_manual_takeover_frame = callback

    def _handle_maneuver_key_press(self, key_name: str, is_injected: bool = False) -> bool:
        """Handle manual maneuver-key takeover logic.

        Returns True when the key press triggered mission cancel/manual takeover,
        otherwise False.
        """
        if is_injected:
            return False
        # _programmatic_key_count guards against is_injected being unreliable for keys
        # wingman actually injects (i/j/k/l). Arrow keys are never injected, so skipping
        # this check lets the user trigger manual takeover during continuous key holds
        # (afterburner, roll) without needing to find a gap between mission key presses.
        if key_name not in ALT_FLIGHT_KEYS:
            with self._programmatic_key_lock:
                if self._programmatic_key_count > 0:
                    return False
        if self._game_battle_since and time.time() - self._game_battle_since < 2.0:
            logger.debug(
                "Controller: Maneuver key '%s' ignored — within 2s grace period of battle or eject entry",
                key_name,
            )
            return False
        if not (self.is_mission_running() or self._ejecting.is_set()):
            return False

        logger.info("Controller: maneuver key '%s' pressed - entering GAME_BATTLE_MANUAL (manual takeover)", key_name)
        self._auto_respawn_restart = False
        self._eject_stop.set()
        self.cancel_mission()
        if self._analyzer is not None:
            try:
                if self._analyzer.game_state in (GameState.GAME_BATTLE, GameState.GAME_BATTLE_EJECT):
                    # Capture the pre-transition frame for live capture (P2_020).
                    # Frame is grabbed BEFORE trigger_event so the screenshot still
                    # shows the GAME_BATTLE HUD.
                    _mt_frame = None
                    if self._on_manual_takeover_frame is not None and self._capture is not None:
                        try:
                            _mt_frame = self._capture.grab_from_thread()
                        except Exception:
                            logger.exception("Controller: failed to capture manual takeover frame")
                    self._analyzer.trigger_event("manual_takeover")
                    if _mt_frame is not None and self._on_manual_takeover_frame is not None:
                        try:
                            self._on_manual_takeover_frame(_mt_frame)
                        except Exception:
                            logger.exception("Controller: _on_manual_takeover_frame callback failed")
            except Exception:
                pass
        return True


    def nose_up(self, hold_seconds: float = 2.5, block: bool = True):
        """Nose-up maneuver: presses and holds the configured nose-up key.

        Args:
            hold_seconds: How long to hold the key (default 2.5 seconds)
        """
        # Use generic executor to perform the key press
        self._execute_key_press(NOSE_UP_KEY, hold_seconds=hold_seconds, block=block, action_name='nose_up')
    
    def nose_down(self, hold_seconds: float = 2.5, block: bool = True):
        """Nose-down maneuver: presses and holds the configured nose-down key.

        Args:
            hold_seconds: How long to hold the key (default 2.5 seconds)
        """
        # Use generic executor to perform the key press
        self._execute_key_press(NOSE_DOWN_KEY, hold_seconds=hold_seconds, block=block, action_name='nose_down')

    def afterburner(self, hold_seconds: float = 2.5, block: bool = True):
        """Afterburner: presses and holds the configured afterburner key.

        Args:
            hold_seconds: How long to hold the key (default 2.5 seconds)
        """
        # Use generic executor to perform the key press
        self._execute_key_press(AFTERBURNER_KEY, hold_seconds=hold_seconds, block=block, action_name='afterburner')

    def _execute_key_press(self, key: str, hold_seconds: float = 2.5, block: bool = True, action_name: str | None = None, ignore_cancel: bool = False):
        """Generic key press executor used by maneuvers.

        Args:
            key: key name to press/release
            hold_seconds: duration to hold the key
            block: if True, run in current thread; otherwise spawn a daemon thread
            action_name: optional label for logging
        """
        label = action_name or key
        
        # Add color coding for specific actions
        color_start = ""
        color_end = ""
        if action_name == "deploy_flares":
            color_start = "\033[93m"  # Yellow
            color_end = "\033[0m"
        elif action_name == "padlock_camera":
            color_start = "\033[94m"  # Blue
            color_end = "\033[0m"
        elif action_name == "fire_active_weapon":
            color_start = "\033[95m"  # Magenta
            color_end = "\033[0m"

        complete_color_start = color_start
        complete_color_end = color_end
        if action_name == "fire_active_weapon":
            complete_color_start = ""
            complete_color_end = ""
        
        logger.debug("%sController: %s - pressing '%s' key for %s seconds%s", color_start, label, key, hold_seconds, color_end)

        def _do_press():
            try:
                if self._simulate_os_input:
                    self._record_action_intent("key_press", key=key, hold_seconds=float(hold_seconds), action=label)
                    start = time.time()
                    while (time.time() - start) < hold_seconds:
                        if not ignore_cancel:
                            if self._mission_cancel.wait(timeout=0.05):
                                logger.debug("Controller: %s cancelled", label)
                                break
                        else:
                            time.sleep(0.05)
                    self._record_action_intent("key_release", key=key, action=label)
                    logger.debug("%sController: %s complete%s", complete_color_start, label, complete_color_end)
                    return
                if not keyboard_module:
                    logger.error("Controller: keyboard library not available for %s", label)
                    return
                logger.debug("Controller: using keyboard library for '%s' press", key)
                with self._programmatic_key_lock:
                    self._programmatic_key_count += 1
                try:
                    keyboard_module.press(key)
                    start = time.time()
                    while (time.time() - start) < hold_seconds:
                        if not ignore_cancel:
                            if self._mission_cancel.wait(timeout=0.05):
                                logger.debug("Controller: %s cancelled", label)
                                break
                        else:
                            time.sleep(0.05)
                    try:
                        keyboard_module.release(key)
                    except Exception:
                        logger.exception("Controller: failed to release '%s' key", key)
                    logger.debug("%sController: %s complete%s", complete_color_start, label, complete_color_end)
                finally:
                    with self._programmatic_key_lock:
                        self._programmatic_key_count -= 1
            except Exception:
                logger.exception("Controller: %s failed", label)

        if block:
            _do_press()
        else:
            t = threading.Thread(target=_do_press, daemon=True)
            t.start()

    def airbrake(self, hold_seconds: float = 1.0, block: bool = True):
        """Apply airbrake by holding the configured airbrake key."""
        self._execute_key_press(AIRBRAKE_KEY, hold_seconds=hold_seconds, block=block, action_name='airbrake')

    def roll_left(self, hold_seconds: float = 0.3, block: bool = True):
        """Roll left by holding the configured roll-left key."""
        self._execute_key_press(ROLL_LEFT_KEY, hold_seconds=hold_seconds, block=block, action_name='roll_left')

    def roll_right(self, hold_seconds: float = 0.3, block: bool = True):
        """Roll right by holding the configured roll-right key."""
        self._execute_key_press(ROLL_RIGHT_KEY, hold_seconds=hold_seconds, block=block, action_name='roll_right')

    def orient_nose_to_target(
        self,
        error_norm: float,
        *,
        deadband: float = 0.05,
        kp: float = 0.30,
        min_hold_sec: float = 0.08,
        max_hold_sec: float = 0.35,
        cooldown_sec: float = 0.15,
    ) -> "str | None":
        """Apply proportional roll correction toward a target.

        Args:
            error_norm: Normalized horizontal error in [-1, 1].
                        Negative = target left of center → roll left.
                        Positive = target right of center → roll right.
            deadband:   No-action zone around zero.
            kp:         Proportional gain; hold_sec = kp * abs(error_norm).
            min_hold_sec / max_hold_sec: Clamp bounds on the roll hold duration.
            cooldown_sec: Minimum interval between consecutive roll commands.

        Returns:
            'left', 'right', or None if suppressed by deadband or cooldown.
        """
        if abs(error_norm) <= deadband:
            return None
        now = time.time()
        if now - self._last_orient_ts < cooldown_sec:
            return None
        hold = float(min(max(kp * abs(error_norm), min_hold_sec), max_hold_sec))
        self._last_orient_ts = now
        if error_norm < 0:
            self.roll_left(hold_seconds=hold, block=False)
            return "left"
        else:
            self.roll_right(hold_seconds=hold, block=False)
            return "right"

    def deploy_flares(self, hold_seconds: float = 0.05, block: bool = True, ignore_cancel: bool = False):
        """Deploy flares (short press of the configured flares key)."""
        self._execute_key_press(DEPLOY_FLARES_KEY, hold_seconds=hold_seconds, block=block, action_name='deploy_flares', ignore_cancel=ignore_cancel)

    def wingsweep(self, hold_seconds: float = 0.5, block: bool = True):
        """Perform a wingsweep maneuver by pressing the configured wingsweep key."""
        self._execute_key_press(WINGSWEEP_KEY, hold_seconds=hold_seconds, block=block, action_name='wingsweep')

    def press_escape(self, hold_seconds: float = 0.05, block: bool = False):
        """Press Escape once, used by safety-recovery handlers."""
        self._execute_key_press(
            'escape',
            hold_seconds=hold_seconds,
            block=block,
            action_name='escape_recovery',
            ignore_cancel=True,
        )

    def padlock_camera(self, hold_seconds: float = 0.1, block: bool = True):
        """Toggle padlock camera by pressing the configured padlock camera key."""
        self._execute_key_press(PADLOCK_CAMERA, hold_seconds=hold_seconds, block=block, action_name='padlock_camera')

    def padlock_target_switch(self, presses: int = 2, delay_between: float = 0.35) -> None:
        """Press padlock N times to cycle to a new target, then pause the auto-padlock loop briefly.

        Called after every 2 missiles fired to spread shots across enemy jets rather than
        concentrating all missiles on one target.
        """
        def _run():
            for i in range(presses):
                if i > 0:
                    time.sleep(delay_between)
                self.padlock_camera(hold_seconds=0.1, block=True)
            # Give the padlock loop a short rest so it doesn't immediately re-lock the old target
            self._padlock_cooldown_until = max(self._padlock_cooldown_until, time.time() + 2.0)
        threading.Thread(target=_run, daemon=True).start()

    def fire_machine_gun(self, hold_seconds: float = 1.0, block: bool = True):
        """Fire machine gun by holding the configured machine-gun key."""
        self._execute_key_press(FIRE_MACHINE_GUN, hold_seconds=hold_seconds, block=block, action_name='fire_machine_gun')

    def fire_active_weapon(self, hold_seconds: float = 0.1, block: bool = True):
        """Activate the currently selected weapon (short press)."""
        self._execute_key_press(FIRE_ACTIVE_WEAPON, hold_seconds=hold_seconds, block=block, action_name='fire_active_weapon')

    def reload_flares(self, block: bool = False):
        """Press SPECIAL_ABILITY to reload flares (triggered when flare count == 2)."""
        logger.info("\033[93m🔥 Reloading flares via SPECIAL_ABILITY key\033[0m")
        self._execute_key_press(SPECIAL_ABILITY, hold_seconds=0.1, block=block, action_name='reload_flares')

    def eject_and_dive(self, on_complete=None):
        """Cancel mission, hold NOSE_DOWN + AFTERBURNER simultaneously.

        NOSE_DOWN is held for x seconds then released.
        AFTERBURNER is held until respawn is detected (or a 120s safety timeout).
        on_complete: optional callable invoked in the finally block after all keys are released.
        """
        logger.info("\033[91m🚀 MISSILES EMPTY — cancelling mission and ejecting\033[0m")
        self.cancel_mission()
        self._eject_stop.clear()
        # Reset the grace-period timestamp so buffered/held flight keys (e.g. 'k' on key-repeat
        # from normal gameplay) cannot cancel the eject within the first 2 seconds of starting it.
        self._game_battle_since = time.time()
        # Force health state to dead so the False→True transition fires when
        # health is detected again after respawn, triggering mission restart.
        if self._analyzer is not None:
            if not self._analyzer._health_lock.acquire(timeout=1.0):
                logger.warning("eject_and_dive: _health_lock timeout — skipping health reset")
            else:
                try:
                    self._analyzer._game_battle_alive = False
                    self._analyzer._health_no_digits_since = 0.0
                finally:
                    self._analyzer._health_lock.release()

        def _run():
            self._ejecting.set()
            try:
                if self._simulate_os_input:
                    self._record_action_intent("key_press", key=NOSE_DOWN_KEY, action="eject_and_dive")
                else:
                    if not keyboard_module:
                        logger.error("Controller: keyboard library not available for eject_and_dive")
                        return
                    keyboard_module.press(NOSE_DOWN_KEY)
                # Wait for the mission thread to fully exit before pressing
                # AFTERBURNER so its _execute_key_press finally block can't
                # release the key after we press it.
                mission_exit_deadline = time.time() + 2.0
                while self.is_mission_running() and time.time() < mission_exit_deadline:
                    time.sleep(0.05)
                if self._simulate_os_input:
                    self._record_action_intent("key_press", key=AFTERBURNER_KEY, action="eject_and_dive")
                else:
                    keyboard_module.press(AFTERBURNER_KEY)
                logger.info("Controller: eject_and_dive — NOSE_DOWN + AFTERBURNER engaged")

                # Hold nose-down for 5s then release; afterburner stays on.
                if not self._eject_stop.wait(timeout=5.0):
                    if self._simulate_os_input:
                        self._record_action_intent("key_release", key=NOSE_DOWN_KEY, action="eject_and_dive")
                    else:
                        try:
                            keyboard_module.release(NOSE_DOWN_KEY)
                        except Exception:
                            pass
                    logger.info("Controller: eject_and_dive — nose-down released, holding afterburner until respawn")

                    # Hold afterburner until respawn screen detected (stop_eject_sequence sets _eject_stop)
                    # or 120s safety timeout. _is_respawning() is not used here because the player may be
                    # alive (health > 0) when the eject starts; relying on it exits the loop immediately.
                    deadline = time.time() + 120.0
                    while time.time() < deadline:
                        if self._eject_stop.wait(timeout=0.5):
                            break
                    else:
                        logger.warning("Controller: eject_and_dive — respawn not detected within 120s, releasing afterburner")
                else:
                    logger.info("Controller: eject_and_dive — cancelled during nose-down phase")
            finally:
                self._ejecting.clear()
                if self._simulate_os_input:
                    self._record_action_intent("key_release", key=AFTERBURNER_KEY, action="eject_and_dive")
                    self._record_action_intent("key_release", key=NOSE_DOWN_KEY, action="eject_and_dive")
                else:
                    try:
                        keyboard_module.release(AFTERBURNER_KEY)
                    except Exception:
                        pass
                    try:
                        keyboard_module.release(NOSE_DOWN_KEY)
                    except Exception:
                        pass
                logger.info("Controller: eject_and_dive complete")
                if on_complete is not None:
                    try:
                        on_complete()
                    except Exception:
                        logger.exception("Controller: eject_and_dive on_complete callback failed")

        threading.Thread(target=_run, daemon=True).start()

    def start_search_and_destroy_loop(self):
        """Start background padlock + weapon-fire loops.

        Loops stop when either _sdl_stop is set (explicit stop) or
        _mission_cancel is set (any cancellation signal), whichever comes first.
        """
        padlock_alive = (self._sdl_padlock_thread is not None
                         and self._sdl_padlock_thread.is_alive())
        weapon_alive = (self._sdl_weapon_thread is not None
                        and self._sdl_weapon_thread.is_alive())
        if self._sdl_stop is not None and not self._sdl_stop.is_set() and (padlock_alive or weapon_alive):
            logger.debug("Controller: search_and_destroy_loop already running")
            return

        self._sdl_stop = threading.Event()
        stop = self._sdl_stop

        def _padlock_loop():
            logger.info("Controller: search_and_destroy padlock loop started")
            try:
                while not stop.is_set() and not self._mission_cancel.is_set():
                    if time.time() >= self._padlock_cooldown_until:
                        self.padlock_camera(hold_seconds=0.1, block=True)
                    for _ in range(60):  # 6 s interruptible
                        if stop.wait(timeout=0.1) or self._mission_cancel.is_set():
                            break
            finally:
                logger.info("Controller: search_and_destroy padlock loop stopped")

        def _weapon_loop():
            logger.info("Controller: search_and_destroy weapon loop started")
            try:
                while not stop.is_set() and not self._mission_cancel.is_set():
                    should_fire = True
                    if self._target_painting_mode and self._analyzer is not None:
                        ammo_lock = self._analyzer._ammo_lock
                        if not ammo_lock.acquire(timeout=0.5):
                            logger.debug("Controller: target_painting ammo lock timeout — firing")
                        else:
                            try:
                                missiles = self._analyzer._ammo_missiles
                            finally:
                                if ammo_lock.locked():
                                    ammo_lock.release()
                            if missiles == 1 and self._analyzer.game_state != GameState.GAME_BATTLE_MANUAL:
                                logger.debug("Controller: target_painting suppressing fire (ammo_missiles=1)")
                                should_fire = False
                    if should_fire:
                        self.fire_active_weapon(hold_seconds=0.1, block=True)
                    steps = max(1, int(self._weapon_loop_interval / 0.1))
                    for _ in range(steps):
                        if stop.wait(timeout=0.1) or self._mission_cancel.is_set():
                            break
            finally:
                logger.info("Controller: search_and_destroy weapon loop stopped")

        self._sdl_padlock_thread = threading.Thread(target=_padlock_loop, daemon=True)
        self._sdl_weapon_thread = threading.Thread(target=_weapon_loop, daemon=True)
        self._sdl_padlock_thread.start()
        self._sdl_weapon_thread.start()
        logger.info("Controller: search_and_destroy_loop started")

    def stop_search_and_destroy_loop(self):
        """Stop the search-and-destroy padlock + weapon-fire loops."""
        if self._sdl_stop is None or self._sdl_stop.is_set():
            logger.debug("Controller: search_and_destroy_loop not running")
            return
        self._sdl_stop.set()
        if self._sdl_padlock_thread:
            self._sdl_padlock_thread.join(timeout=1.0)
            self._sdl_padlock_thread = None
        if self._sdl_weapon_thread:
            self._sdl_weapon_thread.join(timeout=1.0)
            self._sdl_weapon_thread = None
        logger.info("Controller: search_and_destroy_loop stopped")

    def disengage_roll_right(self, duration: float = 10.0):
        """Cancel mission maneuvers then hold ROLL_RIGHT_KEY for `duration` seconds.

        search_and_destroy_loop() keeps running during the roll so the aircraft
        continues tracking and firing at any enemy that comes into view.
        Called when no enemy is detected in ENEMY_CLOSE_BY for 30+ seconds.
        """
        logger.info("\033[93m↩ No enemy for 30s — cancelling mission and rolling right for %.0fs\033[0m", duration)
        self.cancel_mission()

        def _run():
            if not keyboard_module:
                logger.error("Controller: keyboard library not available for disengage_roll_right")
                return
            self.start_search_and_destroy_loop()
            try:
                keyboard_module.press(ROLL_RIGHT_KEY)
                self._interruptible_sleep(duration)
            finally:
                try:
                    keyboard_module.release(ROLL_RIGHT_KEY)
                except Exception:
                    pass
                if not self.is_mission_running():
                    self.stop_search_and_destroy_loop()
            logger.info("Controller: disengage_roll_right complete")
            with self._last_mission_lock:
                last_mission = self._last_mission
            if self._auto_respawn_restart and last_mission and not self.is_mission_running():
                logger.info("Controller: restarting mission after disengage")
                self.restart_last_mission()

        threading.Thread(target=_run, daemon=True).start()

    def start_weapon_loop(self, interval: float | None = None):
        """Start continuously firing the active weapon in a loop.
        
        Args:
            interval: Time between shots in seconds (default 0.2)
        """
        if self._weapon_loop_active:
            logger.debug("Controller: weapon loop already running")
            return
        
        if interval is not None:
            self._weapon_loop_interval = float(interval)
        
        self._weapon_loop_active = True
        self._weapon_loop_stop.clear()

        def _loop():
            logger.info("Controller: weapon loop started (interval=%.2fs)", self._weapon_loop_interval)
            try:
                while True:
                    try:
                        self._execute_key_press(
                            FIRE_ACTIVE_WEAPON,
                            hold_seconds=0.1,
                            block=True,
                            action_name="fire_active_weapon",
                            ignore_cancel=True,
                        )
                    except Exception as e:
                        logger.warning("Controller: weapon loop fire failed: %s", e)
                    if self._weapon_loop_stop.wait(timeout=self._weapon_loop_interval):
                        break
            except Exception:
                logger.exception("Controller: weapon loop error")
            finally:
                self._weapon_loop_active = False
                logger.info("Controller: weapon loop stopped")

        self._weapon_loop_thread = threading.Thread(target=_loop, daemon=True)
        self._weapon_loop_thread.start()

    def stop_weapon_loop(self):
        """Stop the continuous weapon firing loop."""
        if not self._weapon_loop_active:
            logger.debug("Controller: weapon loop not running")
            return
        
        logger.info("Controller: stopping weapon loop")
        self._weapon_loop_stop.set()
        self._weapon_loop_active = False
        if self._weapon_loop_thread:
            self._weapon_loop_thread.join(timeout=2.0)
            self._weapon_loop_thread = None

    def toggle_weapon_loop(self):
        """Toggle the weapon loop on/off. Bound to hotkey 'x'."""
        logger.debug("Controller: toggle_weapon_loop called (current state: %s)", self._weapon_loop_active)
        if self._weapon_loop_active:
            logger.info("Controller: toggling weapon loop OFF")
            self.stop_weapon_loop()
        else:
            logger.info("Controller: toggling weapon loop ON")
            self.start_weapon_loop()

    def _interruptible_sleep(self, seconds: float, check_interval: float = 1.0) -> bool:
        """Sleep in intervals and exit early when mission cancellation is requested.

        Returns:
            True if full duration elapsed, False if interrupted by cancellation.
        """
        remaining = float(seconds)
        while remaining > 0:
            interval = min(check_interval, remaining)
            if self._mission_cancel.wait(timeout=interval):
                return False
            remaining -= interval
        return True

    def mission_loiter(self):
        """This mission sequence performs a predefined set of maneuvers for the Aaarvark, it flies up and tries to stay up
        Compatible Jets: F111, F-14, Mig-23, J20
        """
        # Check if mission is already running
        acquired = self._mission_lock.acquire(blocking=False)
        if not acquired:
            logger.debug("Controller: mission already in progress, skipping")
            return

        logger.info("\033[92mController: mission_loiter - starting mission sequence\033[0m")
        self._mission_complete.clear()
        self._mission_cancel.clear()

        def _mission_runner():
            try:
                # Execute mission maneuvers (maneuvers log their own activity)
                self.nose_up(2.0)
                if self._mission_cancel.is_set():
                    logger.info("Controller: mission cancelled after nose_up")
                    return
                self.wingsweep()
                if self._mission_cancel.is_set():
                    logger.info("Controller: mission cancelled after wingsweep")
                    return
                self.afterburner(10.0)
                if self._mission_cancel.is_set():
                    logger.info("Controller: mission cancelled after afterburner")
                    return
                self.afterburner(10.0)
                if self._mission_cancel.is_set():
                    logger.info("Controller: mission cancelled after afterburner")
                    return
                self.wingsweep()
                if self._mission_cancel.is_set():
                    logger.info("Controller: mission cancelled after wingsweep")
                    return
                self.roll_right(4)
                if self._mission_cancel.is_set():
                    logger.info("Controller: mission cancelled after roll_right")
                    return
                self.afterburner(10)
                if self._mission_cancel.is_set():
                    logger.info("Controller: mission cancelled after afterburner")
                    return
                self.deploy_flares()
                if self._mission_cancel.is_set():
                    logger.info("Controller: mission cancelled after deploy_flares")
                    return
                self.roll_left(10)
                if self._mission_cancel.is_set():
                    logger.info("Controller: mission cancelled after roll_left")
                    return
                self.deploy_flares()
                if self._mission_cancel.is_set():
                    logger.info("Controller: mission cancelled after deploy_flares")
                    return
                self.roll_right(30)
                if self._mission_cancel.is_set():
                    logger.info("Controller: mission cancelled after roll_right")
                    return
                self.roll_left(30)
                if self._mission_cancel.is_set():
                    logger.info("Controller: mission cancelled")
                    return
                #self.nose_down(4.0)
                #time.sleep(10.0)  # additional wait time to stabilize
                logger.info("\033[91mController: mission_loiter - sequence complete\033[0m")
            except Exception:
                logger.exception("Controller: mission_loiter failed")
            finally:
                self._mission_complete.set()
                if self._mission_lock.locked():
                    self._mission_lock.release()

        mission_a = threading.Thread(target=_mission_runner, daemon=True)
        mission_a.start()

        # Wait for mission to complete or exit requested
        while not self._mission_complete.wait(timeout=0.05):
            if self._exit_event and self._exit_event.is_set():
                logger.info("Controller: exit requested, aborting mission wait")
                self.cancel_mission()
                break

    def mission_j20(self):
        """This mission sequence performs a predefined set of maneuvers for the J20 with continuous padlock and weapon fire
        Compatible Jets: J20
        """
        # Check if mission is already running
        acquired = self._mission_lock.acquire(blocking=False)
        if not acquired:
            logger.warning("\033[91mController: mission_j20 already in progress, skipping (lock held)\033[0m")
            return

        logger.info("\033[92mController: mission_j20 - starting mission sequence (lock acquired)\033[0m")
        self._mission_complete.clear()
        self._mission_cancel.clear()

        def _mission_runner():
            try:
                # Execute mission maneuvers (maneuvers log their own activity)
                self.nose_up(2.0)
                if self._mission_cancel.is_set():
                    logger.info("Controller: mission cancelled after nose_up")
                    return

                # Start search-and-destroy loop after first maneuver
                self.start_search_and_destroy_loop()
                logger.info("Controller: mission_j20 background loops started")

                self.afterburner(20.0)
                if self._mission_cancel.is_set():
                    logger.info("Controller: mission cancelled after afterburner")
                    return
                # Roll right and afterburner
                self.roll_right(50, block=False)
                logger.info("\033[91mController: initiating roll_right while afterburner loop is active\033[0m")
                self.afterburner(10)
                if not self._interruptible_sleep(10, check_interval=1.0):
                    logger.info("Controller: mission cancelled during afterburner recharge")
                    return
                logger.info("\033[94mController:  initiated second afterburner\033[0m")
                self.afterburner(10)
                if not self._interruptible_sleep(10, check_interval=1.0):
                    logger.info("Controller: mission cancelled during afterburner recharge")
                    return
                self.afterburner(10)
                logger.info("\033[91mController: initiating final roll right 300 sec \033[0m")

                self.roll_right(300)
                if self._mission_cancel.is_set():
                    logger.info("Controller: mission cancelled after roll_right")
                    return

                self.stop_search_and_destroy_loop()
                #self.nose_down(4.0)
                #time.sleep(10.0)  # additional wait time to stabilize
                logger.info("\033[91mController: mission_j20 - sequence complete\033[0m")
            except Exception:
                logger.exception("Controller: mission_j20 failed")
                self.stop_search_and_destroy_loop()
            finally:
                self._mission_complete.set()
                if self._mission_lock.locked():
                    self._mission_lock.release()
                    logger.info("\033[91mController: mission_j20 - lock released\033[0m")

        mission_a = threading.Thread(target=_mission_runner, daemon=True)
        mission_a.start()

        # Wait for mission to complete or exit requested
        while not self._mission_complete.wait(timeout=0.05):
            if self._exit_event and self._exit_event.is_set():
                logger.info("Controller: exit requested, aborting mission wait")
                self.cancel_mission()
                break

        # Wait for the mission runner thread to fully exit
        mission_a.join(timeout=2.0)
        
        # Small delay to let keyboard library settle after key presses
        time.sleep(0.2)
        
        logger.info("\033[91mController: mission_j20 - method exiting\033[0m")

    def click_grid_region(self, region_num: int, grid_rows: int = 8, grid_cols: int = 8, block: bool = False, count: int = 6, region_name: str = None):
        """Move the mouse to the center of a grid region and left-click it.

        Args:
            region_num: 1-based region number (row-major, left-to-right top-to-bottom).
            grid_rows: Number of grid rows (default 8).
            grid_cols: Number of grid columns (default 8).
            block: If True run in the calling thread; otherwise spawn a daemon thread.
            count: Number of times to click the region. When count > 1 a final click on
                   the ready button (lobby/continue button) is also performed.
            region_name: Human-readable name for the region, used in log messages.
        """
        def _do_click():
            if self._simulate_os_input:
                label = region_name if region_name else str(region_num)
                self._record_action_intent(
                    "click_grid_region",
                    region_num=int(region_num),
                    region_name=label,
                    count=int(count),
                    grid_rows=int(grid_rows),
                    grid_cols=int(grid_cols),
                )
                return
            try:
                if self._capture is None:
                    logger.error("Controller: click_grid_region - no capture reference")
                    return
                region = self._capture.region
                cap_w, cap_h = region[2], region[3]
                cell_w = cap_w / grid_cols
                cell_h = cap_h / grid_rows
                row_idx = (region_num - 1) // grid_cols
                col_idx = (region_num - 1) % grid_cols
                label = region_name if region_name else str(region_num)

                if sys.platform != "win32":
                    # Linux: compute absolute coords from game window offset
                    offset = None
                    for _attempt in range(3):
                        offset = self._capture.game_screen_offset
                        if offset is not None:
                            break
                        time.sleep(0.05)
                    if offset is None:
                        logger.error("click_grid_region: game window offset not known yet (3 retries)")
                        return
                    game_ox, game_oy = offset
                    abs_x = int(game_ox + (col_idx + 0.5) * cell_w)
                    abs_y = int(game_oy + (row_idx + 0.5) * cell_h)
                    logger.info("\033[93m📋 Clicking %s at (%d, %d) [game offset %d,%d] x%d\033[0m",
                                label, abs_x, abs_y, game_ox, game_oy, count)
                    _linux_click(abs_x, abs_y, count)
                    if count > 1 and self._ready_button_region:
                        rbn = self._ready_button_region
                        row_rb = (rbn - 1) // grid_cols
                        col_rb = (rbn - 1) % grid_cols
                        x_rb = int(game_ox + (col_rb + 0.5) * cell_w)
                        y_rb = int(game_oy + (row_rb + 0.5) * cell_h)
                        logger.info("\033[93m📋 Clicking ready_button at (%d, %d)\033[0m", x_rb, y_rb)
                        _linux_click(x_rb, y_rb)
                        if self._analyzer is not None:
                            self._analyzer.trigger_event("manual_reset")
                            logger.info("\033[93m📋 Ready button (region %d) clicked → GAME_LOBBY\033[0m", self._ready_button_region)
                    return

                # Windows: use win32api
                with mss() as sct:
                    monitors = sct.monitors
                    monitor_index = self._capture.monitor_index
                    if monitor_index < 1 or monitor_index >= len(monitors):
                        logger.error("Controller: click_grid_region - monitor index %d out of range", monitor_index)
                        return
                    mon = monitors[monitor_index]
                    abs_left = mon["left"] + region[0]
                    abs_top = mon["top"] + region[1]
                abs_x = int(abs_left + (col_idx + 0.5) * cell_w)
                abs_y = int(abs_top + (row_idx + 0.5) * cell_h)
                logger.info("\033[93m📋 Clicking %s at (%d, %d) [monitor %d offset %d,%d] x%d\033[0m",
                            label, abs_x, abs_y, monitor_index, mon["left"], mon["top"], count)
                def _raw_click(x, y):
                    ctypes.windll.user32.SetCursorPos(x, y)
                    time.sleep(0.05)
                    ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
                    time.sleep(0.05)
                    ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP

                for i in range(count):
                    _raw_click(abs_x, abs_y)
                    if i < count - 1:
                        time.sleep(0.5)

                if count > 1 and self._ready_button_region:
                    rbn = self._ready_button_region
                    row_rb = (rbn - 1) // grid_cols
                    col_rb = (rbn - 1) % grid_cols
                    x_rb = int(abs_left + (col_rb + 0.5) * cell_w)
                    y_rb = int(abs_top + (row_rb + 0.5) * cell_h)
                    logger.info("\033[93m📋 Clicking ready_button at (%d, %d)\033[0m", x_rb, y_rb)
                    _raw_click(x_rb, y_rb)
                    if self._analyzer is not None:
                        self._analyzer.trigger_event("manual_reset")
                        logger.info("\033[93m📋 Ready button (region %d) clicked → GAME_LOBBY\033[0m", self._ready_button_region)
            except Exception:
                logger.exception("Controller: click_grid_region failed")

        if block:
            _do_click()
        else:
            threading.Thread(target=_do_click, daemon=True).start()

    def popup_click_allowed(self, popup: str, cooldown: float = 30.0) -> bool:
        """Return True if `popup` has not been clicked within `cooldown` seconds."""
        last = self._popup_last_clicked.get(popup, 0.0)
        return time.time() - last >= cooldown

    def record_popup_click(self, popup: str) -> None:
        """Record that `popup` was just clicked (starts its cooldown)."""
        self._popup_last_clicked[popup] = time.time()

    def click_crop(self, coords: "CropCoords", block: bool = False, count: int = 1, region_name: str = None):
        """Move the mouse to the centre of a named crop region and left-click it.

        Uses percentage-coordinate CropCoords (from crop_region.py) to derive
        the absolute screen position via crop_centre().

        Args:
            coords: CropCoords percentage-coordinate bounding box for the target region.
            block: If True run in the calling thread; otherwise spawn a daemon thread.
            count: Number of times to click the region (0.5s apart when count > 1).
            region_name: Human-readable label used in log messages.
        """
        def _do_click():
            if self._simulate_os_input:
                label = region_name or f"({coords.x1:.2f},{coords.y1:.2f})"
                self._record_action_intent(
                    "click_crop",
                    region_name=label,
                    count=int(count),
                    coords={"x1": coords.x1, "y1": coords.y1, "x2": coords.x2, "y2": coords.y2},
                )
                return
            try:
                if self._capture is None:
                    logger.error("Controller: click_crop - no capture reference")
                    return
                region = self._capture.region
                cap_w, cap_h = region[2], region[3]
                label = region_name or f"({coords.x1:.2f},{coords.y1:.2f})"

                if sys.platform != "win32":
                    # Linux: compute absolute coords from game window offset
                    offset = None
                    for _attempt in range(3):
                        offset = self._capture.game_screen_offset
                        if offset is not None:
                            break
                        time.sleep(0.05)
                    if offset is None:
                        logger.error("click_crop: game window offset not known yet (3 retries)")
                        return
                    game_ox, game_oy = offset
                    abs_x, abs_y = crop_centre(coords, cap_w, cap_h, game_ox, game_oy)
                    logger.info("\033[93m📋 Clicking %s at (%d, %d) [game offset %d,%d] x%d\033[0m",
                                label, abs_x, abs_y, game_ox, game_oy, count)
                    _linux_click(abs_x, abs_y, count)
                    return

                # Windows: use win32api
                with mss() as sct:
                    monitors = sct.monitors
                    monitor_index = self._capture.monitor_index
                    if monitor_index < 1 or monitor_index >= len(monitors):
                        logger.error("Controller: click_crop - monitor index %d out of range", monitor_index)
                        return
                    mon = monitors[monitor_index]
                    abs_left = mon["left"] + region[0]
                    abs_top = mon["top"] + region[1]
                abs_x, abs_y = crop_centre(coords, cap_w, cap_h, abs_left, abs_top)
                logger.info("\033[93m📋 Clicking %s at (%d, %d) [monitor %d offset %d,%d] x%d\033[0m",
                            label, abs_x, abs_y, monitor_index, mon["left"], mon["top"], count)

                def _raw_click(x, y):
                    ctypes.windll.user32.SetCursorPos(x, y)
                    time.sleep(0.05)
                    ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
                    time.sleep(0.05)
                    ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP

                for i in range(count):
                    _raw_click(abs_x, abs_y)
                    if i < count - 1:
                        time.sleep(0.5)
            except Exception:
                logger.exception("Controller: click_crop failed")

        if block:
            _do_click()
        else:
            threading.Thread(target=_do_click, daemon=True).start()

    def cancel_mission(self):
        """Request cancellation of any running mission.

        Sets the cancel flag which maneuvers poll and stops the standalone
        weapon loop. search_and_destroy_loop self-terminates when it sees
        _mission_cancel. Mission completion/lock release are finalized by
        the mission runner thread.
        """
        logger.info("\033[91mController: cancel_mission called\033[0m")
        self._mission_cancel.set()
        self.stop_weapon_loop()

    def is_mission_running(self) -> bool:
        """Return True when a mission thread currently holds the mission lock."""
        return self._mission_lock.locked()

    def start_game_starting_loop(self):
        """Public orchestration entrypoint for the GAME_STARTING loop."""
        self._start_game_starting_loop()

    def is_auto_respawn_restart_enabled(self) -> bool:
        """Return whether automatic respawn restart is currently enabled."""
        return self._auto_respawn_restart

    def set_auto_respawn_restart(self, enabled: bool) -> None:
        """Enable or disable automatic restart after respawn."""
        self._auto_respawn_restart = bool(enabled)

    def stop_eject_sequence(self) -> None:
        """Cancel an in-progress eject-and-dive sequence if one is active."""
        self._eject_stop.set()

    def _set_last_mission(self, mission_name: str):
        with self._last_mission_lock:
            self._last_mission = mission_name
        self._auto_respawn_restart = True
        self._game_battle_since = time.time()
        if self._analyzer is not None:
            self._analyzer._last_battle_event_ts = time.time()
            logger.info("Controller: mission '%s' started → GAME_BATTLE", mission_name)

    def _start_game_starting_loop(self):
        """Background loop active in GAME_STARTING state.

        Every 5 seconds: press MISSION_J20_KEY and scan the good_luck region for 'Good Luck'.
        Once detected, wait 10 seconds then launch mission_j20.
        """
        # Clear any stale cancel from prior states (mirrors mission_j20 / mission_loiter pattern).
        # cancel_mission() is called on on_enter_GAME_LOBBY; without this clear the loop
        # would see the flag already set and exit immediately.
        self._mission_cancel.clear()

        good_luck_event = threading.Event()
        ocr_running = threading.Event()

        def _do_ocr_scan():
            """Run Good Luck OCR in background; sets good_luck_event on detection."""
            try:
                time.sleep(0.5)  # Allow 'Good Luck' screen to appear before capturing
                if self._capture is None:
                    return

                frame = self._capture.grab_from_thread()
                if self._analyzer is not None and self._analyzer.scan_region_for_good_luck(frame):
                    good_luck_event.set()
                    if self._on_good_luck_frame is not None:
                        try:
                            self._on_good_luck_frame(frame)
                        except Exception:
                            logger.exception("Controller: on_good_luck_frame callback error")
            except Exception:
                logger.exception("Controller: game_starting OCR scan error")
            finally:
                ocr_running.clear()

        def _in_starting():
            return (self._analyzer is not None
                    and self._analyzer.game_state == GameState.GAME_STARTING
                    and not self._mission_cancel.is_set())

        def _loop():
            logger.info("Controller: game_starting loop started - pressing '%s' key every 5s until 'Good Luck' detected", MISSION_J20_KEY)
            loop_start = time.time()
            max_wait = self._starting_max_wait_s  # safety timeout: GAME_STARTING → GAME_STARTING_STALLED if Good Luck never detected
            health_scan_armed = False
            try:
                while _in_starting():
                    # Press MISSION_J20_KEY every interval
                    if self._simulate_os_input:
                        self._record_action_intent("key_tap", key=MISSION_J20_KEY, action="game_starting_loop")
                        logger.info("Controller: game_starting - simulated '%s' key tap", MISSION_J20_KEY)
                    elif keyboard_module:
                        keyboard_module.press_and_release(MISSION_J20_KEY)
                        logger.info("Controller: game_starting - pressed '%s' key", MISSION_J20_KEY)

                    # Start async OCR scan if one isn't already running
                    if self._capture is not None and not ocr_running.is_set():
                        ocr_running.set()
                        threading.Thread(target=_do_ocr_scan, daemon=True).start()

                    # 5-second interruptible wait; breaks early on Good Luck detection or state change.
                    # After 10 s gate: also arm health scan and check game_battle_alive each tick.
                    for _ in range(50):  # 50 * 0.1s = 5s
                        if good_luck_event.wait(timeout=0.1) or not _in_starting():
                            break
                        if not health_scan_armed and time.time() - loop_start >= 10.0:
                            health_scan_armed = True
                            if self._analyzer is not None:
                                self._analyzer._game_starting_health_scan_enabled.set()
                                logger.info("Controller: game_starting health-scan fallback armed (10s gate)")
                        if health_scan_armed and self._analyzer is not None and self._analyzer.game_battle_alive:
                            logger.info(
                                "\033[92mController: game_battle_alive detected in GAME_STARTING "
                                "— launching mission immediately\033[0m")
                            self._analyzer.trigger_event("good_luck_detected")
                            self._set_last_mission("j20")
                            threading.Thread(target=self.mission_j20, daemon=True).start()
                            return

                    if not _in_starting():
                        # If cancel fired while FSM is still GAME_STARTING, push it to stalled.
                        if (self._mission_cancel.is_set()
                                and self._analyzer is not None
                                and self._analyzer.game_state == GameState.GAME_STARTING):
                            self._analyzer.trigger_event("starting_timeout")
                        return

                    if time.time() - loop_start > max_wait:
                        logger.warning("Controller: game_starting timed out after %ds without 'Good Luck'", max_wait)
                        if self._analyzer is not None:
                            self._analyzer.trigger_event("starting_timeout")
                        return

                    if good_luck_event.is_set():
                        good_luck_wait = 13
                        logger.info("\033[92mController: 'Good Luck' detected - waiting %ds before starting '%s' mission\033[0m", good_luck_wait, MISSION_J20_KEY)
                        for _ in range(good_luck_wait * 10):  # N * 0.1s = Ns
                            if not _in_starting():
                                return
                            time.sleep(0.1)
                        if _in_starting():
                            logger.info("Controller: game_starting - launching J20 mission")
                            self._analyzer.trigger_event("good_luck_detected")
                            self._set_last_mission("j20")
                            threading.Thread(target=self.mission_j20, daemon=True).start()
                        return
            except Exception:
                logger.exception("Controller: game_starting loop error")
            finally:
                if self._analyzer is not None:
                    self._analyzer._game_starting_health_scan_enabled.clear()
                logger.info("Controller: game_starting loop stopped")

        threading.Thread(target=_loop, daemon=True).start()

    def restart_last_mission(self):
        """Restart the most recently started mission, defaulting to J20 when none recorded.

        Returns:
            True  — mission was successfully restarted (or started as j20 default).
            False — mission is currently running (lock held); restart skipped.
        """
        if self.is_mission_running():
            logger.warning("\033[91mController: cannot restart mission - previous mission still in progress (lock held)\033[0m")
            return False

        with self._last_mission_lock:
            mission = self._last_mission

        if mission == "j20":
            logger.info("Controller: restarting last mission (J20)")
            threading.Thread(target=self.mission_j20, daemon=True).start()
            return True
        if mission == "loiter":
            logger.info("Controller: restarting last mission (loiter)")
            threading.Thread(target=self.mission_loiter, daemon=True).start()
            return True

        # No prior mission recorded — reached GAME_BATTLE via GAME_UNKNOWN (Good Luck
        # not detected, stalled start). Default to j20 rather than doing nothing.
        logger.info("Controller: no prior mission recorded — defaulting to J20")
        self._set_last_mission("j20")
        threading.Thread(target=self.mission_j20, daemon=True).start()
        return True

    def cleanup(self):
        """Deregister all keyboard hooks registered by this controller."""
        if keyboard_module:
            try:
                keyboard_module.unhook_all()
                logger.info("Controller: all keyboard hooks deregistered")
            except ImportError as exc:
                # keyboard requires root on Linux; not an error if privileges weren't granted.
                logger.warning("Controller: keyboard unhook skipped — %s", exc)
            except Exception:
                logger.exception("Controller: failed to unhook keyboard hooks")