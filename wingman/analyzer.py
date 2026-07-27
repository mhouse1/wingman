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

from .crop_region import get_crop, load_crops, draw_crops


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

try:
    import easyocr
except ImportError:
    easyocr = None

logger = logging.getLogger(__name__)

_DEFAULT_INCOMING_TEMPLATE_SOURCES = (
    "test_screenshots/INCOMING3.png",
)
_DEFAULT_INCOMING_TEMPLATE_SCALES = (1.0,)


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

# Set from config at startup by GameStateAnalyzer.__init__.
# False (default) skips the failed GPU probe and goes straight to CPU init.
_use_gpu: bool = False


def _get_thread_ocr_reader():
    """Return the EasyOCR reader for the current thread, initializing it on first call."""
    if not getattr(_thread_local, 'reader', None):
        with _ocr_init_lock:
            _thread_local.reader = None
            if easyocr:
                try:
                    _thread_local.reader = easyocr.Reader(['en'], gpu=_use_gpu, verbose=False)
                    mode = "GPU" if _use_gpu else "CPU"
                    logger.info("OCR thread %d: initialized EasyOCR reader (%s)", threading.get_ident(), mode)
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

    for label, text_clean, conf in results:
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
        if "MING" in normalized or ("ARNING" in normalized and len(normalized) >= 6):
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


def _process_health_region(health_frame) -> "tuple[int | None, float]":
    """Extract the numeric health value from the health crop via OCR.

    Upscales and thresholds the crop to maximise digit legibility, then strips
    all non-digit characters from the OCR output.

    Args:
        health_frame: numpy array (BGR) — the extracted health crop region.

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

    for img in (binary, upscaled):
        results = reader.readtext(img, detail=0, paragraph=False, workers=0)
        digits = "".join(c for r in results for c in str(r) if c.isdigit())
        if digits:
            return (int(digits), time.time() - t_start)

    return (None, time.time() - t_start)


def _row_value(boxes) -> "int | None":
    """Parse the leading number from one row of x-ordered OCR boxes.

    Boxes are joined left-to-right into a single string, and the first contiguous
    digit run is taken as the value. Because the numbers are left-aligned and the
    'MPH'/'feet' labels follow them, this yields the number while ignoring the
    trailing label — even when the label lands in the same detection box (e.g.
    '27681 feet' → 27681). Returns None when the row contains no digits.
    """
    text = " ".join(str(t) for _, _, t in sorted(boxes, key=lambda b: b[1]))
    match = re.search(r"\d+", text)
    return int(match.group()) if match else None


def _split_telemetry_rows(ocr_results, img_height) -> "tuple[int | None, int | None]":
    """Split detail=1 OCR boxes from the ALTITUDE_SPEED crop into (speed, altitude).

    The crop holds two left-aligned numeric lines — speed (MPH) on top, altitude
    (feet) below. Boxes are grouped into an upper (speed) and lower (altitude) row
    by bounding-box vertical centre, then each row's leading number is parsed. When
    only one line is visible (small vertical spread), it is assigned by the half of
    the crop it occupies. Returns (speed, altitude), each None when that row
    produced no digits. See ADR 038.
    """
    boxes = []
    for bbox, text, _conf in ocr_results:
        if not any(c.isdigit() for c in str(text)):
            continue
        ys = [pt[1] for pt in bbox]
        xs = [pt[0] for pt in bbox]
        boxes.append((sum(ys) / len(ys), min(xs), str(text)))
    if not boxes:
        return (None, None)

    y_centers = [b[0] for b in boxes]
    spread = max(y_centers) - min(y_centers)

    # Single visible line: assign by which half of the crop it sits in.
    if spread < img_height * 0.25:
        value = _row_value(boxes)
        line_y = sum(y_centers) / len(y_centers)
        return (value, None) if line_y < img_height / 2.0 else (None, value)

    mid_row = (max(y_centers) + min(y_centers)) / 2.0
    speed_boxes = [b for b in boxes if b[0] <= mid_row]
    alt_boxes = [b for b in boxes if b[0] > mid_row]
    return (_row_value(speed_boxes) if speed_boxes else None,
            _row_value(alt_boxes) if alt_boxes else None)


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
    gray = cv2.cvtColor(telemetry_frame, cv2.COLOR_BGR2GRAY)
    upscaled = cv2.resize(gray, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    _, binary = cv2.threshold(upscaled, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    for img in (binary, upscaled):
        results = reader.readtext(img, detail=1, paragraph=False, workers=0)
        speed, altitude = _split_telemetry_rows(results, img.shape[0])
        if speed is not None or altitude is not None:
            return (speed, altitude, time.time() - t_start)

    return (None, None, time.time() - t_start)


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


def _process_crop_region(frame, crop_coords, text_tokens):
    """Extract crop and run text detection entirely inside a worker thread.

    Wrapping get_crop() here ensures it is covered by future.result(timeout=N)
    in the lobby quick-scan thread; a synchronous call in the submission loop
    would have no timeout protection and can block indefinitely.
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
    {"trigger": "starting_stalled_reclassify", "source": "GAME_STARTING_STALLED", "dest": "GAME_UNKNOWN"},
    {"trigger": "starting_recovery",  "source": "GAME_STARTING_STALLED", "dest": "GAME_STARTING"},
    {"trigger": "starting_give_up",   "source": "GAME_STARTING_STALLED", "dest": "GAME_LOBBY"},
    {"trigger": "click_to_detected",  "source": ["GAME_BATTLE", "GAME_BATTLE_MANUAL", "GAME_BATTLE_EJECT"], "dest": "GAME_END_B"},
    {"trigger": "manual_takeover",    "source": ["GAME_BATTLE", "GAME_BATTLE_EJECT"], "dest": "GAME_BATTLE_MANUAL"},
    {"trigger": "respawn_reset",      "source": "GAME_BATTLE_MANUAL",     "dest": "GAME_BATTLE"},
    {"trigger": "eject_started",      "source": "GAME_BATTLE",            "dest": "GAME_BATTLE_EJECT"},
    {"trigger": "eject_complete",     "source": "GAME_BATTLE_EJECT",      "dest": "GAME_BATTLE"},
    {"trigger": "manual_force_battle", "source": "*",                    "dest": "GAME_BATTLE"},
    {"trigger": "manual_reset",       "source": "*",                     "dest": "GAME_LOBBY"},
    {"trigger": "continue_clicked",   "source": ["GAME_END_B", "GAME_BATTLE_MANUAL"], "dest": "GAME_LOBBY"},
    {"trigger": "respawn_detected",   "source": "GAME_END_B",            "dest": "GAME_BATTLE"},
]


# ============================================================================
# GameStateAnalyzer Class
# ============================================================================


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
        mission_cfg = config.get("mission", {})
        # Respawn detection config
        respawn_cfg = config.get("respawn_detection", {})

        # GPU flag — propagated to module-level so worker threads pick it up at init time
        global _use_gpu
        _use_gpu = bool(respawn_cfg.get("use_gpu", False))
        logger.info("OCR mode: %s", "GPU" if _use_gpu else "CPU")

        # OCR-based respawn detection (looks for "RESPAWN" text)
        self.use_ocr = respawn_cfg.get("use_ocr", True)

        # Named percentage-coordinate crop regions (ADR 023)
        self.crops = load_crops(config.get("crops", {}))

        # Enemy HSV range for red-color detection in ENEMY_CLOSE_BY crop
        enemy_hsv_cfg = config.get("enemy_hsv", {})
        self._enemy_hsv_lower = np.array(enemy_hsv_cfg.get("lower", [0, 120, 120]), dtype=np.uint8)
        self._enemy_hsv_upper = np.array(enemy_hsv_cfg.get("upper", [10, 255, 255]), dtype=np.uint8)
        
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
        }
        self._incoming_cache_lock = threading.Lock()
        incoming_cfg = config.get("incoming_detection", {})
        self._incoming_template_matching_enabled = bool(incoming_cfg.get("incoming_template_matching_enabled", True))
        self._incoming_template_threshold = float(incoming_cfg.get("incoming_template_threshold", 0.82))
        self._incoming_template_near_threshold_low = float(incoming_cfg.get("incoming_template_near_threshold_low", 0.76))
        self._incoming_template_near_threshold_high = float(incoming_cfg.get("incoming_template_near_threshold_high", 0.81))
        self._incoming_template_fallback_to_ocr = bool(incoming_cfg.get("incoming_template_fallback_to_ocr", True))
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
        self._on_cancel_mission = None           # injected by main.py after construction
        self._on_start_game_starting_loop = None  # injected by main.py after construction
        self._on_lobby_play_click = None         # injected by main.py; called with crop name when PLAY/READY detected
        self._on_lobby_popup_click = None        # injected by main.py; called with popup crop name when a popup is detected
        self._on_lobby_stall = None              # injected by main.py; called when no lobby crops detected for 10s
        self._on_fsm_transition = None           # injected by main.py; called after successful state transitions
        self._on_respawn_detected = None          # injected by main.py; called with the OCR frame when RESPAWN is first detected
        self._unknown_debounce_required = max(1, int(startup_cfg.get("debounce_consecutive_required", 2)))
        self._unknown_candidate_state: "GameState | None" = None
        self._unknown_candidate_count = 0

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
        self._health_window: deque = deque(maxlen=HEALTH_WINDOW_SIZE)
        self._health_ceiling: "int | None" = None
        # Signalled when _game_battle_alive transitions False → True.
        # The main loop waits on this event to restart the mission immediately.
        self.alive_event = threading.Event()
        # Set by _start_game_starting_loop after the 10-second gate to enable the
        # GAME_STARTING health-only OCR scan (ADR 032 battle-alive fallback).
        self._game_starting_health_scan_enabled = threading.Event()

        # Ammo sub-state (GAME_BATTLE only)
        self._ammo_flares: "int | None" = None   # Last known flare count from OCR
        self._ammo_missiles: "int | None" = None  # Last known missile count from OCR
        self._ammo_lock = threading.Lock()
        # Signalled when flares == 2 (reload needed) or missiles == 0 (end mission).
        self.low_flares_event = threading.Event()
        self.no_missiles_event = threading.Event()

        # Flight telemetry (GAME_BATTLE only) — speed (MPH) and altitude (feet),
        # read together from the combined ALTITUDE_SPEED crop (ADR 038).
        self._speed: "int | None" = None
        self._altitude: "int | None" = None
        self._telemetry_lock = threading.Lock()

        # Static frame detection: two consecutive identical incoming_region frames → GAME_END

        # Thread pool executor for parallel OCR processing
        # Use 3 workers: one each for respawn, incoming, and click_to detection
        self._ocr_executor = None
        self._ocr_executor_initialized = False
        self._background_ocr_frame = None
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
                if next_state in (GameState.GAME_LOBBY, GameState.GAME_BATTLE_MANUAL) and self._on_cancel_mission:
                    post_callbacks.append(self._on_cancel_mission)
                if next_state == GameState.GAME_STARTING and self._on_start_game_starting_loop:
                    post_callbacks.append(self._on_start_game_starting_loop)

        for callback in post_callbacks:
            try:
                callback()
            except Exception:
                logger.exception("FSM: post-transition callback failed for trigger '%s'", trigger_name)

        if transitioned and next_state != prev_state and self._on_fsm_transition is not None:
            try:
                self._on_fsm_transition(trigger_name, prev_state.name, next_state.name, time.time())
            except Exception:
                logger.exception("FSM: transition callback failed for trigger '%s'", trigger_name)
        return transitioned

    # ------------------------------------------------------------------
    # Orchestration public API (ADR 039)
    # ------------------------------------------------------------------

    def set_on_cancel_mission(self, callback):
        """Set callback invoked after transitions that must cancel mission logic."""
        self._on_cancel_mission = callback

    def set_on_start_game_starting_loop(self, callback):
        """Set callback invoked when entering GAME_STARTING."""
        self._on_start_game_starting_loop = callback

    def set_on_lobby_play_click(self, callback):
        """Set callback invoked with PLAY/READY crop names from lobby quick-scan."""
        self._on_lobby_play_click = callback

    def set_on_lobby_popup_click(self, callback):
        """Set callback invoked with popup crop names from lobby quick-scan."""
        self._on_lobby_popup_click = callback

    def set_on_lobby_stall(self, callback):
        """Set callback invoked when no lobby crops detected for 10s (stall recovery)."""
        self._on_lobby_stall = callback

    def set_on_fsm_transition(self, callback):
        """Set callback invoked after successful FSM transitions.

        Callback signature: (trigger_name, prev_state_name, next_state_name, timestamp_s).
        """
        self._on_fsm_transition = callback

    def set_on_respawn_detected(self, callback):
        """Set callback invoked when RESPAWN OCR succeeds.

        Callback signature: (frame: np.ndarray) where frame is the full BGR frame
        used for the OCR scan.  Called from the background OCR thread.
        """
        self._on_respawn_detected = callback

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

    def get_speed(self):
        """Return the latest speed (MPH) snapshot from telemetry OCR."""
        if not self._telemetry_lock.acquire(timeout=1.0):
            logger.warning("get_speed: _telemetry_lock timeout — returning stale/no value")
            return None
        try:
            return self._speed
        finally:
            if self._telemetry_lock.locked():
                self._telemetry_lock.release()

    def get_altitude(self):
        """Return the latest altitude (feet) snapshot from telemetry OCR."""
        if not self._telemetry_lock.acquire(timeout=1.0):
            logger.warning("get_altitude: _telemetry_lock timeout — returning stale/no value")
            return None
        try:
            return self._altitude
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
        logger.info("Analyzer: health spike filter reset for respawn")

    def on_enter_GAME_BATTLE_MANUAL(self):
        logger.info("FSM: entering GAME_BATTLE_MANUAL — manual takeover active, auto-restart suppressed")

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

        # Start background threads once on first frame after unknown-state classification.
        if not self._click_to_thread_started:
            self._click_to_thread_started = True
            threading.Thread(target=self._run_click_to_in_background, daemon=True).start()
            logger.debug("Click-to background thread started")
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
        else:
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
        if self.game_state in (GameState.GAME_END_B, GameState.GAME_LOBBY,
                               GameState.GAME_WAITING, GameState.GAME_STARTING):
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
                    )
                    health_future = executor.submit(_process_health_region, health_frame) if health_frame is not None else None
                    ammo_flares_future = executor.submit(_process_health_region, ammo_flares_frame) if ammo_flares_frame is not None else None
                    ammo_missile_future = executor.submit(_process_health_region, ammo_missile_frame) if ammo_missile_frame is not None else None
                    telemetry_future = executor.submit(_process_telemetry_region, telemetry_frame) if telemetry_frame is not None else None
                    t2 = time.time()

                    # Wait for respawn result first — update its cache immediately so the
                    # main loop can react without waiting for the (often slower) incoming OCR.
                    respawn_detected, respawn_ocr_time, respawn_text = respawn_future.result(timeout=120)
                    if self._tracker:
                        self._tracker.record_ocr_crop("respawn", respawn_ocr_time)

                    if respawn_detected:
                        logger.debug("Analyzer: detected 'RESPAWN' text (matched: '%s')", respawn_text)
                        if self.game_state == GameState.GAME_END_B:
                            self._trigger("respawn_detected")
                        if self._on_respawn_detected is not None:
                            try:
                                self._on_respawn_detected(full_frame)
                            except Exception:
                                logger.exception("Analyzer: on_respawn_detected callback error")

                    respawn_result = (True, 1.0, "ocr") if respawn_detected else (False, 0.0, None)
                    with self._ocr_cache_lock:
                        self._ocr_cache['result'] = respawn_result
                        self._ocr_cache['timestamp'] = current_time

                    # Now wait for incoming — its result is independent of respawn.
                    incoming_eval = incoming_future.result(timeout=120)
                    incoming_processing_time = float(incoming_eval.get("processing_time", 0.0))
                    if self._tracker:
                        self._tracker.record_ocr_crop("incoming", incoming_processing_time)
                    t3 = time.time()

                    template_score = float(incoming_eval.get("template_score", -1.0))
                    template_hit = bool(incoming_eval.get("template_hit", False))
                    near_threshold = bool(incoming_eval.get("near_threshold", False))
                    template_label = incoming_eval.get("template_label")
                    fallback_used = bool(incoming_eval.get("fallback_used", False))
                    fallback_hit = bool(incoming_eval.get("fallback_hit", False))
                    fallback_variant = incoming_eval.get("fallback_variant")
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

                    if template_hit:
                        incoming_detected = True
                        detection_source = "template"
                        detected_label = template_label
                    elif near_threshold_confirmation:
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
                    if incoming_detected:
                        self.incoming_event.set()

                    # Wait for health result and update sub-state.
                    health_ocr_time = 0.0
                    if health_future is not None:
                        health_value, health_ocr_time = health_future.result(timeout=120)
                        if self._tracker:
                            self._tracker.record_ocr_crop("health", health_ocr_time)
                        if health_value is not None:
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
                            logger.info("Health: %s | alive=%s", health_value, alive)
                            # Signal False → True transition for immediate mission restart.
                            if alive and not prev_alive:
                                logger.info("Analyzer: health alive transition False→True — resetting health ceiling")
                                with self._health_lock:
                                    self._health_window.clear()
                                    self._health_ceiling = None
                                self.alive_event.set()
                        else:
                            # No digits — only clear alive flag after 3 s of consecutive misses.
                            now_t = time.time()
                            with self._health_lock:
                                no_digits_since = self._health_no_digits_since
                                if no_digits_since == 0.0:
                                    self._health_no_digits_since = now_t
                                    no_digits_since = now_t
                            if no_digits_since == now_t:
                                logger.debug("Analyzer: Health OCR returned no digits (grace timer started)")
                            elif now_t - no_digits_since >= 3.0:
                                with self._health_lock:
                                    self._game_battle_alive = False
                                logger.debug(
                                    "Analyzer: Health OCR no digits for %.1fs → game_battle_alive=False",
                                    now_t - no_digits_since)
                            else:
                                logger.debug(
                                    "Analyzer: Health OCR no digits (%.1fs elapsed, 3s threshold)",
                                    now_t - no_digits_since)
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

                    if respawn_detected:
                        with self._ammo_lock:
                            self._ammo_flares = None
                            self._ammo_missiles = None
                        self.low_flares_event.clear()
                        self.no_missiles_event.clear()
                        logger.debug("Analyzer: skipping ammo updates while respawn is detected")
                    else:
                        if flares_value is not None:
                            with self._ammo_lock:
                                self._ammo_flares = flares_value
                            logger.info("Ammo flares: %d", flares_value)
                            if flares_value == 2:
                                self.low_flares_event.set()
                        if missile_value is not None:
                            with self._ammo_lock:
                                self._ammo_missiles = missile_value
                            logger.info("Ammo missiles: %d", missile_value)
                            if missile_value == 0:
                                self.no_missiles_event.set()

                    # Resolve telemetry future — store speed/altitude snapshots.
                    # Only overwrite on a successful read so a momentary miss (e.g.
                    # during respawn) keeps the last known value rather than nulling it.
                    telemetry_ocr_time = 0.0
                    if telemetry_future is not None:
                        speed_value, altitude_value, telemetry_ocr_time = telemetry_future.result(timeout=120)
                        with self._telemetry_lock:
                            if speed_value is not None:
                                self._speed = speed_value
                            if altitude_value is not None:
                                self._altitude = altitude_value
                        if speed_value is not None or altitude_value is not None:
                            logger.debug("Analyzer: Telemetry speed=%s altitude=%s",
                                         speed_value, altitude_value)

                    t4 = time.time()

                    # Log timing
                    logger.debug(
                        "Analyzer: Parallel OCR Timings - Extract: %.2fs, Submit: %.2fs | "
                        "Respawn OCR: %.2fs | Incoming OCR: %.2fs | Health OCR: %.2fs | "
                        "Flares OCR: %.2fs | Missiles OCR: %.2fs | Telemetry OCR: %.2fs | Total: %.2fs",
                        t1-t0, t2-t1, respawn_ocr_time, incoming_processing_time, health_ocr_time,
                        ammo_flares_ocr_time, ammo_missile_ocr_time, telemetry_ocr_time, t4-t0
                    )
                elif (state == GameState.GAME_STARTING
                        and self._game_starting_health_scan_enabled.is_set()
                        and "HEALTH" in self.crops):
                    # Health-only scan for the battle-alive fallback (ADR 032).
                    health_frame = get_crop(full_frame, *self.crops["HEALTH"][:4])
                    health_future = executor.submit(_process_health_region, health_frame)
                    health_value, _ = health_future.result(timeout=120)
                    if health_value is not None:
                        with self._health_lock:
                            health_value, self._health_ceiling = _apply_health_ceiling_filter(
                                health_value,
                                self._health_window,
                                self._health_ceiling,
                                HEALTH_WINDOW_SIZE,
                                HEALTH_SPIKE_FACTOR,
                                self._health,
                            )
                        if health_value is not None and health_value >= 1:
                            with self._health_lock:
                                prev_alive = self._game_battle_alive
                                self._health = health_value
                                self._game_battle_alive = True
                            logger.info(
                                "Analyzer: health %d detected in GAME_STARTING → game_battle_alive=True",
                                health_value)
                            if not prev_alive:
                                self.alive_event.set()
                else:
                    logger.debug("Skipping GAME_BATTLE crop OCR in %s state", state.name)
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
                logger.debug("Click-to OCR skipped: %s state active", state.name)
                continue
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
                    if self.game_state in (GameState.GAME_BATTLE, GameState.GAME_BATTLE_EJECT):
                        self._trigger("click_to_detected")
                    logger.debug("Analyzer: detected 'Click to' text (matched: '%s') → GAME_END_B", click_to_text)
            except RuntimeError:
                return  # executor shut down — exit the loop cleanly
            except Exception as e:
                if self._click_to_stop.is_set() or self._shutting_down or _is_shutdown_runtime_error(e):
                    logger.debug("Analyzer: click_to OCR loop exiting during shutdown: %s", e)
                    return
                logger.warning("Analyzer: click_to OCR failed: %s", e)

    def _run_game_lobby_quick_scan(self):
        """Scan lobby crops every 1s while in GAME_LOBBY or GAME_WAITING.

        Lobby crops and popup crops are submitted in separate batches so popup OCR
        can use a fresher frame than the one used for CANCEL / PLAY detection.

        Popup scan fires every 5s in both states unless a PLAY/READY click happened
        within the last 5s; CANCEL/PLAY scan fires every cycle in GAME_LOBBY;
        CANCEL alone is also scanned every cycle in GAME_WAITING so a brief CANCEL
        window is not missed between main-loop 3-second polling intervals.
        """
        lobby_crops = [c for c in ("CANCEL", "UNREADY", "PLAY", "READY") if c in self.crops]
        popup_crop_names = ["INVITED", "CREATION_FAILED", "REVEAL_ALL", "SILVER",
                            "UNLOCK_CLOSE", "INSPECT", "event_refresh"]
        popup_crops = [c for c in popup_crop_names if c in self.crops]

        if not lobby_crops and not popup_crops:
            logger.warning("Lobby quick-scan: no crops configured — thread exiting")
            return

        last_popup_scan_ts = 0.0
        lobby_stall_since = 0.0  # timestamp when stall (no crops detected) first started

        while not self._lobby_quick_scan_stop.wait(timeout=1.0):
            cycle_start = time.time()
            state = self.game_state
            if state != GameState.GAME_LOBBY:
                # Stall tracking only applies while continuously in GAME_LOBBY — clear it
                # here so a later re-entry (e.g. after a GAME_WAITING excursion) starts
                # counting fresh instead of comparing against a stale timestamp.
                lobby_stall_since = 0.0
            if state not in (GameState.GAME_LOBBY, GameState.GAME_WAITING):
                continue

            executor = self.ocr_executor
            if executor is None:
                continue

            try:
                # --- CANCEL / UNREADY / PLAY / READY ---
                # GAME_LOBBY: scan all lobby crops.
                # GAME_WAITING: scan CANCEL only — provides 1-second detection cadence
                # instead of relying solely on the 3-second main-loop poll, which can
                # miss a brief CANCEL window (e.g. squad-READY → match-found flow).
                lobby_futures = {}
                lobby_scan_start = None
                handled = False
                play_clicked_this_cycle = False

                crops_to_scan = (
                    lobby_crops if state == GameState.GAME_LOBBY
                    else [c for c in ("CANCEL",) if c in self.crops]
                )
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
                                lobby_futures[crop] = executor.submit(
                                    _process_crop_region,
                                    frame,
                                    self.crops[crop][:4],
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
                        else:
                            self.capture_waiting_cancel_baseline(frame)
                            logger.info(
                                "\033[93m📋 Lobby quick-scan: %s detected (text='%s') — clicking\033[0m",
                                crop, text,
                            )
                            self._last_lobby_play_click_ts = time.time()
                            if self._on_lobby_play_click:
                                self._on_lobby_play_click(crop, frame)
                            self._trigger("play_clicked")
                            handled = True
                            play_clicked_this_cycle = True
                        break

                    if not handled and lobby_futures:
                        if lobby_stall_since == 0.0:
                            lobby_stall_since = time.time()
                        elapsed_stall = time.time() - lobby_stall_since
                        logger.info(
                            "Lobby quick-scan: no lobby crops detected (stalled %.1fs)",
                            elapsed_stall,
                        )
                        if elapsed_stall >= 10.0 and self._on_lobby_stall is not None:
                            logger.info("Lobby quick-scan: stall threshold reached — pressing ESC")
                            try:
                                self._on_lobby_stall()
                            except Exception:
                                logger.exception("Lobby quick-scan: _on_lobby_stall callback failed")
                            lobby_stall_since = time.time()  # cooldown: next press after another 10s
                    elif handled:
                        lobby_stall_since = 0.0

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
                    and current_state_for_popup_gate in (GameState.GAME_LOBBY, GameState.GAME_WAITING)
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
                                popup_futures[crop] = executor.submit(
                                    _process_crop_region,
                                    popup_frame,
                                    self.crops[crop][:4],
                                    self.crops[crop].text or [],
                                )

                if popup_futures:
                    # Re-check state before blocking on results. If we've transitioned out of
                    # GAME_LOBBY / GAME_WAITING while futures were queued (e.g. PLAY was clicked
                    # and we're now in GAME_STARTING), cancel queued futures and skip this batch
                    # entirely to avoid holding up the executor for 50+ seconds.
                    current_state_for_popup = self.game_state
                    if current_state_for_popup not in (GameState.GAME_LOBBY, GameState.GAME_WAITING):
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
                                if self._on_lobby_popup_click:
                                    self._on_lobby_popup_click(crop)
                                popup_detected = True
                                break
                            logger.debug("Lobby quick-scan: popup '%s' not found", crop)
                        except Exception as e:
                            logger.warning(
                                "Lobby quick-scan: popup '%s' scan failed: %s: %s",
                                crop, type(e).__name__, e,
                            )

                    if popup_scan_start is not None:
                        logger.debug(
                            "Lobby quick-scan: popup batch completed in %.2fs%s",
                            time.time() - popup_scan_start,
                            " (detected)" if popup_detected else "",
                        )

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
                    logger.info("Analyzer: UNREADY detected (text='%s') — suppressing PLAY click", text)
                    return None
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

