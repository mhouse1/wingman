import logging
import cv2
import numpy as np


logger = logging.getLogger(__name__)


class Vision:
    def __init__(self, hsv_lower, hsv_upper, debug=False):
        self.hsv_lower = np.array(hsv_lower, dtype=np.uint8)
        self.hsv_upper = np.array(hsv_upper, dtype=np.uint8)
        self.debug = debug

    def find_enemies(self, frame):
        """Return list of enemy centroids in frame coordinates: [(x,y,area), ...]"""
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, self.hsv_lower, self.hsv_upper)
        # optional morphological clean
        kernel = np.ones((3, 3), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        enemies = []
        for c in contours:
            area = cv2.contourArea(c)
            if area < 20:
                continue
            M = cv2.moments(c)
            if M["m00"] == 0:
                continue
            cx = int(M["m10"] / M["m00"])
            cy = int(M["m01"] / M["m00"])
            enemies.append((cx, cy, area))

        logger.debug("Vision: found %d enemies", len(enemies))

        if self.debug:
            debug_img = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR)
            for (x, y, a) in enemies:
                cv2.circle(debug_img, (x, y), 6, (0, 255, 0), 2)
            cv2.imshow("mask", debug_img)
            cv2.waitKey(1)

        return enemies
