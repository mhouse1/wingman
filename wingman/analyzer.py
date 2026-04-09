"""Game state analyzer for detecting respawn, enemies, and other game conditions."""

import logging
import cv2
import numpy as np
import threading
from concurrent.futures import ThreadPoolExecutor
import time
from enum import Enum, auto
from pathlib import Path

from .crop_region import get_crop, load_crops, draw_crops


class GameState(Enum):
    GAME_BATTLE          = auto()  # Active gameplay (default); respawn/incoming scanning active
    GAME_END_B           = auto()  # "Click to Continue" detected; clicking in progress
    GAME_LOBBY           = auto()  # Final continue (region 64) clicked; waiting in lobby
    GAME_STARTING        = auto()  # Play pressed; waiting for "Good Luck" before launching mission
    GAME_STARTING_STALLED = auto() # GAME_STARTING timed out without "Good Luck" detection

try:
    import easyocr
except ImportError:
    easyocr = None

logger = logging.getLogger(__name__)


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
    if not hasattr(_thread_local, 'reader'):
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


def _process_incoming_region(incoming_frame):
    """
    Worker function to process incoming missile region in a thread pool thread.

    Args:
        incoming_frame: numpy array (BGR) of the incoming region — passed by reference, no copy

    Returns:
        tuple: (detected: bool, ocr_time: float, variant_name: str or None, text_found: str or None)
    """
    reader = _get_thread_ocr_reader()
    if reader is None:
        return (False, 0.0, None, None)

    t_start = time.time()
    
    # Preprocess incoming region
    gray_incoming = cv2.cvtColor(incoming_frame, cv2.COLOR_BGR2GRAY)
    _, binary_incoming = cv2.threshold(gray_incoming, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    
    variants = {
        "gray_up_1p4": cv2.resize(gray_incoming, None, fx=1.4, fy=1.4, interpolation=cv2.INTER_CUBIC),
        "binary_otsu_up_1p4": cv2.resize(binary_incoming, None, fx=1.4, fy=1.4, interpolation=cv2.INTER_CUBIC),
    }
    
    # Try variants
    raw_texts = []
    for variant_name, variant_img in variants.items():
        results_incoming = reader.readtext(variant_img, detail=0, paragraph=True, workers=0)
        extracted_text = " ".join(str(result) for result in results_incoming)
        normalized = " ".join(extracted_text.upper().split()).replace(" ", "")
        if normalized:
            raw_texts.append(f"{variant_name}={normalized!r}")

        # Check for MING or WARNING (incoming missile text)
        if "MING" in normalized or ("ARNING" in normalized and len(normalized) >= 6):
            ocr_time = time.time() - t_start
            return (True, ocr_time, variant_name, normalized, raw_texts)

    ocr_time = time.time() - t_start
    return (False, ocr_time, None, None, raw_texts)


def _process_click_to_region(click_to_frame):
    """
    Worker function to process "Click to Continue" region in a thread pool thread.

    Args:
        click_to_frame: numpy array (BGR) — passed by reference, no copy

    Returns:
        tuple: (detected: bool, ocr_time: float, text_found: str or None)
    """
    reader = _get_thread_ocr_reader()
    if reader is None:
        return (False, 0.0, None)

    t_start = time.time()

    gray = cv2.cvtColor(click_to_frame, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    upscaled = cv2.resize(gray, None, fx=1.4, fy=1.4, interpolation=cv2.INTER_CUBIC)

    for img in (upscaled, binary):
        results = reader.readtext(img, detail=0, paragraph=True, workers=0)
        text = " ".join(str(r) for r in results).upper().replace(" ", "")
        if "CLICKTO" in text or "LICKTO" in text or "CLICK" in text:
            ocr_time = time.time() - t_start
            return (True, ocr_time, text)

    ocr_time = time.time() - t_start
    return (False, ocr_time, None)


def _process_event_refresh_region(frame):
    """Worker function to detect 'Event refresh in progress' popup text in a region.

    Scans for the substring 'AGAIN' (from 'try again so...') which appears in the
    'Event refresh in progress' popup that blocks game entry.

    Args:
        frame: numpy array (BGR) — the extracted grid region

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
        if "AGAINSO" in text or "AGAIN" in text:
            ocr_time = time.time() - t_start
            return (True, ocr_time, text)

    ocr_time = time.time() - t_start
    return (False, ocr_time, None)


def _process_good_luck_region(frame):
    """
    Worker function to detect 'Good Luck' text in a region in a thread pool thread.

    Args:
        frame: numpy array (BGR) — passed by reference, no copy

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
        if "GOODLUCK" in text or "GOOD" in text or "LUCK" in text:
            ocr_time = time.time() - t_start
            return (True, ocr_time, text)

    ocr_time = time.time() - t_start
    return (False, ocr_time, None)


def _process_play_button_region(frame):
    """Worker function to detect 'PLAY' or 'READY' text in play_button crop."""
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
        if "PLAY" in text or "READY" in text:
            ocr_time = time.time() - t_start
            return (True, ocr_time, text)

    ocr_time = time.time() - t_start
    return (False, ocr_time, None)


def _process_reveal_all_region(frame):
    """Worker function to detect 'REVEAL ALL' button text in REVEAL_ALL crop."""
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
        if "REVEAL" in text:
            return (True, time.time() - t_start, text)
    return (False, time.time() - t_start, None)


def _process_tap_here_region(frame):
    """Worker function to detect 'TAP HERE TO CONTINUE' text in TAP_HERE_TO_CONTINUE crop."""
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
        if "TAP" in text or "CONTINUE" in text:
            return (True, time.time() - t_start, text)
    return (False, time.time() - t_start, None)


def _process_unlock_close_region(frame):
    """Worker function to detect 'Close' button text in UNLOCK_CLOSE crop."""
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
        if "CLOSE" in text:
            return (True, time.time() - t_start, text)
    return (False, time.time() - t_start, None)


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


# ============================================================================
# GameStateAnalyzer Class
# ============================================================================


class GameStateAnalyzer:
    """Analyzes game screenshots to determine current game state."""
    
    def __init__(self, config):
        """
        Initialize analyzer with configuration.
        
        Args:
            config: Dict with HSV ranges and detection thresholds
        """
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
        
        # EasyOCR reader (lazy initialization on first use)
        self._ocr_reader = None
        
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
        self._click_to_frame_lock = threading.Lock()
        self._click_to_thread_started = False
        self._click_to_stop = threading.Event()

        self._game_end_b = False           # Set when "Click to Continue" is detected
        self._game_lobby = True            # Start in GAME_LOBBY; cleared when a mission begins
        self._game_starting = False        # Set when play is pressed; waiting for "Good Luck"
        self._game_starting_stalled = False  # Set when GAME_STARTING times out without Good Luck
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
        

    @staticmethod
    def _levenshtein_distance(a: str, b: str) -> int:
        """Compute Levenshtein distance between two strings."""
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

    @classmethod
    def _is_respawn_text(cls, text_clean: str) -> bool:
        """Return True when OCR text is a plausible match for respawn label.
        
        The actual in-game text is 'RESPA' (not 'RESPAWN'), so we match that
        with tolerance for OCR errors.
        """
        if not text_clean:
            return False

        # Primary target: match what's actually displayed in-game
        target = "RESPA"
        if target in text_clean:
            return True

        # Fallback: Check for common OCR partial matches (handles severe OCR errors)
        # OCR often misreads characters, so check for partial matches
        # Note: "REPA" is intentionally excluded — it's too short and causes false positives;
        # the Levenshtein check below handles "REPA"-type misreads (distance 1 from "RESPA").
        if "RESP" in text_clean:
            return True
        
        # Levenshtein distance for near-matches (typos with 1-2 character errors)
        # Compare same-length windows for near-matches.
        window_len = len(target)
        candidates = []
        if len(text_clean) < window_len:
            candidates.append(text_clean)
        else:
            for index in range(0, len(text_clean) - window_len + 1):
                candidates.append(text_clean[index:index + window_len])

        for candidate in candidates:
            distance = cls._levenshtein_distance(candidate, target)
            if distance <= 2:
                return True

        return False
    
    @classmethod
    def _is_incoming_text(cls, text_clean: str) -> bool:
        """Return True when OCR text matches incoming missile warning.
        
        The actual in-game text shows 'MING' (from 'INCOMING'), so we match that
        with tolerance for OCR errors.
        """
        if not text_clean:
            return False

        # Primary target: 'MING' visible in game
        target = "MING"
        if target in text_clean:
            return True
        
        # Also check for partial 'INCOMING' text
        if "INCOM" in text_clean or "NCOMING" in text_clean:
            return True
        
        # Levenshtein distance for near-matches (OCR errors)
        window_len = len(target)
        candidates = []
        if len(text_clean) < window_len:
            candidates.append(text_clean)
        else:
            for index in range(0, len(text_clean) - window_len + 1):
                candidates.append(text_clean[index:index + window_len])

        for candidate in candidates:
            distance = cls._levenshtein_distance(candidate, target)
            if distance <= 1:  # Stricter tolerance for MING (shorter word)
                return True

        return False
    
    @property
    def ocr_reader(self):
        """Lazy initialization of EasyOCR reader (10s startup delay)."""
        if self._ocr_reader is None and easyocr:
            logger.info("Initializing EasyOCR reader (this may take ~10 seconds)...")
            try:
                self._ocr_reader = easyocr.Reader(['en'], gpu=True)
                logger.info("EasyOCR reader initialized successfully")
            except Exception as e:
                logger.warning("Failed to initialize EasyOCR with GPU, trying CPU: %s", e)
                try:
                    self._ocr_reader = easyocr.Reader(['en'], gpu=False)
                    logger.info("EasyOCR reader initialized with CPU")
                except Exception as e:
                    logger.error("Failed to initialize EasyOCR: %s", e)
                    return None
        return self._ocr_reader
    
    @property
    def ocr_executor(self):
        """Lazy initialization of ThreadPoolExecutor for parallel OCR."""
        if self._ocr_executor is None and easyocr and not self._ocr_executor_initialized:
            try:
                self._ocr_executor = ThreadPoolExecutor(max_workers=2)
                self._ocr_executor_initialized = True
                logger.info("Initialized ThreadPoolExecutor with 2 workers for parallel OCR")
            except Exception as e:
                logger.error("Failed to initialize ThreadPoolExecutor: %s", e)
                self._ocr_executor_initialized = True  # Prevent retries
                return None
        return self._ocr_executor
    
    @property
    def game_state(self) -> GameState:
        """Current high-level game state."""
        if self._game_starting:
            return GameState.GAME_STARTING
        if self._game_starting_stalled:
            return GameState.GAME_STARTING_STALLED
        if self._game_lobby:
            return GameState.GAME_LOBBY
        if self._game_end_b:
            return GameState.GAME_END_B
        return GameState.GAME_BATTLE

    def cleanup(self):
        """Clean up resources (call when shutting down)."""
        self._click_to_stop.set()
        if self._ocr_executor is not None:
            try:
                self._ocr_executor.shutdown(wait=False)
                logger.info("ThreadPoolExecutor shut down successfully")
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

        # Start the click_to background thread once on first frame
        if not self._click_to_thread_started:
            self._click_to_thread_started = True
            threading.Thread(target=self._run_click_to_in_background, daemon=True).start()
            logger.debug("Click-to background thread started")

        state = {
            'is_respawning': False,
            'respawn_confidence': 0.0,
            'respawn_method': None,
            'is_incoming': False,
            'incoming_confidence': 0.0,
            'incoming_method': None,
            'is_click_to': False,
            'click_to_method': None,
            'game_state': self.game_state,
        }

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
        
        # Save crop overlay if enabled (every frame)
        if self.show_grid_highlighted:
            try:
                output_dir = Path("tests") / "test-output"
                output_dir.mkdir(parents=True, exist_ok=True)
                output_path = str(output_dir / "output_crops.png")
                annotated = draw_crops(frame, self.crops)
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
        if self.game_state in (GameState.GAME_END_B, GameState.GAME_LOBBY, GameState.GAME_STARTING):
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
            self._background_ocr_lock.release()

        # Return cached result (may be stale) while background OCR runs
        return cached_result
    
    def _run_ocr_in_background(self):
        """Run OCR in background using thread pool for parallel region processing."""
        while True:
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
                    return

                # Extract respawn and incoming crops (click_to has its own thread)
                respawn_frame = get_crop(full_frame, *self.crops["respawn"])
                incoming_frame = get_crop(full_frame, *self.crops["incoming"])
                t1 = time.time()

                # Submit both tasks to thread pool for parallel processing
                # Numpy arrays are passed by reference — no serialization needed
                respawn_future = executor.submit(_process_respawn_region, respawn_frame)
                incoming_future = executor.submit(_process_incoming_region, incoming_frame)
                t2 = time.time()

                # Wait for respawn result first — update its cache immediately so the
                # main loop can react without waiting for the (often slower) incoming OCR.
                respawn_detected, respawn_ocr_time, respawn_text = respawn_future.result(timeout=120)

                if respawn_detected:
                    logger.debug("Analyzer: detected 'RESPAWN' text (matched: '%s')", respawn_text)
                    self._game_end_b = False
                    self._game_lobby = False

                respawn_result = (True, 1.0, "ocr") if respawn_detected else (False, 0.0, None)
                with self._ocr_cache_lock:
                    self._ocr_cache['result'] = respawn_result
                    self._ocr_cache['timestamp'] = current_time

                # Now wait for incoming — its result is independent of respawn.
                incoming_detected, incoming_ocr_time, variant_name, incoming_text, incoming_raw = incoming_future.result(timeout=120)
                t3 = time.time()

                if incoming_detected:
                    logger.info("\033[95m🚀 INCOMING MISSILE DETECTED (variant=%s) - text='%s'\033[0m", variant_name, incoming_text)
                    self._game_end_b = False
                    self._game_lobby = False
                elif incoming_raw:
                    logger.debug("Analyzer: No match in INCOMING region — raw OCR: %s", ", ".join(incoming_raw))
                else:
                    logger.debug("Analyzer: No text detected in INCOMING region")

                incoming_result = (True, 1.0, "ocr") if incoming_detected else (False, 0.0, None)
                with self._incoming_cache_lock:
                    self._incoming_cache['result'] = incoming_result
                    self._incoming_cache['timestamp'] = current_time
                if incoming_detected:
                    self.incoming_event.set()

                # Log timing
                logger.debug(
                    "Analyzer: Parallel OCR Timings - Extract: %.2fs, Submit: %.2fs | "
                    "Respawn OCR: %.2fs | Incoming OCR: %.2fs | Total: %.2fs",
                    t1-t0, t2-t1, respawn_ocr_time, incoming_ocr_time, t3-t0
                )
            except Exception as e:
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
            if state == GameState.GAME_STARTING:
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
                click_to_frame = get_crop(frame, *self.crops["click_to"])
                click_to_detected, _, click_to_text = executor.submit(
                    _process_click_to_region, click_to_frame
                ).result(timeout=120)
                result = (True, 1.0, "ocr") if click_to_detected else (False, 0.0, None)
                with self._click_to_cache_lock:
                    self._click_to_cache['result'] = result
                    self._click_to_cache['timestamp'] = time.time()
                if click_to_detected:
                    self._game_end_b = True
                    logger.debug("Analyzer: detected 'Click to' text (matched: '%s') → GAME_END_B", click_to_text)
            except RuntimeError:
                return  # executor shut down — exit the loop cleanly
            except Exception as e:
                logger.warning("Analyzer: click_to OCR failed: %s", e)

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
            'game_state': GameState.GAME_BATTLE,
        }
    
    def reset_cache(self):
        """Reset OCR caches - useful when switching between different images/scenes."""
        self._ocr_cache['timestamp'] = 0.0
        self._ocr_cache['result'] = (False, 0.0, None)
        self._incoming_cache['timestamp'] = 0.0
        self._incoming_cache['result'] = (False, 0.0, None)
        self._click_to_cache['timestamp'] = 0.0
        self._click_to_cache['result'] = (False, 0.0, None)
        logger.debug("OCR caches reset")

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
            region_frame = get_crop(frame, *self.crops["good_luck"])
            detected, _, text = executor.submit(
                _process_good_luck_region, region_frame
            ).result(timeout=30)
            if detected:
                logger.info("Analyzer: 'Good Luck' detected in good_luck crop (text='%s')", text)
            else:
                logger.debug("Analyzer: 'Good Luck' not found in good_luck crop")
            return detected
        except Exception as e:
            logger.warning("Analyzer: Good Luck scan failed: %s", e)
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
            region_frame = get_crop(frame, *self.crops["event_refresh"])
            detected, _, text = executor.submit(
                _process_event_refresh_region, region_frame
            ).result(timeout=30)
            if detected:
                logger.info("Analyzer: 'Event refresh' popup detected in event_refresh crop (text='%s')", text)
            else:
                logger.debug("Analyzer: Event refresh popup not found in event_refresh crop")
            return detected
        except Exception as e:
            logger.warning("Analyzer: Event refresh scan failed: %s", e)
            return False

    def scan_region_for_play_button(self, frame) -> bool:
        """Synchronously scan play_button crop for 'PLAY' or 'READY' text."""
        executor = self.ocr_executor
        if executor is None:
            logger.warning("Analyzer: OCR executor not available for play button scan")
            return False
        try:
            region_frame = get_crop(frame, *self.crops["play_button"])
            detected, _, text = executor.submit(
                _process_play_button_region, region_frame
            ).result(timeout=30)
            if detected:
                logger.info("Analyzer: 'PLAY/READY' detected in play_button crop (text='%s')", text)
            else:
                logger.debug("Analyzer: 'PLAY/READY' not found in play_button crop")
            return detected
        except Exception as e:
            logger.warning("Analyzer: Play button scan failed: %s", e)
            return False

    def scan_region_for_lobby_popups(self, frame):
        """Scan for lobby popup buttons that may be blocking the play button.

        Checks REVEAL_ALL, TAP_HERE_TO_CONTINUE, and UNLOCK_CLOSE crops in order.

        Args:
            frame: Full BGR frame from screen capture.

        Returns:
            The crop name (str) of the first detected popup, or None if none found.
        """
        executor = self.ocr_executor
        if executor is None:
            logger.warning("Analyzer: OCR executor not available for lobby popup scan")
            return None

        checks = [
            ("REVEAL_ALL",           _process_reveal_all_region),
            ("TAP_HERE_TO_CONTINUE", _process_tap_here_region),
            ("UNLOCK_CLOSE",         _process_unlock_close_region),
        ]
        for crop_name, worker_fn in checks:
            if crop_name not in self.crops:
                logger.debug("Analyzer: crop '%s' not in config — skipping popup check", crop_name)
                continue
            try:
                region_frame = get_crop(frame, *self.crops[crop_name])
                detected, _, text = executor.submit(worker_fn, region_frame).result(timeout=30)
                if detected:
                    logger.info("Analyzer: lobby popup '%s' detected (text='%s')", crop_name, text)
                    return crop_name
                else:
                    logger.debug("Analyzer: lobby popup '%s' not found", crop_name)
            except Exception as e:
                logger.warning("Analyzer: lobby popup scan for '%s' failed: %s", crop_name, e)
        return None

