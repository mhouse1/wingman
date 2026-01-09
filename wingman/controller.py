import time
import logging
import threading
import pyautogui

logger = logging.getLogger(__name__)


class Controller:
    def __init__(self, region, fire_button="left", fire_hold_seconds: float = 0.0):
        # region is (left, top, width, height)
        self.region = region
        self.fire_button = fire_button
        self.fire_hold_seconds = float(fire_hold_seconds or 0.0)
        self._firing_lock = threading.Lock()

    def move_toward(self, target_xy, smoothing=1.0):
        """Move mouse toward target_xy (frame coordinates). smoothing in (0..1)."""
        tx, ty = target_xy
        # convert to absolute screen coords
        abs_x = self.region[0] + int(tx)
        abs_y = self.region[1] + int(ty)
        cur_x, cur_y = pyautogui.position()
        nx = cur_x + (abs_x - cur_x) * smoothing
        ny = cur_y + (abs_y - cur_y) * smoothing
        logger.debug("Controller: moving mouse to (%d, %d) from (%d, %d)", abs_x, abs_y, cur_x, cur_y)
        pyautogui.moveTo(int(nx), int(ny), duration=0)

    def _hold_and_release(self):
        try:
            pyautogui.mouseDown(button='left')
            logger.debug("Controller: mouseDown (holding for %s seconds)", self.fire_hold_seconds)
            time.sleep(self.fire_hold_seconds)
            pyautogui.mouseUp(button='left')
            logger.debug("Controller: mouseUp")
        finally:
            try:
                self._firing_lock.release()
            except RuntimeError:
                pass

    def fire(self, hold_seconds: float = 3.0):
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
