import time
import logging
import threading
import pyautogui
import sys

try:
    import keyboard as keyboard_module
except Exception:
    keyboard_module = None

logger = logging.getLogger(__name__)

# Key bindings (change these to remap controls)
NOSE_UP_KEY = 'i'
NOSE_DOWN_KEY = 'k'
AFTERBURNER_KEY = 'e'
AIRBRAKE_KEY = 'd'
ROLL_LEFT_KEY = 'j'
ROLL_RIGHT_KEY = 'l'
DEPLOY_FLARES_KEY = 'space'
FIRE_MACHINIE_GUN = 'a'
FIRE_ACTIVE_WEAPON = 'f'
WINGSWEEP_KEY = 'w'
SWITCH_WEAPON = 'g'
SPECIAL_ABILITY = 'q'
PADLOCK_CAMERA = 'p'
TOGGLE_WEAPON_LOOP_KEY = 'x'  # Press X to toggle weapon firing loop
MISSION_J20_KEY = 'u'  # Press U to start J20 mission

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

class Controller:
    def __init__(self, region, fire_button="left", fire_hold_seconds: float = 0.0, exit_event=None):
        # region is (left, top, width, height)
        self.region = region
        self.fire_button = fire_button
        self.fire_hold_seconds = float(fire_hold_seconds or 0.0)
        self._firing_lock = threading.Lock()
        self._mission_lock = threading.Lock()
        self._mission_complete = threading.Event()
        self._mission_cancel = threading.Event()
        self._exit_event = exit_event  # Event to signal program exit
        
        # Weapon loop state
        self._weapon_loop_active = False
        self._weapon_loop_thread = None
        self._weapon_loop_interval = 0.5  # Fire every 0.5 seconds
        
        # Register hotkey for weapon loop toggle
        if keyboard_module:
            try:
                keyboard_module.add_hotkey(TOGGLE_WEAPON_LOOP_KEY, self.toggle_weapon_loop)
                logger.info("Controller: registered hotkey '%s' to toggle weapon loop", TOGGLE_WEAPON_LOOP_KEY)
            except Exception:
                logger.exception("Controller: failed to register weapon loop hotkey")
            
            try:
                def start_j20_mission(e):
                    logger.info("Controller: U key pressed - starting J20 mission")
                    threading.Thread(target=self.mission_j20, daemon=True).start()
                
                keyboard_module.on_press_key(MISSION_J20_KEY, start_j20_mission, suppress=False)
                logger.info("Controller: registered hotkey '%s' to start J20 mission", MISSION_J20_KEY)
            except Exception:
                logger.exception("Controller: failed to register J20 mission hotkey")

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

    def _execute_key_press(self, key: str, hold_seconds: float = 2.5, block: bool = True, action_name: str | None = None):
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
                    if self._mission_cancel.is_set():
                        logger.debug("Controller: %s cancelled", label)
                        break
                    time.sleep(0.05)
                try:
                    keyboard_module.release(key)
                except Exception:
                    logger.exception("Controller: failed to release '%s' key", key)
                logger.debug("%sController: %s complete%s", color_start, label, color_end)
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

    def deploy_flares(self, hold_seconds: float = 0.05, block: bool = True):
        """Deploy flares (short press of the configured flares key)."""
        self._execute_key_press(DEPLOY_FLARES_KEY, hold_seconds=hold_seconds, block=block, action_name='deploy_flares')

    def wingsweep(self, hold_seconds: float = 0.5, block: bool = True):
        """Perform a wingsweep maneuver by pressing the configured wingsweep key."""
        self._execute_key_press(WINGSWEEP_KEY, hold_seconds=hold_seconds, block=block, action_name='wingsweep')

    def padlock_camera(self, hold_seconds: float = 0.1, block: bool = True):
        """Toggle padlock camera by pressing the configured padlock camera key."""
        self._execute_key_press(PADLOCK_CAMERA, hold_seconds=hold_seconds, block=block, action_name='padlock_camera')

    def fire_machine_gun(self, hold_seconds: float = 1.0, block: bool = True):
        """Fire machine gun by holding the configured machine-gun key."""
        self._execute_key_press(FIRE_MACHINIE_GUN, hold_seconds=hold_seconds, block=block, action_name='fire_machine_gun')

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
                try:
                    self._mission_lock.release()
                except RuntimeError:
                    pass

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
            logger.warning("Controller: mission_j20 already in progress, skipping (lock held)")
            return

        logger.info("\033[92mController: mission_j20 - starting mission sequence (lock acquired)\033[0m")
        self._mission_complete.clear()
        self._mission_cancel.clear()

        # Background loop flags and thread references
        padlock_loop_active = threading.Event()
        weapon_loop_active = threading.Event()
        padlock_thread = None
        weapon_thread = None

        def _padlock_loop():
            """Background loop to press padlock camera every 5 seconds"""
            logger.info("Controller: mission_j20 padlock loop started")
            try:
                while padlock_loop_active.is_set() and not self._mission_cancel.is_set():
                    self.padlock_camera(hold_seconds=0.1, block=True)
                    # Interruptible sleep - check for cancellation every 0.1 seconds
                    for _ in range(50):  # 50 * 0.1 = 5 seconds
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
                self.wingsweep()
                if self._mission_cancel.is_set():
                    logger.info("Controller: mission cancelled after wingsweep")
                    return
                
                # Start background loops after first wingsweep
                padlock_loop_active.set()
                weapon_loop_active.set()
                padlock_thread = threading.Thread(target=_padlock_loop, daemon=True)
                weapon_thread = threading.Thread(target=_weapon_fire_loop, daemon=True)
                padlock_thread.start()
                weapon_thread.start()
                logger.info("Controller: mission_j20 background loops started")
                
                self.afterburner(10.0)
                if self._mission_cancel.is_set():
                    logger.info("Controller: mission cancelled after afterburner")
                    padlock_loop_active.clear()
                    weapon_loop_active.clear()
                    return
                self.afterburner(10.0)
                if self._mission_cancel.is_set():
                    logger.info("Controller: mission cancelled after afterburner")
                    padlock_loop_active.clear()
                    weapon_loop_active.clear()
                    return
                self.wingsweep()
                if self._mission_cancel.is_set():
                    logger.info("Controller: mission cancelled after wingsweep")
                    padlock_loop_active.clear()
                    weapon_loop_active.clear()
                    return
                self.roll_right(4)
                if self._mission_cancel.is_set():
                    logger.info("Controller: mission cancelled after roll_right")
                    padlock_loop_active.clear()
                    weapon_loop_active.clear()
                    return
                self.afterburner(10)
                if self._mission_cancel.is_set():
                    logger.info("Controller: mission cancelled after afterburner")
                    padlock_loop_active.clear()
                    weapon_loop_active.clear()
                    return
                self.deploy_flares()
                if self._mission_cancel.is_set():
                    logger.info("Controller: mission cancelled after deploy_flares")
                    padlock_loop_active.clear()
                    weapon_loop_active.clear()
                    return
                self.roll_left(10)
                if self._mission_cancel.is_set():
                    logger.info("Controller: mission cancelled after roll_left")
                    padlock_loop_active.clear()
                    weapon_loop_active.clear()
                    return
                self.deploy_flares()
                if self._mission_cancel.is_set():
                    logger.info("Controller: mission cancelled after deploy_flares")
                    padlock_loop_active.clear()
                    weapon_loop_active.clear()
                    return
                self.roll_right(30)
                if self._mission_cancel.is_set():
                    logger.info("Controller: mission cancelled after roll_right")
                    padlock_loop_active.clear()
                    weapon_loop_active.clear()
                    return
                self.roll_left(30)
                if self._mission_cancel.is_set():
                    logger.info("Controller: mission cancelled")
                    padlock_loop_active.clear()
                    weapon_loop_active.clear()
                    return
                
                # Stop background loops
                padlock_loop_active.clear()
                weapon_loop_active.clear()
                
                # Wait for background threads to fully stop
                if padlock_thread is not None:
                    padlock_thread.join(timeout=1.0)
                if weapon_thread is not None:
                    weapon_thread.join(timeout=1.0)
                
                #self.nose_down(4.0)
                #time.sleep(10.0)  # additional wait time to stabilize
                logger.info("\033[91mController: mission_j20 - sequence complete\033[0m")
            except Exception:
                logger.exception("Controller: mission_j20 failed")
                padlock_loop_active.clear()
                weapon_loop_active.clear()
                # Wait for background threads to stop
                if padlock_thread is not None:
                    padlock_thread.join(timeout=1.0)
                if weapon_thread is not None:
                    weapon_thread.join(timeout=1.0)
            finally:
                self._mission_complete.set()
                try:
                    self._mission_lock.release()
                    logger.info("\033[91mController: mission_j20 - lock released\033[0m")
                except RuntimeError:
                    logger.error("Controller: mission_j20 - failed to release lock (was already released)")
                    pass

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

    def cancel_mission(self):
        """Request cancellation of any running mission.

        Sets the cancel flag which maneuvers poll; also sets the mission-complete
        event so callers waiting on completion will unblock.
        """
        logger.info("\033[91mController: cancel_mission called\033[0m")
        self._mission_cancel.set()
        self.stop_weapon_loop()  # Stop weapon loop when mission is cancelled
        try:
            self._mission_complete.set()
        except Exception:
            logger.exception("Controller: failed to set mission_complete during cancel")