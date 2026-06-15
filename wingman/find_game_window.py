"""Diagnostic: capture a PipeWire frame, overlay a coordinate grid, save to /tmp/wingman_grid.png.

Run with MetalStorm visible on screen:
    make find-game

Open the saved image, find the top-left corner of the MetalStorm window, and note the x,y
coordinates shown by the nearest grid label. Then set in wingman/config.yaml:

    game_window_offset: {x: <x>, y: <y>}
"""
import sys
import time

import cv2
import gi
import numpy as np

gi.require_version("Gst", "1.0")
from gi.repository import Gst

Gst.init(None)

from wingman.portal import acquire_screencast_node

GRID_STEP = 200   # pixels between grid lines (full-res)
OUT_PATH = "/tmp/wingman_grid.png"

node_id, bus = acquire_screencast_node()
print(f"node_id={node_id}")

pipeline_str = (
    f"pipewiresrc path={node_id} ! videoconvert "
    f"! video/x-raw,format=BGR "
    f"! appsink name=sink max-buffers=1 drop=true sync=false"
)
p = Gst.parse_launch(pipeline_str)
sink = p.get_by_name("sink")
p.set_state(Gst.State.PLAYING)
time.sleep(2)

sample = sink.emit("try-pull-sample", 5 * Gst.SECOND)
if sample is None:
    print("ERROR: no frame received within 5 s", file=sys.stderr)
    sys.exit(1)

st = sample.get_caps().get_structure(0)
w, h = st.get_int("width")[1], st.get_int("height")[1]
print(f"Native frame: {w}x{h}")

buf = sample.get_buffer()
ok, mi = buf.map(Gst.MapFlags.READ)
frame = np.frombuffer(mi.data, dtype=np.uint8).reshape(h, w, 3).copy()
buf.unmap(mi)
p.set_state(Gst.State.NULL)

# Draw grid lines and coordinate labels
annotated = frame.copy()
font = cv2.FONT_HERSHEY_SIMPLEX
font_scale = 0.6
thickness = 1
line_color = (0, 255, 0)      # green
label_color = (0, 255, 255)   # yellow
bg_color = (0, 0, 0)

for x in range(0, w, GRID_STEP):
    cv2.line(annotated, (x, 0), (x, h - 1), line_color, 1)
    label = str(x)
    (tw, th), _ = cv2.getTextSize(label, font, font_scale, thickness)
    cv2.rectangle(annotated, (x + 2, 2), (x + tw + 6, th + 6), bg_color, -1)
    cv2.putText(annotated, label, (x + 4, th + 4), font, font_scale, label_color, thickness)

for y in range(0, h, GRID_STEP):
    cv2.line(annotated, (0, y), (w - 1, y), line_color, 1)
    label = str(y)
    (tw, th), _ = cv2.getTextSize(label, font, font_scale, thickness)
    cv2.rectangle(annotated, (2, y + 2), (tw + 6, y + th + 6), bg_color, -1)
    cv2.putText(annotated, label, (4, y + th + 4), font, font_scale, label_color, thickness)

# Save half-res for easy viewing
out = cv2.resize(annotated, (w // 2, h // 2))
cv2.imwrite(OUT_PATH, out)
print(f"Saved (half-res, grid step {GRID_STEP}px full-res): {OUT_PATH}")
print()
print("Open the image, find the MetalStorm window's top-left corner, and read the grid label.")
print("Then set in wingman/config.yaml:")
print("    game_window_offset: {x: <x>, y: <y>}")
