"""Read HUD speed and altitude from recording frames with EasyOCR (run in the project environment)."""

import sys
from pathlib import Path
import json
import logging
import cv2

logging.disable(logging.CRITICAL)
import easyocr

video, out = sys.argv[1], sys.argv[2]
ks = (
    json.loads(Path(sys.argv[3]).read_text())
    if len(sys.argv) > 3
    else [1666, 1670, 1675, 1683, 2956, 4300]
)
rd = easyocr.Reader(["en"], gpu=False, verbose=False)
cap = cv2.VideoCapture(video)
res = {}
pos = -1
for k in ks:
    if k != pos + 1:
        cap.set(cv2.CAP_PROP_POS_FRAMES, k)
    ok, f = cap.read()
    pos = k
    if not ok:
        break
    h, w = f.shape[:2]
    c = f[int(0.6333 * h) - 2 : int(0.6722 * h) + 3, int(0.3167 * w) - 3 : int(0.3167 * w) + 26]
    c = cv2.resize(c, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
    boxes = rd.readtext(c, allowlist="0123456789", detail=1)
    rows = sorted(((float(b[0][1]), t, p) for b, t, p in boxes if t), key=lambda r: r[0])
    res[k] = [(t, round(float(p), 2)) for _, t, p in rows]
    if len(ks) < 20:
        print(k, res[k])
        cv2.imwrite(out.replace(".json", "_%d.png" % k), c)
Path(out).write_text(json.dumps(res))
