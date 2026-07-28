"""ADR 038 — telemetry OCR corpus accuracy and timing test.

Runs the real telemetry OCR pipeline (no monkeypatching) over the operator-
labeled screenshot corpus in test_screenshots/telemetry/. Ground truth is
filename-encoded:

    telemetry<ID>_<YYYYMMDD>_<HHMMSS>_spd<value>_alt<value>_<day|night>.png

Acceptance gate (ADR 038): both values read exactly on at least 90 percent of
corpus frames. Wrong reads on the remainder are acceptable at the raw layer —
the plausibility filter in wingman/telemetry.py owns rejection — but every
mismatch is printed for operator review (labeling slip vs real OCR failure
mode, never silently resolved). Per-frame processing time is reported so
preprocessing changes can be judged on speed as well as accuracy.

Run via:
    make ocr
Marked @pytest.mark.slow so it is excluded from the default `make test` run.
"""

import re
import time
from pathlib import Path

import cv2
import pytest
import yaml

try:
    import easyocr as _easyocr_check  # noqa: F401
    _EASYOCR_AVAILABLE = True
except ImportError:
    _EASYOCR_AVAILABLE = False

pytestmark = pytest.mark.slow

_REPO = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _REPO / "wingman" / "config.yaml"
_CORPUS_DIR = _REPO / "test_screenshots" / "telemetry"

_FILENAME_RE = re.compile(
    r"telemetry([A-Z])_(\d{8})_(\d{6})_spd(\d+|na)_alt(\d+|na)_(day|night)\.png"
)

_MIN_EXACT_MATCH_RATE = 0.90


def _load_corpus():
    frames = []
    for path in sorted(_CORPUS_DIR.glob("telemetry*.png")):
        m = _FILENAME_RE.fullmatch(path.name)
        if m is None:
            continue
        letter, _date, _time_part, spd, alt, lighting = m.groups()
        frames.append({
            "id": letter,
            "path": path,
            "speed": None if spd == "na" else int(spd),
            "altitude": None if alt == "na" else int(alt),
            "lighting": lighting,
        })
    return frames


@pytest.mark.skipif(not _EASYOCR_AVAILABLE, reason="easyocr not installed")
def test_telemetry_corpus_exact_match_rate():
    from wingman.analyzer import _process_telemetry_region
    from wingman.crop_region import get_crop

    corpus = _load_corpus()
    assert corpus, f"no labeled corpus frames found in {_CORPUS_DIR}"

    with open(_CONFIG_PATH, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    coords = cfg["crops"]["ALTITUDE_SPEED"]["coords"]
    (x1, y1), (x2, y2) = coords

    results = []
    for entry in corpus:
        frame = cv2.imread(str(entry["path"]))
        assert frame is not None, f"unreadable corpus frame: {entry['path'].name}"
        crop = get_crop(frame, x1, y1, x2, y2)

        t0 = time.time()
        speed_read, altitude_read, _ = _process_telemetry_region(crop)
        elapsed = time.time() - t0

        speed_ok = entry["speed"] is None or speed_read == entry["speed"]
        alt_ok = entry["altitude"] is None or altitude_read == entry["altitude"]
        results.append({
            "id": entry["id"],
            "lighting": entry["lighting"],
            "expected": (entry["speed"], entry["altitude"]),
            "read": (speed_read, altitude_read),
            "exact": speed_ok and alt_ok,
            "elapsed_s": elapsed,
        })

    exact = [r for r in results if r["exact"]]
    misses = [r for r in results if not r["exact"]]
    rate = len(exact) / len(results)
    timings = sorted(r["elapsed_s"] for r in results)
    mean_s = sum(timings) / len(timings)
    p95_s = timings[max(0, int(len(timings) * 0.95) - 1)]

    day_n = sum(1 for r in results if r["lighting"] == "day")
    day_ok = sum(1 for r in results if r["lighting"] == "day" and r["exact"])
    night_n = len(results) - day_n
    night_ok = len(exact) - day_ok

    print(f"\n[telemetry corpus] {len(results)} frames | exact {len(exact)} "
          f"({rate:.1%}) | day {day_ok}/{day_n} night {night_ok}/{night_n} "
          f"| time mean {mean_s:.2f}s p95 {p95_s:.2f}s")
    for r in misses:
        print(f"  MISMATCH telemetry{r['id']} ({r['lighting']}): "
              f"expected spd/alt {r['expected']} read {r['read']} — "
              f"operator review: labeling slip or OCR failure mode?")

    assert rate >= _MIN_EXACT_MATCH_RATE, (
        f"telemetry corpus exact-match rate {rate:.1%} below "
        f"{_MIN_EXACT_MATCH_RATE:.0%} gate; mismatches: "
        + ", ".join(f"telemetry{r['id']}" for r in misses)
    )
