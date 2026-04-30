"""Game state analyzer for detecting respawn, enemies, and other game conditions."""

import logging
import cv2
import numpy as np
import threading
from concurrent.futures import ThreadPoolExecutor
import time
from enum import Enum, auto
from pathlib import Path

from transitions import Machine, MachineError

from .crop_region import get_crop, load_crops, draw_crops


class GameState(Enum):
    GAME_BATTLE          = auto()  # Active gameplay (default); respawn/incoming scanning active
    GAME_END_B           = auto()  # "Click to Continue" detected; clicking in progress
    GAME_LOBBY           = auto()  # Final continue (region 64) clicked; waiting in lobby
    GAME_WAITING         = auto()  # PLAY clicked; waiting for CANCEL crop to confirm matchmaking
    GAME_STARTING        = auto()  # Matchmaking confirmed; waiting for "Good Luck" before launching mission
    GAME_STARTING_STALLED = auto() # GAME_STARTING timed out without "Good Luck" detection
    GAME_BATTLE_MANUAL   = auto()  # Player took manual control; auto-mission restart suppressed

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
        "REVEAL_ALL", "TAP_HERE_TO_CONTINUE", "UNLOCK_CLOSE", "FINAL_CONTINUE",
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
}


# ============================================================================
# FSM Transition Table (ADR 025)
# ============================================================================

_FSM_TRANSITIONS = [
    {"trigger": "cancel_detected",    "source": "GAME_LOBBY",            "dest": "GAME_STARTING"},
    {"trigger": "cancel_detected",    "source": "GAME_WAITING",          "dest": "GAME_STARTING"},
    {"trigger": "waiting_timeout",    "source": "GAME_WAITING",          "dest": "GAME_LOBBY"},
    {"trigger": "good_luck_detected", "source": "GAME_STARTING",         "dest": "GAME_BATTLE"},
    {"trigger": "starting_timeout",   "source": "GAME_STARTING",         "dest": "GAME_STARTING_STALLED"},
    {"trigger": "starting_recovery",  "source": "GAME_STARTING_STALLED", "dest": "GAME_STARTING"},
    {"trigger": "starting_give_up",   "source": "GAME_STARTING_STALLED", "dest": "GAME_LOBBY"},
    {"trigger": "click_to_detected",  "source": ["GAME_BATTLE", "GAME_BATTLE_MANUAL"], "dest": "GAME_END_B"},
    {"trigger": "manual_takeover",    "source": "GAME_BATTLE",            "dest": "GAME_BATTLE_MANUAL"},
    {"trigger": "respawn_reset",      "source": "GAME_BATTLE_MANUAL",     "dest": "GAME_BATTLE"},
    {"trigger": "manual_reset",       "source": "*",                     "dest": "GAME_LOBBY"},
    {"trigger": "continue_clicked",   "source": ["GAME_END_B", "GAME_BATTLE_MANUAL"], "dest": "GAME_LOBBY"},
    {"trigger": "respawn_detected",   "source": "GAME_END_B",            "dest": "GAME_BATTLE"},
]


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

        # Enemy HSV range for red-color detection in ENEMY_CLOSE_BY crop
        enemy_hsv_cfg = config.get("enemy_hsv", {})
        self._enemy_hsv_lower = np.array(enemy_hsv_cfg.get("lower", [0, 120, 120]), dtype=np.uint8)
        self._enemy_hsv_upper = np.array(enemy_hsv_cfg.get("upper", [10, 255, 255]), dtype=np.uint8)
        
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
        self._lobby_quick_scan_thread_started = False
        self._lobby_quick_scan_stop = threading.Event()

        # FSM — single authoritative state field managed by the transitions library.
        # Trigger methods (play_clicked, cancel_detected, …) are added to this instance
        # by Machine.__init__. All callers use self._trigger() for thread-safe dispatch.
        self._state_lock = threading.Lock()
        self._on_cancel_mission = None           # injected by main.py after construction
        self._on_start_game_starting_loop = None  # injected by main.py after construction
        self._on_lobby_play_click = None         # injected by main.py; called with crop name when PLAY/READY detected
        self._on_lobby_popup_click = None        # injected by main.py; called with popup crop name when a popup is detected

        Machine(
            model=self,
            states=[s.name for s in GameState],
            transitions=_FSM_TRANSITIONS,
            initial=GameState.GAME_LOBBY.name,
            ignore_invalid_triggers=False,
        )

        # Health sub-state (GAME_BATTLE only)
        self._health: "int | None" = None  # Last known health value from OCR
        self._game_battle_alive = False    # True when health >= 1 in GAME_BATTLE
        self._health_lock = threading.Lock()
        self._health_no_digits_since = 0.0  # timestamp when health OCR started returning no digits
        # Signalled when _game_battle_alive transitions False → True.
        # The main loop waits on this event to restart the mission immediately.
        self.alive_event = threading.Event()

        # Ammo sub-state (GAME_BATTLE only)
        self._ammo_flares: "int | None" = None   # Last known flare count from OCR
        self._ammo_missiles: "int | None" = None  # Last known missile count from OCR
        self._ammo_lock = threading.Lock()
        # Signalled when flares == 2 (reload needed) or missiles == 0 (end mission).
        self.low_flares_event = threading.Event()
        self.no_missiles_event = threading.Event()

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
                self._ocr_executor = ThreadPoolExecutor(max_workers=13)
                self._ocr_executor_initialized = True
                logger.info("Initialized ThreadPoolExecutor with 13 workers for parallel OCR")
            except Exception as e:
                logger.error("Failed to initialize ThreadPoolExecutor: %s", e)
                self._ocr_executor_initialized = True  # Prevent retries
                return None
        return self._ocr_executor
    
    def _trigger(self, trigger_name: str) -> bool:
        """Thread-safe FSM trigger dispatch. Returns False on invalid transitions."""
        with self._state_lock:
            fn = getattr(self, trigger_name, None)
            if fn is None:
                logger.error("FSM: unknown trigger '%s'", trigger_name)
                return False
            try:
                return fn()
            except MachineError as e:
                logger.warning("FSM: ignored invalid trigger '%s' from state %s: %s",
                               trigger_name, self.game_state, e)
                return False

    # ------------------------------------------------------------------
    # FSM entry hooks — called automatically by transitions on state entry
    # ------------------------------------------------------------------

    def on_enter_GAME_LOBBY(self):
        if self._on_cancel_mission:
            self._on_cancel_mission()

    def on_enter_GAME_STARTING(self):
        if self._on_start_game_starting_loop:
            self._on_start_game_starting_loop()

    def on_enter_GAME_BATTLE(self):
        self._health_no_digits_since = 0.0
        with self._health_lock:
            if self._health is not None and self._health >= 1:
                self.alive_event.set()

    def on_enter_GAME_BATTLE_MANUAL(self):
        logger.info("FSM: entering GAME_BATTLE_MANUAL — manual takeover active, auto-restart suppressed")
        if self._on_cancel_mission:
            self._on_cancel_mission()

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
        self._click_to_stop.set()
        self._lobby_quick_scan_stop.set()
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

        # Start background threads once on first frame
        if not self._click_to_thread_started:
            self._click_to_thread_started = True
            threading.Thread(target=self._run_click_to_in_background, daemon=True).start()
            logger.debug("Click-to background thread started")
        if not self._lobby_quick_scan_thread_started:
            self._lobby_quick_scan_thread_started = True
            threading.Thread(target=self._run_game_lobby_quick_scan, daemon=True).start()
            logger.info("Lobby quick-scan background thread started")

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

                state = self.game_state
                # Only process GAME_BATTLE crops in GAME_BATTLE or GAME_END_B
                if state not in (GameState.GAME_BATTLE, GameState.GAME_BATTLE_MANUAL, GameState.GAME_END_B):
                    logger.debug(f"Skipping GAME_BATTLE crop OCR in {state.name} state")
                    time.sleep(0.2)
                else:
                    # Extract respawn, incoming, health, and ammo crops (click_to has its own thread)
                    respawn_frame = get_crop(full_frame, *self.crops["respawn"][:4])
                    incoming_frame = get_crop(full_frame, *self.crops["incoming"][:4])
                    health_frame = get_crop(full_frame, *self.crops["HEALTH"][:4]) if "HEALTH" in self.crops else None
                    ammo_flares_frame = get_crop(full_frame, *self.crops["AMMO_FLARES"][:4]) if "AMMO_FLARES" in self.crops else None
                    ammo_missile_frame = get_crop(full_frame, *self.crops["AMMO_MISSILE"][:4]) if "AMMO_MISSILE" in self.crops else None
                    t1 = time.time()

                    # Submit all tasks to the thread pool for parallel processing.
                    # Numpy arrays are passed by reference — no serialization needed.
                    respawn_future = executor.submit(_process_respawn_region, respawn_frame)
                    incoming_future = executor.submit(_process_incoming_region, incoming_frame)
                    health_future = executor.submit(_process_health_region, health_frame) if health_frame is not None else None
                    ammo_flares_future = executor.submit(_process_health_region, ammo_flares_frame) if ammo_flares_frame is not None else None
                    ammo_missile_future = executor.submit(_process_health_region, ammo_missile_frame) if ammo_missile_frame is not None else None
                    t2 = time.time()

                    # Wait for respawn result first — update its cache immediately so the
                    # main loop can react without waiting for the (often slower) incoming OCR.
                    respawn_detected, respawn_ocr_time, respawn_text = respawn_future.result(timeout=120)

                    if respawn_detected:
                        logger.debug("Analyzer: detected 'RESPAWN' text (matched: '%s')", respawn_text)
                        if self.game_state == GameState.GAME_END_B:
                            self._trigger("respawn_detected")

                    respawn_result = (True, 1.0, "ocr") if respawn_detected else (False, 0.0, None)
                    with self._ocr_cache_lock:
                        self._ocr_cache['result'] = respawn_result
                        self._ocr_cache['timestamp'] = current_time

                    # Now wait for incoming — its result is independent of respawn.
                    incoming_detected, incoming_ocr_time, variant_name, incoming_text, incoming_raw = incoming_future.result(timeout=120)
                    t3 = time.time()

                    if incoming_detected:
                        logger.info("\033[95m🚀 INCOMING MISSILE DETECTED (variant=%s) - text='%s'\033[0m", variant_name, incoming_text)
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

                    # Wait for health result and update sub-state.
                    health_ocr_time = 0.0
                    if health_future is not None:
                        health_value, health_ocr_time = health_future.result(timeout=120)
                        if health_value is not None:
                            # Digits found — reset no-digits timer and update state.
                            self._health_no_digits_since = 0.0
                            alive = health_value >= 1
                            with self._health_lock:
                                prev_alive = self._game_battle_alive
                                self._health = health_value
                                self._game_battle_alive = alive
                            logger.info("Health: %d | alive=%s", health_value, alive)
                            # Signal False → True transition for immediate mission restart.
                            if alive and not prev_alive:
                                logger.info("Analyzer: health alive transition False→True")
                                self.alive_event.set()
                        else:
                            # No digits — only clear alive flag after 3 s of consecutive misses.
                            now_t = time.time()
                            if self._health_no_digits_since == 0.0:
                                self._health_no_digits_since = now_t
                                logger.debug("Analyzer: Health OCR returned no digits (grace timer started)")
                            elif now_t - self._health_no_digits_since >= 3.0:
                                with self._health_lock:
                                    self._game_battle_alive = False
                                logger.debug(
                                    "Analyzer: Health OCR no digits for %.1fs → game_battle_alive=False",
                                    now_t - self._health_no_digits_since)
                            else:
                                logger.debug(
                                    "Analyzer: Health OCR no digits (%.1fs elapsed, 3s threshold)",
                                    now_t - self._health_no_digits_since)
                    # Resolve ammo futures and fire events.
                    ammo_flares_ocr_time = 0.0
                    ammo_missile_ocr_time = 0.0
                    if ammo_flares_future is not None:
                        flares_value, ammo_flares_ocr_time = ammo_flares_future.result(timeout=120)
                        if flares_value is not None:
                            with self._ammo_lock:
                                self._ammo_flares = flares_value
                            logger.info("Ammo flares: %d", flares_value)
                            if flares_value == 2:
                                self.low_flares_event.set()
                    if ammo_missile_future is not None:
                        missile_value, ammo_missile_ocr_time = ammo_missile_future.result(timeout=120)
                        if missile_value is not None:
                            with self._ammo_lock:
                                self._ammo_missiles = missile_value
                            logger.info("Ammo missiles: %d", missile_value)
                            if missile_value == 0:
                                self.no_missiles_event.set()

                    t4 = time.time()

                    # Log timing
                    logger.debug(
                        "Analyzer: Parallel OCR Timings - Extract: %.2fs, Submit: %.2fs | "
                        "Respawn OCR: %.2fs | Incoming OCR: %.2fs | Health OCR: %.2fs | "
                        "Flares OCR: %.2fs | Missiles OCR: %.2fs | Total: %.2fs",
                        t1-t0, t2-t1, respawn_ocr_time, incoming_ocr_time, health_ocr_time,
                        ammo_flares_ocr_time, ammo_missile_ocr_time, t4-t0
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
            if state in (GameState.GAME_STARTING, GameState.GAME_WAITING):
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
                    if self.game_state == GameState.GAME_BATTLE:
                        self._trigger("click_to_detected")
                    logger.debug("Analyzer: detected 'Click to' text (matched: '%s') → GAME_END_B", click_to_text)
            except RuntimeError:
                return  # executor shut down — exit the loop cleanly
            except Exception as e:
                logger.warning("Analyzer: click_to OCR failed: %s", e)

    def _run_game_lobby_quick_scan(self):
        """Scan lobby crops every 1s while in GAME_LOBBY or GAME_WAITING.

        All crops for a cycle are submitted to the executor in one parallel batch so
        CANCEL, PLAY, and popup OCR all run simultaneously. Results are resolved in
        priority order: CANCEL/UNREADY → PLAY/READY → popup crops.

        Popup scan fires every 5s in both states; CANCEL/PLAY scan fires every cycle
        in GAME_LOBBY only.
        """
        lobby_crops = [c for c in ("CANCEL", "UNREADY", "PLAY", "READY") if c in self.crops]
        popup_crop_names = ["INVITED", "CREATION_FAILED", "REVEAL_ALL",
                            "UNLOCK_CLOSE", "INSPECT", "event_refresh"]
        popup_crops = [c for c in popup_crop_names if c in self.crops]

        if not lobby_crops and not popup_crops:
            logger.warning("Lobby quick-scan: no crops configured — thread exiting")
            return

        last_play_click_ts = 0.0
        last_popup_scan_ts = 0.0

        while not self._lobby_quick_scan_stop.wait(timeout=1.0):
            state = self.game_state
            if state not in (GameState.GAME_LOBBY, GameState.GAME_WAITING):
                continue
            executor = self.ocr_executor
            if executor is None:
                continue
            with self._click_to_frame_lock:
                frame = self._click_to_latest_frame
            if frame is None:
                continue
            try:
                now = time.time()
                do_popup_scan = bool(popup_crops) and now - last_popup_scan_ts >= 5.0

                # Submit all futures for this cycle in one parallel batch so all
                # OCR (lobby + popup) runs simultaneously rather than sequentially.
                futures = {}
                if state == GameState.GAME_LOBBY and lobby_crops:
                    for crop in lobby_crops:
                        futures[crop] = executor.submit(
                            _process_text_region,
                            get_crop(frame, *self.crops[crop][:4]),
                            self.crops[crop].text or [],
                        )
                if do_popup_scan:
                    last_popup_scan_ts = now
                    for crop in popup_crops:
                        futures[crop] = executor.submit(
                            _process_text_region,
                            get_crop(frame, *self.crops[crop][:4]),
                            self.crops[crop].text or [],
                        )

                if not futures:
                    continue

                # --- CANCEL / UNREADY (GAME_LOBBY only) ---
                handled = False
                if state == GameState.GAME_LOBBY:
                    for crop in ("CANCEL", "UNREADY"):
                        if crop not in futures:
                            continue
                        detected, _, text = futures[crop].result(timeout=120)
                        if detected:
                            logger.info(
                                "\033[92m✓ Lobby quick-scan: %s detected (text='%s') → GAME_STARTING\033[0m",
                                crop, text)
                            self._trigger("cancel_detected")
                            handled = True
                            break

                # --- PLAY / READY (GAME_LOBBY only) ---
                if not handled and state == GameState.GAME_LOBBY:
                    for crop in ("PLAY", "READY"):
                        if crop not in futures:
                            continue
                        detected, _, text = futures[crop].result(timeout=120)
                        if detected:
                            if time.time() - last_play_click_ts < 60.0:
                                logger.debug(
                                    "Lobby quick-scan: %s visible but click suppressed (%.1fs since last click)",
                                    crop, time.time() - last_play_click_ts)
                                handled = True
                            elif self.game_state == GameState.GAME_STARTING:
                                logger.debug(
                                    "Lobby quick-scan: %s visible but state is now GAME_STARTING — skipping click",
                                    crop)
                                handled = True
                            else:
                                logger.info(
                                    "\033[93m📋 Lobby quick-scan: %s detected (text='%s') — clicking\033[0m",
                                    crop, text)
                                last_play_click_ts = time.time()
                                if self._on_lobby_play_click:
                                    self._on_lobby_play_click(crop)
                                handled = True
                            break
                    if not handled:
                        logger.info("Lobby quick-scan: no lobby crops detected")

                # --- Popup crops (both states, every 5s) ---
                if do_popup_scan:
                    for crop in popup_crops:
                        if crop not in futures:
                            continue
                        try:
                            detected, _, text = futures[crop].result(timeout=120)
                            if detected:
                                logger.info(
                                    "Lobby quick-scan: popup '%s' detected (text='%s')", crop, text)
                                if self._on_lobby_popup_click:
                                    self._on_lobby_popup_click(crop)
                                break
                            else:
                                logger.debug("Lobby quick-scan: popup '%s' not found", crop)
                        except Exception as e:
                            logger.warning(
                                "Lobby quick-scan: popup '%s' scan failed: %s: %s",
                                crop, type(e).__name__, e)
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

