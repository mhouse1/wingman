import time
import logging
import threading
import pyautogui

try:
    import keyboard as keyboard_module
except Exception:
    keyboard_module = None

logger = logging.getLogger(__name__)

# Key bindings (change these to remap controls)
NOSE_UP_KEY = 'e'
NOSE_DOWN_KEY = 'd'
AFTERBURNER_KEY = 'a'
AIRBRAKE_KEY = 'g'
ROLL_LEFT_KEY = 's'
ROLL_RIGHT_KEY = 'f'
DEPLOY_FLARES_KEY = 'space'
FIRE_MACHINIE_GUN = 'w'
FIRE_ACTIVE_WEAPON = 'r'
WINGSWEEP_KEY = '3'
SWITCH_WEAPON = 'b'
SPECIAL_ABILITY = 'q'


class Controller:
    def __init__(self, region, fire_button="left", fire_hold_seconds: float = 0.0):
        # region is (left, top, width, height)
        self.region = region
        self.fire_button = fire_button
        self.fire_hold_seconds = float(fire_hold_seconds or 0.0)
        self._firing_lock = threading.Lock()
        self._mission_lock = threading.Lock()
        self._mission_complete = threading.Event()
        self._mission_cancel = threading.Event()

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
        logger.debug("Controller: %s - pressing '%s' key for %s seconds", label, key, hold_seconds)

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
                logger.debug("Controller: %s complete", label)
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

    def fire_machine_gun(self, hold_seconds: float = 1.0, block: bool = True):
        """Fire machine gun by holding the configured machine-gun key."""
        self._execute_key_press(FIRE_MACHINIE_GUN, hold_seconds=hold_seconds, block=block, action_name='fire_machine_gun')

    def fire_active_weapon(self, hold_seconds: float = 0.1, block: bool = True):
        """Activate the currently selected weapon (short press)."""
        self._execute_key_press(FIRE_ACTIVE_WEAPON, hold_seconds=hold_seconds, block=block, action_name='fire_active_weapon')

    def begin_mission(self):
        """This mission sequence performs a predefined set of maneuvers for the Aaarvark, it flies up and tries to stay up
        Compatible Jets: F111, F-14, Mig-23, J20
        """
        # Check if mission is already running
        acquired = self._mission_lock.acquire(blocking=False)
        if not acquired:
            logger.debug("Controller: mission already in progress, skipping")
            return

        logger.info("Controller: begin_mission - starting mission sequence")
        self._mission_complete.clear()
        self._mission_cancel.clear()

        def _mission_runner():
            try:
                # Execute mission maneuvers (maneuvers log their own activity)
                self.nose_up(2.0)
                self.wingsweep()
                self.afterburner(10.0)
                self.afterburner(10.0)
                self.wingsweep()
                self.roll_right(4)
                self.afterburner(10)
                self.deploy_flares()
                self.roll_left(10)
                self.deploy_flares()
                self.roll_right(30)
                self.roll_left(30)
                #self.nose_down(4.0)
                #time.sleep(10.0)  # additional wait time to stabilize
                logger.info("Controller: begin_mission - sequence complete")
            except Exception:
                logger.exception("Controller: begin_mission failed")
            finally:
                self._mission_complete.set()
                try:
                    self._mission_lock.release()
                except RuntimeError:
                    pass

        mission_a = threading.Thread(target=_mission_runner, daemon=True)
        mission_a.start()
        
        # Wait for mission to complete
        self._mission_complete.wait()

    def cancel_mission(self):
        """Request cancellation of any running mission.

        Sets the cancel flag which maneuvers poll; also sets the mission-complete
        event so callers waiting on completion will unblock.
        """
        logger.info("Controller: cancel_mission called")
        self._mission_cancel.set()
        try:
            self._mission_complete.set()
        except Exception:
            logger.exception("Controller: failed to set mission_complete during cancel")
