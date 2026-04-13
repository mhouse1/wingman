import time
import logging
import threading
import ctypes
import sys
import os
import cv2
import numpy as np
from datetime import datetime
from pathlib import Path
from mss import mss

from .crop_region import CropCoords, crop_centre, draw_crops

try:
    import keyboard as keyboard_module
except Exception:
    keyboard_module = None

logger = logging.getLogger(__name__)

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
TOGGLE_WEAPON_LOOP_KEY = 'x'  # Press X to toggle weapon firing loop
MISSION_J20_KEY = 'u'  # Press U to start J20 mission
MISSION_LOITER_KEY = 'y'  # Press Y to start loiter mission
CANCEL_MISSION_KEY = 'end'   # Press End to cancel active mission
CAPTURE_SCREEN_SHOT = 'v'  # Press V to capture a screenshot (for testing/debugging)
AUTO_MISSION_KEY = 'm'  # Press M to start an automatic mission based on detected game state (not implemented yet)
SIMULATE_RESPAWN_KEY = 'b'  # Press B to inject a fake respawn OCR result (testing)
"""
EMOTE1 # Moving to
EMOTE2 # Help!
EMOTE3 # Defend
EMOTE4 # Attack
EMOTE5 # Goodluck
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
    def __init__(self, region, fire_button="left", fire_hold_seconds: float = 0.0, exit_event=None, analyzer=None, weapon_loop_interval: float = None, capture=None, on_auto_mission_key=None, crops: "dict[str, CropCoords] | None" = None):
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
        self._lobby_play_not_visible_since = 0.0  # tracks how long play button has been absent in lobby

        # Padlock camera cooldown: set when the key is pressed manually
        self._padlock_cooldown_until = 0.0

        # Weapon loop state (configurable via config or start_weapon_loop)
        self._weapon_loop_active = False
        self._weapon_loop_thread = None
        self._weapon_loop_interval = float(weapon_loop_interval or 0.5)  # Firing interval from config or default
        
        # Register hotkey for weapon loop toggle and other hotkeys
        if keyboard_module:
            # Cancel mission if maneuver keys are pressed during GAME_BATTLE
            def maneuver_cancel_hotkey(e):
                if self._game_battle_since and time.time() - self._game_battle_since < 2.0:
                    logger.debug("Controller: Maneuver key '%s' ignored — within 2s grace period of GAME_BATTLE entry", e.name if hasattr(e, 'name') else e)
                    return
                if self._analyzer and hasattr(self._analyzer, 'game_state'):
                    try:
                        state = self._analyzer.game_state()
                    except Exception:
                        state = None
                    # Accept both Enum and string for compatibility
                    if state and (getattr(state, 'name', None) == 'GAME_BATTLE' or str(state) == 'GAME_BATTLE'):
                        logger.info("Controller: Maneuver key '%s' pressed during GAME_BATTLE - cancelling mission", e.name if hasattr(e, 'name') else e)
                        self.cancel_mission()
            for key in [NOSE_UP_KEY, NOSE_DOWN_KEY, ROLL_LEFT_KEY, ROLL_RIGHT_KEY]:
                try:
                    keyboard_module.on_press_key(key, maneuver_cancel_hotkey, suppress=False)
                    logger.info("Controller: registered maneuver cancel hotkey '%s'", key)
                except Exception:
                    logger.exception("Controller: failed to register maneuver cancel hotkey '%s'", key)

            # Exit script hotkey (Backspace)
            try:
                def exit_script_hotkey(e):
                    logger.info("Controller: Backspace key pressed - exiting script")
                    if self._exit_event:
                        self._exit_event.set()
                keyboard_module.on_press_key('backspace', exit_script_hotkey, suppress=False)
                logger.info("Controller: registered hotkey 'backspace' to exit script")
            except Exception:
                logger.exception("Controller: failed to register exit script hotkey")

            # Cancel mission hotkey (End)
            try:
                def cancel_mission_hotkey(e):
                    logger.info("Controller: '%s' key pressed - cancelling mission and disabling auto-respawn restart", CANCEL_MISSION_KEY)
                    self._auto_respawn_restart = False
                    self.cancel_mission()
                keyboard_module.on_press_key(CANCEL_MISSION_KEY, cancel_mission_hotkey, suppress=False)
                logger.info("Controller: registered hotkey '%s' to cancel mission", CANCEL_MISSION_KEY)
            except Exception:
                logger.exception("Controller: failed to register cancel mission hotkey")

            # Maneuver keys cancel mission when pressed during GAME_BATTLE (manual takeover)
            try:
                def maneuver_key_pressed(e):
                    if self._game_battle_since and time.time() - self._game_battle_since < 2.0:
                        logger.debug("Controller: Maneuver key '%s' ignored — within 2s grace period of GAME_BATTLE entry", e.name if hasattr(e, 'name') else e)
                        return
                    if self.is_mission_running():
                        logger.info("Controller: maneuver key '%s' pressed - cancelling mission (manual takeover)", e.name)
                        self._auto_respawn_restart = False
                        self.cancel_mission()
                for _key in (NOSE_UP_KEY, NOSE_DOWN_KEY, ROLL_LEFT_KEY, ROLL_RIGHT_KEY):
                    keyboard_module.on_press_key(_key, maneuver_key_pressed, suppress=False)
                logger.info("Controller: registered maneuver keys (%s/%s/%s/%s) to cancel mission on manual press",
                            NOSE_UP_KEY, NOSE_DOWN_KEY, ROLL_LEFT_KEY, ROLL_RIGHT_KEY)
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
                    if self._analyzer is not None and self._analyzer._game_starting:
                        logger.info("Controller: '%s' key pressed during GAME_STARTING - entering GAME_BATTLE", MISSION_J20_KEY)
                    else:
                        logger.info("Controller: '%s' key pressed - starting J20 mission", MISSION_J20_KEY)
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
                        with self._analyzer._ocr_cache_lock:
                            self._analyzer._ocr_cache['result'] = (True, 1.0, "ocr")
                            self._analyzer._ocr_cache['timestamp'] = time.time()
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
                            # Create new mss instance for thread-safety (mss uses thread-local storage)
                            with mss() as sct:
                                # Get monitor rect from capture instance
                                monitor = self._capture.get_monitor_rect()
                                s = sct.grab(monitor)
                                frame = np.array(s)
                                # mss returns BGRA, convert to BGR
                                frame = frame[:, :, :3]
                            
                            # Draw named crop overlays for calibration verification
                            crops = self._crops
                            if self._analyzer is not None:
                                crops = getattr(self._analyzer, "crops", crops)
                            frame_with_crops = draw_crops(frame, crops)

                            # Create output directory if it doesn't exist
                            output_dir = Path("tests/test-output")
                            output_dir.mkdir(parents=True, exist_ok=True)

                            # Generate timestamp filename
                            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                            filename = output_dir / f"screenshot_{timestamp}.png"

                            # Save screenshot with crop overlays
                            cv2.imwrite(str(filename), frame_with_crops)
                            logger.info("Controller: Screenshot saved to %s with crop overlays", filename)
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

            # Auto-mission hotkey: when M is pressed in GAME_LOBBY, click PLAY/READY directly
            try:
                def auto_mission_key_pressed(_e):
                    if self._analyzer is None or not self._analyzer._game_lobby:
                        return
                    if self._on_auto_mission_key is not None:
                        self._on_auto_mission_key()
                    crop = next(
                        (c for c in ("PLAY", "READY") if c in self._crops),
                        None,
                    )
                    if crop is None:
                        logger.warning("Controller: '%s' pressed but no PLAY/READY crop configured", AUTO_MISSION_KEY)
                        return
                    logger.info("Controller: '%s' pressed in GAME_LOBBY - clicking %s", AUTO_MISSION_KEY, crop)
                    self._analyzer._game_lobby = False
                    self._analyzer._game_starting = True
                    self.click_crop(self._crops[crop], block=False, count=1, region_name=crop)
                    self._start_game_starting_loop()
                keyboard_module.on_press_key(AUTO_MISSION_KEY, auto_mission_key_pressed, suppress=False)
                logger.info("Controller: registered hotkey '%s' to click PLAY/READY in GAME_LOBBY", AUTO_MISSION_KEY)
            except Exception:
                logger.exception("Controller: failed to register auto mission hotkey")

    def start_auto_mission(self, force: bool = False):
        """Click the play button and enter GAME_STARTING state.

        Called both from the AUTO_MISSION_KEY hotkey and automatically by the
        main loop when unattended_mode is active and GAME_LOBBY is detected.

        Args:
            force: if True, bypass the GAME_STARTING guard (used by manual M key press
                   to assume GAME_LOBBY and restart the cycle regardless of current state).
        """
        if self._analyzer is None:
            return
        if not force and self._analyzer._game_starting:
            logger.debug("Controller: start_auto_mission called but already in GAME_STARTING - ignoring")
            return

        def _run():
            if self._analyzer is None:
                return

            if self._capture is None:
                logger.warning("Controller: start_auto_mission - no capture source; skipping play button click")
                return

            try:
                with mss() as sct:
                    s = sct.grab(self._capture.get_monitor_rect())
                    frame = np.array(s)[:, :, :3]
            except Exception:
                logger.exception("Controller: start_auto_mission - failed to capture frame for play button OCR")
                return

            detected_crop = self._analyzer.scan_region_for_play_button(frame)
            if detected_crop is None:
                now = time.time()
                if self._lobby_play_not_visible_since == 0.0:
                    self._lobby_play_not_visible_since = now
                    logger.info("Controller: start_auto_mission - PLAY/READY not visible; starting 3s popup-check timer")
                elif now - self._lobby_play_not_visible_since >= 3.0:
                    logger.info("Controller: play button absent for %.1fs — scanning for lobby popups",
                                now - self._lobby_play_not_visible_since)
                    popup = self._analyzer.scan_region_for_lobby_popups(frame)
                    if popup:
                        logger.info("Controller: dismissing lobby popup '%s'", popup)
                        self.click_crop(self._crops[popup], block=True, count=1, region_name=popup)
                        if popup == REGION_REVEAL_ALL:
                            time.sleep(3.0)
                            logger.info("Controller: REVEAL_ALL second click after 3s delay")
                            self.click_crop(self._crops[popup], block=True, count=1, region_name=popup)
                    else:
                        logger.info("Controller: no lobby popups detected; waiting for play button")
                return

            # Play/Ready button visible — reset popup absence timer
            self._lobby_play_not_visible_since = 0.0
            # Re-check state after OCR: another thread may have already clicked play
            if self._analyzer._game_starting:
                logger.debug("Controller: start_auto_mission - state already GAME_STARTING after OCR; skipping click")
                return
            logger.info("Controller: start_auto_mission - clicking %s and entering GAME_STARTING", detected_crop)
            self._analyzer._game_lobby = False
            self._analyzer._game_end_b = False
            self._analyzer._game_starting = True
            self.click_crop(self._crops[detected_crop], block=False, count=1, region_name=detected_crop)
            self._start_game_starting_loop()

        threading.Thread(target=_run, daemon=True).start()

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
                if not keyboard_module:
                    logger.error("Controller: keyboard library not available for %s", label)
                    return
                logger.debug("Controller: using keyboard library for '%s' press", key)
                keyboard_module.press(key)
                start = time.time()
                while (time.time() - start) < hold_seconds:
                    if not ignore_cancel and self._mission_cancel.is_set():
                        logger.debug("Controller: %s cancelled", label)
                        break
                    time.sleep(0.05)
                try:
                    keyboard_module.release(key)
                except Exception:
                    logger.exception("Controller: failed to release '%s' key", key)
                logger.debug("%sController: %s complete%s", complete_color_start, label, complete_color_end)
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

    def deploy_flares(self, hold_seconds: float = 0.05, block: bool = True, ignore_cancel: bool = False):
        """Deploy flares (short press of the configured flares key)."""
        self._execute_key_press(DEPLOY_FLARES_KEY, hold_seconds=hold_seconds, block=block, action_name='deploy_flares', ignore_cancel=ignore_cancel)

    def wingsweep(self, hold_seconds: float = 0.5, block: bool = True):
        """Perform a wingsweep maneuver by pressing the configured wingsweep key."""
        self._execute_key_press(WINGSWEEP_KEY, hold_seconds=hold_seconds, block=block, action_name='wingsweep')

    def padlock_camera(self, hold_seconds: float = 0.1, block: bool = True):
        """Toggle padlock camera by pressing the configured padlock camera key."""
        self._execute_key_press(PADLOCK_CAMERA, hold_seconds=hold_seconds, block=block, action_name='padlock_camera')

    def fire_machine_gun(self, hold_seconds: float = 1.0, block: bool = True):
        """Fire machine gun by holding the configured machine-gun key."""
        self._execute_key_press(FIRE_MACHINE_GUN, hold_seconds=hold_seconds, block=block, action_name='fire_machine_gun')

    def fire_active_weapon(self, hold_seconds: float = 0.1, block: bool = True):
        """Activate the currently selected weapon (short press)."""
        self._execute_key_press(FIRE_ACTIVE_WEAPON, hold_seconds=hold_seconds, block=block, action_name='fire_active_weapon')

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
        
        # Clear mission cancel flag so weapon loop can fire properly
        self._mission_cancel.clear()
        self._weapon_loop_active = True
        
        def _loop():
            logger.info("Controller: weapon loop started (interval=%.2fs)", self._weapon_loop_interval)
            try:
                while self._weapon_loop_active:
                    try:
                        # Use shorter hold time for better game responsiveness
                        self.fire_active_weapon(hold_seconds=0.1, block=True)
                    except Exception as e:
                        logger.warning("Controller: weapon loop fire failed: %s", e)
                    time.sleep(self._weapon_loop_interval)
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
        self._weapon_loop_active = False
        if self._weapon_loop_thread:
            self._weapon_loop_thread.join(timeout=1.0)
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
            if self._mission_cancel.is_set():
                return False
            interval = min(check_interval, remaining)
            time.sleep(interval)
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
        while not self._mission_complete.is_set():
            if self._exit_event and self._exit_event.is_set():
                logger.info("Controller: exit requested, aborting mission wait")
                self.cancel_mission()
                break
            time.sleep(0.05)

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

        # Background loop flags and thread references
        padlock_loop_active = threading.Event()
        weapon_loop_active = threading.Event()
        self._padlock_thread = None
        self._weapon_thread = None

        def _padlock_loop():
            """Background loop to press padlock camera every 6 seconds"""
            logger.info("Controller: mission_j20 padlock loop started")
            try:
                while padlock_loop_active.is_set() and not self._mission_cancel.is_set():
                    if time.time() < self._padlock_cooldown_until:
                        logger.debug("Controller: padlock loop skipping press - manual cooldown active")
                    else:
                        self.padlock_camera(hold_seconds=0.1, block=True)
                    # Interruptible sleep - check for cancellation every 0.1 seconds
                    for _ in range(60):  # 60 * 0.1 = 6 seconds
                        if not padlock_loop_active.is_set() or self._mission_cancel.is_set():
                            break
                        time.sleep(0.1)
            except Exception:
                logger.exception("Controller: mission_j20 padlock loop error")
            finally:
                logger.info("Controller: mission_j20 padlock loop stopped")

        def _weapon_fire_loop():
            """Background loop to fire active weapon every 1 second"""
            logger.info("Controller: mission_j20 weapon fire loop started")
            try:
                while weapon_loop_active.is_set() and not self._mission_cancel.is_set():
                    self.fire_active_weapon(hold_seconds=0.1, block=True)
                    # Interruptible sleep - check for cancellation every 0.1 seconds
                    for _ in range(10):  # 10 * 0.1 = 1 second
                        if not weapon_loop_active.is_set() or self._mission_cancel.is_set():
                            break
                        time.sleep(0.1)
            except Exception:
                logger.exception("Controller: mission_j20 weapon fire loop error")
            finally:
                logger.info("Controller: mission_j20 weapon fire loop stopped")

        def _mission_runner():
            try:
                # Execute mission maneuvers (maneuvers log their own activity)
                self.nose_up(2.0)
                if self._mission_cancel.is_set():
                    logger.info("Controller: mission cancelled after nose_up")
                    return

                # Start background loops after first wingsweep
                padlock_loop_active.set()
                weapon_loop_active.set()
                self._padlock_thread = threading.Thread(target=_padlock_loop, daemon=True)
                self._weapon_thread = threading.Thread(target=_weapon_fire_loop, daemon=True)
                self._padlock_thread.start()
                self._weapon_thread.start()
                logger.info("Controller: mission_j20 background loops started")
                
                self.afterburner(20.0)
                if self._mission_cancel.is_set():
                    logger.info("Controller: mission cancelled after afterburner")
                    padlock_loop_active.clear()
                    weapon_loop_active.clear()
                    return
                # Roll right and afterburner
                self.roll_right(50, block=False)
                logger.info("\033[91mController: initiating roll_right while afterburner loop is active\033[0m")
                self.afterburner(10)
                if not self._interruptible_sleep(10, check_interval=1.0):
                    logger.info("Controller: mission cancelled during afterburner recharge")
                    padlock_loop_active.clear()
                    weapon_loop_active.clear()
                    return
                logger.info("\033[94mController:  initiated second afterburner\033[0m")
                self.afterburner(10)
                if not self._interruptible_sleep(10, check_interval=1.0):
                    logger.info("Controller: mission cancelled during afterburner recharge")
                    padlock_loop_active.clear()
                    weapon_loop_active.clear()
                    return
                self.afterburner(10)
                logger.info("\033[91mController: initiating final roll right 300 sec \033[0m")

                self.roll_right(300)
                if self._mission_cancel.is_set():
                    logger.info("Controller: mission cancelled after roll_right")
                    padlock_loop_active.clear()
                    weapon_loop_active.clear()
                    return

                # Stop background loops
                padlock_loop_active.clear()
                weapon_loop_active.clear()
                
                # Wait for background threads to fully stop
                if self._padlock_thread is not None:
                    self._padlock_thread.join(timeout=1.0)
                if self._weapon_thread is not None:
                    self._weapon_thread.join(timeout=1.0)
                
                #self.nose_down(4.0)
                #time.sleep(10.0)  # additional wait time to stabilize
                logger.info("\033[91mController: mission_j20 - sequence complete\033[0m")
            except Exception:
                logger.exception("Controller: mission_j20 failed")
                padlock_loop_active.clear()
                weapon_loop_active.clear()
                # Wait for background threads to stop
                if self._padlock_thread is not None:
                    self._padlock_thread.join(timeout=1.0)
                if self._weapon_thread is not None:
                    self._weapon_thread.join(timeout=1.0)
            finally:
                self._mission_complete.set()
                if self._mission_lock.locked():
                    self._mission_lock.release()
                    logger.info("\033[91mController: mission_j20 - lock released\033[0m")

        mission_a = threading.Thread(target=_mission_runner, daemon=True)
        mission_a.start()
        
        # Wait for mission to complete or exit requested
        while not self._mission_complete.is_set():
            if self._exit_event and self._exit_event.is_set():
                logger.info("Controller: exit requested, aborting mission wait")
                self.cancel_mission()
                break
            time.sleep(0.05)
        
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
            if sys.platform != "win32":
                logger.error("click_grid_region: Win32 mouse_event not available on %s", sys.platform)
                return
            try:
                if self._capture is None:
                    logger.error("Controller: click_grid_region - no capture reference")
                    return
                # Create a new mss instance — mss uses thread-local storage so the
                # main-thread instance cannot be used from a daemon thread.
                with mss() as sct:
                    monitors = sct.monitors
                    monitor_index = self._capture.monitor_index
                    if monitor_index < 1 or monitor_index >= len(monitors):
                        logger.error("Controller: click_grid_region - monitor index %d out of range", monitor_index)
                        return
                    mon = monitors[monitor_index]
                    region = self._capture.region
                    abs_left = mon["left"] + region[0]
                    abs_top = mon["top"] + region[1]
                    cap_w = region[2]
                    cap_h = region[3]
                cell_w = cap_w / grid_cols
                cell_h = cap_h / grid_rows
                row = (region_num - 1) // grid_cols
                col = (region_num - 1) % grid_cols
                abs_x = int(abs_left + (col + 0.5) * cell_w)
                abs_y = int(abs_top + (row + 0.5) * cell_h)
                label = region_name if region_name else str(region_num)
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

                if count > 1:
                    # Final click on ready button (lobby/continue button)
                    rbn = self._ready_button_region
                    row_rb = (rbn - 1) // grid_cols
                    col_rb = (rbn - 1) % grid_cols
                    x_rb = int(abs_left + (col_rb + 0.5) * cell_w)
                    y_rb = int(abs_top + (row_rb + 0.5) * cell_h)
                    logger.info("\033[93m📋 Clicking ready_button at (%d, %d)\033[0m", x_rb, y_rb)
                    _raw_click(x_rb, y_rb)
                    if self._analyzer is not None:
                        self._analyzer._game_lobby = True
                        logger.info("\033[93m📋 Ready button (region %d) clicked → GAME_LOBBY\033[0m", self._ready_button_region)
            except Exception:
                logger.exception("Controller: click_grid_region failed")

        if block:
            _do_click()
        else:
            threading.Thread(target=_do_click, daemon=True).start()

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
            if sys.platform != "win32":
                logger.error("click_crop: Win32 mouse_event not available on %s", sys.platform)
                return
            try:
                if self._capture is None:
                    logger.error("Controller: click_crop - no capture reference")
                    return
                with mss() as sct:
                    monitors = sct.monitors
                    monitor_index = self._capture.monitor_index
                    if monitor_index < 1 or monitor_index >= len(monitors):
                        logger.error("Controller: click_crop - monitor index %d out of range", monitor_index)
                        return
                    mon = monitors[monitor_index]
                    region = self._capture.region
                    abs_left = mon["left"] + region[0]
                    abs_top = mon["top"] + region[1]
                    cap_w = region[2]
                    cap_h = region[3]
                abs_x, abs_y = crop_centre(coords, cap_w, cap_h, abs_left, abs_top)
                label = region_name or f"({coords.x1:.2f},{coords.y1:.2f})"
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
        weapon loop. Mission completion/lock release are finalized by the
        mission runner thread.
        """
        logger.info("\033[91mController: cancel_mission called\033[0m")
        self._mission_cancel.set()
        self.stop_weapon_loop()  # Stop weapon loop when mission is cancelled

    def is_mission_running(self) -> bool:
        """Return True when a mission thread currently holds the mission lock."""
        return self._mission_lock.locked()

    def _set_last_mission(self, mission_name: str):
        with self._last_mission_lock:
            self._last_mission = mission_name
        self._auto_respawn_restart = True
        self._game_battle_since = time.time()
        if self._analyzer is not None:
            self._analyzer._last_battle_event_ts = time.time()
            self._analyzer._game_end_b = False
            self._analyzer._game_lobby = False
            self._analyzer._game_starting = False
            self._analyzer._game_starting_stalled = False
            logger.info("Controller: mission '%s' started → GAME_BATTLE", mission_name)

    def _start_game_starting_loop(self):
        """Background loop active in GAME_STARTING state.

        Every 5 seconds: press MISSION_J20_KEY and scan the good_luck region for 'Good Luck'.
        Once detected, wait 10 seconds then launch mission_j20.
        """
        good_luck_event = threading.Event()
        ocr_running = threading.Event()

        def _do_ocr_scan():
            """Run Good Luck OCR in background; sets good_luck_event on detection."""
            try:
                time.sleep(0.5)  # Allow 'Good Luck' screen to appear before capturing
                with mss() as sct:
                    s = sct.grab(self._capture.get_monitor_rect())
                    frame = np.array(s)[:, :, :3]
                if self._analyzer is not None and self._analyzer.scan_region_for_good_luck(frame):
                    good_luck_event.set()
            except Exception:
                logger.exception("Controller: game_starting OCR scan error")
            finally:
                ocr_running.clear()

        def _loop():
            logger.info("Controller: game_starting loop started - pressing '%s' key every 5s until 'Good Luck' detected", MISSION_J20_KEY)
            loop_start = time.time()
            max_wait = 180  # safety timeout: clear _game_starting if Good Luck never detected
            try:
                while self._analyzer is not None and self._analyzer._game_starting:
                    # Press MISSION_J20_KEY every interval
                    if keyboard_module:
                        keyboard_module.press_and_release(MISSION_J20_KEY)
                        logger.info("Controller: game_starting - pressed '%s' key", MISSION_J20_KEY)

                    # Start async OCR scan if one isn't already running
                    if self._capture is not None and not ocr_running.is_set():
                        ocr_running.set()
                        threading.Thread(target=_do_ocr_scan, daemon=True).start()

                    # 5-second interruptible wait; breaks early on Good Luck detection
                    for _ in range(50):  # 50 * 0.1s = 5s
                        if good_luck_event.is_set() or self._analyzer is None or not self._analyzer._game_starting:
                            break
                        time.sleep(0.1)

                    if not (self._analyzer is not None and self._analyzer._game_starting):
                        return

                    if time.time() - loop_start > max_wait:
                        logger.warning("Controller: game_starting timed out after %ds without 'Good Luck' - entering GAME_STARTING_STALLED", max_wait)
                        if self._analyzer is not None:
                            self._analyzer._game_starting = False
                            self._analyzer._game_starting_stalled = True
                        return

                    if good_luck_event.is_set():
                        good_luck_wait = 13
                        logger.info("\033[92mController: 'Good Luck' detected - waiting %ds before starting '%s' mission\033[0m", good_luck_wait, MISSION_J20_KEY)
                        for _ in range(good_luck_wait * 10):  # N * 0.1s = Ns
                            if self._analyzer is None or not self._analyzer._game_starting:
                                return
                            time.sleep(0.1)
                        if self._analyzer is not None and self._analyzer._game_starting:
                            logger.info("Controller: game_starting - launching J20 mission")
                            self._set_last_mission("j20")
                            threading.Thread(target=self.mission_j20, daemon=True).start()
                        return
            except Exception:
                logger.exception("Controller: game_starting loop error")
            finally:
                logger.info("Controller: game_starting loop stopped")

        threading.Thread(target=_loop, daemon=True).start()

    def restart_last_mission(self):
        """Restart the most recently started mission.

        Returns:
            True  — mission was successfully restarted.
            False — mission is currently running (lock held); restart skipped.
            None  — no previous mission has been recorded; nothing to restart.
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

        logger.info("Controller: no last mission to restart")
        return None  # None = no previous mission (distinct from False = failed/locked)