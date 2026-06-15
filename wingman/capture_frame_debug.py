"""Diagnostic: capture one frame via PipeWire and save to /tmp/wingman_native.png."""
import sys
import time

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst
import numpy as np
import cv2

Gst.init(None)

from wingman.portal import acquire_screencast_node

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
arr = np.frombuffer(mi.data, dtype=np.uint8).reshape(h, w, 3).copy()
buf.unmap(mi)

out = "/tmp/wingman_native.png"
cv2.imwrite(out, cv2.resize(arr, (w // 2, h // 2)))
print(f"Saved (half-res): {out}")

p.set_state(Gst.State.NULL)
