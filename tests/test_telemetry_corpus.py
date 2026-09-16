"""ADR 038 — telemetry OCR corpus accuracy and timing test.

Runs the real telemetry OCR pipeline (no monkeypatching) over the operator-
labeled screenshot corpus in test_screenshots/telemetry/. Ground truth is
filename-encoded:

    telemetry<ID>_<YYYYMMDD>_<HHMMSS>_spd<value>_alt<value>_<day|night>.png

Acceptance gate (ADR 038, recalibrated 2026-09-16): every frame not in
_KNOWN_HARD_FRAMES must read exactly — a blanket percentage threshold was
tried first (90%) and found to just go permanently red without catching
anything, since 3 of 17 frames hit an already-understood failure mode (see
_KNOWN_HARD_FRAMES) that a percentage bar can't distinguish from a genuine
new regression. Requiring exactness on the *other* 14 keeps the gate able to
catch a real regression — on any of those 14, or a newly added frame — while
being honest that these 3 specific ones are not currently solvable without
real OCR work, not "acceptable at the raw layer" by design.

Investigated 2026-09-15/16: confirmed via direct inspection that all three
labels are correct (not a labeling slip) and the misreads are genuine —
EasyOCR is *confidently wrong* on two of the three (0.999 and 0.989
confidence on incorrect values), so confidence-gating the existing fallback
passes doesn't help. A 3-way majority vote across the existing HSV/binary/
upscaled passes DOES fix all three — but was found, by testing against the
full corpus rather than just these three, to introduce a NEW wrong answer on
telemetryB (a currently-correct low-confidence primary read gets outvoted by
two fallback passes that disagree with each other). Not shipped, since
trading a known, transparent gap for a hidden one is a worse outcome. See
the anomaly/ADR history around 2026-09-15/16 for the full investigation if
attempting this again — start from why majority-vote regressed telemetryB
before trying another combination rule.

Wrong reads on the three known-hard frames are still printed for operator
review every run — never silently resolved — and the plausibility filter in
wingman/telemetry.py owns rejecting a wrong value from reaching a tactic
decision at runtime regardless of what this test measures. Per-frame
processing time is reported so preprocessing changes can be judged on speed
as well as accuracy.

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

_MIN_EXACT_MATCH_RATE = 0.90  # retained for the printed summary line only

# Investigated 2026-09-15/16 (see module docstring): genuine, reproducible
# EasyOCR misreads, not labeling errors, not corpus drift (all corpus files
# share one mtime). telemetryC: altitude "27164" truncates to "2716" at
# 0.999 confidence. telemetryD: speed "214" misreads as "2141" — the one
# genuinely low-confidence case (0.34). telemetryM: speed "748" misreads as
# "148" at 0.989 confidence. Revisit this set only after re-running the
# investigation above, not by just appending a new ID when a future frame
# happens to fail — that would defeat the point of naming these explicitly.
_KNOWN_HARD_FRAMES = frozenset({"C", "D", "M"})


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
    if not corpus:
        pytest.skip(f"no labeled corpus frames found in {_CORPUS_DIR} "
                    "(untracked test corpus, ADR 100 D7)")

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
    known_hard_misses = [r for r in misses if r["id"] in _KNOWN_HARD_FRAMES]
    unexpected_misses = [r for r in misses if r["id"] not in _KNOWN_HARD_FRAMES]
    for r in known_hard_misses:
        print(f"  KNOWN-HARD telemetry{r['id']} ({r['lighting']}): "
              f"expected spd/alt {r['expected']} read {r['read']} — "
              f"documented gap, not gated (see _KNOWN_HARD_FRAMES)")
    for r in unexpected_misses:
        print(f"  MISMATCH telemetry{r['id']} ({r['lighting']}): "
              f"expected spd/alt {r['expected']} read {r['read']} — "
              f"operator review: labeling slip or OCR failure mode?")

    resolved_hard = [r["id"] for r in results
                     if r["id"] in _KNOWN_HARD_FRAMES and r["exact"]]
    if resolved_hard:
        print(f"  NOTE: previously-known-hard frame(s) now read correctly: "
              f"{', '.join(resolved_hard)} — safe to remove from "
              f"_KNOWN_HARD_FRAMES")

    # Exactness required on every frame NOT already known-hard — this is
    # what actually catches a future regression (the 90% blanket rate this
    # replaced could not, since 3/17 already-understood misses ate the same
    # margin a real new one would need to trip it).
    assert not unexpected_misses, (
        "telemetry corpus regression — exact match failed on frame(s) not "
        "in _KNOWN_HARD_FRAMES: "
        + ", ".join(f"telemetry{r['id']} (expected {r['expected']}, "
                    f"read {r['read']})" for r in unexpected_misses)
    )
