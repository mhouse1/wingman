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
DEPLOY_FLARES_KEY = 'x'


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

    def fire(self, hold_seconds: float = 1.0):
        """Fire the weapon.

        By default this click-and-hold lasts `hold_seconds` (default 1.0s).
        If `hold_seconds` is None or 0, an instant click/press is performed.
        """
        chosen_hold = hold_seconds if hold_seconds is not None else self.fire_hold_seconds
        logger.debug("Controller: firing (%s) hold=%s", self.fire_button, chosen_hold)
        # if using left mouse and hold is requested, perform hold in background to avoid blocking
        if self.fire_button == "left" and chosen_hold and chosen_hold > 0:
            acquired = self._firing_lock.acquire(blocking=False)
            if not acquired:
                logger.debug("Controller: already firing, skipping overlapping fire")
                return

            def _hold_runner():
                try:
                    pyautogui.mouseDown(button='left')
                    logger.debug("Controller: mouseDown (holding for %s seconds)", chosen_hold)
                    time.sleep(chosen_hold)
                    pyautogui.mouseUp(button='left')
                    logger.debug("Controller: mouseUp")
                finally:
                    try:
                        self._firing_lock.release()
                    except RuntimeError:
                        pass

            t = threading.Thread(target=_hold_runner, daemon=True)
            t.start()
            return

        # fallback: instant click or keyboard key
        if self.fire_button == "left":
            pyautogui.click()
        else:
            pyautogui.press(self.fire_button)

    def continuous_move(self, dx_per_sec: float, dy_per_sec: float, duration: float, interval: float = 0.02):
        """Move the mouse continuously by (dx_per_sec, dy_per_sec) for `duration` seconds.
        Runs in a background thread so it doesn't block the main loop.
        """
        def _runner():
            end = time.time() + duration
            logger.info("Controller: starting continuous move dx=%s dy=%s for %ss", dx_per_sec, dy_per_sec, duration)
            while time.time() < end:
                # move by per-interval amount
                step_x = dx_per_sec * interval
                step_y = dy_per_sec * interval
                try:
                    pyautogui.moveRel(int(step_x), int(step_y), duration=0)
                except Exception:
                    logger.exception("Controller: continuous move failed")
                    break
                time.sleep(interval)
            logger.info("Controller: finished continuous move")

        t = threading.Thread(target=_runner, daemon=True)
        t.start()

    def continuous_move_up(self, speed_px_per_sec: float, duration: float):
        # negative y for upward movement
        self.continuous_move(0.0, -abs(speed_px_per_sec), duration)

    def nose_up(self, hold_seconds: float = 2.5, block: bool = True):
        """Nose-up maneuver: presses and holds the configured nose-up key.

        Args:
            hold_seconds: How long to hold the key (default 2.5 seconds)
        """
        logger.debug("Controller: nose_up - pressing '%s' key for %s seconds", NOSE_UP_KEY, hold_seconds)
        def _do_press():
            try:
                if not keyboard_module:
                    logger.error("Controller: keyboard library not available for nose_up")
                    return
                logger.debug("Controller: using keyboard library for '%s' press", NOSE_UP_KEY)
                keyboard_module.press(NOSE_UP_KEY)
                start = time.time()
                while (time.time() - start) < hold_seconds:
                    if self._mission_cancel.is_set():
                        logger.debug("Controller: nose_up cancelled")
                        break
                    time.sleep(0.05)
                try:
                    keyboard_module.release(NOSE_UP_KEY)
                except Exception:
                    logger.exception("Controller: failed to release '%s' key", NOSE_UP_KEY)
                logger.debug("Controller: nose_up complete")
            except Exception:
                logger.exception("Controller: nose_up failed")

        if block:
            _do_press()
        else:
            t = threading.Thread(target=_do_press, daemon=True)
            t.start()
    
    def nose_down(self, hold_seconds: float = 2.5, block: bool = True):
        """Nose-down maneuver: presses and holds the configured nose-down key.

        Args:
            hold_seconds: How long to hold the key (default 2.5 seconds)
        """
        logger.debug("Controller: nose_down - pressing '%s' key for %s seconds", NOSE_DOWN_KEY, hold_seconds)
        def _do_press():
            try:
                if not keyboard_module:
                    logger.error("Controller: keyboard library not available for nose_down")
                    return
                logger.debug("Controller: using keyboard library for '%s' press", NOSE_DOWN_KEY)
                keyboard_module.press(NOSE_DOWN_KEY)
                start = time.time()
                while (time.time() - start) < hold_seconds:
                    if self._mission_cancel.is_set():
                        logger.debug("Controller: nose_down cancelled")
                        break
                    time.sleep(0.05)
                try:
                    keyboard_module.release(NOSE_DOWN_KEY)
                except Exception:
                    logger.exception("Controller: failed to release '%s' key", NOSE_DOWN_KEY)
                logger.debug("Controller: nose_down complete")
            except Exception:
                logger.exception("Controller: nose_down failed")

        if block:
            _do_press()
        else:
            t = threading.Thread(target=_do_press, daemon=True)
            t.start()

    def afterburner(self, hold_seconds: float = 2.5, block: bool = True):
        """Afterburner: presses and holds the configured afterburner key.

        Args:
            hold_seconds: How long to hold the key (default 2.5 seconds)
        """
        logger.debug("Controller: afterburner - pressing '%s' for %s seconds", AFTERBURNER_KEY, hold_seconds)
        def _do_press():
            try:
                if not keyboard_module:
                    logger.error("Controller: keyboard library not available for afterburner")
                    return
                logger.debug("Controller: using keyboard library for '%s' press", AFTERBURNER_KEY)
                keyboard_module.press(AFTERBURNER_KEY)
                start = time.time()
                while (time.time() - start) < hold_seconds:
                    if self._mission_cancel.is_set():
                        logger.debug("Controller: afterburner cancelled")
                        break
                    time.sleep(0.05)
                try:
                    keyboard_module.release(AFTERBURNER_KEY)
                except Exception:
                    logger.exception("Controller: failed to release '%s' key", AFTERBURNER_KEY)
                logger.debug("Controller: afterburner complete")
            except Exception:
                logger.exception("Controller: afterburner failed")

        if block:
            _do_press()
        else:
            t = threading.Thread(target=_do_press, daemon=True)
            t.start()

    def begin_mission(self):
        """Run mission sequence: nose up 4s, afterburner 2s, nose down 3s.

        Blocks until mission completes to prevent overlapping missions.
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
                self.afterburner(20.0)
                self.nose_down(4.0)
                time.sleep(10.0)  # additional wait time to stabilize
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
