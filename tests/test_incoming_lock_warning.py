"""ADR 184: the incoming crop reads LOCK WARNING as well as INCOMING.

Operator, 2026-11-16, on screenshot_20261113_112801.png: the roll should hold "as
soon as LOCK WARNING is detected". The crop before ADR 184 (x 0.4521 to 0.5486)
cut the warning to "OCKWARNIN", which holds neither fallback token, so no alert
fired. Real OCR on the operator's frame (tests/fixtures/incoming/, add with
`git add -f`).
"""

from pathlib import Path

import cv2
import pytest

from wingman import analyzer as A
import yaml

_LOCK_WARNING = Path("tests/fixtures/incoming/lock_warning_20261113_112801.png")
_INCOMING = Path("test_screenshots/INCOMING.png")


def _evaluate(path):
    with open("wingman/config.yaml") as f:
        cfg = yaml.safe_load(f)
    (x1, y1), (x2, y2) = cfg["crops"]["incoming"]["coords"]
    det = cfg["incoming_detection"]
    templates = A._build_incoming_templates(
        (x1, y1, x2, y2),
        A._resolve_template_source_paths(det["incoming_template_sources"]),
        det["incoming_template_scales"])
    frame = cv2.imread(str(path))
    h, w = frame.shape[:2]
    region = frame[int(y1 * h):int(y2 * h), int(x1 * w):int(x2 * w)]
    return A._process_incoming_region(
        region, templates, det["incoming_template_matching_enabled"],
        det["incoming_template_threshold"], det["incoming_template_near_threshold_low"],
        det["incoming_template_near_threshold_high"], det["incoming_template_fallback_to_ocr"],
        tuple(det["incoming_fallback_tokens"]))


@pytest.fixture
def require_easyocr():
    pytest.importorskip("easyocr", reason="EasyOCR not installed")


def test_the_shipped_crop_detects_lock_warning(require_easyocr):
    result = _evaluate(_LOCK_WARNING)
    assert result["template_hit"] or result["fallback_hit"], result.get("fallback_raw")


def test_the_shipped_crop_still_detects_incoming(require_easyocr):
    result = _evaluate(_INCOMING)
    assert result["template_hit"] or result["fallback_hit"]
