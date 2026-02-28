import numpy as np
from mss import mss


from mss import mss
import numpy as np

class Capture:
    def __init__(self, region, monitor_index=1):
        self.region = region
        self.monitor_index = monitor_index
        self.sct = mss()

    def get_monitor_rect(self):
        # mss monitors: 1=primary, 2=secondary, etc. 0=all
        monitors = self.sct.monitors
        if self.monitor_index < 1 or self.monitor_index >= len(monitors):
            raise ValueError(f"Monitor index {self.monitor_index} out of range. Found {len(monitors)-1} monitors.")
        mon = monitors[self.monitor_index]
        # Apply region offset within the selected monitor
        left = mon["left"] + self.region[0]
        top = mon["top"] + self.region[1]
        width = self.region[2]
        height = self.region[3]
        return {"left": left, "top": top, "width": width, "height": height}

    def get_frame(self):
        """Return a BGR image of the configured region on the selected monitor."""
        monitor = self.get_monitor_rect()
        s = self.sct.grab(monitor)
        img = np.array(s)
        # mss returns BGRA
        img = img[:, :, :3]
        return img
