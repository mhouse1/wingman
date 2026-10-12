"""Save pictures of the game around the moments a survey flight goes wrong, for looking at afterwards.

Run beside a live survey session (Design 017, phase 4b). It follows wingman's
log and grabs the nested display itself, so it sees what wingman saw without
being part of it:

- a look at the full map that found no map, a map found open with no look in
  progress, or a map still open after a closing press: the last 12 s of the
  picture, half size, four frames a second, and the 4 s after;
- the compass unread: the MINIMAP crop at full size, lossless, plus a dozen
  with the compass read, for comparison.

Both of the day's reader faults were found from these pictures (2026-10-04):
the full map's see-through ring over bright cloud, and pieces of rim markers
counted as the compass letter.

    survey_watch.py <wingman.log> <out dir> [display, default :3]

Run it in the project environment (`uv run --active python ...`): it needs mss.
It stops when wingman does.
"""

import collections
import os
import subprocess
import sys
import time

log_path, out_dir = sys.argv[1], sys.argv[2]
os.environ["DISPLAY"] = sys.argv[3] if len(sys.argv) > 3 else ":3"

import cv2  # noqa: E402
import mss  # noqa: E402
import numpy as np  # noqa: E402

MINIMAP = (0.8319, 0.0044, 0.9986, 0.2689)  # the MINIMAP crop of wingman/config.yaml
LOOK_FAULTS = (
    ("did not open", "not_opened"),
    ("no look is in progress", "stray"),
    ("still open", "still_open"),
)


def wingman_running() -> bool:
    return subprocess.run(["pgrep", "-f", "[w]ingman.main"], capture_output=True).returncode == 0


def new_log_text(position: int) -> "tuple[str, int]":
    try:
        if os.path.getsize(log_path) < position:
            position = 0  # the log was rotated by a new session
        with open(log_path, errors="replace") as handle:
            handle.seek(position)
            return handle.read(), handle.tell()
    except OSError:
        return "", position


def main() -> None:
    os.makedirs(out_dir, exist_ok=True)
    recent = collections.deque(maxlen=48)
    position = os.path.getsize(log_path) if os.path.exists(log_path) else 0
    pending, countdown = None, 0
    saved = {"unread": 0, "read": 0}
    last_crop = 0.0
    with mss.mss() as screen:
        monitor = screen.monitors[0]
        while wingman_running():
            started = time.time()
            try:
                frame = np.asarray(screen.grab(monitor))[:, :, :3]
            except Exception:
                time.sleep(1.0)
                continue
            ok, jpeg = cv2.imencode(".jpg", cv2.resize(frame, (960, 600)), [cv2.IMWRITE_JPEG_QUALITY, 80])
            if ok:
                recent.append((started, jpeg))
            text, position = new_log_text(position)

            if pending is None:
                for phrase, name in LOOK_FAULTS:
                    if phrase in text:
                        pending, countdown = f"{time.strftime('%H%M%S')}_{name}", 16
                        break
            else:
                countdown -= 1
                if countdown == 0:
                    folder = os.path.join(out_dir, pending)
                    os.makedirs(folder, exist_ok=True)
                    for taken, data in recent:
                        stamp = time.strftime("%H%M%S", time.localtime(taken))
                        name = f"{stamp}_{int((taken % 1) * 1000):03d}.jpg"
                        with open(os.path.join(folder, name), "wb") as handle:
                            handle.write(data.tobytes())
                    print("wrote", len(recent), "frames to", folder, flush=True)
                    pending = None

            states = [line for line in text.splitlines() if "SURVEY: state=" in line]
            kind = None
            if states and "SURVEY POS" not in text:
                if "state=no-heading" in states[-1] and saved["unread"] < 80:
                    kind = "unread"
                elif "state=cruise" in states[-1] and saved["read"] < 12:
                    kind = "read"
            if kind and started - last_crop >= 1.5:
                height, width = frame.shape[:2]
                x0, y0, x1, y1 = MINIMAP
                crop = frame[int(y0 * height) : int(y1 * height), int(x0 * width) : int(x1 * width)]
                cv2.imwrite(os.path.join(out_dir, f"{time.strftime('%H%M%S')}_compass_{kind}.png"), crop)
                saved[kind] += 1
                last_crop = started
            time.sleep(max(0.0, 0.25 - (time.time() - started)))
    print("watch ended", saved, flush=True)


if __name__ == "__main__":
    main()
