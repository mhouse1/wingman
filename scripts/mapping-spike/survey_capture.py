"""Capture a survey flight: full-size pictures of the game at a steady rate, for mapping.

Wingman does not have to be running. Start the game on the nested display, fly
the arena by hand in level passes, and run this beside it. It writes one JPEG
per picture and an index of when each was taken. A picture identical to the one
before it (a menu, a frozen screen) is not written.

Run it in the project environment, which has the screen grabber:

    uv run --active python scripts/mapping-spike/survey_capture.py OUT_DIR [--fps 5] [--seconds N] [--display :3]

Stop it with Ctrl-C. Design 017, phase 4.
"""

import argparse
import os
import time
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
parser.add_argument("out_dir")
parser.add_argument("--fps", type=float, default=5.0, help="pictures a second (default 5)")
parser.add_argument(
    "--seconds", type=float, default=0.0, help="stop after this long (default: run until Ctrl-C)"
)
parser.add_argument(
    "--display", default=":3", help="X display the game is on (default :3, the nested display)"
)
parser.add_argument("--quality", type=int, default=95, help="JPEG quality (default 95)")
args = parser.parse_args()

os.environ["DISPLAY"] = args.display  # before the grabber is imported

import cv2
import mss
import numpy as np

out = Path(args.out_dir)
out.mkdir(parents=True, exist_ok=True)
index_path = out / "index.csv"
new_index = not index_path.exists()
# Carry on numbering after an earlier capture into the same folder.
count = len(list(out.glob("frame_*.jpg")))
interval = 1.0 / args.fps
written = skipped = late = 0

with mss.mss() as grabber, index_path.open("a", encoding="utf-8") as index:
    if new_index:
        index.write("frame,unix_time\n")
    screen = grabber.monitors[0]
    print(
        "capturing %dx%d from %s at %.1f a second into %s (Ctrl-C to stop)"
        % (screen["width"], screen["height"], args.display, args.fps, out)
    )
    started = time.monotonic()
    due = started
    previous = None
    try:
        while not args.seconds or time.monotonic() - started < args.seconds:
            wait = due - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            elif wait < -interval:
                late += 1
                due = time.monotonic()  # fell behind: do not try to catch up in a burst
            due += interval
            taken = time.time()
            picture = np.asarray(grabber.grab(screen))[:, :, :3]
            small = picture[::8, ::8]
            if previous is not None and np.array_equal(small, previous):
                skipped += 1
                continue
            previous = small.copy()
            name = "frame_%06d.jpg" % count
            cv2.imwrite(str(out / name), picture, [cv2.IMWRITE_JPEG_QUALITY, args.quality])
            index.write("%s,%.3f\n" % (name, taken))
            count += 1
            written += 1
            if written % 100 == 0:
                index.flush()
                print("  %d pictures, %.0f s" % (written, time.monotonic() - started))
    except KeyboardInterrupt:
        pass

elapsed = time.monotonic() - started
size_mb = sum(f.stat().st_size for f in out.glob("frame_*.jpg")) / 1e6
print(
    "wrote %d pictures in %.0f s (%.1f a second), skipped %d unchanged, fell behind %d times; folder is %.0f MB"
    % (written, elapsed, written / max(elapsed, 1e-9), skipped, late, size_mb)
)
