import numpy as np
from mss import mss


class Capture:
    def __init__(self, region):
        self.region = region
        self.sct = mss()

    def get_frame(self):
        """Return a BGR image of the configured region."""
        monitor = {
            "left": self.region[0],
            "top": self.region[1],
            "width": self.region[2],
            "height": self.region[3],
        }
        s = self.sct.grab(monitor)
        img = np.array(s)
        # mss returns BGRA
        img = img[:, :, :3]
        # convert to BGR (mss already returns BGRA/BGR ordering for most platforms)
        return img
