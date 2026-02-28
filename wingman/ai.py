import time
import math
import logging


logger = logging.getLogger(__name__)


class SimpleAI:
    def __init__(self, region, smoothing=0.3, fire_cooldown=0.2):
        self.region = region
        self.smoothing = smoothing
        self.fire_cooldown = fire_cooldown
        self._last_fire = 0.0

    def decide(self, enemies):
        """Decide action given list of enemies (x,y,area). Returns dict with 'target' and 'fire'."""
        if not enemies:
            logger.debug("AI: no enemies")
            return {"target": None, "fire": False}

        # choose nearest to screen center
        cx = self.region[2] // 2
        cy = self.region[3] // 2
        best = None
        best_dist = 1e9
        for (x, y, a) in enemies:
            d = math.hypot(x - cx, y - cy)
            if d < best_dist:
                best_dist = d
                best = (x, y)

        fire = False
        now = time.time()
        if now - self._last_fire >= self.fire_cooldown:
            fire = True
            self._last_fire = now

        logger.debug("AI: chosen target=%s fire=%s", best, fire)

        return {"target": best, "fire": fire, "smoothing": self.smoothing}
