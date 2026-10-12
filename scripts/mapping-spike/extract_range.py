"""Write a range of a recording's frames as a stretch for reconstruct.py.

gate_extract.py picks its own stretches. This writes the one asked for, with
the mask gate_extract.py made for the recording, so a pass found in the log can
be reconstructed whatever the gate thought of it.

    extract_range.py <video> <mask.png> <out dir> <first frame> <last frame> <every n-th>
"""

import sys
from pathlib import Path
import cv2

video, mask_path, out = sys.argv[1], sys.argv[2], Path(sys.argv[3])
first, last, step = int(sys.argv[4]), int(sys.argv[5]), int(sys.argv[6])
mask = cv2.imread(mask_path, 0)
images, masks = out / "images", out / "masks"
images.mkdir(parents=True, exist_ok=True)
masks.mkdir(parents=True, exist_ok=True)
cap = cv2.VideoCapture(video)
cap.set(cv2.CAP_PROP_POS_FRAMES, first)
written = 0
for i in range(first, last + 1):
    ok, frame = cap.read()
    if not ok:
        break
    if (i - first) % step:
        continue
    name = f"f{i:06d}.jpg"
    cv2.imwrite(str(images / name), frame, [cv2.IMWRITE_JPEG_QUALITY, 96])
    cv2.imwrite(str(masks / (name + ".png")), mask)
    written += 1
print("wrote", written, "frames to", out)
