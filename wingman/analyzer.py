"""Game state analyzer for detecting respawn, enemies, and other game conditions."""

import logging
import re
import cv2
import numpy as np
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import time
from enum import Enum, auto
from pathlib import Path

HEALTH_WINDOW_SIZE  = 10   # readings kept in rolling window (~10 s at 1 Hz)
HEALTH_SPIKE_FACTOR = 1.5  # reject readings more than 50 % above the established ceiling

from transitions import Machine, MachineError

from .crop_region import CropCoords, get_crop, load_crops, draw_crops
from .telemetry import TelemetryProcessor, pitch_band_from_angle_deg


class GameState(Enum):
    GAME_UNKNOWN         = auto()  # Startup state; classify current frame before normal runtime flow
    GAME_BATTLE          = auto()  # Active gameplay (default); respawn/incoming scanning active
    GAME_END_B           = auto()  # "Click to Continue" detected; clicking in progress
    GAME_LOBBY           = auto()  # Final continue (region 64) clicked; waiting in lobby
    GAME_WAITING         = auto()  # PLAY clicked; waiting for CANCEL crop to confirm matchmaking
    GAME_STARTING        = auto()  # Matchmaking confirmed; waiting for "Good Luck" before launching mission
    GAME_STARTING_STALLED = auto() # GAME_STARTING timed out without "Good Luck" detection
    GAME_BATTLE_MANUAL   = auto()  # Player took manual control; auto-mission restart suppressed
    GAME_BATTLE_EJECT    = auto()  # Eject sequence active (missiles empty); respawn detection only


# ADR 074: states where the popup quick-scan runs and dismissal actions are
# allowed. GAME_UNKNOWN is included because a modal popup there hides every
# classification marker — dismissal is the only recovery path.
# GAME_STARTING_STALLED is included because a popup (e.g. the flight-pass
# promo) can be what blocked the "Good Luck" detection in the first place —
# checking during the stall window beats waiting out the 20 s reclassify.
POPUP_DISMISS_STATES = (GameState.GAME_LOBBY, GameState.GAME_WAITING,
                        GameState.GAME_UNKNOWN, GameState.GAME_STARTING_STALLED)

# ADR 102: states where the quick-scan re-checks whether the LOBBY is in fact
# still on screen. Separate from the popup set on purpose — this permits ONE
# lobby crop to be read, not popup dismissal. 2026-09-01: PLAY was clicked, the
# FSM went LOBBY to WAITING to STARTING on a CANCEL read, and the match never
# began; the game sat at the lobby with PLAY visible for 150 s while wingman
# pressed 'u' and probed health, until the starting timeout fired.
LOBBY_RECHECK_STATES = (GameState.GAME_STARTING,)
# Consecutive agreeing reads before the state is walked back. The quick-scan
# runs at roughly a 1 s cadence, so this is ~3 s of PLAY being continuously
# visible — enough that a single stray read cannot abort a match that really is
# starting, and still 50x faster than the 150 s timeout it replaces.
STARTING_PLAY_CONFIRM_READS = 3

# States where a round is genuinely under way and stopping would abandon an
# aircraft in flight. ADR 094's deferred exit waits these out; everything else
# — including GAME_UNKNOWN before the first classification, and GAME_END_B once
# the round is scored — is a safe moment to stop.
BATTLE_STATES = frozenset({
    GameState.GAME_BATTLE,
    GameState.GAME_BATTLE_MANUAL,
    GameState.GAME_BATTLE_EJECT,
})

# ADR 084: states where the FSM has lost the screen and a recovery action is
# warranted. Deliberately EXCLUDES GAME_LOBBY / GAME_WAITING — unlike the popup
# crops, these actions leave squads and close modals next to an "Exit" button,
# so they must not fire while the FSM still knows where it is.
STALL_ACTION_STATES = (GameState.GAME_UNKNOWN, GameState.GAME_STARTING_STALLED)

# Scan order: most specific screen first. The batch stops at the first hit, so a
# generic match must never pre-empt a precise one.
STALL_RECOVERY_CROPS = ("STALL_PROFILE", "STALL_RETRY",
                        "STALL_EXIT_TO_DESKTOP", "STALL_AIRCRAFT")

# Gated on UNREADY dwell rather than state dwell: UNREADY makes
# scan_region_for_play_button return None, which makes _classify_unknown_state
# fail forever, so this screen strands the FSM without ever looking like a popup.
STALL_UNREADY_CROP = "STALL_MULTI_PLAYER"


class GameEvent(Enum):
    """Orchestration events the analyzer publishes (ADR 060 Phase 1).

    Replaces ADR 039's single-slot `set_on_*` setters: subscribing to a
    nonexistent event is an AttributeError at wiring time rather than a silent
    runtime no-op, and every event fans out to any number of subscribers.
    Payloads are documented per event; `emit()` passes them through verbatim.
    """
    CANCEL_MISSION = auto()            # ()          — transition requires mission cancel
    START_GAME_STARTING_LOOP = auto()  # ()          — entered GAME_STARTING
    LOBBY_PLAY_CLICK = auto()          # (crop, frame)
    MANUAL_TAKEOVER = auto()           # SAF-001: operator has the aircraft
    LOBBY_POPUP_CLICK = auto()         # (crop,)
    LOBBY_POPUP_ABSENT = auto()        # ()          — popup batch completed, none detected
    STALL_RECOVERY_ACTION = auto()     # (crop,)     — stall-recovery screen detected (ADR 084)
    LOBBY_STALL = auto()               # ()          — no lobby crops detected for the stall window
    FSM_TRANSITION = auto()            # (trigger, prev_state_name, next_state_name, ts)
    RESPAWN_DETECTED = auto()          # (frame,)    — fired from the background OCR thread


try:
    import easyocr
except ImportError:
    easyocr = None

logger = logging.getLogger(__name__)

_DEFAULT_INCOMING_TEMPLATE_SOURCES = (
    "test_screenshots/INCOMING.png",
)
_DEFAULT_INCOMING_TEMPLATE_SCALES = (1.0,)

# OCR fallback substrings for the INCOMING warning. Keep in sync with
# incoming_fallback_tokens in config.yaml. If detections stop while the log
# shows warning-like reads with mangled edge characters (e.g. NCOMIN), the
# crop is clipping the text — recalibrate rather than loosening these.
_DEFAULT_INCOMING_FALLBACK_TOKENS = ("MING", "ARNING")


def _binarize_template_image(image_bgr: np.ndarray) -> np.ndarray:
    """Return an Otsu-binarized template image for high-contrast matching."""
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return binary


def _resolve_template_source_paths(config_sources: "list[str] | None") -> "list[Path]":
    """Resolve template source paths relative to repository root when needed."""
    base_dir = Path(__file__).resolve().parent.parent
    sources = config_sources or list(_DEFAULT_INCOMING_TEMPLATE_SOURCES)
    resolved: "list[Path]" = []
    for entry in sources:
        p = Path(entry)
        if not p.is_absolute():
            p = (base_dir / p).resolve()
        resolved.append(p)
    return resolved


def _build_incoming_templates(
    incoming_crop: "tuple[float, float, float, float]",
    source_paths: "list[Path]",
    scales: "list[float]",
) -> "list[tuple[str, np.ndarray]]":
    """Build binary template variants from configured source screenshots."""
    templates: "list[tuple[str, np.ndarray]]" = []
    x1, y1, x2, y2 = incoming_crop
    for src in source_paths:
        frame = cv2.imread(str(src))
        if frame is None:
            logger.warning("Incoming template source unreadable: %s", src)
            continue
        crop = get_crop(frame, x1, y1, x2, y2)
        if crop.size == 0:
            logger.warning("Incoming template source crop is empty: %s", src)
            continue

        base_template_binary = _binarize_template_image(crop)
        src_stem = src.stem
        for scale in scales:
            try:
                scale_val = float(scale)
            except (TypeError, ValueError):
                continue
            if scale_val <= 0.0:
                continue
            scaled_binary = cv2.resize(base_template_binary, None, fx=scale_val, fy=scale_val, interpolation=cv2.INTER_CUBIC)
            h, w = scaled_binary.shape[:2]
            if h < 5 or w < 5:
                continue
            scale_suffix = int(round(scale_val * 100))
            templates.append((f"{src_stem}_s{scale_suffix}", scaled_binary))
    return templates


def _match_incoming_template_score(
    incoming_binary: np.ndarray,
    templates: "list[tuple[str, np.ndarray]]",
) -> "tuple[float, str | None]":
    """Return the best binary-template score and corresponding template label."""
    best_score = -1.0
    best_label: "str | None" = None
    ih, iw = incoming_binary.shape[:2]
    for label, template_binary in templates:
        th, tw = template_binary.shape[:2]
        if th > ih or tw > iw:
            continue
        response = cv2.matchTemplate(incoming_binary, template_binary, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, _ = cv2.minMaxLoc(response)
        score = float(max_val)
        if score > best_score:
            best_score = score
            best_label = label
    return best_score, best_label


def _is_shutdown_runtime_error(exc: Exception) -> bool:
    """Return True for expected RuntimeError messages during interpreter/executor shutdown."""
    if not isinstance(exc, RuntimeError):
        return False
    msg = str(exc).lower()
    return (
        "interpreter shutdown" in msg
        or "cannot schedule new futures" in msg
        or "cannot schedule new task" in msg
        or "shutdown" in msg
    )


# ============================================================================
# OCR Worker Functions (called from thread pool — numpy arrays passed directly)
# ============================================================================

# Thread-local EasyOCR readers: each pool thread owns one reader, so calls run
# concurrently without locking while still sharing the process address space
# (no IPC serialization, and CUDA context is shared across threads on Windows).
_thread_local = threading.local()

# Serializes EasyOCR reader initialization across threads.
# EasyOCR downloads model files to a shared temp path on first run; if two threads
# initialize concurrently they race on the same temp.zip, causing FileNotFoundError.
# The lock ensures only one thread downloads at a time — subsequent threads find the
# model already on disk and initialize instantly.
_ocr_init_lock = threading.Lock()

# ADR 038 telemetry extraction: HSV bounds isolating the green HUD telemetry
# text (measured on the labeled day/night corpus), and the per-row OCR
# confidence below which the fallback preprocessing variants are consulted.
_TELEMETRY_HSV_LOWER = np.array([30, 40, 80], dtype=np.uint8)
_TELEMETRY_HSV_UPPER = np.array([90, 255, 255], dtype=np.uint8)
_TELEMETRY_ROW_CONF_MIN = 0.6

# Set from config at startup by GameStateAnalyzer.__init__.
# False (default) skips the failed GPU probe and goes straight to CPU init.
_use_gpu: bool = False


# Readers are thread-local and each holds ~300 MB of model weights, so the
# legitimate lifetime total is roughly one per LONG-LIVED thread: 13 pool
# workers plus a handful of analyzer daemons. Initialising on a transient
# thread allocates and discards a model per call — 1,138 GAME_STARTING health
# probes produced 1,213 initialisations on 2026-08-22 before that probe was
# moved onto the pool. This counter makes a recurrence self-reporting instead
# of hiding in the console at INFO.
_OCR_READER_INIT_BUDGET = 25
_ocr_reader_inits = 0


def _get_thread_ocr_reader():
    """Return the EasyOCR reader for the current thread, initializing it on first call."""
    global _ocr_reader_inits
    if not getattr(_thread_local, 'reader', None):
        with _ocr_init_lock:
            _thread_local.reader = None
            if easyocr:
                try:
                    _thread_local.reader = easyocr.Reader(['en'], gpu=_use_gpu, verbose=False)
                    mode = "GPU" if _use_gpu else "CPU"
                    _ocr_reader_inits += 1
                    if _ocr_reader_inits > _OCR_READER_INIT_BUDGET:
                        logger.warning(
                            "OCR reader init #%d on thread '%s' — exceeds the %d expected "
                            "for long-lived threads. Something is running OCR on transient "
                            "threads; each init allocates ~300 MB (Performance 008).",
                            _ocr_reader_inits, threading.current_thread().name,
                            _OCR_READER_INIT_BUDGET)
                    logger.debug("OCR thread %d: initialized EasyOCR reader (%s)", threading.get_ident(), mode)
                except Exception as e:
                    logger.warning("OCR thread %d: EasyOCR init failed: %s", threading.get_ident(), e)
    return _thread_local.reader


def _process_respawn_region(respawn_frame):
    """Detect RESPAWN text in a pre-extracted region frame.

    Args:
        respawn_frame: numpy BGR array of the respawn grid region.

    Returns:
        tuple: (detected: bool, ocr_time: float, text_found: str or None)
    """
    reader = _get_thread_ocr_reader()
    if reader is None:
        return (False, 0.0, None)

    t_start = time.time()

    # Preprocess respawn region
    gray_respawn = cv2.cvtColor(respawn_frame, cv2.COLOR_BGR2GRAY)
    _, binary_respawn = cv2.threshold(gray_respawn, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    small_gray = cv2.resize(gray_respawn, None, fx=0.7, fy=0.7, interpolation=cv2.INTER_AREA)
    small_binary = cv2.resize(binary_respawn, None, fx=0.7, fy=0.7, interpolation=cv2.INTER_AREA)

    # Run OCR on both grayscale and thresholded images
    results = []
    for img, label in [(small_gray, 'gray'), (small_binary, 'binary')]:
        ocr_results = reader.readtext(img, detail=1, paragraph=False, workers=0)
        for (_, text, conf) in ocr_results:
            text_clean = ''.join(c for c in text.strip().upper() if c.isalpha())
            results.append((label, text_clean, conf))

    ocr_time = time.time() - t_start

    # Log all OCR results for debugging
    logger.debug(f"Respawn OCR results: {results}")

    for label, text_clean, _conf in results:
        if _respawn_text_matches(text_clean):
            logger.debug(f"Respawn detected (variant: {label}, text: {text_clean})")
            return (True, ocr_time, text_clean)

    return (False, ocr_time, None)


def _process_incoming_region(
    incoming_frame,
    incoming_templates,
    template_matching_enabled: bool,
    template_threshold: float,
    template_near_threshold_low: float,
    template_near_threshold_high: float,
    fallback_to_ocr: bool,
    fallback_tokens: "tuple[str, ...]" = _DEFAULT_INCOMING_FALLBACK_TOKENS,
):
    """
    Worker function to process incoming missile region in a thread pool thread.

    Args:
        incoming_frame: numpy array (BGR) of the incoming region — passed by reference, no copy

    Returns:
        dict with incoming template/ocr evaluation details.
    """
    t_start = time.time()

    # Template-primary detection path.
    incoming_binary = _binarize_template_image(incoming_frame)
    template_score = -1.0
    template_label = None
    template_hit = False
    near_threshold = False
    if template_matching_enabled:
        template_score, template_label = _match_incoming_template_score(incoming_binary, incoming_templates)
        template_hit = template_label is not None and template_score >= template_threshold
        near_threshold = (
            template_label is not None
            and template_near_threshold_low <= template_score <= template_near_threshold_high
        )

    result = {
        "template_score": template_score,
        "template_label": template_label,
        "template_hit": bool(template_hit),
        "near_threshold": bool(near_threshold),
        "fallback_used": False,
        "fallback_hit": False,
        "fallback_variant": None,
        "fallback_text": None,
        "fallback_raw": [],
        "processing_time": 0.0,
    }

    if template_hit or not fallback_to_ocr:
        result["processing_time"] = time.time() - t_start
        return result

    reader = _get_thread_ocr_reader()
    if reader is None:
        result["processing_time"] = time.time() - t_start
        return result

    result["fallback_used"] = True
    gray_incoming = cv2.cvtColor(incoming_frame, cv2.COLOR_BGR2GRAY)
    variants = {
        "gray_up_1p4": cv2.resize(gray_incoming, None, fx=1.4, fy=1.4, interpolation=cv2.INTER_CUBIC),
        "binary_otsu_up_1p4": cv2.resize(incoming_binary, None, fx=1.4, fy=1.4, interpolation=cv2.INTER_CUBIC),
    }

    raw_texts = []
    for variant_name, variant_img in variants.items():
        ocr_results = reader.readtext(variant_img, detail=0, paragraph=True, workers=0)
        extracted_text = " ".join(str(entry) for entry in ocr_results)
        normalized = " ".join(extracted_text.upper().split()).replace(" ", "")
        if normalized:
            raw_texts.append(f"{variant_name}={normalized!r}")
        if any(token in normalized for token in fallback_tokens):
            result["fallback_hit"] = True
            result["fallback_variant"] = variant_name
            result["fallback_text"] = normalized
            break

    result["fallback_raw"] = raw_texts
    result["processing_time"] = time.time() - t_start
    return result


def _process_text_region(frame, text_tokens: "list[str]"):
    """Generic OCR worker: detect any of the given text tokens in a crop region.

    Args:
        frame: numpy array (BGR) — the extracted crop region.
        text_tokens: list of uppercase substrings; returns True on the first hit.

    Returns:
        tuple: (detected: bool, ocr_time: float, text_found: str or None)
    """
    reader = _get_thread_ocr_reader()
    if reader is None:
        return (False, 0.0, None)

    t_start = time.time()
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    upscaled = cv2.resize(gray, None, fx=1.4, fy=1.4, interpolation=cv2.INTER_CUBIC)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    for img in (upscaled, binary):
        results = reader.readtext(img, detail=0, paragraph=True, workers=0)
        text = " ".join(str(r) for r in results).upper().replace(" ", "")
        if any(token in text for token in text_tokens):
            return (True, time.time() - t_start, text)

    return (False, time.time() - t_start, None)


# ADR 080: crops whose digits render in the HUD's pale green (the ammo
# counters are white and stay on the legacy variant order).
_GREEN_DIGIT_LABELS = ("health", "fuel")


def _hsv_green_digit_mask(frame_bgr):
    """Isolate pale-green HUD digits by hue, inverted for OCR (ADR 080 d3).

    Luminance thresholding fails over sky: the green digits and blue sky
    share brightness, so Otsu splits mid-glyph and emits fragments. Hue
    separates them on every measured background.
    """
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (35, 30, 120), (95, 255, 255))
    upscaled = cv2.resize(mask, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    return cv2.bitwise_not(upscaled)


def _process_health_region(health_frame, label: str = "health") -> "tuple[int | None, float]":
    """Extract the numeric health value from the health crop via OCR.

    Upscales and thresholds the crop to maximise digit legibility, then strips
    all non-digit characters from the OCR output. Also used for the ammo
    counters (label='ammo_flares'/'ammo_missiles').

    Args:
        health_frame: numpy array (BGR) — the extracted health crop region.
        label: crop name used in the no-digits debug log.

    Returns:
        tuple: (health_value: int or None, ocr_time: float)
               health_value is None when no digits are found.
    """
    reader = _get_thread_ocr_reader()
    if reader is None:
        return (None, 0.0)

    t_start = time.time()
    gray = cv2.cvtColor(health_frame, cv2.COLOR_BGR2GRAY)
    upscaled = cv2.resize(gray, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    _, binary = cv2.threshold(upscaled, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # ADR 080 d3: the HEALTH and FUEL digits are the HUD's pale green, which
    # collapses under luminance thresholding whenever the background is sky
    # (nose-up flight) — the measured source of the confirmed-read dropouts.
    # For those labels a hue mask goes FIRST (9/9 dropout frames read exactly
    # right vs 1/9 via Otsu, 2026-08-18), gray second, and the fragment-prone
    # Otsu binary last (its partial reads — '50' from 250 — otherwise win the
    # early return and feed the confirm window garbage). The ammo counters
    # are white digits (the mask sees nothing there) and keep the original
    # variant order untouched.
    if label in _GREEN_DIGIT_LABELS:
        variants = (_hsv_green_digit_mask(health_frame), upscaled, binary)
    else:
        variants = (binary, upscaled)

    raw_reads = []
    for img in variants:
        results = reader.readtext(img, detail=0, paragraph=False, workers=0)
        raw_reads.extend(str(r) for r in results)
        digits = "".join(c for r in results for c in str(r) if c.isdigit())
        if digits:
            return (int(digits), time.time() - t_start)

    # No digits in either variant — log what OCR actually saw so a dead or
    # misaligned crop is diagnosable from the session log (2026-08-13: the
    # AMMO_MISSILE crop produced 2 readings in an hour with no trace of why).
    logger.debug("Analyzer: %s OCR found no digits — raw: %r", label, raw_reads)
    return (None, time.time() - t_start)


def _row_value(boxes) -> "tuple[int | None, float]":
    """Parse the leading number from one row of x-ordered OCR boxes.

    Boxes are joined left-to-right into a single string, and the first contiguous
    digit run is taken as the value. Because the numbers are left-aligned and the
    'MPH'/'feet' labels follow them, this yields the number while ignoring the
    trailing label — even when the label lands in the same detection box (e.g.
    '27681 feet' → 27681). Returns (value, confidence); confidence is the
    minimum OCR confidence across the row's digit boxes (conservative — one
    doubtful box taints the row), 0.0 when the row has no digits.
    """
    boxes_sorted = sorted(boxes, key=lambda b: b[1])
    text = " ".join(str(t) for _, _, t, _ in boxes_sorted)
    match = re.search(r"\d+", text)
    if not match:
        return (None, 0.0)
    conf = min(c for _, _, _, c in boxes_sorted)
    return (int(match.group()), float(conf))


def _split_telemetry_rows(ocr_results, img_height) -> "tuple[int | None, int | None, float, float]":
    """Split detail=1 OCR boxes from the ALTITUDE_SPEED crop into speed/altitude rows.

    The crop holds two left-aligned numeric lines — speed (MPH) on top, altitude
    (feet) below. Boxes are grouped into an upper (speed) and lower (altitude) row
    by bounding-box vertical centre, then each row's leading number is parsed. When
    only one line is visible (small vertical spread), it is assigned by the half of
    the crop it occupies. Returns (speed, altitude, speed_conf, alt_conf); values
    are None (conf 0.0) when that row produced no digits. See ADR 038.
    """
    boxes = []
    for bbox, text, conf in ocr_results:
        if not any(c.isdigit() for c in str(text)):
            continue
        ys = [pt[1] for pt in bbox]
        xs = [pt[0] for pt in bbox]
        boxes.append((sum(ys) / len(ys), min(xs), str(text), conf))
    if not boxes:
        return (None, None, 0.0, 0.0)

    y_centers = [b[0] for b in boxes]
    spread = max(y_centers) - min(y_centers)

    # Single visible line: assign by which half of the crop it sits in.
    if spread < img_height * 0.25:
        value, conf = _row_value(boxes)
        line_y = sum(y_centers) / len(y_centers)
        if line_y < img_height / 2.0:
            return (value, None, conf, 0.0)
        return (None, value, 0.0, conf)

    mid_row = (max(y_centers) + min(y_centers)) / 2.0
    speed_boxes = [b for b in boxes if b[0] <= mid_row]
    alt_boxes = [b for b in boxes if b[0] > mid_row]
    speed, speed_conf = _row_value(speed_boxes) if speed_boxes else (None, 0.0)
    alt, alt_conf = _row_value(alt_boxes) if alt_boxes else (None, 0.0)
    return (speed, alt, speed_conf, alt_conf)


def _process_telemetry_region(telemetry_frame) -> "tuple[int | None, int | None, float]":
    """Extract speed (MPH) and altitude (feet) from the combined ALTITUDE_SPEED crop.

    A single OCR pass reads both stacked numeric lines; the 'MPH'/'feet' labels are
    read as text and ignored by taking each row's leading digit run, so they need
    not be cropped out (a digit allowlist can't exclude them — it forces the label
    glyphs into junk digits). Boxes are split into speed/altitude rows by vertical
    position (see ADR 038). Reading both lines in one pass roughly halves the
    readtext invocations versus two separate per-value crops.

    Args:
        telemetry_frame: numpy array (BGR) — the extracted ALTITUDE_SPEED crop.

    Returns:
        tuple: (speed: int | None, altitude: int | None, ocr_time: float)
    """
    reader = _get_thread_ocr_reader()
    if reader is None:
        return (None, None, 0.0)

    t_start = time.time()

    # Preprocessing variants in corpus-tuned order (ADR 038 day/night tuning):
    # 1. HSV green-isolation mask at 3x — primary and trusted. The HUD text is
    #    the same green day and night, so masking is robust to the
    #    bright-terrain backgrounds that wash out grayscale/Otsu contrast on
    #    day maps (corpus: Otsu alone lost leading digits on 3 of 5 day
    #    frames; the HSV pass read 33 of 34 corpus rows exactly).
    # 2. Otsu binary and plain gray at 2x — fallback, consulted ONLY when an
    #    HSV row is missing entirely. Low confidence deliberately does NOT
    #    trigger fallback: live frames (motion blur, HUD flicker) sit below
    #    any usable confidence gate so often that conf-gated fallback ran the
    #    extra passes on most battle ticks — measured 1.72s mean in-loop
    #    (session run_20260728_055827), stretching the 1.5s tick to ~2.8s and
    #    doubling incoming→flare reaction latency. At runtime the ADR 030
    #    plausibility filter owns wrong-number defense (96 bogus reads
    #    rejected in that same session); a rare truncated read costs one
    #    filtered spike, not a wrong value downstream.
    #
    # Replacement rule when a fallback pass does run: a fallback row replaces
    # a present HSV row only when the fallback is confident AND strictly
    # longer in digits. Digit LOSS is this stack's characteristic error
    # (truncation: 27164 read as 2716 when the digits touch the 'feet'
    # label), while HSV row confidence does not separate correct from
    # truncated reads (corpus: a correct row at 0.40 vs a truncated one at
    # 0.45, and one correct row at 0.01) — so confidence alone must never
    # override a present HSV value with a same-length one.
    hsv = cv2.cvtColor(telemetry_frame, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, _TELEMETRY_HSV_LOWER, _TELEMETRY_HSV_UPPER)
    hsv_img = cv2.resize(mask, None, fx=3.0, fy=3.0, interpolation=cv2.INTER_CUBIC)

    results = reader.readtext(hsv_img, detail=1, paragraph=False, workers=0)
    speed, altitude, speed_conf, alt_conf = _split_telemetry_rows(results, hsv_img.shape[0])

    if speed is None or altitude is None:
        gray = cv2.cvtColor(telemetry_frame, cv2.COLOR_BGR2GRAY)
        upscaled = cv2.resize(gray, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
        _, binary = cv2.threshold(upscaled, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

        def _adopt(current, current_conf, candidate, candidate_conf):
            if candidate is None:
                return current, current_conf
            if current is None:
                return candidate, candidate_conf
            if (candidate_conf >= _TELEMETRY_ROW_CONF_MIN
                    and len(str(candidate)) > len(str(current))):
                return candidate, candidate_conf
            return current, current_conf

        for img in (binary, upscaled):
            fb = reader.readtext(img, detail=1, paragraph=False, workers=0)
            fb_speed, fb_alt, fb_speed_conf, fb_alt_conf = _split_telemetry_rows(fb, img.shape[0])
            speed, speed_conf = _adopt(speed, speed_conf, fb_speed, fb_speed_conf)
            altitude, alt_conf = _adopt(altitude, alt_conf, fb_alt, fb_alt_conf)
            if speed is not None and altitude is not None:
                break

    return (speed, altitude, time.time() - t_start)


def _apply_health_ceiling_filter(
    value: int,
    window: deque,
    ceiling: "int | None",
    window_size: int,
    spike_factor: float,
    last_accepted: "int | None",
) -> "tuple[int | None, int | None]":
    """Reject OCR health readings that are implausibly large (stray prefix digits).

    Returns (filtered_value, new_ceiling).
    filtered_value is last_accepted when the reading is rejected as a spike,
    or None when rejected and no prior accepted reading exists.
    """
    if ceiling is None:
        window.append(value)
        new_ceiling = max(window) if len(window) == window_size else None
        return (value, new_ceiling)
    if value <= ceiling * spike_factor:
        window.append(value)
        return (value, max(window))
    logger.warning(
        "Analyzer: Health OCR spike rejected: %d (ceiling=%d, factor=%.1f)",
        value, ceiling, spike_factor)
    return (last_accepted, ceiling)


def _crop_for_ocr(frame, crop_coords):
    """A detached copy of one crop, safe to hand to a queued OCR task.

    ADR 103. get_crop returns a numpy VIEW whose .base is the whole frame, so a
    queued task holding a view pins all 6.9 MB of it (1920x1200x3). The copy is
    the point of this function: a lobby crop is tens of KB, so a backlog costs
    megabytes instead of gigabytes.

    Cancelling the future is not an alternative. CPython leaves the _WorkItem —
    and its arguments — in the executor queue until a worker pops it, which is
    exactly what a stalled pool never does.
    """
    return np.ascontiguousarray(get_crop(frame, *crop_coords))


def _process_crop_region(frame, crop_coords, text_tokens):
    """Extract crop and run text detection entirely inside a worker thread.

    Wrapping get_crop() here puts it under the caller's future.result(timeout=N).

    ADR 103: the quick-scan no longer submits through this, because passing the
    whole frame means a queued task pins it. The timeout argument does not
    survive scrutiny anyway — get_crop is a bounded numpy slice and copy, not
    something that can block indefinitely. Retained for the callers that still
    crop a frame they are about to discard.
    """
    return _process_text_region(get_crop(frame, *crop_coords), text_tokens)


def _levenshtein_distance_simple(a: str, b: str) -> int:
    """Simple Levenshtein distance for worker processes."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    prev_row = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        curr_row = [i]
        for j, char_b in enumerate(b, start=1):
            insertions = prev_row[j] + 1
            deletions = curr_row[j - 1] + 1
            substitutions = prev_row[j - 1] + (char_a != char_b)
            curr_row.append(min(insertions, deletions, substitutions))
        prev_row = curr_row
    return prev_row[-1]


def _respawn_text_matches(text_clean: str) -> bool:
    """Return True if text_clean is a plausible OCR read of the respawn label.

    This is the authoritative matching logic used by _process_respawn_region.
    Keeping it as a standalone function makes it directly unit-testable without
    needing to spin up EasyOCR or a multiprocessing pool.

    The in-game label is 'RESPA'; EasyOCR at 0.7x scale typically returns the
    4-char string 'REPA' (missing the 'S').  That read has Levenshtein distance 1
    from 'RESPA' so it must be accepted; raising the min-length to 5 would
    silently break detection.  max_dist=2 is intentional: it tolerates two-char
    OCR errors while remaining tight enough to reject unrelated words.
    """
    target = "RESPAWN"
    if not text_clean or len(text_clean) < 4:
        return False
    if len(text_clean) <= 6:
        max_dist = 2
        for i in range(len(target) - 4):  # substrings: RESPA, ESPAW, SPAWN
            if _levenshtein_distance_simple(text_clean, target[i:i + 5]) <= max_dist:
                return True
    else:
        if _levenshtein_distance_simple(text_clean, target) <= 2:
            return True
    return False


# Crops that are relevant for each game state.  Only these will be overlaid
# on debug screenshots — all others are filtered out.
_STATE_CROPS: "dict[GameState, set[str]]" = {
    GameState.GAME_UNKNOWN: {
        "PLAY", "READY", "UNREADY", "click_to", "HEALTH",
    },
    GameState.GAME_BATTLE: {
        "respawn", "incoming", "click_to",
        "HEALTH", "AMMO_FLARES", "AMMO_MISSILE", "ENEMY_CLOSE_BY",
    },
    GameState.GAME_END_B: {
        "click_to", "FINAL_CONTINUE",
    },
    GameState.GAME_LOBBY: {
        "PLAY", "READY", "UNREADY", "CANCEL",
        "CREATION_FAILED", "INSPECT", "INVITED",
        "REVEAL_ALL", "TAP_HERE_TO_CONTINUE", "UNLOCK_CLOSE", "FINAL_CONTINUE", "SILVER",
    },
    GameState.GAME_WAITING: {
        "PLAY", "READY", "CANCEL",
    },
    GameState.GAME_STARTING: {
        "good_luck",
    },
    GameState.GAME_STARTING_STALLED: {
        "good_luck",
    },
    GameState.GAME_BATTLE_MANUAL: {
        "respawn", "incoming", "click_to",
        "HEALTH", "AMMO_FLARES", "AMMO_MISSILE", "ENEMY_CLOSE_BY",
    },
    GameState.GAME_BATTLE_EJECT: {
        "respawn", "HEALTH", "AMMO_MISSILE",
    },
}


# ============================================================================
# FSM Transition Table (ADR 025)
# ============================================================================

_FSM_TRANSITIONS = [
    {"trigger": "unknown_to_end_detected",    "source": "GAME_UNKNOWN",          "dest": "GAME_END_B"},
    {"trigger": "unknown_to_lobby_detected",  "source": "GAME_UNKNOWN",          "dest": "GAME_LOBBY"},
    {"trigger": "unknown_to_battle_detected", "source": "GAME_UNKNOWN",          "dest": "GAME_BATTLE"},
    {"trigger": "play_clicked",        "source": "GAME_LOBBY",            "dest": "GAME_WAITING"},
    {"trigger": "cancel_detected",    "source": "GAME_LOBBY",            "dest": "GAME_STARTING"},
    {"trigger": "cancel_detected",    "source": "GAME_WAITING",          "dest": "GAME_STARTING"},
    {"trigger": "waiting_timeout",    "source": "GAME_WAITING",          "dest": "GAME_LOBBY"},
    {"trigger": "good_luck_detected", "source": "GAME_STARTING",         "dest": "GAME_BATTLE"},
    {"trigger": "starting_timeout",   "source": "GAME_STARTING",         "dest": "GAME_STARTING_STALLED"},
    # ADR 102: the match never began — PLAY is still on screen.
    {"trigger": "starting_play_visible", "source": "GAME_STARTING",      "dest": "GAME_LOBBY"},
    {"trigger": "starting_stalled_reclassify", "source": "GAME_STARTING_STALLED", "dest": "GAME_UNKNOWN"},
    {"trigger": "starting_recovery",  "source": "GAME_STARTING_STALLED", "dest": "GAME_STARTING"},
    {"trigger": "starting_give_up",   "source": "GAME_STARTING_STALLED", "dest": "GAME_LOBBY"},
    {"trigger": "click_to_detected",  "source": ["GAME_BATTLE", "GAME_BATTLE_MANUAL", "GAME_BATTLE_EJECT"], "dest": "GAME_END_B"},
    {"trigger": "manual_takeover",    "source": ["GAME_BATTLE", "GAME_BATTLE_EJECT"], "dest": "GAME_BATTLE_MANUAL"},
    {"trigger": "respawn_reset",      "source": "GAME_BATTLE_MANUAL",     "dest": "GAME_BATTLE"},
    # SAF-001: the operator hands the aircraft back explicitly. Without
    # this, takeover survived only until the next death — measured
    # 2026-08-30 at 15 s and 85 s, both ended by respawn detection.
    {"trigger": "manual_release",     "source": "GAME_BATTLE_MANUAL",     "dest": "GAME_BATTLE"},
    {"trigger": "eject_started",      "source": "GAME_BATTLE",            "dest": "GAME_BATTLE_EJECT"},
    {"trigger": "eject_complete",     "source": "GAME_BATTLE_EJECT",      "dest": "GAME_BATTLE"},
    {"trigger": "manual_force_battle", "source": "*",                    "dest": "GAME_BATTLE"},
    {"trigger": "manual_reset",       "source": "*",                     "dest": "GAME_LOBBY"},
    {"trigger": "continue_clicked",   "source": ["GAME_END_B", "GAME_BATTLE_MANUAL"], "dest": "GAME_LOBBY"},
    {"trigger": "respawn_detected",   "source": "GAME_END_B",            "dest": "GAME_BATTLE"},
]


def _minimap_circle_mask(width: int, height: int, radius_px: float) -> np.ndarray:
    """uint8 disc mask (255 inside) centred on the crop (Design 003).

    Excludes the bounding-box corners — the live game world renders behind the
    circular minimap — and, via the configured inset radius, the rim compass
    letters.
    """
    cx, cy = (width - 1) / 2.0, (height - 1) / 2.0
    yy, xx = np.ogrid[:height, :width]
    dist_sq = (xx - cx) ** 2 + (yy - cy) ** 2
    return ((dist_sq <= radius_px ** 2) * 255).astype(np.uint8)


def _scan_minimap_components(
    crop,
    hsv_lower,
    hsv_upper,
    mask_radius_frac: float,
    min_blob_px: int,
    max_blob_px: int,
    circle_mask=None,
    hue_wraps: bool = True,
):
    """Per-component polar scan of the minimap crop (Design 003 revision 3).

    Returns a list of ``(bearing_deg, radius_frac, area_px)`` tuples, one per
    red component surviving the circle mask and the area band — the band
    rejects rim art, ring badges, and the red locked-target ring and
    route-line overlays, which are large or elongated components.
    ``bearing_deg`` is measured from the up-axis, positive clockwise, in
    (−180, 180]; on the heading-up minimap this is the bearing relative to
    the aircraft nose. Empty list when nothing survives.
    """
    height, width = crop.shape[:2]
    radius_px = mask_radius_frac * min(width, height) / 2.0
    if radius_px <= 0:
        return []
    if circle_mask is None:
        circle_mask = _minimap_circle_mask(width, height, radius_px)
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, hsv_lower, hsv_upper)
    # Red straddles the hue origin, so the enemy scan must also take the
    # 170-180 band, as detect_enemy_red does. ADR 028 revision 4 reuses this
    # scan for the friendly icons, whose hue sits mid-range: adding the
    # wrap-around band there would fold red enemies into the friendly count and
    # steer the aircraft at the thing it is meant to be avoiding.
    if hue_wraps:
        wrap_lower = np.array([170, hsv_lower[1], hsv_lower[2]], dtype=np.uint8)
        wrap_upper = np.array([180, hsv_upper[1], hsv_upper[2]], dtype=np.uint8)
        mask |= cv2.inRange(hsv, wrap_lower, wrap_upper)
    mask &= circle_mask
    n_labels, _, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
    centre_x = (width - 1) / 2.0
    centre_y = (height - 1) / 2.0
    components = []
    for label in range(1, n_labels):
        area = int(stats[label, cv2.CC_STAT_AREA])
        if area < min_blob_px or area > max_blob_px:
            continue
        blob_x, blob_y = centroids[label]
        dx = blob_x - centre_x
        dy = blob_y - centre_y
        bearing_deg = float(np.degrees(np.arctan2(dx, -dy)))
        radius_frac = min(1.0, float(np.hypot(dx, dy)) / radius_px)
        components.append((bearing_deg, radius_frac, area))
    return components


def _scan_minimap_red(
    crop,
    hsv_lower,
    hsv_upper,
    mask_radius_frac: float,
    min_blob_px: int,
    max_blob_px: int,
    circle_mask=None,
):
    """Whole-map red-icon centroid — the aggregate of _scan_minimap_components.

    Kept from Design 003 revision 2 for the frame regression tests and the
    planned ENEMY_CLOSE_BY consolidation. Returns
    ``(bearing_deg, radius_frac, blob_count, pixel_count)``, or
    ``(None, None, 0, 0)`` when nothing survives the filters.
    """
    components = _scan_minimap_components(
        crop, hsv_lower, hsv_upper, mask_radius_frac,
        min_blob_px, max_blob_px, circle_mask,
    )
    if not components:
        return None, None, 0, 0
    total_area = 0
    sum_x = 0.0
    sum_y = 0.0
    for bearing_deg, radius_frac, area in components:
        theta = np.radians(bearing_deg)
        sum_x += area * radius_frac * np.sin(theta)
        sum_y += area * radius_frac * np.cos(theta)
        total_area += area
    x = sum_x / total_area
    y = sum_y / total_area
    bearing_deg = float(np.degrees(np.arctan2(x, y)))
    radius_frac = min(1.0, float(np.hypot(x, y)))
    return bearing_deg, radius_frac, len(components), total_area


# ============================================================================
# GameStateAnalyzer Class
# ============================================================================


# --- ADR 123: nose direction ------------------------------------------------
# A continuously maintained answer to "is the nose up or down", derived from the
# altitude rate. Kept as STATE rather than recomputed on demand because the
# consumer needs it at an instant the telemetry may not have refreshed on.
NOSE_UP = "up"
NOSE_DOWN = "down"
NOSE_UNKNOWN = "unknown"


class GameStateAnalyzer:
    """Analyzes game screenshots to determine current game state."""

    def __init__(self, config, tracker=None):
        """
        Initialize analyzer with configuration.

        Args:
            config:  Dict with HSV ranges and detection thresholds
            tracker: Optional PerformanceTracker instance (ADR 031)
        """
        self._tracker = tracker
        startup_cfg = config.get("startup_state_detection", {})
        # Respawn detection config
        respawn_cfg = config.get("respawn_detection", {})

        # GPU flag — propagated to module-level so worker threads pick it up at init time
        global _use_gpu
        _use_gpu = bool(respawn_cfg.get("use_gpu", False))
        logger.info("OCR mode: %s", "GPU" if _use_gpu else "CPU")

        # OCR-based respawn detection (looks for "RESPAWN" text)
        self.use_ocr = respawn_cfg.get("use_ocr", True)

        # ADR 064 rollout mode: ocr | shadow | dual.
        # shadow — health detector scores itself, acts on nothing (Phase A′).
        # dual   — health detector fires the respawn plumbing when OCR missed (Phase B′).
        # The dead ADR 062 values (health/health_only) warn and fall back to shadow.
        mode = str(respawn_cfg.get("mode", "shadow")).lower()
        if mode not in ("ocr", "shadow", "dual"):
            log = logger.warning if mode in ("health", "health_only") else logger.error
            log("respawn_detection.mode=%r not supported (ADR 064: ocr|shadow|dual) — falling back to shadow", mode)
            mode = "shadow"
        self._respawn_detection_mode = mode

        # Named percentage-coordinate crop regions (ADR 023)
        self.crops = load_crops(config.get("crops", {}))

        # Enemy HSV range for red-color detection in ENEMY_CLOSE_BY crop
        enemy_hsv_cfg = config.get("enemy_hsv", {})
        self._enemy_hsv_lower = np.array(enemy_hsv_cfg.get("lower", [0, 120, 120]), dtype=np.uint8)
        self._enemy_hsv_upper = np.array(enemy_hsv_cfg.get("upper", [10, 255, 255]), dtype=np.uint8)

        # MINIMAP red-icon scan parameters (Design 003 / ADR 028)
        minimap_cfg = config.get("minimap", {})
        self._minimap_mask_radius_frac = float(minimap_cfg.get("mask_radius_frac", 0.93))
        # ADR 028 revision 4: friendly / objective icons, used only when no
        # enemy is on the minimap. Measured on the Design 010 frames at hue
        # 40-85; deliberately NOT the enemy bounds and never hue-wrapped.
        _friendly_cfg = minimap_cfg.get("friendly_hsv", {}) or {}
        self._friendly_hsv_lower = np.array(
            _friendly_cfg.get("lower", [40, 90, 90]), dtype=np.uint8)
        self._friendly_hsv_upper = np.array(
            _friendly_cfg.get("upper", [85, 255, 255]), dtype=np.uint8)
        # Design 010 instrumentation: map-boundary polyline and the
        # RETURN TO BATTLE banner. Nothing steers on these yet.
        _b_cfg = minimap_cfg.get("boundary_hsv", {}) or {}
        self._boundary_hsv_lower = np.array(_b_cfg.get("lower", [8, 120, 120]), dtype=np.uint8)
        self._boundary_hsv_upper = np.array(_b_cfg.get("upper", [28, 255, 255]), dtype=np.uint8)
        self._boundary_min_px = int(minimap_cfg.get("boundary_min_px", 20))
        # ADR 108: reconnection kernel and the line-vs-terrain shape gate.
        self._boundary_close_kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (5, 5))
        self._boundary_close_iters = int(
            (minimap_cfg or {}).get("boundary_close_iters", 1))
        self._boundary_max_thickness_frac = float(
            (minimap_cfg or {}).get("boundary_max_thickness_frac", 0.10))
        self._boundary_min_span_frac = float(
            minimap_cfg.get("boundary_min_span_frac", 0.5))
        _rtb = config.get("return_to_battle", {}) or {}
        self._rtb_region = tuple(_rtb.get("region", [0.36, 0.32, 0.64, 0.378]))
        self._rtb_min_frac = float(_rtb.get("min_red_frac", 0.10))
        self._rtb_ocr_region = tuple(_rtb.get("ocr_region", [0.44, 0.32, 0.56, 0.378]))
        self._rtb_tokens = [str(t).upper() for t in _rtb.get(
            "text", ["RETURNTO", "TOBATTLE", "NTOBAT", "RNTOBAT", "TOBAT"])]
        self._minimap_min_blob_px = int(minimap_cfg.get("min_blob_px", 4))
        self._minimap_max_blob_px = int(minimap_cfg.get("max_blob_px", 120))
        self._minimap_circle_cache: "tuple[int, int, np.ndarray] | None" = None
        # ADR 123: nose direction, maintained from every telemetry update.
        self._nose_direction = NOSE_UNKNOWN
        self._nose_direction_deadband_mps = float(
            (config.get("telemetry", {}) or {}).get(
                "nose_direction_deadband_mps", 5.0))

        # OCR result caching for performance (avoid running OCR every frame)
        self._ocr_cache = {
            'result': (False, 0.0, None),  # (is_respawning, confidence, method)
            'timestamp': 0.0,
            'cooldown': respawn_cfg.get("ocr_cooldown", 0.1)  # Seconds between OCR runs
        }
        self._ocr_cache_lock = threading.Lock()  # Thread-safe cache updates

        # Incoming missile cache (separate from respawn)
        self._incoming_cache = {
            'result': (False, 0.0, None),  # (is_incoming, confidence, method)
            'timestamp': 0.0,
            # ADR 096: the reaction metric bundles detection duration with the
            # wait for tick pickup. These split it. `frame_ts` is when the frame
            # this result came from was captured; `detect_done_ts` is when the
            # detector finished. Both monotonic-free (time.time) to match
            # `timestamp` and the tick handler's clock.
            'frame_ts': 0.0,
            'detect_done_ts': 0.0,
        }
        self._incoming_cache_lock = threading.Lock()
        incoming_cfg = config.get("incoming_detection", {})
        self._incoming_template_matching_enabled = bool(incoming_cfg.get("incoming_template_matching_enabled", True))
        self._incoming_template_threshold = float(incoming_cfg.get("incoming_template_threshold", 0.82))
        self._incoming_template_near_threshold_low = float(incoming_cfg.get("incoming_template_near_threshold_low", 0.76))
        self._incoming_template_near_threshold_high = float(incoming_cfg.get("incoming_template_near_threshold_high", 0.81))
        self._incoming_template_fallback_to_ocr = bool(incoming_cfg.get("incoming_template_fallback_to_ocr", True))
        fallback_tokens_cfg = incoming_cfg.get("incoming_fallback_tokens")
        self._incoming_fallback_tokens = (
            tuple(str(t).upper() for t in fallback_tokens_cfg if str(t).strip())
            if fallback_tokens_cfg else _DEFAULT_INCOMING_FALLBACK_TOKENS
        )
        self._incoming_template_telemetry_info = bool(incoming_cfg.get("incoming_template_telemetry_info", True))
        self._incoming_debounce_ms = int(incoming_cfg.get("incoming_debounce_ms", 500))
        self._incoming_debounce_window_s = max(0.0, self._incoming_debounce_ms / 1000.0)
        template_scales_cfg = incoming_cfg.get("incoming_template_scales", list(_DEFAULT_INCOMING_TEMPLATE_SCALES))
        template_scales: "list[float]" = []
        for raw_scale in (template_scales_cfg or []):
            try:
                template_scales.append(float(raw_scale))
            except (TypeError, ValueError):
                logger.warning("Invalid incoming template scale ignored: %r", raw_scale)
        if not template_scales:
            template_scales = list(_DEFAULT_INCOMING_TEMPLATE_SCALES)
        template_sources_cfg = incoming_cfg.get("incoming_template_sources", list(_DEFAULT_INCOMING_TEMPLATE_SOURCES))
        self._incoming_templates = _build_incoming_templates(
            self.crops["incoming"][:4],
            _resolve_template_source_paths(template_sources_cfg),
            template_scales,
        )
        self._incoming_last_positive_ts = 0.0
        self._incoming_near_threshold_pending = False
        self._incoming_near_threshold_pending_ts = 0.0

        if self._incoming_template_matching_enabled:
            logger.info(
                "Incoming template matching enabled: templates=%d threshold=%.2f near=[%.2f, %.2f] fallback_to_ocr=%s",
                len(self._incoming_templates),
                self._incoming_template_threshold,
                self._incoming_template_near_threshold_low,
                self._incoming_template_near_threshold_high,
                self._incoming_template_fallback_to_ocr,
            )
            if not self._incoming_templates:
                logger.warning("Incoming template matching enabled but no templates were loaded")
        # Signalled by the background OCR thread each time a new incoming result is written.
        # The main loop waits on this event during its sleep interval to react without spinning.
        self.incoming_event = threading.Event()

        # "Click to Continue" cache (lower priority than respawn/incoming)
        self._click_to_cache = {
            'result': (False, 0.0, None),  # (is_click_to, confidence, method)
            'timestamp': 0.0,
        }
        self._click_to_cache_lock = threading.Lock()
        # Click-to runs on its own low-frequency thread; track latest frame separately
        self._click_to_latest_frame = None
        self._click_to_frame_ts = 0.0
        self._click_to_frame_lock = threading.Lock()
        self._click_to_thread_started = False
        self._click_to_stop = threading.Event()
        _stall_cfg = config.get("stall_recovery", {}) or {}
        self._stall_action_after_s = float(_stall_cfg.get("action_after_s", 15.0))
        # ADR 093: ceiling past which the ADR 087 ESC suppression lifts.
        _blk_cfg = config.get("lobby_blackout", {}) or {}
        self._blackout_esc_ceiling_s = float(
            _blk_cfg.get("blackout_esc_ceiling_s", 120.0))
        self._lobby_blackout_since = 0.0   # ADR 087: sustained GAME_LOBBY blackout
        self._exit_dialog_seen_ts = 0.0    # ADR 087: Exit-to-Desktop modal on screen
        self._stall_unready_dwell_s = float(_stall_cfg.get("unready_dwell_s", 30.0))
        self._stall_scan_interval_s = float(_stall_cfg.get("scan_interval_s", 5.0))
        self._unready_since = 0.0
        self._stall_state_since = 0.0
        # ADR 094: predicate that suppresses automatic round-starting clicks.
        # Set by main() to the controller's pending finish-round exit. It must be
        # consulted HERE rather than in the LOBBY_PLAY_CLICK subscriber, because
        # this site also fires _trigger("play_clicked") — a subscriber that
        # declined to click would still leave the FSM in GAME_WAITING, stranding
        # the operator's exit until a whole further round completed.
        self._suppress_round_start = None
        # ADR 102: consecutive PLAY reads while the FSM believes the match is
        # starting. Reset on leaving GAME_STARTING so a streak cannot span two
        # separate stalls.
        self._starting_play_streak = 0
        self._lobby_quick_scan_thread_started = False
        self._lobby_quick_scan_stop = threading.Event()
        self._lobby_quick_scan_thread: "threading.Thread | None" = None
        self._shutting_down = False
        self._last_lobby_play_click_ts = 0.0  # reset on GAME_LOBBY re-entry
        self._waiting_cancel_baseline_gray: "np.ndarray | None" = None
        self._waiting_cancel_baseline_shape: "tuple[int, int] | None" = None
        self._waiting_cancel_baseline_lock = threading.Lock()

        # FSM — single authoritative state field managed by the transitions library.
        # Trigger methods (play_clicked, cancel_detected, …) are added to this instance
        # by Machine.__init__. All callers use self._trigger() for thread-safe dispatch.
        self._state_lock = threading.Lock()
        # ADR 060 Phase 1: orchestration subscribers, {GameEvent: [(name, callback)]}.
        # Registration happens at wiring time; emit() dispatches outside the lock
        # so a slow subscriber cannot block registration or another emit.
        self._subscribers: "dict[GameEvent, list[tuple[str, object]]]" = {}
        self._subscribers_lock = threading.Lock()
        self._unknown_debounce_required = max(1, int(startup_cfg.get("debounce_consecutive_required", 2)))
        self._unknown_candidate_state: "GameState | None" = None
        self._unknown_candidate_count = 0

        # ADR 080 d1: live-flight health-dropout histogram. The stale flag
        # taints the OPEN confirmed-read gap the moment telemetry goes stale
        # (death/menu), keeping those gaps out of the dropout distribution.
        # Starts True so the pre-first-read window can never count.
        self._dropout_buckets = {"lt2s": 0, "2to5s": 0, "5to10s": 0,
                                 "10to20s": 0, "gte20s": 0}
        self._dropout_gaps: "list[float]" = []
        self._gap_saw_stale_telemetry = True

        Machine(
            model=self,
            states=[s.name for s in GameState],
            transitions=_FSM_TRANSITIONS,
            initial=GameState.GAME_UNKNOWN.name,
            ignore_invalid_triggers=False,
        )

        # Health sub-state (GAME_BATTLE only)
        self._health: "int | None" = None  # Last known health value from OCR
        self._game_battle_alive = False    # True when health >= 1 in GAME_BATTLE
        self._health_lock = threading.Lock()
        self._health_no_digits_since = 0.0  # timestamp when health OCR started returning no digits
        # Shared no-digits window (ADR 062): alive flag clears and the shadow
        # detector's weak death tier marks after this many seconds without digits.
        health_cfg = config.get("health", {})
        self._death_no_digits_s = float(health_cfg.get("death_no_digits_s", 6.0))
        # ADR 063 value confirmation: a raw read only becomes the health value
        # when it recurs within the window; garbage fragments never recur.
        self._health_max_plausible = int(health_cfg.get("max_plausible", 500))
        self._health_confirm_tolerance = int(health_cfg.get("value_confirm_tolerance", 15))
        self._health_raw_window: deque = deque(
            maxlen=max(2, int(health_cfg.get("value_confirm_window", 3))))
        # ADR 064 composite evidence: weak tier keys on confirmed-reading absence,
        # halved when confirmed health declined sharply just before evidence began.
        self._death_no_confirmed_s = float(health_cfg.get("death_no_confirmed_s", 8.0))
        self._decline_evidence_drop = int(health_cfg.get("decline_evidence_drop", 80))
        self._decline_evidence_window_s = float(health_cfg.get("decline_evidence_window_s", 6.0))
        self._health_window: deque = deque(maxlen=HEALTH_WINDOW_SIZE)
        self._health_ceiling: "int | None" = None
        # ADR 061 death provenance: True only when health OCR read a value below
        # 1 CONFIRMED by the next reading (another sub-1 read or no digits).
        # A single 0 that bounces straight back to healthy is an OCR misread —
        # the 2026-08-01 11:01 session produced 5 such bounces in 20 minutes.
        # The eject's synthetic health-dead reset and the no-digits fallback
        # never set it.
        self._death_observed = False
        self._death_pending = False   # sub-1 read seen, awaiting confirmation
        # Latched copy of _death_observed at the moment of each alive transition,
        # so the main loop can ask "was this alive transition preceded by an
        # observed death" (respawn evidence during GAME_BATTLE_EJECT).
        self._alive_after_observed_death = False
        # ADR 062/064 health respawn detector state (shadow: log-only; dual: fires plumbing).
        self._shadow_mark_tier: "str | None" = None   # "strong" | "weak" | None
        self._shadow_mark_ts = 0.0
        # ADR 064 confirmed-absence clock: timestamp of the last ADR-063-CONFIRMED
        # health reading. Unlike the raw no-digits clock, this runs through both
        # true digit absence AND hallucinated overlay digits (which never confirm),
        # and is NOT touched by reset_health_for_respawn() — OCR plumbing wiping
        # shadow evidence caused 5 structural misses in the 11:01 session.
        self._last_confirmed_read_ts = 0.0
        # Recent confirmed (ts, value) pairs for the ADR 064 decline prior.
        self._confirmed_history: deque = deque(maxlen=10)
        # Instrumentation: worst mid-battle gap between confirmed reads, and how
        # many gaps exceeded the evidence window (checks the 8.0s default).
        self._max_confirmed_gap_s = 0.0
        self._confirmed_gap_over_threshold = 0
        self._shadow_fires: "list[tuple[float, str, float]]" = []   # (ts, tier, dead_for_s)
        self._shadow_ocr_respawn_edges: "list[float]" = []          # rising-edge timestamps
        self._shadow_prev_ocr_respawn = False
        # Respawn-latency instrumentation (measurement gate for any ADR 064
        # extension): at each OCR rising edge, how stale the health evidence
        # already was. since_confirmed_s overestimates true death duration by
        # up to the normal inter-confirm gap — read it against
        # max_confirmed_gap_s, not as an absolute.
        self._ocr_edge_latencies: "list[dict]" = []
        # ADR 064 dual mode: set when composite evidence fires while OCR has not
        # detected the episode; the main loop treats it as respawn-detected.
        self.health_respawn_event = threading.Event()
        # Signalled when _game_battle_alive transitions False → True.
        # The main loop waits on this event to restart the mission immediately.
        self.alive_event = threading.Event()
        # Instrumentation for the GAME_STARTING health probe (2026-08-05): how many
        # attempts, when the scan armed, and when a raw value first appeared. These
        # answer "how early is HEALTH readable after Good Luck", which no existing
        # measurement covers.
        self._starting_scan_attempts = 0
        self._starting_scan_armed_ts = 0.0
        self._starting_probe_last_ts = 0.0
        self._starting_probe_running = False
        self._starting_probe_interval_s = float(
            config.get("mission", {}).get("starting_health_probe_interval_s", 0.75))
        self._starting_scan_first_ts = 0.0
        self._starting_scan_first_raw_ts = 0.0
        # Set by _start_game_starting_loop after the 10-second gate to enable the
        # GAME_STARTING health-only OCR scan (ADR 032 battle-alive fallback).
        self._game_starting_health_scan_enabled = threading.Event()
        # Last (health, alive) pair actually logged, so unchanged readings
        # don't reprint every tick -- Health/Ammo were the majority of console
        # output with nothing new to show.
        self._last_logged_health = None

        # Ammo sub-state (GAME_BATTLE only)
        self._ammo_flares: "int | None" = None   # Last known flare count from OCR
        self._ammo_missiles: "int | None" = None  # Last known missile count from OCR
        self._ammo_lock = threading.Lock()
        self._last_logged_flares = None
        self._last_logged_missiles = None
        # Signalled when flares == 2 (reload needed) or missiles == 0 (end mission).
        self.low_flares_event = threading.Event()
        self.no_missiles_event = threading.Event()

        # Afterburner fuel sub-state (GAME_BATTLE only, ADR 075). The FUEL_100
        # crop shows the fuel percentage as bare digits (no '%' symbol);
        # readings outside 0-100 are OCR garbage and rejected.
        _fuel_cfg = config.get("fuel", {})
        self._fuel_pct: "int | None" = None
        self._fuel_ts = 0.0
        self._fuel_lock = threading.Lock()
        self._last_logged_fuel = None
        self._fuel_stale_after_s = float(_fuel_cfg.get("stale_after_s", 6.0))

        # Flight telemetry (GAME_BATTLE only) — speed (MPH) and altitude (feet),
        # read together from the combined ALTITUDE_SPEED crop (ADR 038).
        # ADR 038: plausibility filter + smoothing + rate live in the pure
        # telemetry module; the analyzer only serializes access.
        _telemetry_cfg = config.get("telemetry", {})
        self._telemetry = TelemetryProcessor(_telemetry_cfg)
        self._telemetry_lock = threading.Lock()
        # Non-blocking telemetry OCR (ADR 038 safety rule: never block the
        # main loop on altitude-speed OCR). The in-flight future is harvested
        # on a later tick when done; a new one is submitted only when none is
        # pending and the tick throttle allows.
        self._telemetry_future = None
        # Capture timestamp of the frame behind the in-flight future. Rates
        # must be derived from frame times, not harvest times — the async
        # harvest lands one to two ticks after capture, and rate computed on
        # harvest clocks produced physically impossible values (descent rate
        # exceeding total speed, session 2026-07-28).
        self._telemetry_frame_ts = 0.0
        self._telemetry_tick_counter = 0
        self._telemetry_every_n_ticks = max(1, int(_telemetry_cfg.get("ocr_every_n_ticks", 2)))

        # Static frame detection: two consecutive identical incoming_region frames → GAME_END

        # Thread pool executor for parallel OCR processing
        # Use 3 workers: one each for respawn, incoming, and click_to detection
        self._ocr_executor = None
        self._ocr_executor_initialized = False
        self._background_ocr_frame = None
        self._background_ocr_frame_ts = 0.0   # ADR 096: capture time of that frame
        self._background_ocr_pending_frame = None
        self._background_ocr_running = False
        self._background_ocr_thread = None  # Still use a thread to coordinate async results
        self._background_ocr_lock = threading.Lock()
        self._background_ocr_stop = threading.Event()
        self._last_battle_event_ts = 0.0

        # Fallback HSV detection (if OCR unavailable)
        self.respawn_text_hsv_lower = np.array(
            respawn_cfg.get("text_hsv_lower", [0, 0, 180]),
            dtype=np.uint8
        )
        self.respawn_text_hsv_upper = np.array(
            respawn_cfg.get("text_hsv_upper", [180, 50, 255]),
            dtype=np.uint8
        )

        debug_cfg = config.get("debug", {})
        self.debug = debug_cfg.get("show_window", False)
        self.show_grid_highlighted = debug_cfg.get("show_grid_highlighted", False)

        # Debug output directory for OCR preprocessing images
        debug_output_dir = debug_cfg.get("debug_output_dir", "tests/test-output")
        self.debug_output_dir = Path(debug_output_dir)
        if not self.debug_output_dir.exists():
            self.debug_output_dir.mkdir(parents=True, exist_ok=True)


    @property
    def ocr_executor(self):
        """Lazy initialization of ThreadPoolExecutor for parallel OCR."""
        if self._shutting_down:
            return None
        if self._ocr_executor is None and easyocr and not self._ocr_executor_initialized:
            if not self._background_ocr_lock.acquire(timeout=1.0):
                return None
            try:
                if self._ocr_executor is None and not self._ocr_executor_initialized:
                    try:
                        self._ocr_executor = ThreadPoolExecutor(max_workers=13)
                        self._ocr_executor_initialized = True
                        logger.info("Initialized ThreadPoolExecutor with 13 workers for parallel OCR")
                        for _ in range(13):
                            self._ocr_executor.submit(_get_thread_ocr_reader)
                        logger.debug("OCR worker pre-warm submitted (13 tasks)")
                    except Exception as e:
                        if _is_shutdown_runtime_error(e) or self._shutting_down:
                            logger.debug("ThreadPoolExecutor init skipped during shutdown: %s", e)
                        else:
                            logger.error("Failed to initialize ThreadPoolExecutor: %s", e)
                        self._ocr_executor_initialized = True
                        return None
            finally:
                if self._background_ocr_lock.locked():
                    self._background_ocr_lock.release()
        return self._ocr_executor

    def _trigger(self, trigger_name: str) -> bool:
        """Thread-safe FSM trigger dispatch. Returns False on invalid transitions.

        State mutation remains protected by _state_lock, but external side effects
        (mission cancel, starting-loop kickoff) are deferred until after the lock
        is released to avoid long critical sections.
        """
        post_callbacks = []
        with self._state_lock:
            fn = getattr(self, trigger_name, None)
            if fn is None:
                logger.error("FSM: unknown trigger '%s'", trigger_name)
                return False

            prev_state = self.game_state
            try:
                transitioned = bool(fn())
            except MachineError as e:
                logger.warning("FSM: ignored invalid trigger '%s' from state %s: %s",
                               trigger_name, self.game_state, e)
                return False

            next_state = self.game_state
            if transitioned and next_state != prev_state:
                if next_state in (GameState.GAME_LOBBY, GameState.GAME_BATTLE_MANUAL):
                    post_callbacks.append(GameEvent.CANCEL_MISSION)
                if next_state == GameState.GAME_STARTING:
                    post_callbacks.append(GameEvent.START_GAME_STARTING_LOOP)

        # Deferred until the state lock is released: side effects must not run
        # inside the critical section (unchanged ADR 039 ordering).
        for event in post_callbacks:
            self.emit(event)

        if transitioned and next_state != prev_state:
            self.emit(GameEvent.FSM_TRANSITION,
                      trigger_name, prev_state.name, next_state.name, time.time())
        return transitioned

    # ------------------------------------------------------------------
    # Orchestration public API (ADR 039, mechanism revised by ADR 060 Phase 1)
    # ------------------------------------------------------------------

    def subscribe(self, event: GameEvent, callback, *, name: str, replace: bool = False) -> None:
        """Register `callback` for `event` under a unique `name`.

        Unlike the single-slot setters this replaces, several subscribers may
        share one event — FSM_TRANSITION fans out to replay assertions, live
        capture, and mission stats simultaneously instead of the three being
        mutually exclusive.

        Raises TypeError for a non-GameEvent, and ValueError when `name` is
        already registered for this event unless `replace=True` — a silent
        overwrite is the failure mode this registry exists to prevent.
        """
        if not isinstance(event, GameEvent):
            raise TypeError(f"event must be a GameEvent, got {type(event).__name__}")
        if not callable(callback):
            raise TypeError(f"callback for {event.name} is not callable")
        with self._subscribers_lock:
            subs = self._subscribers.setdefault(event, [])
            for i, (existing, _) in enumerate(subs):
                if existing == name:
                    if not replace:
                        raise ValueError(
                            f"subscriber {name!r} already registered for {event.name}")
                    subs[i] = (name, callback)
                    return
            subs.append((name, callback))
        logger.debug("Event registry: %r subscribed to %s", name, event.name)

    def unsubscribe(self, event: GameEvent, *, name: str) -> bool:
        """Remove a subscriber. Returns True when one was removed."""
        with self._subscribers_lock:
            subs = self._subscribers.get(event, [])
            for i, (existing, _) in enumerate(subs):
                if existing == name:
                    del subs[i]
                    return True
        return False

    def has_subscribers(self, event: GameEvent) -> bool:
        with self._subscribers_lock:
            return bool(self._subscribers.get(event))

    def emit(self, event: GameEvent, *args) -> None:
        """Dispatch `args` to every subscriber of `event`.

        Each callback is isolated: one raising does not prevent the others from
        running (the try/except that was copy-pasted at every former `_on_*`
        call site now lives here only). Dispatch happens on a snapshot taken
        outside the registration lock, so callbacks may subscribe or emit.
        """
        with self._subscribers_lock:
            subs = list(self._subscribers.get(event, ()))
        for name, callback in subs:
            try:
                callback(*args)
            except Exception:
                logger.exception("Event %s: subscriber %r failed", event.name, name)

    # -- ADR 039 compatibility shims --------------------------------------
    # Preserve single-slot replace semantics under a fixed subscriber name, so
    # existing callers (and tests that re-register) behave exactly as before.

    def set_on_cancel_mission(self, callback):
        """Set callback invoked after transitions that must cancel mission logic."""
        self.subscribe(GameEvent.CANCEL_MISSION, callback, name="legacy", replace=True)

    def set_on_start_game_starting_loop(self, callback):
        """Set callback invoked when entering GAME_STARTING."""
        self.subscribe(GameEvent.START_GAME_STARTING_LOOP, callback, name="legacy", replace=True)

    def set_on_lobby_play_click(self, callback):
        """Set callback invoked with PLAY/READY crop names from lobby quick-scan."""
        self.subscribe(GameEvent.LOBBY_PLAY_CLICK, callback, name="legacy", replace=True)

    def set_on_lobby_popup_click(self, callback):
        """Set callback invoked with popup crop names from lobby quick-scan."""
        self.subscribe(GameEvent.LOBBY_POPUP_CLICK, callback, name="legacy", replace=True)

    def set_on_lobby_stall(self, callback):
        """Set callback invoked when no lobby crops detected for 10s (stall recovery)."""
        self.subscribe(GameEvent.LOBBY_STALL, callback, name="legacy", replace=True)

    def set_on_fsm_transition(self, callback):
        """Set callback invoked after successful FSM transitions.

        Callback signature: (trigger_name, prev_state_name, next_state_name, timestamp_s).
        """
        self.subscribe(GameEvent.FSM_TRANSITION, callback, name="legacy", replace=True)

    def set_on_respawn_detected(self, callback):
        """Set callback invoked when RESPAWN OCR succeeds.

        Callback signature: (frame: np.ndarray) where frame is the full BGR frame
        used for the OCR scan.  Called from the background OCR thread.
        """
        self.subscribe(GameEvent.RESPAWN_DETECTED, callback, name="legacy", replace=True)

    def trigger_event(self, name: str) -> bool:
        """Dispatch an FSM trigger via the thread-safe trigger wrapper."""
        return self._trigger(name)

    def get_ammo_missiles(self):
        """Return the latest missile count snapshot."""
        if not self._ammo_lock.acquire(timeout=1.0):
            logger.warning("get_ammo_missiles: _ammo_lock timeout — returning stale/no value")
            return None
        try:
            return self._ammo_missiles
        finally:
            if self._ammo_lock.locked():
                self._ammo_lock.release()

    def get_ammo_flares(self):
        """Return the latest flare count snapshot."""
        if not self._ammo_lock.acquire(timeout=1.0):
            logger.warning("get_ammo_flares: _ammo_lock timeout — returning stale/no value")
            return None
        try:
            return self._ammo_flares
        finally:
            if self._ammo_lock.locked():
                self._ammo_lock.release()

    def _process_fuel_reading(self, value: "int | None"):
        """Range-gate and store one afterburner-fuel OCR reading (ADR 075).

        The FUEL_100 crop shows 0-100 bare digits; anything outside that range
        is an OCR misread (digit bleed from neighbouring HUD text) and must
        not enter the cache — a garbage 8100 read as "plenty of fuel" would
        keep the burner held through a genuinely empty tank.
        """
        if value is None:
            return
        if not (0 <= value <= 100):
            logger.debug("Analyzer: fuel reading %d outside 0-100 — rejected", value)
            return
        with self._fuel_lock:
            self._fuel_pct = value
            self._fuel_ts = time.time()
        if value != self._last_logged_fuel:
            logger.debug("Afterburner fuel: %d%%", value)
            self._last_logged_fuel = value

    def get_afterburner_fuel_pct(self) -> "int | None":
        """Latest afterburner fuel percentage, or None when unknown/stale.

        Staleness matters here more than for ammo: fuel changes continuously
        while the burner is held, so a reading older than ``stale_after_s``
        says nothing about the current tank and must not gate the burner.
        """
        if not self._fuel_lock.acquire(timeout=1.0):
            logger.warning("get_afterburner_fuel_pct: _fuel_lock timeout — returning None")
            return None
        try:
            if self._fuel_pct is None:
                return None
            if time.time() - self._fuel_ts > self._fuel_stale_after_s:
                return None
            return self._fuel_pct
        finally:
            if self._fuel_lock.locked():
                self._fuel_lock.release()

    def _harvest_telemetry_future(self) -> float:
        """Collect a finished telemetry OCR pass without blocking (ADR 038).

        Returns the harvested pass's OCR seconds for the tick timing log, or
        0.0 when nothing was ready. The reading lands one to two ticks after
        its frame was captured — well inside the staleness budget — and the
        filter timestamps it at harvest time, so rate derivation stays on one
        consistent clock.
        """
        fut = self._telemetry_future
        if fut is None or not fut.done():
            return 0.0
        self._telemetry_future = None
        try:
            speed_value, altitude_value, telemetry_ocr_time = fut.result(timeout=1)
        except Exception:
            logger.exception("Analyzer: telemetry OCR pass failed")
            return 0.0
        if self._tracker:
            self._tracker.record_ocr_crop("telemetry", telemetry_ocr_time)
        # Timestamp readings with the frame's capture time so rates reflect
        # real Δaltitude/Δframe-time; signal age then honestly includes the
        # OCR latency.
        _frame_ts = self._telemetry_frame_ts or time.time()
        with self._telemetry_lock:
            rejected_before = self._telemetry.rejected_total
            self._telemetry.update(speed_value, altitude_value, _frame_ts)
            rejected_after = self._telemetry.rejected_total
            snap = self._telemetry.snapshot(_frame_ts)
        if rejected_after > rejected_before:
            logger.warning(
                "Analyzer: telemetry plausibility filter rejected reading "
                "(speed_raw=%s altitude_raw=%s, total_rejected=%d)",
                speed_value, altitude_value, rejected_after)
        if speed_value is not None or altitude_value is not None:
            angle = snap.pitch_angle_deg()
            if angle is not None:
                band = pitch_band_from_angle_deg(angle)
                nose = f"{angle:+.0f}\N{DEGREE SIGN} ({band})"
            else:
                nose = "n/a"
            logger.info("Altitude: %s | Speed: %s | Nose: %s",
                        altitude_value, speed_value, nose)
            self._update_nose_direction(snap)
        return telemetry_ocr_time

    def _update_nose_direction(self, snap) -> None:
        """Track NOSE_UP / NOSE_DOWN from the altitude rate. ADR 123.

        A DEADBAND, and the last known value is held inside it. Level flight
        has an altitude rate that jitters around zero, and a direction that
        flips every tick is not a direction — the consumer acts on it once, at
        an instant it did not choose.
        """
        try:
            rate = snap.altitude.rate if snap.altitude_fresh() else None
        except Exception:
            rate = None
        if rate is None:
            return
        if rate > self._nose_direction_deadband_mps:
            self._nose_direction = NOSE_UP
        elif rate < -self._nose_direction_deadband_mps:
            self._nose_direction = NOSE_DOWN
        # Inside the deadband the previous direction stands.

    def nose_direction(self) -> str:
        """NOSE_UP, NOSE_DOWN or NOSE_UNKNOWN. ADR 123."""
        return self._nose_direction

    def get_telemetry(self):
        """Return one atomic TelemetrySnapshot (speed + altitude + rates).

        Single accessor by design: consumers divide altitude rate by speed for
        nose-direction estimation, so the pair must come from the same cycle —
        separate getters could return a torn pair (ADR 038). Returns None on
        lock timeout.
        """
        if not self._telemetry_lock.acquire(timeout=1.0):
            logger.warning("get_telemetry: _telemetry_lock timeout — returning no snapshot")
            return None
        try:
            return self._telemetry.snapshot(time.time())
        finally:
            if self._telemetry_lock.locked():
                self._telemetry_lock.release()

    def get_respawn_cache_result(self):
        """Return the cached respawn tuple (is_respawning, confidence, method)."""
        with self._ocr_cache_lock:
            return self._ocr_cache['result']

    def get_incoming_cache_result(self):
        """Return the cached incoming tuple (is_incoming, confidence, method)."""
        with self._incoming_cache_lock:
            return self._incoming_cache['result']

    def get_incoming_cache_timestamp(self) -> float:
        """Return timestamp for the latest incoming cache update."""
        with self._incoming_cache_lock:
            return self._incoming_cache['timestamp']

    def get_incoming_latency_marks(self) -> "tuple[float, float, float]":
        """(frame_ts, pass_start_ts, detect_done_ts) for the cached result (ADR 096).

        Splits what the `reaction` metric bundles: detection duration versus the
        wait for the tick handler to pick the result up. Zeros mean the marks
        predate this instrumentation or no detection has run yet.
        """
        with self._incoming_cache_lock:
            return (self._incoming_cache.get('frame_ts', 0.0),
                    self._incoming_cache['timestamp'],
                    self._incoming_cache.get('detect_done_ts', 0.0))

    def ocr_queue_depth(self) -> "int | None":
        """Work queued but not yet started in the OCR pool (Performance 008).

        Pool saturation is the mechanism FUTURE 001 item 5 predicted and the
        2026-08-14 soak measured indirectly (0.1 s polls stretched to 2-3.5 s).
        Sampling the depth makes it a first-class observation. Returns None
        when the pool has not been created or the attribute is unavailable —
        this is a diagnostic, never a correctness dependency.
        """
        executor = self._ocr_executor
        if executor is None:
            return None
        try:
            return executor._work_queue.qsize()
        except Exception:
            return None

    def get_click_to_cache_result(self):
        """Return the cached click-to tuple (detected, confidence, method)."""
        with self._click_to_cache_lock:
            return self._click_to_cache['result']

    def get_click_to_cache_timestamp(self) -> float:
        """Return timestamp for the latest click-to cache update."""
        with self._click_to_cache_lock:
            return self._click_to_cache['timestamp']

    def capture_waiting_cancel_baseline(self, frame) -> bool:
        """Capture/refresh GAME_WAITING fallback baseline from the CANCEL crop.

        Returns True when a baseline was successfully captured.
        """
        if "CANCEL" not in self.crops:
            return False
        try:
            cancel_crop = get_crop(frame, *self.crops["CANCEL"][:4])
            gray = cv2.cvtColor(cancel_crop, cv2.COLOR_BGR2GRAY)
            with self._waiting_cancel_baseline_lock:
                self._waiting_cancel_baseline_gray = gray
                self._waiting_cancel_baseline_shape = gray.shape
            return True
        except Exception as e:
            logger.debug("Analyzer: waiting baseline capture failed: %s", e)
            return False

    def compute_waiting_cancel_diff(self, frame) -> "float | None":
        """Compute normalized mean absolute diff against CANCEL baseline.

        Returns None when baseline is missing or invalid for current crop shape.
        """
        if "CANCEL" not in self.crops:
            return None
        try:
            cancel_crop = get_crop(frame, *self.crops["CANCEL"][:4])
            gray = cv2.cvtColor(cancel_crop, cv2.COLOR_BGR2GRAY)
        except Exception as e:
            logger.debug("Analyzer: waiting diff crop failed: %s", e)
            return None

        with self._waiting_cancel_baseline_lock:
            baseline = self._waiting_cancel_baseline_gray
            baseline_shape = self._waiting_cancel_baseline_shape
            if baseline is None or baseline_shape is None:
                return None
            if gray.shape != baseline_shape:
                # Invalidate baseline on shape change; recapture in GAME_LOBBY.
                self._waiting_cancel_baseline_gray = None
                self._waiting_cancel_baseline_shape = None
                logger.debug(
                    "Analyzer: waiting baseline invalidated due to shape change (%s -> %s)",
                    baseline_shape,
                    gray.shape,
                )
                return None

        diff = cv2.absdiff(gray, baseline)
        return float(np.mean(diff) / 255.0)

    def inject_respawn_ocr_result(self, detected: bool, confidence: float, method: str = "ocr") -> None:
        """Testing helper to inject a respawn OCR cache result."""
        with self._ocr_cache_lock:
            self._ocr_cache['result'] = (bool(detected), float(confidence), method)
            self._ocr_cache['timestamp'] = time.time()

    # ------------------------------------------------------------------
    # FSM entry hooks — called automatically by transitions on state entry
    # ------------------------------------------------------------------

    def on_enter_GAME_LOBBY(self):
        self._last_lobby_play_click_ts = 0.0
        with self._health_lock:
            self._health_window.clear()
            self._health_ceiling = None
        self._health_raw_window.clear()
        with self._telemetry_lock:
            self._telemetry.reset()
        # The background OCR loop does not spin outside battle states, so its
        # non-battle-branch mark clear never runs — clear here instead. A mark
        # carried across the lobby fired on the next battle's first health read
        # (observed 2026-08-01 10:01 session: weak fires with dead_for 84-117s).
        self._shadow_clear_mark()

    def on_enter_GAME_UNKNOWN(self):
        self._unknown_candidate_state = None
        self._unknown_candidate_count = 0

    def on_enter_GAME_STARTING(self):
        pass

    def on_enter_GAME_BATTLE(self):
        with self._health_lock:
            self._health_no_digits_since = 0.0
            self._health_window.clear()
            self._health_ceiling = None
            if self._health is not None and self._health >= 1:
                self.alive_event.set()
        with self._telemetry_lock:
            self._telemetry.reset()
        # Drop any in-flight OCR from the previous battle so its stale frame
        # cannot seed the freshly reset filter when battle resumes.
        self._telemetry_future = None

    def reset_health_for_respawn(self):
        """Clear health spike filter after a respawn so full-health readings aren't rejected.

        The spike ceiling is set relative to health at time of death (e.g. ceiling=68 when
        the player died at 28% of 240 max health). Without this reset, the ceiling blocks
        post-respawn full-health readings (240 > 68×1.5=102) indefinitely.
        """
        with self._health_lock:
            self._health_window.clear()
            self._health_ceiling = None
            self._health_no_digits_since = 0.0
            self._game_battle_alive = False
            # ADR 063: pre-respawn raw reads must not help confirm post-respawn
            # values across the discontinuity.
            self._health_raw_window.clear()
            # The cached value is from the PREVIOUS life. Leaving it set let
            # on_enter_GAME_BATTLE (respawn_reset path) re-arm alive_event from
            # pre-death health whenever health OCR missed the 0 during the death
            # animation — silently degrading "restart when health returns" to
            # "restart when the respawn screen clears".
            self._health = None
        # ADR 080: a respawn gap is never a live-flight dropout.
        self._gap_saw_stale_telemetry = True
        # Respawn teleports the aircraft — a legitimate discontinuity the
        # telemetry plausibility filter would otherwise reject for several
        # ticks, so recalibrate it alongside the health filter.
        with self._telemetry_lock:
            self._telemetry.reset()
        # Drop any in-flight telemetry OCR: a pre-respawn frame harvested after
        # this reset would seed the freshly recalibrated filter with pre-death
        # values and delta-reject genuine post-respawn readings for ~9s
        # (mirrors the same guard in on_enter_GAME_BATTLE).
        self._telemetry_future = None
        logger.info("Analyzer: health spike filter reset for respawn")

    def on_enter_GAME_BATTLE_MANUAL(self):
        logger.info("FSM: entering GAME_BATTLE_MANUAL — manual takeover active, auto-restart suppressed")
        # SAF-001: stop every writer and release every key, however takeover was
        # reached. The transition alone leaves tactic holds running in their own
        # threads and keys already pressed still pressed — X holds key state,
        # not this process.
        self.emit(GameEvent.MANUAL_TAKEOVER)

    def on_enter_GAME_BATTLE_EJECT(self):
        logger.info("FSM: entering GAME_BATTLE_EJECT — eject sequence active")

    def on_enter_GAME_STARTING_STALLED(self):
        logger.warning("FSM: GAME_STARTING → GAME_STARTING_STALLED (Good Luck not detected in time)")

    @property
    def game_state(self) -> GameState:
        """Current high-level game state (read from FSM)."""
        return GameState[self.state]

    @property
    def game_battle_alive(self) -> bool:
        """True when the last health reading during GAME_BATTLE was >= 1."""
        with self._health_lock:
            return self._game_battle_alive

    @property
    def alive_after_observed_death(self) -> bool:
        """True when the most recent alive transition followed an OBSERVED death.

        Observed means health OCR explicitly read a value below 1 (ADR 061) —
        the eject sequence's synthetic health-dead reset and the no-digits
        fallback do not count. Used by the main loop to distinguish a real
        respawn from the spurious eject-start alive transition.
        """
        with self._health_lock:
            return self._alive_after_observed_death

    def _schedule_starting_health_probe(self, frame):
        """Run the armed GAME_STARTING HEALTH probe on a background thread.

        Deliberately separate from the battle OCR path (ADR 032, made reachable
        2026-08-05): it scans HEALTH only, never touches the respawn/incoming
        caches, and returns nothing — so a stale battle respawn result cannot
        leak into GAME_STARTING. Self-throttled; the caller is the per-tick
        analyze_frame.

        @relation(FR-004, scope=function)
        """
        if self._shutting_down or "HEALTH" not in self.crops:
            return
        now = time.time()
        if now - self._starting_probe_last_ts < self._starting_probe_interval_s:
            return
        if self._starting_probe_running:
            return
        executor = self.ocr_executor
        if executor is None:
            return
        self._starting_probe_last_ts = now
        self._starting_probe_running = True

        def _probe():
            try:
                health_frame = get_crop(frame, *self.crops["HEALTH"][:4])
                raw, _ = _process_health_region(health_frame)
                self._starting_scan_attempts += 1
                since_arm = now - (self._starting_scan_armed_ts or now)
                if raw is None:
                    logger.info(
                        "GAME_STARTING health probe #%d (+%.1fs since armed): no digits",
                        self._starting_scan_attempts, since_arm)
                    return
                if self._starting_scan_first_raw_ts == 0.0:
                    self._starting_scan_first_raw_ts = time.time()
                logger.info(
                    "GAME_STARTING health probe #%d (+%.1fs since armed): raw=%s",
                    self._starting_scan_attempts, since_arm, raw)
                confirmed = self._confirm_health_value(raw)
                if confirmed is None:
                    logger.info(
                        "GAME_STARTING health probe #%d: raw=%s UNCONFIRMED "
                        "(ADR 063 needs a second agreeing read)",
                        self._starting_scan_attempts, raw)
                    return
                if confirmed < 1:
                    return
                with self._health_lock:
                    prev_alive = self._game_battle_alive
                    self._health = confirmed
                    self._game_battle_alive = True
                    if not prev_alive:
                        self._alive_after_observed_death = self._death_observed
                        self._death_observed = False
                logger.info(
                    "\033[92mAnalyzer: health %d confirmed in GAME_STARTING "
                    "(+%.1fs since armed) → game_battle_alive=True\033[0m",
                    confirmed, since_arm)
                if not prev_alive:
                    self.alive_event.set()
            except Exception:
                logger.exception("Analyzer: GAME_STARTING health probe failed")
            finally:
                self._starting_probe_running = False

        # Run on the OCR pool, NOT a fresh thread. EasyOCR readers are
        # thread-local (~300 MB of model weights each), so a per-probe thread
        # built and discarded one on every probe: 1,138 probes produced 1,213
        # reader initialisations in the 2026-08-22 02:18 session against a
        # single 13-worker pool. That is ~350 GB of allocate/free churn per
        # session and a prime suspect for the Performance 008 heap growth.
        # The executor was already fetched above as a guard and then unused.
        try:
            executor.submit(_probe)
        except RuntimeError as e:
            # Pool shutting down: drop the probe rather than resurrect a thread.
            self._starting_probe_running = False
            logger.debug("Analyzer: health probe not submitted (%s)", e)

    def arm_starting_health_scan(self):
        """Enable the GAME_STARTING health-only probe and reset its instrumentation.

        Public entry point so the controller does not poke the private Event
        (the coupling CR-013 flagged elsewhere).
        """
        self._starting_scan_attempts = 0
        self._starting_scan_armed_ts = time.time()
        self._starting_probe_last_ts = 0.0
        self._starting_scan_first_ts = 0.0
        self._starting_scan_first_raw_ts = 0.0
        self._game_starting_health_scan_enabled.set()

    def disarm_starting_health_scan(self):
        """Disable the probe and report what it saw (2026-08-05 instrumentation).

        The summary line is the measurement that matters: how long after arming
        the HEALTH crop first produced a raw value. Until that number exists there
        is no basis for shortening the post-Good-Luck wait.

        @relation(FR-004.1, scope=function)
        """
        was_armed = self._game_starting_health_scan_enabled.is_set()
        self._game_starting_health_scan_enabled.clear()
        if not was_armed or self._starting_scan_armed_ts == 0.0:
            return
        armed_for = time.time() - self._starting_scan_armed_ts
        if self._starting_scan_first_raw_ts > 0.0:
            first_raw = self._starting_scan_first_raw_ts - self._starting_scan_armed_ts
            logger.info(
                "GAME_STARTING health probe summary: %d attempts over %.1fs — "
                "first raw read at +%.1fs",
                self._starting_scan_attempts, armed_for, first_raw)
        else:
            logger.info(
                "GAME_STARTING health probe summary: %d attempts over %.1fs — "
                "NO raw read at any point",
                self._starting_scan_attempts, armed_for)

    def mark_health_dead_synthetic(self):
        """Force health state to dead WITHOUT marking an observed death (ADR 061).

        Called by eject_and_dive to arm the dead→alive transition that triggers
        the post-respawn mission restart. Synthetic by definition: it must never
        count as respawn evidence, so _death_observed is cleared alongside.
        """
        if not self._health_lock.acquire(timeout=1.0):
            logger.warning("mark_health_dead_synthetic: _health_lock timeout — skipping health reset")
            return
        try:
            self._game_battle_alive = False
            self._health_no_digits_since = 0.0
            self._death_observed = False
            self._death_pending = False
        finally:
            if self._health_lock.locked():
                self._health_lock.release()

    def _confirm_health_value(self, raw: int) -> "int | None":
        """ADR 063 recurrence confirmation: return raw when it recurs, else None.

        A read is confirmed when at least 2 of the last value_confirm_window raw
        reads (including this one) agree within value_confirm_tolerance. Garbage
        fragments and concatenations vary read-to-read and never confirm; the
        true value recurs constantly. Reads above max_plausible are discarded
        before entering the window so they cannot self-confirm.

        @relation(SAF-004, scope=function)
        """
        if raw > self._health_max_plausible:
            logger.debug("Analyzer: health read %d over max_plausible %d — discarded",
                         raw, self._health_max_plausible)
            return None
        self._health_raw_window.append(raw)
        agreeing = sum(
            1 for m in self._health_raw_window
            if abs(m - raw) <= self._health_confirm_tolerance
        )
        if agreeing >= 2:
            return raw
        logger.debug("Analyzer: health read %d unconfirmed (window=%s) — holding previous value",
                     raw, list(self._health_raw_window))
        return None

    def _process_health_reading(self, health_value):
        """Process one health OCR result: spike filter, alive flag, death provenance.

        Extracted from the background OCR loop (ADR 061/062) so the two-tier
        death marking and the alive-transition latch are unit-testable. Called
        from the background OCR thread; health_value None means no digits read.
        Raw values pass the ADR 063 recurrence filter first — an unconfirmed
        read counts as digits-present (clocks reset) but changes nothing else.
        """
        if health_value is not None:
            health_value = self._confirm_health_value(health_value)
            if health_value is None:
                # Digits were present but unconfirmed: reset the raw no-digits
                # clock (the alive flag claims ABSENCE of digits, and these were
                # digits) and hold every other piece of state as-is. The ADR 064
                # confirmed-absence clock deliberately keeps running — garbage
                # digits hallucinated on the respawn overlay never confirm, so
                # unconfirmed presence is still evidence-grade absence.
                with self._health_lock:
                    self._health_no_digits_since = 0.0
                self._evaluate_confirmed_absence()
                return
        if health_value is not None:
            # Evaluate the confirmed-absence gap BEFORE moving the anchor: when
            # this confirmed read is the first in a death-length while (overlay
            # just cleared), the weak mark must form now so the alive transition
            # below can fire on it.
            self._evaluate_confirmed_absence()
            self._record_confirmed_read(health_value)
            confirmed_death = False
            with self._health_lock:
                health_value, self._health_ceiling = _apply_health_ceiling_filter(
                    health_value,
                    self._health_window,
                    self._health_ceiling,
                    HEALTH_WINDOW_SIZE,
                    HEALTH_SPIKE_FACTOR,
                    self._health,
                )
                prev_alive = self._game_battle_alive
                alive = health_value >= 1 if health_value is not None else False
                self._health = health_value
                self._game_battle_alive = alive
                self._health_no_digits_since = 0.0
                if health_value is not None and health_value < 1:
                    # Sub-1 read: confirm on the second consecutive dead-ish
                    # reading (this one, or a no-digits follow-up below).
                    if self._death_pending:
                        self._death_pending = False
                        self._death_observed = True
                        confirmed_death = True
                    else:
                        self._death_pending = True
                elif alive and self._death_pending:
                    # Single 0 bounced straight back to healthy — OCR misread.
                    self._death_pending = False
                    logger.debug("Analyzer: single sub-1 health read bounced — death not confirmed")
            if confirmed_death:
                self._shadow_mark_death("strong")
            if (health_value, alive) != self._last_logged_health:
                logger.info("Health: %s | alive=%s", health_value, alive)
                self._last_logged_health = (health_value, alive)
            # Signal False → True transition for immediate mission restart.
            if alive and not prev_alive:
                logger.info("Analyzer: health alive transition False→True — resetting health ceiling")
                with self._health_lock:
                    self._health_window.clear()
                    self._health_ceiling = None
                    # Latch death provenance for this transition (ADR 061).
                    self._alive_after_observed_death = self._death_observed
                    self._death_observed = False
                self.alive_event.set()
            if alive:
                self._shadow_maybe_fire(transitioned=(not prev_alive))
        else:
            # No digits — only clear alive flag after the shared no-digits window.
            now_t = time.time()
            with self._health_lock:
                no_digits_since = self._health_no_digits_since
                if no_digits_since == 0.0:
                    self._health_no_digits_since = now_t
                    no_digits_since = now_t
                if self._death_pending:
                    # Sub-1 read followed by digits vanishing (death animation /
                    # respawn overlay) — that is a confirmed observed death.
                    self._death_pending = False
                    self._death_observed = True
                    confirmed_death = True
                else:
                    confirmed_death = False
            if confirmed_death:
                self._shadow_mark_death("strong")
            self._evaluate_confirmed_absence()
            if no_digits_since == now_t:
                logger.debug("Analyzer: Health OCR returned no digits (grace timer started)")
            elif now_t - no_digits_since >= self._death_no_digits_s:
                with self._health_lock:
                    self._game_battle_alive = False
                logger.debug(
                    "Analyzer: Health OCR no digits for %.1fs → game_battle_alive=False",
                    now_t - no_digits_since)
            else:
                logger.debug(
                    "Analyzer: Health OCR no digits (%.1fs elapsed, %.1fs threshold)",
                    now_t - no_digits_since, self._death_no_digits_s)

    # ------------------------------------------------------------------
    # ADR 062/064 — health respawn detector (shadow scores, dual acts)
    # ------------------------------------------------------------------

    def _record_confirmed_read(self, value: int) -> None:
        """Update the ADR 064 confirmed-read anchor, decline history, and gap stats."""
        now_t = time.time()
        if self._last_confirmed_read_ts > 0.0:
            gap = now_t - self._last_confirmed_read_ts
            if gap > self._max_confirmed_gap_s:
                self._max_confirmed_gap_s = gap
            if gap >= self._death_no_confirmed_s:
                self._confirmed_gap_over_threshold += 1
            # ADR 080 d1: live-flight dropout histogram. A gap that ever saw
            # stale telemetry is a death/menu gap (respawn screens render no
            # HUD) and stays out; what remains is health OCR failing while
            # the aircraft demonstrably flies.
            if (not self._gap_saw_stale_telemetry
                    and self.game_state == GameState.GAME_BATTLE):
                b = self._dropout_buckets
                if gap < 2.0:
                    b["lt2s"] += 1
                elif gap < 5.0:
                    b["2to5s"] += 1
                elif gap < 10.0:
                    b["5to10s"] += 1
                elif gap < 20.0:
                    b["10to20s"] += 1
                else:
                    b["gte20s"] += 1
                if len(self._dropout_gaps) < 20000:
                    self._dropout_gaps.append(round(gap, 2))
        # A confirmed read opens a new gap window, telemetry-clean until the
        # per-cycle sampling in _evaluate_confirmed_absence says otherwise.
        self._gap_saw_stale_telemetry = False
        self._last_confirmed_read_ts = now_t
        self._confirmed_history.append((now_t, value))

    def health_confirmed_gap_s(self) -> "float | None":
        """Seconds since the last confirmed health read (None before the
        first). ADR 080 d2: polled by the dropout frame recorder."""
        anchor = self._last_confirmed_read_ts
        if anchor <= 0.0:
            return None
        return time.time() - anchor

    def telemetry_hud_live(self) -> bool:
        """Public liveness accessor (ADR 079/080)."""
        return self._telemetry_hud_live()

    def health_dropout_summary(self) -> dict:
        """ADR 080 d1: session histogram of live-flight confirmed-read gaps."""
        gaps = sorted(self._dropout_gaps)
        p95 = gaps[max(0, int(len(gaps) * 0.95) - 1)] if gaps else None
        return {
            "buckets": dict(self._dropout_buckets),
            "count": len(gaps),
            "over_5s": sum(1 for g in gaps if g >= 5.0),
            "p95_s": p95,
            "max_s": gaps[-1] if gaps else None,
        }

    def _decline_before(self, evidence_start: float) -> bool:
        """True when confirmed health fell by >= decline_evidence_drop in the window before evidence began.

        Sub-1 values are excluded: a confirmed 0 is a death claim (the strong
        tier's business), not a damage-trend datapoint — including it let a
        garbage-zero dip at eject onset fake a decline and halve the window
        (2026-08-02 07:58 session false fires).
        """
        window_start = evidence_start - self._decline_evidence_window_s
        vals = [v for ts, v in self._confirmed_history
                if window_start <= ts <= evidence_start and v >= 1]
        if len(vals) < 2:
            return False
        return (max(vals) - vals[-1]) >= self._decline_evidence_drop

    def _evaluate_confirmed_absence(self) -> None:
        """ADR 064 weak tier: mark death when no CONFIRMED reading for the evidence window.

        Runs through both true digit absence and hallucinated overlay digits
        (which never confirm). The window halves when confirmed health was in
        rapid decline just before the last confirmed read — a death prior.
        """
        # ADR 080: per-cycle telemetry sampling for the open confirmed-read
        # gap — runs on every health cycle (digits or not), before any mode
        # gating. The `not` guard keeps it to one lock acquire per gap.
        if not self._gap_saw_stale_telemetry and not self._telemetry_hud_live():
            self._gap_saw_stale_telemetry = True
        if self._respawn_detection_mode not in ("shadow", "dual"):
            return
        anchor = self._last_confirmed_read_ts
        if anchor <= 0.0:
            return
        required = self._death_no_confirmed_s
        if self._decline_before(anchor):
            required /= 2.0
        if time.time() - anchor >= required:
            # ADR 079: a live HUD disproves the dead premise. Health OCR
            # drops out for 7-25s mid-flight (four false fires 2026-08-17)
            # while telemetry keeps reading — a dead aircraft renders no
            # HUD, so fresh telemetry at mark time means OCR dropout, not
            # death. Real deaths mark mid-respawn-screen where telemetry
            # has been stale for seconds (mark time separates the cases;
            # at fire time the new life's HUD is fresh for real respawns
            # too — the ADR 078 measurement).
            if self._telemetry_hud_live():
                logger.debug(
                    "Health respawn detector: weak mark suppressed — "
                    "telemetry live during the confirmed-read gap (ADR 079)")
                return
            self._shadow_mark_death("weak")

    def _telemetry_hud_live(self) -> bool:
        """True when a fresh telemetry sample exists — the HUD is rendering,
        so the aircraft exists (ADR 079).

        @relation(SAF-003, scope=function)
        """
        try:
            snap = self.get_telemetry()
        except Exception:
            return False
        return snap is not None and snap.altitude_fresh()

    def _shadow_mark_death(self, tier: str):
        """Record a death mark for the health detector. Strong upgrades weak; weak never downgrades."""
        if self._respawn_detection_mode not in ("shadow", "dual"):
            return
        if self._shadow_mark_tier is None or (tier == "strong" and self._shadow_mark_tier == "weak"):
            if self._shadow_mark_tier is None:
                self._shadow_mark_ts = time.time()
            self._shadow_mark_tier = tier
            logger.debug("Health respawn detector: death mark set (tier=%s)", tier)

    def _shadow_maybe_fire(self, transitioned: bool = True):
        """Fire the health respawn decision when a confirmed alive read follows a death mark.

        shadow mode: recorded and logged only. dual mode (ADR 064): additionally
        sets health_respawn_event so the main loop runs the respawn plumbing —
        unless respawn OCR is currently detecting the overlay (it owns the episode).

        @relation(SAF-003, scope=function)

        transitioned: whether this read is the dead→alive transition. Weak-tier
        evidence REQUIRES it (ADR 064 amendment, 2026-08-02 05:37 session): a
        confirmed-read gap alone also occurs mid-combat in garbage regimes
        (measured up to 11s, overlapping real-respawn gaps of 8-11.4s), and all
        9 of that session's false fires were transition-less weak fires while
        all 11 matches coincided with a real transition. Strong-tier evidence
        is intrinsic and fires regardless.
        """
        if self._respawn_detection_mode not in ("shadow", "dual") or self._shadow_mark_tier is None:
            return
        now_t = time.time()
        dead_for = now_t - self._shadow_mark_ts
        tier = self._shadow_mark_tier
        self._shadow_mark_tier = None
        if tier == "weak" and not transitioned:
            logger.debug(
                "Health respawn detector: weak evidence without alive transition — "
                "discarded (mid-combat confirmation gap, not a respawn)")
            return
        if tier == "weak" and self.game_state != GameState.GAME_BATTLE:
            # ADR 064 amendment 2 (2026-08-02, sessions 07:58/09:25): weak fires
            # are valid only in plain GAME_BATTLE. Eject onset deliberately
            # thrashes health state (all verified false fires triggered 1-2s
            # into GAME_BATTLE_EJECT; ADR 061's observed-death path owns eject
            # termination), and in GAME_BATTLE_MANUAL the operator owns the
            # aircraft. Real respawns fire in GAME_BATTLE: the OCR/eject paths
            # exit those states before health returns.
            logger.debug(
                "Health respawn detector: weak evidence discarded in %s (fires only in GAME_BATTLE)",
                self.game_state.name)
            return
        if dead_for > 30.0:
            # A real death→respawn cycle inside battle completes well under 30s
            # (overlay ~8s). A mark this old survived a path that skipped the
            # battle-exit clear — discard it rather than fire a phantom respawn.
            logger.debug(
                "Health respawn detector: stale death mark discarded (tier=%s, %.1fs old)",
                tier, dead_for,
            )
            return
        self._shadow_fires.append((now_t, tier, dead_for))
        if self._respawn_detection_mode == "dual":
            with self._ocr_cache_lock:
                ocr_currently_detecting = bool(self._ocr_cache['result'][0])
            last_edge = self._shadow_ocr_respawn_edges[-1] if self._shadow_ocr_respawn_edges else 0.0
            if ocr_currently_detecting or (now_t - last_edge) < 20.0:
                # OCR already owns this episode — either the overlay is on
                # screen now, or its edge fired within the episode window.
                # Without the edge check, a slow post-overlay health confirm
                # (>10s, outside the main loop's dedup cooldown) double-fired
                # the respawn plumbing (observed in the ADR 044 replay lane).
                logger.info(
                    "Health respawn detector: evidence fired (tier=%s, dead_for=%.1fs) — OCR owns the episode, standing down",
                    tier, dead_for,
                )
            else:
                # Performance 008: carry the evidence that distinguishes a real
                # respawn from an OCR-starvation artifact. A death->respawn
                # cycle cannot complete in under ~8s (the overlay alone runs
                # that long), so a short dead_for beside a long preceding OCR
                # pass is a confirmation gap, not a death — 17 of 31 weak fires
                # in the 2026-08-20 session looked like this. Logged rather
                # than acted on until the ADR 064 amendment lands.
                logger.info(
                    "\033[93m💛 HEALTH RESPAWN FALLBACK firing (tier=%s, dead_for=%.1fs) — OCR missed this respawn (ADR 064 dual)"
                    " [context: health_window=%s last_respawn_ocr=%.2fs]\033[0m",
                    tier, dead_for, list(self._health_window),
                    getattr(self, "_last_respawn_ocr_s", float("nan")),
                )
                self.health_respawn_event.set()
        else:
            logger.info(
                "SHADOW respawn detector: would fire (tier=%s, dead_for=%.1fs) — log-only, ADR 064 Phase A'",
                tier, dead_for,
            )

    def _shadow_clear_mark(self):
        """Drop any pending death mark and evidence state (called when leaving battle states)."""
        if self._shadow_mark_tier is not None:
            logger.debug("Health respawn detector: death mark cleared (left battle state)")
            self._shadow_mark_tier = None
        self._last_confirmed_read_ts = 0.0
        self._confirmed_history.clear()
        self.health_respawn_event.clear()
        with self._health_lock:
            self._death_pending = False

    def _shadow_record_ocr_respawn(self, respawn_detected: bool):
        """Record rising edges of OCR respawn detection for agreement matching.

        Each edge also snapshots how long health evidence had already been
        absent — the headroom a corroborated-absence detector could reclaim
        by firing before the RESPAWN text renders.
        """
        if self._respawn_detection_mode in ("shadow", "dual"):
            if respawn_detected and not self._shadow_prev_ocr_respawn:
                now_t = time.time()
                self._shadow_ocr_respawn_edges.append(now_t)
                since_confirmed = (round(now_t - self._last_confirmed_read_ts, 2)
                                   if self._last_confirmed_read_ts else None)
                no_digits_for = (round(now_t - self._health_no_digits_since, 2)
                                 if self._health_no_digits_since else None)
                self._ocr_edge_latencies.append(
                    {"since_confirmed_s": since_confirmed,
                     "no_digits_for_s": no_digits_for})
                logger.info(
                    "RespawnLatency: OCR edge — since_confirmed=%ss no_digits_for=%ss",
                    since_confirmed, no_digits_for)
        self._shadow_prev_ocr_respawn = respawn_detected

    def shadow_respawn_summary(self) -> "dict | None":
        """Return the agreement summary, or None when the health detector is off.

        A fire matched to an OCR rising edge within 15 s counts as agreement;
        unmatched fires are false fires; unmatched OCR edges are misses. Also
        reports the ADR 064 confirmed-gap instrumentation used to sanity-check
        the death_no_confirmed_s default against real data.
        """
        if self._respawn_detection_mode not in ("shadow", "dual"):
            return None
        edges = list(self._shadow_ocr_respawn_edges)
        fires = list(self._shadow_fires)
        deltas = []
        unmatched_fires = 0
        remaining_edges = list(edges)
        for fire_ts, _tier, _dead_for in fires:
            best = None
            for e in remaining_edges:
                d = abs(fire_ts - e)
                if d <= 15.0 and (best is None or d < abs(fire_ts - best)):
                    best = e
            if best is None:
                unmatched_fires += 1
            else:
                deltas.append(round(fire_ts - best, 2))
                remaining_edges.remove(best)
        edge_lat = [e["since_confirmed_s"] for e in self._ocr_edge_latencies
                    if e["since_confirmed_s"] is not None]
        return {
            "mode": self._respawn_detection_mode,
            "shadow_fires": len(fires),
            "ocr_respawns": len(edges),
            "matched": len(deltas),
            "matched_within_5s": sum(1 for d in deltas if abs(d) <= 5.0),
            "false_fires": unmatched_fires,
            "missed_ocr_respawns": len(remaining_edges),
            "fire_deltas_s": deltas,
            "max_confirmed_gap_s": round(self._max_confirmed_gap_s, 2),
            "confirmed_gaps_over_threshold": self._confirmed_gap_over_threshold,
            "ocr_edge_latencies": list(self._ocr_edge_latencies),
            "edge_since_confirmed_mean_s": (
                round(sum(edge_lat) / len(edge_lat), 2) if edge_lat else None),
            "edge_since_confirmed_max_s": (
                round(max(edge_lat), 2) if edge_lat else None),
        }

    def crops_for_state(self, state: "GameState | None" = None) -> "dict[str, CropCoords]":
        """Return only the crops relevant to the given game state.

        If state is None, the current game state is used.  Any crop name not
        present in the config is silently skipped.
        """
        s = state if state is not None else self.game_state
        names = _STATE_CROPS.get(s, set())
        return {k: v for k, v in self.crops.items() if k in names}

    def cleanup(self):
        """Clean up resources (call when shutting down)."""
        self._shutting_down = True
        self._click_to_stop.set()
        self._lobby_quick_scan_stop.set()
        self._background_ocr_stop.set()
        if self._ocr_executor is not None:
            try:
                # Join workers during cleanup so background OCR threads do not linger
                # across test cases or process shutdown boundaries.
                self._ocr_executor.shutdown(wait=True, cancel_futures=True)
                logger.info("ThreadPoolExecutor shut down successfully")
                if self._tracker is not None:
                    try:
                        self._tracker.on_session_end()
                    except Exception as e:
                        logger.warning("PerformanceTracker: on_session_end failed: %s", e)
            except Exception as e:
                logger.warning("Error shutting down ThreadPoolExecutor: %s", e)
            self._ocr_executor = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.cleanup()
        return False

    def analyze_frame(self, frame):
        """Analyze a single frame and return game state.

        Args:
            frame: numpy array (BGR image from screen capture).

        Returns:
            dict: Game state with keys:
                - is_respawning: bool
                - respawn_confidence: float (0.0-1.0)
                - respawn_method: str or None
        """
        if frame is None or frame.size == 0:
            logger.warning("Analyzer: received invalid frame")
            return self._empty_state()

        # Keep latest full frame available for the click_to background thread
        with self._click_to_frame_lock:
            self._click_to_latest_frame = frame
            self._click_to_frame_ts = time.time()

        # Must run BEFORE the GAME_UNKNOWN branch below, which returns early:
        # ADR 074 makes GAME_UNKNOWN a popup-dismiss state, so the scanner has
        # to be alive *while* unclassified, not only after classification.
        self._ensure_lobby_quick_scan_thread()

        state = {
            'is_respawning': False,
            'respawn_confidence': 0.0,
            'respawn_method': None,
            'is_incoming': False,
            'incoming_confidence': 0.0,
            'incoming_method': None,
            'is_click_to': False,
            'click_to_method': None,
            'health': None,
            'game_battle_alive': False,
            'game_state': self.game_state,
        }

        if self.game_state == GameState.GAME_UNKNOWN:
            candidate = self._classify_unknown_state(frame)
            if candidate is None:
                self._unknown_candidate_state = None
                self._unknown_candidate_count = 0
            elif candidate == self._unknown_candidate_state:
                self._unknown_candidate_count += 1
            else:
                self._unknown_candidate_state = candidate
                self._unknown_candidate_count = 1

            if (
                self._unknown_candidate_state is not None
                and self._unknown_candidate_count >= self._unknown_debounce_required
            ):
                trigger_name = {
                    GameState.GAME_END_B: "unknown_to_end_detected",
                    GameState.GAME_LOBBY: "unknown_to_lobby_detected",
                    GameState.GAME_BATTLE: "unknown_to_battle_detected",
                }[self._unknown_candidate_state]
                if self._trigger(trigger_name):
                    logger.info(
                        "FSM: GAME_UNKNOWN classified as %s via %s",
                        self.game_state.name,
                        trigger_name,
                    )
                self._unknown_candidate_state = None
                self._unknown_candidate_count = 0

            state['game_state'] = self.game_state
            return state

        # Click-to detection is only meaningful once the state is classified.
        if not self._click_to_thread_started:
            self._click_to_thread_started = True
            threading.Thread(target=self._run_click_to_in_background, daemon=True).start()
            logger.debug("Click-to background thread started")

        respawn_detected, confidence, method = self._detect_respawn(frame)

        state['is_respawning'] = respawn_detected
        state['respawn_confidence'] = confidence
        state['respawn_method'] = method

        # Detect incoming missiles - use cached result from background OCR
        with self._incoming_cache_lock:
            incoming_detected, incoming_conf, incoming_method = self._incoming_cache['result']

        state['is_incoming'] = incoming_detected
        state['incoming_confidence'] = incoming_conf
        state['incoming_method'] = incoming_method

        # Detect "Click to Continue" - use cached result from background OCR (lower priority)
        with self._click_to_cache_lock:
            click_to_detected, _, click_to_method = self._click_to_cache['result']

        state['is_click_to'] = click_to_detected
        state['click_to_method'] = click_to_method

        with self._health_lock:
            state['health'] = self._health
            state['game_battle_alive'] = self._game_battle_alive

        # Save crop overlay if enabled (every frame) — only state-relevant crops
        if self.show_grid_highlighted:
            try:
                output_dir = Path("tests") / "test-output"
                output_dir.mkdir(parents=True, exist_ok=True)
                output_path = str(output_dir / "output_crops.png")
                annotated = draw_crops(frame, self.crops_for_state())
                import cv2 as _cv2
                _cv2.imwrite(output_path, annotated)
                logger.debug("Saved crop overlay to %s", output_path)
            except Exception as e:
                logger.warning("Failed to save highlighted grid: %s", e)



        return state

    def _detect_respawn(self, frame):
        """
        Detect if respawn screen is visible using OCR.
        Looks for "RESPAWN" text in the frame.

        Returns:
            tuple: (is_respawning: bool, confidence: float, method: str)
        """
        if self.use_ocr and easyocr:
            return self._detect_respawn_ocr(frame)
        if not easyocr:
            logger.warning("EasyOCR not available, respawn detection disabled")
        return False, 0.0, None

    def _detect_respawn_ocr(self, frame):
        """
        Use EasyOCR to detect "RESPAWN" text in the frame.
        Non-blocking: uses caching + background thread to avoid blocking main loop.

        Returns:
            tuple: (is_respawning: bool, confidence: float, method: str)
        """
        # Skip OCR entirely outside active battle — no respawn/incoming events are
        # relevant in these states, and transitions are driven externally.
        #
        # ONE exception (2026-08-05): GAME_STARTING while the battle-alive health
        # probe is armed. Scheduling background OCR is the only path that reaches
        # _run_ocr_in_background, so this early return made ADR 032's probe
        # unreachable — it logged "0 attempts" over an 18.8s armed window. The
        # probe still scans HEALTH only; the respawn/incoming work stays skipped
        # because _run_ocr_in_background branches on state, and this call returns
        # the same negative respawn result either way.
        if self.game_state in (GameState.GAME_END_B, GameState.GAME_LOBBY,
                               GameState.GAME_WAITING):
            return (False, 0.0, None)
        if self.game_state == GameState.GAME_STARTING:
            if not self._game_starting_health_scan_enabled.is_set():
                return (False, 0.0, None)
            self._schedule_starting_health_probe(frame)
            return (False, 0.0, None)

        # Check if we can use cached result (throttle OCR)
        current_time = time.time()
        with self._ocr_cache_lock:
            time_since_last_ocr = current_time - self._ocr_cache['timestamp']
            cached_result = self._ocr_cache['result']

            # Cache still valid - return immediately (non-blocking)
            if time_since_last_ocr < self._ocr_cache['cooldown']:
                if self.debug:
                    logger.debug("Using cached OCR result (%.2fs old)", time_since_last_ocr)
                return cached_result

        # Cache expired - schedule background OCR (non-blocking).
        # If OCR is already running, update pending frame; otherwise, start thread.
        if not self._background_ocr_lock.acquire(timeout=5.0):
            logger.warning("Analyzer: background OCR lock timeout - skipping frame")
            return cached_result
        try:
            if self._background_ocr_running:
                self._background_ocr_pending_frame = frame
                logger.debug("Background OCR busy; will process latest pending frame next")
            else:
                self._background_ocr_frame = frame
                self._background_ocr_frame_ts = time.time()   # ADR 096
                self._background_ocr_pending_frame = None
                self._background_ocr_running = True
                self._background_ocr_thread = threading.Thread(
                    target=self._run_ocr_in_background,
                    daemon=True
                )
                self._background_ocr_thread.start()
                logger.debug("Background OCR scheduled")
        finally:
            if self._background_ocr_lock.locked():
                self._background_ocr_lock.release()

        # Return cached result (may be stale) while background OCR runs
        return cached_result

    def _run_ocr_in_background(self):
        """Run OCR in background using thread pool for parallel region processing."""
        while not self._background_ocr_stop.is_set():
            with self._background_ocr_lock:
                full_frame = self._background_ocr_frame

            if full_frame is None:
                with self._background_ocr_lock:
                    self._background_ocr_running = False
                return

            try:
                t0 = time.time()
                current_time = t0

                executor = self.ocr_executor
                if executor is None:
                    logger.warning("OCR executor not initialized")
                    with self._background_ocr_lock:
                        self._background_ocr_running = False
                    return

                state = self.game_state
                if state in (GameState.GAME_BATTLE, GameState.GAME_BATTLE_MANUAL, GameState.GAME_END_B, GameState.GAME_BATTLE_EJECT):
                    # Extract respawn, incoming, health, and ammo crops (click_to has its own thread)
                    respawn_frame = get_crop(full_frame, *self.crops["respawn"][:4])
                    incoming_frame = get_crop(full_frame, *self.crops["incoming"][:4])
                    health_frame = get_crop(full_frame, *self.crops["HEALTH"][:4]) if "HEALTH" in self.crops else None
                    ammo_flares_frame = get_crop(full_frame, *self.crops["AMMO_FLARES"][:4]) if "AMMO_FLARES" in self.crops else None
                    ammo_missile_frame = get_crop(full_frame, *self.crops["AMMO_MISSILE"][:4]) if "AMMO_MISSILE" in self.crops else None
                    fuel_frame = get_crop(full_frame, *self.crops["FUEL_100"][:4]) if "FUEL_100" in self.crops else None
                    telemetry_frame = get_crop(full_frame, *self.crops["ALTITUDE_SPEED"][:4]) if "ALTITUDE_SPEED" in self.crops else None
                    t1 = time.time()

                    # Submit all tasks to the thread pool for parallel processing.
                    # Numpy arrays are passed by reference — no serialization needed.
                    respawn_future = executor.submit(_process_respawn_region, respawn_frame)
                    incoming_future = executor.submit(
                        _process_incoming_region,
                        incoming_frame,
                        self._incoming_templates,
                        self._incoming_template_matching_enabled,
                        self._incoming_template_threshold,
                        self._incoming_template_near_threshold_low,
                        self._incoming_template_near_threshold_high,
                        self._incoming_template_fallback_to_ocr,
                        self._incoming_fallback_tokens,
                    )
                    health_future = executor.submit(_process_health_region, health_frame) if health_frame is not None else None
                    ammo_flares_future = executor.submit(_process_health_region, ammo_flares_frame, "ammo_flares") if ammo_flares_frame is not None else None
                    ammo_missile_future = executor.submit(_process_health_region, ammo_missile_frame, "ammo_missiles") if ammo_missile_frame is not None else None
                    fuel_future = executor.submit(_process_health_region, fuel_frame, "fuel") if fuel_frame is not None else None
                    # Telemetry is fire-and-forget: harvest last submission's
                    # result if it finished, then maybe submit a fresh frame.
                    # The tick NEVER waits on telemetry (ADR 038 safety rule) —
                    # measured on session run_20260728_172933, blocking on it
                    # made telemetry the critical path on 92% of ticks.
                    telemetry_ocr_time = self._harvest_telemetry_future()
                    self._telemetry_tick_counter += 1
                    if (telemetry_frame is not None
                            and self._telemetry_future is None
                            and self._telemetry_tick_counter >= self._telemetry_every_n_ticks):
                        self._telemetry_tick_counter = 0
                        self._telemetry_frame_ts = time.time()
                        self._telemetry_future = executor.submit(
                            _process_telemetry_region, telemetry_frame)
                    t2 = time.time()

                    # Wait for respawn result first — update its cache immediately so the
                    # main loop can react without waiting for the (often slower) incoming OCR.
                    respawn_detected, respawn_ocr_time, respawn_text = respawn_future.result(timeout=120)
                    # Performance 008: the fallback-fire log reads this to show
                    # whether a "death" coincided with a starved OCR pass.
                    self._last_respawn_ocr_s = respawn_ocr_time
                    if self._tracker:
                        self._tracker.record_ocr_crop("respawn", respawn_ocr_time)

                    if respawn_detected:
                        logger.debug("Analyzer: detected 'RESPAWN' text (matched: '%s')", respawn_text)
                        if self.game_state == GameState.GAME_END_B:
                            self._trigger("respawn_detected")
                        self.emit(GameEvent.RESPAWN_DETECTED, full_frame)

                    respawn_result = (True, 1.0, "ocr") if respawn_detected else (False, 0.0, None)
                    with self._ocr_cache_lock:
                        self._ocr_cache['result'] = respawn_result
                        self._ocr_cache['timestamp'] = current_time
                    self._shadow_record_ocr_respawn(respawn_detected)

                    # Now wait for incoming — its result is independent of respawn.
                    incoming_eval = incoming_future.result(timeout=120)
                    incoming_processing_time = float(incoming_eval.get("processing_time", 0.0))
                    if self._tracker:
                        self._tracker.record_ocr_crop("incoming", incoming_processing_time)

                    template_score = float(incoming_eval.get("template_score", -1.0))
                    template_hit = bool(incoming_eval.get("template_hit", False))
                    near_threshold = bool(incoming_eval.get("near_threshold", False))
                    template_label = incoming_eval.get("template_label")
                    fallback_hit = bool(incoming_eval.get("fallback_hit", False))
                    fallback_text = incoming_eval.get("fallback_text")
                    fallback_raw = incoming_eval.get("fallback_raw") or []

                    now_t = time.time()
                    near_threshold_confirmation = False
                    if near_threshold:
                        if (
                            self._incoming_near_threshold_pending
                            and (now_t - self._incoming_near_threshold_pending_ts) <= 0.75
                        ):
                            near_threshold_confirmation = True
                            self._incoming_near_threshold_pending = False
                        else:
                            self._incoming_near_threshold_pending = True
                            self._incoming_near_threshold_pending_ts = now_t
                    elif template_hit:
                        self._incoming_near_threshold_pending = False
                    else:
                        self._incoming_near_threshold_pending = False

                    incoming_detected = False
                    detection_source = "none"
                    detected_label = None

                    if template_hit or near_threshold_confirmation:
                        incoming_detected = True
                        detection_source = "template"
                        detected_label = template_label
                    elif fallback_hit:
                        incoming_detected = True
                        detection_source = "ocr_fallback"
                        detected_label = fallback_text

                    debounce_suppressed = False
                    if incoming_detected and self._incoming_debounce_window_s > 0.0:
                        if (now_t - self._incoming_last_positive_ts) < self._incoming_debounce_window_s:
                            debounce_suppressed = True
                            incoming_detected = False
                            detection_source = "none"
                        else:
                            self._incoming_last_positive_ts = now_t

                    if incoming_detected:
                        logger.info(
                            "\033[95m🚀 INCOMING MISSILE DETECTED (source=%s template=%s score=%.3f text=%s)\033[0m",
                            detection_source,
                            template_label,
                            template_score,
                            detected_label,
                        )
                    elif fallback_raw:
                        logger.debug("Analyzer: No match in INCOMING region — raw OCR: %s", ", ".join(fallback_raw))
                    else:
                        logger.debug("Analyzer: No text detected in INCOMING region")

                    _incoming_log = logger.info if self._incoming_template_telemetry_info else logger.debug
                    _incoming_log(
                        "Analyzer: incoming_template detector=incoming_template template_score=%.3f "
                        "template_threshold=%.3f near_threshold_confirmation=%s detection_source=%s "
                        "detected=%s debounce_suppressed=%s incoming_processing_ms=%d",
                        template_score,
                        self._incoming_template_threshold,
                        near_threshold_confirmation,
                        detection_source,
                        incoming_detected,
                        debounce_suppressed,
                        int(incoming_processing_time * 1000),
                    )

                    incoming_method = detection_source if incoming_detected else None
                    incoming_conf = template_score if detection_source == "template" else (1.0 if detection_source == "ocr_fallback" else 0.0)
                    incoming_result = (incoming_detected, float(incoming_conf), incoming_method)
                    with self._incoming_cache_lock:
                        self._incoming_cache['result'] = incoming_result
                        self._incoming_cache['timestamp'] = current_time
                        # ADR 096: keep `timestamp` as-is so the historical
                        # `reaction` series stays comparable, and add the split.
                        self._incoming_cache['frame_ts'] = self._background_ocr_frame_ts
                        self._incoming_cache['detect_done_ts'] = time.time()
                    if incoming_detected:
                        self.incoming_event.set()

                    # Wait for health result and update sub-state.
                    health_ocr_time = 0.0
                    if health_future is not None:
                        health_value, health_ocr_time = health_future.result(timeout=120)
                        if self._tracker:
                            self._tracker.record_ocr_crop("health", health_ocr_time)
                        self._process_health_reading(health_value)
                    # Resolve ammo futures and fire events.
                    ammo_flares_ocr_time = 0.0
                    ammo_missile_ocr_time = 0.0
                    if ammo_flares_future is not None:
                        flares_value, ammo_flares_ocr_time = ammo_flares_future.result(timeout=120)
                        if self._tracker:
                            self._tracker.record_ocr_crop("ammo_flares", ammo_flares_ocr_time)
                    else:
                        flares_value = None
                    if ammo_missile_future is not None:
                        missile_value, ammo_missile_ocr_time = ammo_missile_future.result(timeout=120)
                        if self._tracker:
                            self._tracker.record_ocr_crop("ammo_missiles", ammo_missile_ocr_time)
                    else:
                        missile_value = None
                    fuel_ocr_time = 0.0
                    if fuel_future is not None:
                        fuel_value, fuel_ocr_time = fuel_future.result(timeout=120)
                        if self._tracker:
                            self._tracker.record_ocr_crop("fuel", fuel_ocr_time)
                        self._process_fuel_reading(fuel_value)

                    if respawn_detected:
                        with self._ammo_lock:
                            self._ammo_flares = None
                            self._ammo_missiles = None
                        self.low_flares_event.clear()
                        self.no_missiles_event.clear()
                        self._last_logged_flares = None
                        self._last_logged_missiles = None
                        logger.debug("Analyzer: skipping ammo updates while respawn is detected")
                    else:
                        if flares_value is not None:
                            with self._ammo_lock:
                                self._ammo_flares = flares_value
                            if flares_value != self._last_logged_flares:
                                logger.info("Ammo flares: %d", flares_value)
                                self._last_logged_flares = flares_value
                            if flares_value == 2:
                                self.low_flares_event.set()
                        if missile_value is not None:
                            with self._ammo_lock:
                                self._ammo_missiles = missile_value
                            if missile_value != self._last_logged_missiles:
                                logger.info("Ammo missiles: %d", missile_value)
                                self._last_logged_missiles = missile_value
                            if missile_value == 0:
                                self.no_missiles_event.set()

                    t4 = time.time()

                    # Log timing
                    logger.debug(
                        "Analyzer: Parallel OCR Timings - Extract: %.2fs, Submit: %.2fs | "
                        "Respawn OCR: %.2fs | Incoming OCR: %.2fs | Health OCR: %.2fs | "
                        "Flares OCR: %.2fs | Missiles OCR: %.2fs | Fuel OCR: %.2fs | Telemetry OCR: %.2fs | Total: %.2fs",
                        t1-t0, t2-t1, respawn_ocr_time, incoming_processing_time, health_ocr_time,
                        ammo_flares_ocr_time, ammo_missile_ocr_time, fuel_ocr_time, telemetry_ocr_time, t4-t0
                    )
                # NOTE: the GAME_STARTING health-probe branch that used to live here
                # was removed 2026-08-05. It was unreachable — _detect_respawn_ocr
                # returns before scheduling background OCR in that state — and is now
                # served by _schedule_starting_health_probe(), which scans HEALTH only
                # and never touches the respawn/incoming caches.
                else:
                    logger.debug("Skipping GAME_BATTLE crop OCR in %s state", state.name)
                    self._shadow_clear_mark()
                    self._background_ocr_stop.wait(timeout=0.2)
            except Exception as e:
                if self._background_ocr_stop.is_set() or self._shutting_down or _is_shutdown_runtime_error(e):
                    logger.debug("Analyzer: OCR background loop exiting during shutdown: %s", e)
                    with self._background_ocr_lock:
                        self._background_ocr_frame = None
                        self._background_ocr_pending_frame = None
                        self._background_ocr_running = False
                    return
                logger.warning("Analyzer: OCR detection failed: %s", e)

            with self._background_ocr_lock:
                if self._background_ocr_pending_frame is not None:
                    # Process the most recent pending frame immediately
                    self._background_ocr_frame = self._background_ocr_pending_frame
                    self._background_ocr_pending_frame = None
                    logger.debug("Background OCR processing latest pending frame")
                    continue

                self._background_ocr_frame = None
                self._background_ocr_running = False
                return

    def _run_click_to_in_background(self):
        """Poll for 'Click to Continue' on a low-frequency independent schedule.

        Runs every 5 seconds in its own daemon thread so it never delays the
        high-priority respawn/incoming OCR cycle.
        """
        interval = 5.0
        while not self._click_to_stop.wait(timeout=interval):
            state = self.game_state
            if state in (GameState.GAME_UNKNOWN, GameState.GAME_STARTING, GameState.GAME_WAITING):
                continue
            if state in (GameState.GAME_END_B, GameState.GAME_LOBBY):
                # ADR 087 addendum 4: the self-suppression below assumes the FSM
                # is RIGHT about being in the lobby. "GAME_END_B timeout —
                # forcing recovery to GAME_LOBBY" can make it wrong, and then
                # the lie disables the one detector that would clear the screen
                # holding it there: on 2026-08-21 the post-match PERFORMANCE
                # panel with "Click to Continue..." sat unread for 17 minutes
                # while every lobby crop read blank.
                #
                # A lobby blackout is exactly the evidence that the premise
                # failed, so the scan resumes. In a healthy lobby the crops
                # match, no blackout is active, and the 2026-07-30 double
                # click-through stays suppressed.
                if not (state == GameState.GAME_LOBBY
                        and self.lobby_blackout_active()):
                    logger.debug("Click-to OCR skipped: %s state active", state.name)
                    continue
                logger.info(
                    "Click-to OCR re-enabled: GAME_LOBBY blackout — the forced "
                    "state may be wrong (ADR 087)")
            with self._click_to_frame_lock:
                frame = self._click_to_latest_frame
            if frame is None:
                continue
            executor = self.ocr_executor
            if executor is None:
                continue
            try:
                click_to_frame = get_crop(frame, *self.crops["click_to"][:4])
                click_to_detected, _, click_to_text = executor.submit(
                    _process_text_region, click_to_frame, self.crops["click_to"].text or []
                ).result(timeout=30)
                result = (True, 1.0, "ocr") if click_to_detected else (False, 0.0, None)
                with self._click_to_cache_lock:
                    self._click_to_cache['result'] = result
                    self._click_to_cache['timestamp'] = time.time()
                if click_to_detected:
                    # GAME_BATTLE_MANUAL is a legal source for this trigger (see the
                    # transition table above) and must be included: without it the FSM
                    # never reached GAME_END_B, so the self-suppression below (which
                    # skips the scan in GAME_END_B/GAME_LOBBY) never engaged, this
                    # poller re-detected the same on-screen prompt 5s later, and the
                    # main loop ran a second full click-through — 7 extra clicks plus a
                    # stray PLAY click while already in the lobby, followed by a long
                    # "no lobby crops detected" blackout (2026-07-30 16:39).
                    if self.game_state in (GameState.GAME_BATTLE,
                                           GameState.GAME_BATTLE_EJECT,
                                           GameState.GAME_BATTLE_MANUAL):
                        self._trigger("click_to_detected")
                    logger.debug("Analyzer: detected 'Click to' text (matched: '%s') → GAME_END_B", click_to_text)
            except RuntimeError:
                return  # executor shut down — exit the loop cleanly
            except Exception as e:
                if self._click_to_stop.is_set() or self._shutting_down or _is_shutdown_runtime_error(e):
                    logger.debug("Analyzer: click_to OCR loop exiting during shutdown: %s", e)
                    return
                logger.warning("Analyzer: click_to OCR failed: %s", e)

    def set_round_start_suppressor(self, predicate) -> None:
        """Install a predicate that, when true, blocks automatic round starts.

        Used by the FINISH_ROUND_THEN_EXIT hotkey (ADR 094) so that pressing it
        in the lobby stops there instead of racing the quick-scan into another
        round. Never raises: a failing predicate must not take perception down,
        so it is treated as "do not suppress" — the pre-existing behaviour.
        """
        self._suppress_round_start = predicate

    def _round_start_suppressed(self) -> bool:
        """True when an automatic PLAY/READY click must not fire."""
        if self._suppress_round_start is None:
            return False
        try:
            return bool(self._suppress_round_start())
        except Exception:
            logger.exception("Analyzer: round-start suppressor raised — not suppressing")
            return False

    def _ensure_lobby_quick_scan_thread(self):
        """Start (or restart) the popup quick-scan thread.

        Called on every frame, including while still in GAME_UNKNOWN. ADR 074
        made GAME_UNKNOWN a popup-dismiss state precisely because a modal popup
        there hides every classification marker — so gating the scanner on
        successful classification defeats the recovery path it was added for.
        A session that boots straight into a popup (2026-08-19 04:29,
        NEW_FLIGHT_PASS) previously stranded until a manual 'm' press.
        """
        if self._shutting_down or self._lobby_quick_scan_stop.is_set():
            return
        thread_dead = (self._lobby_quick_scan_thread is not None
                       and not self._lobby_quick_scan_thread.is_alive())
        if not self._lobby_quick_scan_thread_started or thread_dead:
            if thread_dead:
                logger.warning("Lobby quick-scan thread died unexpectedly — restarting")
                self._lobby_quick_scan_stop.clear()
            self._lobby_quick_scan_thread_started = True
            self._lobby_quick_scan_thread = threading.Thread(
                target=self._run_game_lobby_quick_scan, daemon=True)
            self._lobby_quick_scan_thread.start()
            logger.info("Lobby quick-scan background thread started")

    def lobby_blackout_active(self) -> bool:
        """True while GAME_LOBBY has been showing no lobby crop (ADR 087).

        Gates every ESC source. During a blackout ESC has no demonstrated
        benefit and one demonstrated harm: it opens the Exit-to-Desktop modal,
        which is itself a blackout. See exit_dialog_visible for why the
        narrower dialog flag is not enough on its own.
        """
        return self._lobby_blackout_since != 0.0

    def lobby_blackout_age_s(self) -> float:
        """Seconds since the current lobby blackout began, 0.0 if none."""
        if self._lobby_blackout_since == 0.0:
            return 0.0
        return max(0.0, time.time() - self._lobby_blackout_since)

    def blackout_esc_suppressed(self) -> bool:
        """True while ESC must stay suppressed for a lobby blackout (ADR 093).

        ADR 087 suppressed ESC during a blackout because ESC opens the
        Exit-to-Desktop modal, re-creating it seconds after recovery cancels
        it. That reasoning is sound but had no ceiling, so when the blackout
        came from a screen no crop recognises the suppression became permanent
        — on 2026-08-24 a PROFILE overlay held the session inert for 110
        minutes with every recovery path ineligible.

        So the suppression is a delay, not a veto. Past the ceiling the trade
        inverts: the cancel-then-reopen cycle is bounded and self-correcting
        (STALL_EXIT_TO_DESKTOP cancels the dialog, at ~23s per iteration),
        while paralysis is terminal. Churn beats paralysis.
        """
        if not self.lobby_blackout_active():
            return False
        ceiling = self._blackout_esc_ceiling_s
        if ceiling <= 0:
            return True          # ceiling disabled — ADR 087 behaviour
        return self.lobby_blackout_age_s() < ceiling

    def exit_dialog_visible(self, stale_after_s: float = 12.0) -> bool:
        """True while the Exit-to-Desktop modal was seen recently (ADR 087).

        Every ESC source must consult this: ESC is what opens the modal, so a
        press while it is up re-opens what recovery just closed. The staleness
        window covers roughly two stall-scan intervals, so the flag lapses on
        its own if the scan stops confirming the dialog.
        """
        if not self._exit_dialog_seen_ts:
            return False
        return time.time() - self._exit_dialog_seen_ts <= stale_after_s

    def _stall_recovery_targets(self, state):
        """Return the stall-recovery crops eligible to act right now (ADR 084).

        Empty during healthy operation — these actions leave squads and dismiss
        modals sitting next to an "Exit to Desktop" button, so the gate is
        deliberately tighter than the popup crops': an unclassifiable state that
        has PERSISTED, not merely occurred.
        """
        targets = []
        now = time.time()
        if (state in STALL_ACTION_STATES
                and self._stall_state_since
                and now - self._stall_state_since >= self._stall_action_after_s):
            targets.extend(c for c in STALL_RECOVERY_CROPS if c in self.crops)
        # ADR 087 independent gate: a sustained GAME_LOBBY blackout is a real
        # stall, but GAME_LOBBY is not in STALL_ACTION_STATES so the batch above
        # never runs for it. The ESC pressed on every LOBBY_STALL beat is what
        # OPENS the "Exit to Desktop" dialog, whose own crop then goes unscanned
        # — wingman deadlocks against a modal it created (2026-08-21, 8 minutes,
        # 187 blank cycles; the captured frame shows Exit highlighted as the
        # default button).
        #
        # Deliberately narrower than the batch above: ONLY the dialog wingman
        # can create itself, whose action is a Cancel click. STALL_RETRY and
        # STALL_AIRCRAFT stay gated on a genuinely unclassifiable state.
        if (self._lobby_blackout_since
                and now - self._lobby_blackout_since >= self._stall_action_after_s
                and "STALL_EXIT_TO_DESKTOP" in self.crops
                and "STALL_EXIT_TO_DESKTOP" not in targets):
            targets.append("STALL_EXIT_TO_DESKTOP")
        # ADR 093: a full-screen PROFILE overlay is neither the lobby, nor a
        # calibrated popup, nor the exit dialog, so on 2026-08-24 all three
        # recovery paths found nothing and wingman sat inert for 110 minutes.
        # Eligible on the same blackout gate and for the same reason as the
        # dialog above: the action is a close-button click, strictly
        # de-escalating, with no destructive control beside it.
        if (self._lobby_blackout_since
                and now - self._lobby_blackout_since >= self._stall_action_after_s
                and "STALL_PROFILE" in self.crops
                and "STALL_PROFILE" not in targets):
            targets.insert(0, "STALL_PROFILE")
        # Independent gate: a stuck UNREADY blocks classification outright, so it
        # is timed from the UNREADY read itself rather than from the state.
        if (self._unready_since
                and now - self._unready_since >= self._stall_unready_dwell_s
                and STALL_UNREADY_CROP in self.crops):
            targets.append(STALL_UNREADY_CROP)
        return targets

    def _run_game_lobby_quick_scan(self):
        """Scan lobby crops every 1s while in a POPUP_DISMISS_STATES state.

        Lobby crops and popup crops are submitted in separate batches so popup OCR
        can use a fresher frame than the one used for CANCEL / PLAY detection.

        Popup scan fires every 5s in all participating states unless a PLAY/READY
        click happened within the last 5s; CANCEL/PLAY scan fires every cycle in
        GAME_LOBBY; CANCEL alone is also scanned every cycle in GAME_WAITING so a
        brief CANCEL window is not missed between main-loop 3-second polling
        intervals. GAME_UNKNOWN runs the popup batch ONLY (ADR 074): a modal
        popup there hides every classification marker, so dismissal is the sole
        recovery path.
        """
        lobby_crops = [c for c in ("CANCEL", "UNREADY", "PLAY", "READY") if c in self.crops]
        # Must cover every dismissible crop the GAME_LOBBY state declares in
        # _STATE_CROPS, or a screen wingman has a calibrated crop for is never
        # scanned. TAP_HERE_TO_CONTINUE and FINAL_CONTINUE were declared there
        # and missing here: a PILOT LEVEL UP screen ("Tap Here to Continue")
        # stranded the lobby for 40 minutes on 2026-08-22 08:40 while the crop
        # that dismisses it sat unused. test_lobby_popup_coverage guards the
        # two lists against drifting apart again.
        popup_crop_names = ["INVITED", "CREATION_FAILED", "REVEAL_ALL", "SILVER",
                            "UNLOCK_CLOSE", "INSPECT", "event_refresh",
                            "NEW_FLIGHT_PASS", "TAP_HERE_TO_CONTINUE",
                            "FINAL_CONTINUE"]
        popup_crops = [c for c in popup_crop_names if c in self.crops]

        if not lobby_crops and not popup_crops:
            logger.warning("Lobby quick-scan: no crops configured — thread exiting")
            return

        last_popup_scan_ts = 0.0
        last_stall_scan_ts = 0.0
        lobby_stall_since = 0.0  # timestamp when stall (no crops detected) first started

        while not self._lobby_quick_scan_stop.wait(timeout=1.0):
            cycle_start = time.time()
            state = self.game_state
            if state != GameState.GAME_LOBBY:
                # Stall tracking only applies while continuously in GAME_LOBBY — clear it
                # here so a later re-entry (e.g. after a GAME_WAITING excursion) starts
                # counting fresh instead of comparing against a stale timestamp.
                lobby_stall_since = 0.0
                self._lobby_blackout_since = 0.0
            # ADR 084: dwell in an unclassifiable state, reset on any classified state
            # so a brief GAME_UNKNOWN blip mid-transition never opens the gate.
            if state in STALL_ACTION_STATES:
                if self._stall_state_since == 0.0:
                    self._stall_state_since = time.time()
            else:
                self._stall_state_since = 0.0
            # ADR 074: GAME_UNKNOWN participates for the POPUP batch only —
            # lobby crops stay excluded there: classification owns marker
            # detection in GAME_UNKNOWN, and clicking PLAY/CANCEL from an
            # unclassified state would be wrong.
            if state not in POPUP_DISMISS_STATES and state not in LOBBY_RECHECK_STATES:
                continue

            executor = self.ocr_executor
            if executor is None:
                continue

            # Bound to the whole try/finally below: the cleanup cancels whatever
            # is left in these, and must never raise NameError over the real
            # exception when a cycle fails before they are populated.
            lobby_futures = {}
            popup_futures = {}
            try:
                # --- CANCEL / UNREADY / PLAY / READY ---
                # GAME_LOBBY: scan all lobby crops.
                # GAME_WAITING: scan CANCEL only — provides 1-second detection cadence
                # instead of relying solely on the 3-second main-loop poll, which can
                # miss a brief CANCEL window (e.g. squad-READY → match-found flow).
                if state not in LOBBY_RECHECK_STATES and self._starting_play_streak:
                    self._starting_play_streak = 0
                lobby_futures = {}
                lobby_scan_start = None
                handled = False
                play_clicked_this_cycle = False

                if state == GameState.GAME_LOBBY:
                    crops_to_scan = lobby_crops
                elif state == GameState.GAME_WAITING:
                    crops_to_scan = [c for c in ("CANCEL",) if c in self.crops]
                elif state in LOBBY_RECHECK_STATES:
                    # ADR 102: PLAY only. Nothing is clicked from here — the
                    # detection walks the state back and the ordinary lobby
                    # path does the clicking, so this cannot click PLAY into a
                    # match that is genuinely starting.
                    crops_to_scan = [c for c in ("PLAY",) if c in self.crops]
                else:
                    # GAME_UNKNOWN / GAME_STARTING_STALLED: popup batch only
                    # (ADR 074) — no lobby-crop clicking from those states.
                    crops_to_scan = []
                if crops_to_scan:
                    with self._click_to_frame_lock:
                        frame = self._click_to_latest_frame
                        frame_ts = self._click_to_frame_ts

                    if frame is not None:
                        frame_age = time.time() - frame_ts
                        if frame_age > 3.0:
                            logger.debug(
                                "Lobby quick-scan: skipping stale lobby frame (%.1fs old)",
                                frame_age,
                            )
                        else:
                            lobby_scan_start = time.time()
                            for crop in crops_to_scan:
                                # ADR 103: crop-and-copy HERE, so a queued task
                                # holds tens of KB instead of the 6.9 MB frame.
                                lobby_futures[crop] = executor.submit(
                                    _process_text_region,
                                    _crop_for_ocr(frame, self.crops[crop][:4]),
                                    self.crops[crop].text or [],
                                )

                if state == GameState.GAME_LOBBY:
                    for crop in ("CANCEL", "UNREADY"):
                        if crop not in lobby_futures:
                            continue
                        try:
                            detected, _, text = lobby_futures[crop].result(timeout=20)
                        except Exception as e:
                            logger.warning("Lobby quick-scan: %s result failed: %s", crop, e)
                            continue
                        if detected:
                            if crop == "UNREADY":
                                # UNREADY means this player already clicked READY and is waiting
                                # for squad members. The correct transition is play_clicked →
                                # GAME_WAITING, not cancel_detected → GAME_STARTING.
                                logger.info(
                                    "\033[93m📋 Lobby quick-scan: UNREADY detected — squad not ready yet → GAME_WAITING\033[0m")
                                self._last_lobby_play_click_ts = time.time()
                                self._trigger("play_clicked")
                            else:
                                logger.info(
                                    "\033[92m✓ Lobby quick-scan: CANCEL detected (text='%s') → GAME_STARTING\033[0m",
                                    text)
                                self._trigger("cancel_detected")
                            handled = True
                            break

                if not handled and state == GameState.GAME_WAITING and "CANCEL" in lobby_futures:
                    try:
                        detected, _, text = lobby_futures["CANCEL"].result(timeout=20)
                    except Exception as e:
                        logger.warning("Lobby quick-scan: CANCEL result failed in GAME_WAITING: %s", e)
                        detected = False
                    if detected:
                        logger.info(
                            "\033[92m✓ Lobby quick-scan: CANCEL detected in GAME_WAITING (text='%s') → GAME_STARTING\033[0m",
                            text)
                        self._trigger("cancel_detected")
                        handled = True
                    else:
                        logger.debug("Lobby quick-scan: CANCEL not found in GAME_WAITING")

                if not handled and state in LOBBY_RECHECK_STATES:
                    detected = False
                    if "PLAY" in lobby_futures:
                        try:
                            detected, _, text = lobby_futures["PLAY"].result(timeout=20)
                        except Exception as e:
                            logger.warning(
                                "Lobby quick-scan: PLAY result failed in %s: %s",
                                state.name, e)
                            detected = False
                    if detected:
                        self._starting_play_streak += 1
                        if self._starting_play_streak >= STARTING_PLAY_CONFIRM_READS:
                            logger.warning(
                                "\033[93m📋 Lobby quick-scan: PLAY still visible after "
                                "%d reads in %s — the match never started, "
                                "returning to GAME_LOBBY (ADR 102)\033[0m",
                                self._starting_play_streak, state.name)
                            # The suppression exists to stop a second click on a
                            # PLAY that worked. This is the proof it did not, so
                            # clearing it is the point — otherwise the lobby is
                            # re-entered and then sits for the rest of the 60 s
                            # window without clicking.
                            self._last_lobby_play_click_ts = 0.0
                            self._starting_play_streak = 0
                            self._trigger("starting_play_visible")
                        else:
                            logger.info(
                                "Lobby quick-scan: PLAY visible in %s (%d/%d reads)",
                                state.name, self._starting_play_streak,
                                STARTING_PLAY_CONFIRM_READS)
                    elif self._starting_play_streak:
                        logger.debug(
                            "Lobby quick-scan: PLAY no longer visible in %s — "
                            "streak reset", state.name)
                        self._starting_play_streak = 0
                    handled = True

                if not handled and state == GameState.GAME_LOBBY:
                    for crop in ("PLAY", "READY"):
                        if crop not in lobby_futures:
                            continue
                        try:
                            detected, _, text = lobby_futures[crop].result(timeout=20)
                        except Exception as e:
                            logger.warning("Lobby quick-scan: %s result failed: %s", crop, e)
                            continue
                        if not detected:
                            continue
                        if time.time() - self._last_lobby_play_click_ts < 60.0:
                            logger.debug(
                                "Lobby quick-scan: %s visible but click suppressed (%.1fs since last click)",
                                crop, time.time() - self._last_lobby_play_click_ts,
                            )
                            handled = True
                        elif self.game_state == GameState.GAME_STARTING:
                            logger.debug(
                                "Lobby quick-scan: %s visible but state is now GAME_STARTING — skipping click",
                                crop,
                            )
                            handled = True
                        elif self._round_start_suppressed():
                            # ADR 094: the operator asked to stop. Staying in
                            # GAME_LOBBY keeps the main loop's safe point true,
                            # so the deferred exit fires on the next tick.
                            logger.info(
                                "\033[93m🏁 FINISH ROUND: %s visible but exit is pending — "
                                "not starting another round\033[0m", crop,
                            )
                            handled = True
                        else:
                            self.capture_waiting_cancel_baseline(frame)
                            logger.info(
                                "\033[93m📋 Lobby quick-scan: %s detected (text='%s') — clicking\033[0m",
                                crop, text,
                            )
                            self._last_lobby_play_click_ts = time.time()
                            self.emit(GameEvent.LOBBY_PLAY_CLICK, crop, frame)
                            self._trigger("play_clicked")
                            handled = True
                            play_clicked_this_cycle = True
                        break

                    if not handled and lobby_futures:
                        if lobby_stall_since == 0.0:
                            lobby_stall_since = time.time()
                        if self._lobby_blackout_since == 0.0:
                            # ADR 087: total blackout duration. Separate from
                            # lobby_stall_since, which restarts on every ESC.
                            self._lobby_blackout_since = time.time()
                        elapsed_stall = time.time() - lobby_stall_since
                        logger.info(
                            "Lobby quick-scan: no lobby crops detected (stalled %.1fs)",
                            elapsed_stall,
                        )
                        if (elapsed_stall >= 10.0
                                and self.has_subscribers(GameEvent.LOBBY_STALL)):
                            logger.info("Lobby quick-scan: stall threshold reached")
                            self.emit(GameEvent.LOBBY_STALL)
                            lobby_stall_since = time.time()  # cooldown: next press after another 10s
                    elif handled:
                        lobby_stall_since = 0.0
                        self._lobby_blackout_since = 0.0

                if lobby_futures and lobby_scan_start is not None:
                    logger.debug(
                        "Lobby quick-scan: lobby batch completed in %.2fs",
                        time.time() - lobby_scan_start,
                    )

                # After a PLAY/READY click, skip popup OCR briefly so the main loop can
                # focus on GAME_WAITING CANCEL detection without spending this cycle on popups.
                popup_cooldown_remaining = 5.0 - (time.time() - self._last_lobby_play_click_ts)
                current_state_for_popup_gate = self.game_state
                do_popup_scan = (
                    bool(popup_crops)
                    and not play_clicked_this_cycle
                    and popup_cooldown_remaining <= 0.0
                    and time.time() - last_popup_scan_ts >= 5.0
                    and current_state_for_popup_gate in POPUP_DISMISS_STATES
                )
                if bool(popup_crops) and popup_cooldown_remaining > 0.0:
                    logger.debug(
                        "Lobby quick-scan: popup scan suppressed for %.1fs after PLAY/READY click",
                        popup_cooldown_remaining,
                    )

                # --- Popup crops (both states, every 5s) ---
                popup_futures = {}
                popup_scan_start = None
                if do_popup_scan:
                    with self._click_to_frame_lock:
                        popup_frame = self._click_to_latest_frame
                        popup_frame_ts = self._click_to_frame_ts

                    if popup_frame is not None:
                        popup_frame_age = time.time() - popup_frame_ts
                        if popup_frame_age > 3.0:
                            logger.debug(
                                "Lobby quick-scan: skipping stale popup frame (%.1fs old)",
                                popup_frame_age,
                            )
                        else:
                            last_popup_scan_ts = time.time()
                            popup_scan_start = time.time()
                            for crop in popup_crops:
                                popup_futures[crop] = executor.submit(     # ADR 103
                                    _process_text_region,
                                    _crop_for_ocr(popup_frame, self.crops[crop][:4]),
                                    self.crops[crop].text or [],
                                )

                if popup_futures:
                    # Re-check state before blocking on results. If we've transitioned out of
                    # GAME_LOBBY / GAME_WAITING while futures were queued (e.g. PLAY was clicked
                    # and we're now in GAME_STARTING), cancel queued futures and skip this batch
                    # entirely to avoid holding up the executor for 50+ seconds.
                    current_state_for_popup = self.game_state
                    if current_state_for_popup not in POPUP_DISMISS_STATES:
                        for f in popup_futures.values():
                            f.cancel()
                        logger.debug(
                            "Lobby quick-scan: popup futures cancelled — state changed to %s",
                            current_state_for_popup,
                        )
                        popup_futures = {}

                if popup_futures:
                    popup_detected = False
                    for crop in popup_crops:
                        if crop not in popup_futures:
                            continue
                        try:
                            detected, _, text = popup_futures[crop].result(timeout=20)
                            if detected:
                                logger.info(
                                    "Lobby quick-scan: popup '%s' detected (text='%s')",
                                    crop, text,
                                )
                                self.emit(GameEvent.LOBBY_POPUP_CLICK, crop)
                                popup_detected = True
                                break
                            logger.debug("Lobby quick-scan: popup '%s' not found", crop)
                        except Exception as e:
                            logger.warning(
                                "Lobby quick-scan: popup '%s' scan failed: %s: %s",
                                crop, type(e).__name__, e,
                            )

                    if not popup_detected:
                        # The screen is popup-free: tells the ADR 074 recorder a
                        # prior dismissal actually worked, so a continuing stall
                        # is not blamed on popup handling.
                        self.emit(GameEvent.LOBBY_POPUP_ABSENT)

                    if popup_scan_start is not None:
                        logger.debug(
                            "Lobby quick-scan: popup batch completed in %.2fs%s",
                            time.time() - popup_scan_start,
                            " (detected)" if popup_detected else "",
                        )

                # --- Stall-recovery crops (ADR 084, gated on a real stall) ---
                stall_targets = self._stall_recovery_targets(state)
                if stall_targets and time.time() - last_stall_scan_ts >= self._stall_scan_interval_s:
                    with self._click_to_frame_lock:
                        stall_frame = self._click_to_latest_frame
                        stall_frame_ts = self._click_to_frame_ts
                    if stall_frame is not None and time.time() - stall_frame_ts <= 3.0:
                        last_stall_scan_ts = time.time()
                        for crop in stall_targets:
                            try:
                                detected, _, text = executor.submit(   # ADR 103
                                    _process_text_region,
                                    _crop_for_ocr(stall_frame, self.crops[crop][:4]),
                                    self.crops[crop].text or [],
                                ).result(timeout=20)
                            except Exception as e:
                                logger.warning(
                                    "Stall recovery: '%s' scan failed: %s: %s",
                                    crop, type(e).__name__, e)
                                continue
                            if detected:
                                logger.warning(
                                    "\033[93m🔧 Stall recovery: '%s' detected (text='%s', state=%s)\033[0m",
                                    crop, text, state.name)
                                if crop == "STALL_EXIT_TO_DESKTOP":
                                    # ADR 087: ESC is what OPENS this modal, so
                                    # every ESC source must stand down while it
                                    # is up or they re-open what recovery closes.
                                    self._exit_dialog_seen_ts = time.time()
                                self.emit(GameEvent.STALL_RECOVERY_ACTION, crop)
                                break
                            if crop == "STALL_EXIT_TO_DESKTOP":
                                self._exit_dialog_seen_ts = 0.0
                            logger.debug("Stall recovery: '%s' not found", crop)

                cycle_elapsed = time.time() - cycle_start
                if cycle_elapsed > 15.0:
                    logger.warning(
                        "Lobby quick-scan: slow cycle %.1fs (OCR timeout or hung worker)",
                        cycle_elapsed,
                    )
                else:
                    logger.debug("Lobby quick-scan: cycle completed in %.2fs", cycle_elapsed)
            except RuntimeError:
                return  # executor shut down
            except Exception as e:
                logger.warning("Lobby quick-scan: scan failed: %s: %s", type(e).__name__, e)
            finally:
                # ADR 103: drop work this cycle never read. A cycle submits every
                # lobby crop but the handlers break on the first hit, so up to
                # three futures per cycle go unconsumed.
                #
                # This is NOT the memory fix and must not be mistaken for one:
                # cancel() leaves the _WorkItem in the executor queue until a
                # worker pops it, so a stalled pool releases nothing. The frames
                # are kept out of the queue by _crop_for_ocr at the submission
                # sites instead. What this buys is not running OCR that nobody
                # will read once the pool drains.
                # cancel() takes a lock, checks state and returns a bool; it does
                # not raise, so it needs no guard.
                for _fut in list(lobby_futures.values()) + list(popup_futures.values()):
                    _fut.cancel()

    def _empty_state(self):
        """Return empty game state for error cases."""
        return {
            'is_respawning': False,
            'respawn_confidence': 0.0,
            'respawn_method': None,
            'is_incoming': False,
            'incoming_confidence': 0.0,
            'incoming_method': None,
            'is_click_to': False,
            'click_to_method': None,
            'health': None,
            'game_battle_alive': False,
            'game_state': GameState.GAME_UNKNOWN,
        }

    def _classify_unknown_state(self, frame) -> "GameState | None":
        """Classify startup GAME_UNKNOWN frame into a known runtime state.

        Precedence: GAME_END_B > GAME_LOBBY > GAME_BATTLE.
        """
        if self.scan_region_for_click_to(frame):
            return GameState.GAME_END_B

        if self.scan_region_for_play_button(frame) is not None:
            return GameState.GAME_LOBBY

        health_value = self._scan_region_for_health_value(frame)
        if health_value is not None:
            return GameState.GAME_BATTLE

        return None

    def scan_region_for_click_to(self, frame) -> bool:
        """Synchronously scan the click_to crop for the end-of-round prompt."""
        if "click_to" not in self.crops:
            return False
        executor = self.ocr_executor
        if executor is None:
            return False
        try:
            region_frame = get_crop(frame, *self.crops["click_to"][:4])
            detected, _, _ = executor.submit(
                _process_text_region, region_frame, self.crops["click_to"].text or []
            ).result(timeout=30)
            return bool(detected)
        except Exception as e:
            logger.debug("Analyzer: click_to scan failed in unknown classification: %s", e)
            return False

    def _scan_region_for_health_value(self, frame) -> "int | None":
        """Return numeric health OCR value from HEALTH crop, or None if unavailable."""
        if "HEALTH" not in self.crops:
            return None
        try:
            health_frame = get_crop(frame, *self.crops["HEALTH"][:4])
            if not np.any(health_frame):
                return None
        except Exception as e:
            logger.debug("Analyzer: health crop failed in unknown classification: %s", e)
            return None

        executor = self.ocr_executor
        if executor is None:
            return None
        try:
            health_value, _ = executor.submit(_process_health_region, health_frame).result(timeout=30)
            return health_value
        except Exception as e:
            logger.debug("Analyzer: health OCR failed in unknown classification: %s", e)
            return None

    def reset_cache(self):
        """Reset OCR caches - useful when switching between different images/scenes."""
        with self._ocr_cache_lock:
            self._ocr_cache['timestamp'] = 0.0
            self._ocr_cache['result'] = (False, 0.0, None)
        with self._incoming_cache_lock:
            self._incoming_cache['timestamp'] = 0.0
            self._incoming_cache['result'] = (False, 0.0, None)
        with self._click_to_cache_lock:
            self._click_to_cache['timestamp'] = 0.0
            self._click_to_cache['result'] = (False, 0.0, None)
        self._incoming_last_positive_ts = 0.0
        self._incoming_near_threshold_pending = False
        self._incoming_near_threshold_pending_ts = 0.0
        logger.debug("OCR caches reset")

    def detect_enemy_red(self, frame) -> bool:
        """Return True if the ENEMY_CLOSE_BY crop contains red pixels (enemy marker visible).

        Uses a pure HSV colour mask — no OCR, runs synchronously on the calling thread.
        Red wraps in HSV; the primary range [0–10] from enemy_hsv config is checked, plus
        the wrap-around range [170–180] is always included.
        """
        if "ENEMY_CLOSE_BY" not in self.crops:
            return False
        try:
            crop = get_crop(frame, *self.crops["ENEMY_CLOSE_BY"][:4])
            hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
            mask = cv2.inRange(hsv, self._enemy_hsv_lower, self._enemy_hsv_upper)
            # Always include the wrap-around red range (hue 170–180)
            wrap_lower = np.array([170, self._enemy_hsv_lower[1], self._enemy_hsv_lower[2]], dtype=np.uint8)
            wrap_upper = np.array([180, self._enemy_hsv_upper[1], self._enemy_hsv_upper[2]], dtype=np.uint8)
            mask |= cv2.inRange(hsv, wrap_lower, wrap_upper)
            return bool(np.any(mask))
        except Exception as e:
            logger.warning("Analyzer: detect_enemy_red failed: %s", e)
            return False

    def detect_enemy_map_bearing(self, frame) -> dict:
        """Scan the MINIMAP crop for red enemy icons → relative bearing and distance.

        Pure HSV mask + connected components — no OCR, runs synchronously on
        the calling thread (same cost class as detect_enemy_red). The circle
        mask is cached per crop geometry. Fail-safe: missing crop or any
        exception returns bearing/radius None with zero counts.
        Design 003 / ADR 028.
        """
        empty = {"bearing_deg": None, "radius_frac": None, "blob_count": 0, "pixel_count": 0}
        if "MINIMAP" not in self.crops:
            return empty
        try:
            crop = get_crop(frame, *self.crops["MINIMAP"][:4])
            height, width = crop.shape[:2]
            cache = self._minimap_circle_cache
            if cache is None or cache[0] != width or cache[1] != height:
                radius_px = self._minimap_mask_radius_frac * min(width, height) / 2.0
                cache = (width, height, _minimap_circle_mask(width, height, radius_px))
                self._minimap_circle_cache = cache
            bearing_deg, radius_frac, blob_count, pixel_count = _scan_minimap_red(
                crop,
                self._enemy_hsv_lower,
                self._enemy_hsv_upper,
                self._minimap_mask_radius_frac,
                self._minimap_min_blob_px,
                self._minimap_max_blob_px,
                circle_mask=cache[2],
            )
            return {
                "bearing_deg": bearing_deg,
                "radius_frac": radius_frac,
                "blob_count": blob_count,
                "pixel_count": pixel_count,
            }
        except Exception as e:
            logger.warning("Analyzer: detect_enemy_map_bearing failed: %s", e)
            return empty

    def detect_map_boundary(self, frame) -> "tuple | None":
        """Nearest map-boundary point on the minimap, or None.

        Design 010, INSTRUMENTATION ONLY — nothing steers on this yet. Returns
        ``(nearest_dist_frac, forward_offset_frac, lateral_offset_frac)`` in
        units of the minimap radius, measured from the aircraft at the centre.
        Forward is along the nose (up): positive means the boundary lies ahead.
        Lateral is across it: positive means the nearest point is to the RIGHT.

        Measured on the Design 010 frames, across three different maps
        including a night one: Step0 (flying away) 0.59 / -0.59, Step1 (about
        to cross) 0.10 / +0.09, Step2 (outside) 0.62 / +0.61. The boundary is a
        HUD overlay drawn at a constant colour — hue 16.9-18.5 while the map
        background ranged V 66.6-118.4 — so the mask does not care which map is
        loaded.
        """
        if self.crops is None or "MINIMAP" not in self.crops:
            return None
        try:
            crop = get_crop(frame, *self.crops["MINIMAP"][:4])
            height, width = crop.shape[:2]
            radius = min(width, height) / 2.0
            if radius <= 0:
                return None
            hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
            mask = cv2.inRange(hsv, self._boundary_hsv_lower, self._boundary_hsv_upper)
            cache = self._minimap_circle_cache
            if cache is None or cache[0] != width or cache[1] != height:
                r_px = self._minimap_mask_radius_frac * radius
                cache = (width, height, _minimap_circle_mask(width, height, r_px))
                self._minimap_circle_cache = cache
            mask &= cache[2]
            # ADR 108: reconnect the line before measuring it. The mask finds
            # the boundary — 550 to 1400 px of it on the nine 2026-09-03
            # crossing frames — but MetalStorm's minimap update left it thin and
            # antialiased, so it arrives as 20 to 174 fragments. The span filter
            # below then rejects every one of them, and the detector read
            # NOTHING on 81% of ticks: five of eight crossings that session had
            # no boundary reading in the 30 s before they happened.
            mask = cv2.morphologyEx(
                (mask > 0).astype(np.uint8) * 255, cv2.MORPH_CLOSE,
                self._boundary_close_kernel,
                iterations=self._boundary_close_iters)
            n_labels, labels, stats, _ = cv2.connectedComponentsWithStats(
                (mask > 0).astype(np.uint8), connectivity=8)
            # Pick the most line-LIKE component, not the largest. Spatial
            # coherence alone is satisfied by a big terrain blob, and on the new
            # minimap that is what the largest component often is.
            #
            # The test is the component's LOCAL thickness — the largest
            # distance-to-background inside it. The boundary is a stroke of
            # fixed width, so this stays ~1.4 px however long or curved it runs;
            # a landmass is thick in the middle whatever its outline does.
            #
            # Two weaker tests were tried first and both failed on real data.
            # Bounding-box FILL rejects a straight line, which fills its own
            # thin box completely — caught by the synthetic tests. Aggregate
            # thinness (area / span^2) passes a large irregular blob, because a
            # ragged outline inflates the span: on 2026-09-03 desert terrain
            # read 0.334 against a 0.5 gate and produced 32 turns in 12 minutes,
            # one at round start with the aircraft nowhere near an edge.
            #
            # Measured on the live corpus: the real line runs 1.4-9.2 px and
            # terrain 34.5-50.2 px. Expressed as a fraction of the minimap
            # radius so it does not depend on capture resolution.
            min_span = self._boundary_min_span_frac * radius
            max_thick_px = self._boundary_max_thickness_frac * radius
            best = None
            for i in range(1, n_labels):
                span = max(stats[i, cv2.CC_STAT_WIDTH],
                           stats[i, cv2.CC_STAT_HEIGHT])
                if span < min_span or span <= 0:
                    continue
                comp = (labels == i).astype(np.uint8)
                if cv2.distanceTransform(comp, cv2.DIST_L2, 3).max() > max_thick_px:
                    continue
                if best is None or span > best[0]:
                    best = (span, i)
            if best is None:
                return None
            ys, xs = np.nonzero(labels == best[1])
            if len(xs) < self._boundary_min_px:
                return None
            cx = (width - 1) / 2.0
            cy = (height - 1) / 2.0
            dx = xs - cx
            dy = ys - cy
            dist = np.hypot(dx, dy)
            i = int(np.argmin(dist))
            # ADR 122: the LATERAL component too, positive to the right of the
            # nose. It was computed here and thrown away, which left the turn
            # with no way to know which side the edge was on — so it always
            # rolled right, and half of those rolls turned INTO the boundary.
            return (float(dist[i] / radius), float(-dy[i] / radius),
                    float(dx[i] / radius))
        except Exception as e:
            logger.warning("Analyzer: detect_map_boundary failed: %s", e)
            return None

    def detect_return_to_battle(self, frame) -> bool:
        """True while the RETURN TO BATTLE banner is on screen (Design 010).

        Colour test rather than OCR: the plate separates at 0.390 red fraction
        against 0.000-0.007 on frames without it, and instrumentation must not
        add an OCR crop to the tick budget. EJECTED occupies the same position
        with a dark plate and does NOT match, which is the distinction that
        matters — one is a warning, the other is the outcome.
        """
        try:
            x1, y1, x2, y2 = self._rtb_region
            h, w = frame.shape[:2]
            crop = frame[int(y1 * h):int(y2 * h), int(x1 * w):int(x2 * w)]
            if crop.size == 0:
                return False
            hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
            m = cv2.inRange(hsv, np.array([0, 90, 60], np.uint8),
                            np.array([10, 255, 200], np.uint8))
            m |= cv2.inRange(hsv, np.array([170, 90, 60], np.uint8),
                             np.array([179, 255, 200], np.uint8))
            frac = float((m > 0).mean())
            # Logged so min_red_frac can be tuned from the distribution of real
            # and false triggers rather than from the four calibration frames.
            # Raising it would cut wasted confirmations but risks MISSES, which
            # are the invisible failure here — OCR only ever retracts, never
            # adds — so it is left permissive and arbitrated instead.
            if frac >= self._rtb_min_frac:
                logger.debug("RTB: red_frac=%.3f (threshold %.3f)",
                             frac, self._rtb_min_frac)
            return bool(frac >= self._rtb_min_frac)
        except Exception as e:
            logger.warning("Analyzer: detect_return_to_battle failed: %s", e)
            return False

    def confirm_return_to_battle_async(self, frame, on_result) -> None:
        """OCR the banner ONCE per crossing to confirm the colour verdict.

        The colour test decides in 0.07 ms and drives the counting; this is the
        audit trail behind it, so a future red HUD element cannot silently
        inflate the crossing count. Submitted to the OCR pool rather than run
        inline: a read costs ~340 ms at p95, which is a fifth of the tick budget
        and must never land on the tick that detected the crossing.
        """
        try:
            # A NARROWER crop than the colour test uses. The colour test wants
            # the whole plate for a stable red fraction; the OCR only needs
            # enough of the middle to match a partial token, and the middle
            # slice reads in 63 ms against 123 ms for the full banner. Narrower
            # still (115 px) works for this banner but is not adopted: the
            # countdown digit shifts the centring, and there is only one banner
            # frame to validate against.
            x1, y1, x2, y2 = self._rtb_ocr_region
            h, w = frame.shape[:2]
            crop = frame[int(y1 * h):int(y2 * h), int(x1 * w):int(x2 * w)].copy()
            if crop.size == 0:
                return
            executor = self.ocr_executor
            if executor is None:
                return
            # Partial tokens, as the incoming crop does with MING / ARNING. A
            # narrowed crop degrades characters at the edges — measured reads
            # include 'ETURNTOBATTLE:' and 'JRNTOBATTE' — so matching the full
            # string would reject banners that are plainly present.
            fut = executor.submit(_process_text_region, crop,
                                  self._rtb_tokens)
            fut.add_done_callback(
                lambda f: on_result(f.result()[0], f.result()[2]))
        except Exception as e:
            logger.debug("Analyzer: RTB confirmation skipped: %s", e)

    def detect_friendly_map_components(self, frame) -> "list | None":
        """Friendly / objective minimap icons, same polar form as the enemy scan.

        ADR 028 revision 4. The enemy scan answers "where is the fight?"; this
        answers "where is the fight when nothing red is visible?", which on the
        measured frames is 57% of battle ticks. Same return shape, so the
        navigator can bin it through the identical ring logic.
        """
        if self.crops is None or "MINIMAP" not in self.crops:
            return None
        try:
            crop = get_crop(frame, *self.crops["MINIMAP"][:4])
            height, width = crop.shape[:2]
            cache = self._minimap_circle_cache
            if cache is None or cache[0] != width or cache[1] != height:
                radius_px = self._minimap_mask_radius_frac * min(width, height) / 2.0
                cache = (width, height, _minimap_circle_mask(width, height, radius_px))
                self._minimap_circle_cache = cache
            return _scan_minimap_components(
                crop,
                self._friendly_hsv_lower,
                self._friendly_hsv_upper,
                self._minimap_mask_radius_frac,
                self._minimap_min_blob_px,
                self._minimap_max_blob_px,
                circle_mask=cache[2],
                hue_wraps=False,   # green does not straddle the hue origin
            )
        except Exception as e:
            logger.warning("Analyzer: detect_friendly_map_components failed: %s", e)
            return None

    def detect_enemy_map_components(self, frame) -> "list | None":
        """Per-component polar scan of the MINIMAP crop (Design 003 revision 3).

        Returns a list of ``(bearing_deg, radius_frac, area_px)`` tuples —
        possibly empty (scan worked, nothing red) — or ``None`` when the crop
        is missing or the scan raises (fail-safe). Ring binning is
        policy-side: ``engage_nav.bin_rings``.
        """
        if "MINIMAP" not in self.crops:
            return None
        try:
            crop = get_crop(frame, *self.crops["MINIMAP"][:4])
            height, width = crop.shape[:2]
            cache = self._minimap_circle_cache
            if cache is None or cache[0] != width or cache[1] != height:
                radius_px = self._minimap_mask_radius_frac * min(width, height) / 2.0
                cache = (width, height, _minimap_circle_mask(width, height, radius_px))
                self._minimap_circle_cache = cache
            return _scan_minimap_components(
                crop,
                self._enemy_hsv_lower,
                self._enemy_hsv_upper,
                self._minimap_mask_radius_frac,
                self._minimap_min_blob_px,
                self._minimap_max_blob_px,
                circle_mask=cache[2],
            )
        except Exception as e:
            logger.warning("Analyzer: detect_enemy_map_components failed: %s", e)
            return None

    def scan_region_for_good_luck(self, frame) -> bool:
        """Synchronously scan the good_luck crop for 'Good Luck' text via OCR pool.

        Args:
            frame: Full BGR frame from screen capture.

        Returns:
            True if 'Good Luck' text is detected.
        """
        executor = self.ocr_executor
        if executor is None:
            logger.warning("Analyzer: OCR executor not available for Good Luck scan")
            return False
        try:
            region_frame = get_crop(frame, *self.crops["good_luck"][:4])
            # timeout=120: all 13 workers initialize serially under _ocr_init_lock
            # (~6-8 s each = up to 104 s total). A 30 s timeout silently drops
            # detections when the assigned worker is still queued for init.
            detected, _, text = executor.submit(
                _process_text_region, region_frame, self.crops["good_luck"].text or []
            ).result(timeout=120)
            if detected:
                logger.info("Analyzer: 'Good Luck' detected in good_luck crop (text='%s')", text)
            else:
                logger.debug("Analyzer: 'Good Luck' not found in good_luck crop")
            return detected
        except Exception as e:
            logger.warning("Analyzer: Good Luck scan failed: %r", e)
            return False

    def scan_region_for_event_refresh(self, frame) -> bool:
        """Synchronously scan the event_refresh crop for 'Event refresh in progress' popup.

        Detects the popup by looking for 'again so' / 'AGAIN' in the OCR output,
        which appears in the 'try again so...' message of the popup.

        Args:
            frame: Full BGR frame from screen capture.

        Returns:
            True if the event refresh popup is detected.
        """
        executor = self.ocr_executor
        if executor is None:
            logger.warning("Analyzer: OCR executor not available for event refresh scan")
            return False
        try:
            region_frame = get_crop(frame, *self.crops["event_refresh"][:4])
            detected, _, text = executor.submit(
                _process_text_region, region_frame, self.crops["event_refresh"].text or []
            ).result(timeout=30)
            if detected:
                logger.info("Analyzer: 'Event refresh' popup detected in event_refresh crop (text='%s')", text)
            else:
                logger.debug("Analyzer: Event refresh popup not found in event_refresh crop")
            return detected
        except Exception as e:
            logger.warning("Analyzer: Event refresh scan failed: %s", e)
            return False

    def scan_region_for_play_button(self, frame) -> "str | None":
        """Scan PLAY, READY, and UNREADY crops in parallel.

        Returns the crop name (PLAY or READY) if the play button is detected,
        or None if neither is found or UNREADY is detected (suppresses clicking).
        """
        executor = self.ocr_executor
        if executor is None:
            logger.warning("Analyzer: OCR executor not available for play button scan")
            return None
        try:
            scan_crops = [c for c in ("PLAY", "READY", "UNREADY") if c in self.crops]
            futures = {
                crop: executor.submit(_process_text_region, get_crop(frame, *self.crops[crop][:4]), self.crops[crop].text or [])
                for crop in scan_crops
            }
            # Resolve UNREADY first; if detected, suppress clicking entirely
            if "UNREADY" in futures:
                detected, _, text = futures["UNREADY"].result(timeout=30)
                if detected:
                    # ADR 084: track dwell here rather than per-state. Returning
                    # None also makes _classify_unknown_state fail, so a stuck
                    # UNREADY strands the FSM in GAME_UNKNOWN indefinitely — the
                    # dwell has to accumulate across whatever state we are in.
                    if self._unready_since == 0.0:
                        self._unready_since = time.time()
                    logger.info(
                        "Analyzer: UNREADY detected (text='%s') — suppressing PLAY click (%.0fs)",
                        text, time.time() - self._unready_since)
                    return None
                self._unready_since = 0.0
                logger.debug("Analyzer: UNREADY not found")
            for crop in ("PLAY", "READY"):
                if crop not in futures:
                    continue
                detected, _, text = futures[crop].result(timeout=30)
                if detected:
                    logger.info("Analyzer: '%s' detected in %s crop (text='%s')", crop, crop, text)
                    return crop
                logger.debug("Analyzer: '%s' not found in %s crop", crop, crop)
            return None
        except Exception as e:
            logger.warning("Analyzer: Play button scan failed: %s", e)
            return None

    def scan_region_for_cancel(self, frame) -> bool:
        """Synchronously scan the CANCEL crop to confirm matchmaking is active.

        Returns True if the CANCEL button is visible (player is in the matchmaking
        queue), False if not found or the crop is not configured.
        """
        if "CANCEL" not in self.crops:
            logger.debug("Analyzer: CANCEL crop not configured — skipping scan")
            return False
        executor = self.ocr_executor
        if executor is None:
            logger.warning("Analyzer: OCR executor not available for CANCEL scan")
            return False
        try:
            region_frame = get_crop(frame, *self.crops["CANCEL"][:4])
            detected, _, text = executor.submit(
                _process_text_region, region_frame, self.crops["CANCEL"].text or []
            ).result(timeout=30)
            if detected:
                logger.info("Analyzer: CANCEL button detected (text='%s') → matchmaking active", text)
            else:
                logger.debug("Analyzer: CANCEL button not found in CANCEL crop")
            return detected
        except Exception as e:
            logger.warning("Analyzer: CANCEL scan failed: %s", e)
            return False

