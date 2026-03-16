"""Game state analyzer for detecting respawn, enemies, and other game conditions."""

import logging
import cv2
import numpy as np
import threading
import multiprocessing as mp
import time
from enum import Enum, auto
from pathlib import Path
import os


class GameState(Enum):
    GAME_BATTLE = auto()  # Active gameplay: respawn or incoming missiles possible
    GAME_END = auto()     # Post-match: "Click to Continue" screen shown

try:
    import easyocr
except ImportError:
    easyocr = None

logger = logging.getLogger(__name__)


# ============================================================================
# Multiprocessing Worker Functions (must be at module level for pickling)
# ============================================================================

# Process-local OCR readers (initialized once per worker process)
_process_ocr_reader = None


def _init_ocr_reader():
    """Initialize EasyOCR reader in worker process (called once)."""
    global _process_ocr_reader
    if _process_ocr_reader is None and easyocr:
        try:
            _process_ocr_reader = easyocr.Reader(['en'], gpu=True, verbose=False)
            logger.debug("Initialized EasyOCR reader (GPU) in worker process PID=%d", os.getpid())
        except Exception as e:
            logger.warning("Failed to initialize EasyOCR GPU in worker: %s", e)
            try:
                _process_ocr_reader = easyocr.Reader(['en'], gpu=False, verbose=False)
                logger.debug("Initialized EasyOCR reader (CPU) in worker process PID=%d", os.getpid())
            except Exception as e:
                logger.warning("Failed to initialize EasyOCR in worker: %s", e)
    return _process_ocr_reader


def _process_respawn_region(respawn_frame_bytes, shape, dtype_str):
    """
    Worker function to process respawn region in separate process.
    
    Args:
        respawn_frame_bytes: bytes representation of numpy array
        shape: tuple of array shape
        dtype_str: string representation of dtype
    
    Returns:
        tuple: (detected: bool, ocr_time: float, text_found: str or None)
    """
    import time
    import cv2
    import numpy as np
    
    reader = _init_ocr_reader()
    if reader is None:
        return (False, 0.0, None)
    
    # Reconstruct numpy array from bytes
    dtype = np.dtype(dtype_str)
    respawn_frame = np.frombuffer(respawn_frame_bytes, dtype=dtype).reshape(shape)
    
    t_start = time.time()
    
    # Preprocess respawn region
    gray_respawn = cv2.cvtColor(respawn_frame, cv2.COLOR_BGR2GRAY)
    _, binary_respawn = cv2.threshold(gray_respawn, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    small_gray = cv2.resize(gray_respawn, None, fx=0.7, fy=0.7, interpolation=cv2.INTER_AREA)
    small_binary = cv2.resize(binary_respawn, None, fx=0.7, fy=0.7, interpolation=cv2.INTER_AREA)

    # Run OCR on both grayscale and thresholded images
    results = []
    for img, label in [(small_gray, 'gray'), (small_binary, 'binary')]:
        ocr_results = reader.readtext(img, detail=1, paragraph=False)
        for (bbox, text, conf) in ocr_results:
            text_clean = ''.join(c for c in text.strip().upper() if c.isalpha())
            results.append((label, text_clean, conf))

    ocr_time = time.time() - t_start

    # Log all OCR results for debugging
    logger.debug(f"Respawn OCR results: {results}")

    # Check for RESPAWN text (relaxed matching)
    target = "RESPAWN"
    for label, text_clean, conf in results:
        if len(text_clean) >= 4:
            # Levenshtein distance check
            if len(text_clean) <= 6:
                # Short text: check for RESPA variants
                max_dist = 2  # Relaxed tolerance
                for i in range(len(target) - 4):
                    substring = target[i:i+5]
                    dist = _levenshtein_distance_simple(text_clean, substring)
                    if dist <= max_dist:
                        logger.debug(f"Respawn detected (variant: {label}, text: {text_clean}, dist: {dist})")
                        return (True, ocr_time, text_clean)
            else:
                # Longer text: full match
                dist = _levenshtein_distance_simple(text_clean, target)
                if dist <= 2:
                    logger.debug(f"Respawn detected (variant: {label}, text: {text_clean}, dist: {dist})")
                    return (True, ocr_time, text_clean)

    return (False, ocr_time, None)


def _process_incoming_region(incoming_frame_bytes, shape, dtype_str):
    """
    Worker function to process incoming missile region in separate process.
    
    Args:
        incoming_frame_bytes: bytes representation of numpy array
        shape: tuple of array shape
        dtype_str: string representation of dtype
    
    Returns:
        tuple: (detected: bool, ocr_time: float, variant_name: str or None, text_found: str or None)
    """
    import time
    import cv2
    import numpy as np
    
    reader = _init_ocr_reader()
    if reader is None:
        return (False, 0.0, None, None)
    
    # Reconstruct numpy array from bytes
    dtype = np.dtype(dtype_str)
    incoming_frame = np.frombuffer(incoming_frame_bytes, dtype=dtype).reshape(shape)
    
    t_start = time.time()
    
    # Preprocess incoming region
    gray_incoming = cv2.cvtColor(incoming_frame, cv2.COLOR_BGR2GRAY)
    _, binary_incoming = cv2.threshold(gray_incoming, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    
    variants = {
        "gray_up_1p4": cv2.resize(gray_incoming, None, fx=1.4, fy=1.4, interpolation=cv2.INTER_CUBIC),
        "binary_otsu_up_1p4": cv2.resize(binary_incoming, None, fx=1.4, fy=1.4, interpolation=cv2.INTER_CUBIC),
    }
    
    # Try variants
    for variant_name, variant_img in variants.items():
        results_incoming = reader.readtext(variant_img, detail=0, paragraph=True)
        extracted_text = " ".join(str(result) for result in results_incoming)
        normalized = " ".join(extracted_text.upper().split()).replace(" ", "")
        
        # Check for MING or WARNING (incoming missile text)
        if "MING" in normalized or ("ARNING" in normalized and len(normalized) >= 6):
            ocr_time = time.time() - t_start
            return (True, ocr_time, variant_name, normalized)
    
    ocr_time = time.time() - t_start
    return (False, ocr_time, None, None)


def _process_click_to_region(click_to_frame_bytes, shape, dtype_str):
    """
    Worker function to process "Click to Continue" region in separate process.

    Returns:
        tuple: (detected: bool, ocr_time: float, text_found: str or None)
    """
    import time
    import cv2
    import numpy as np

    reader = _init_ocr_reader()
    if reader is None:
        return (False, 0.0, None)

    dtype = np.dtype(dtype_str)
    click_to_frame = np.frombuffer(click_to_frame_bytes, dtype=dtype).reshape(shape)

    t_start = time.time()

    gray = cv2.cvtColor(click_to_frame, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    upscaled = cv2.resize(gray, None, fx=1.4, fy=1.4, interpolation=cv2.INTER_CUBIC)

    for img in (upscaled, binary):
        results = reader.readtext(img, detail=0, paragraph=True)
        text = " ".join(str(r) for r in results).upper().replace(" ", "")
        if "CLICKTO" in text or "LICKTO" in text or "CLICK" in text:
            ocr_time = time.time() - t_start
            return (True, ocr_time, text)

    ocr_time = time.time() - t_start
    return (False, ocr_time, None)


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
        
        # Grid configuration for region extraction (default 8x8 = 64 regions)
        grid_size = respawn_cfg.get("grid_size", 8)
        try:
            grid_size = int(grid_size)
        except (TypeError, ValueError):
            logger.warning("Invalid respawn_detection.grid_size=%r, defaulting to 8", grid_size)
            grid_size = 8
        self.grid_rows = max(2, grid_size)
        self.grid_cols = max(2, grid_size)

        # OCR-based respawn detection (looks for "RESPAWN" text)
        self.use_ocr = respawn_cfg.get("use_ocr", True)
        self.respawn_region = respawn_cfg.get("region", 44)  # Region 44 for RESPA in 8x8 mapping
        self.incoming_region = respawn_cfg.get("incoming_region", 21)  # Region 21 for MING in 8x8 mapping
        self.click_to_region = respawn_cfg.get("click_to_region", 60)  # Region 60 for "Click to Continue"

        # Validate region numbers against the configured grid at startup
        total_regions = self.grid_rows * self.grid_cols
        for name, value in [("respawn_detection.region", self.respawn_region),
                             ("respawn_detection.incoming_region", self.incoming_region),
                             ("respawn_detection.click_to_region", self.click_to_region)]:
            if not isinstance(value, int) or not (1 <= value <= total_regions):
                raise ValueError(
                    f"Config error: {name}={value!r} is out of range for a "
                    f"{self.grid_rows}x{self.grid_cols} grid (valid: 1–{total_regions})"
                )
        
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

        # Game state: track last battle event to gate click_to OCR
        self._last_battle_event_ts = 0.0
        self._battle_quiet_period = respawn_cfg.get("battle_quiet_period", 5.0)
        # Static frame detection: two consecutive identical incoming_region frames → GAME_END
        self._prev_incoming_frame = None
        self._static_incoming_count = 0
        self._static_frame_threshold = respawn_cfg.get("static_frame_threshold", 3.0)  # mean abs pixel diff

        # Multiprocessing pool for parallel OCR processing
        # Use 2 workers: one for respawn, one for incoming detection
        self._ocr_pool = None
        self._ocr_pool_initialized = False
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
        
        # Screenshot capture overlay grid size (independent from detection grid).
        # Example: 6 -> 6x6, 8 -> 8x8.
        capture_grid_size = debug_cfg.get("capture_grid_size", 6)
        try:
            capture_grid_size = int(capture_grid_size)
        except (TypeError, ValueError):
            logger.warning("Invalid debug.capture_grid_size=%r, defaulting to 6", capture_grid_size)
            capture_grid_size = 6
        self.capture_grid_size = max(2, capture_grid_size)

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
        if "RESP" in text_clean or "REPA" in text_clean:
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
    def ocr_pool(self):
        """Lazy initialization of multiprocessing pool for parallel OCR."""
        if self._ocr_pool is None and easyocr and not self._ocr_pool_initialized:
            try:
                # Use spawn method for clean process initialization (especially on Windows)
                ctx = mp.get_context('spawn')
                self._ocr_pool = ctx.Pool(
                    processes=3,  # one worker per OCR task (respawn, incoming, click_to)
                    initializer=_init_ocr_reader,
                )
                self._ocr_pool_initialized = True
                logger.info("Initialized multiprocessing pool with 3 workers for parallel OCR (GPU enabled if available)")
            except Exception as e:
                logger.error("Failed to initialize multiprocessing pool: %s", e)
                self._ocr_pool_initialized = True  # Prevent retries
                return None
        return self._ocr_pool
    
    @property
    def game_state(self) -> GameState:
        """Current high-level game state based on recent battle event activity."""
        if time.time() - self._last_battle_event_ts < self._battle_quiet_period:
            return GameState.GAME_BATTLE
        return GameState.GAME_END

    def cleanup(self):
        """Clean up resources (call when shutting down)."""
        if self._ocr_pool is not None:
            try:
                self._ocr_pool.close()
                self._ocr_pool.join()
                logger.info("Multiprocessing pool shut down successfully")
            except Exception as e:
                logger.warning("Error shutting down multiprocessing pool: %s", e)
            self._ocr_pool = None
    
    def analyze_frame(self, frame, region=None):
        """
        Analyze a single frame and return game state.
        
        Args:
            frame: numpy array (BGR image from screen capture)
                region: Optional grid region index to analyze (1..N*N).
                    N is respawn_detection.grid_size (default 8).
                    If None, analyzes full frame.
            
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

        # Extract region if specified
        analysis_frame = frame
        total_regions = self.grid_rows * self.grid_cols
        if region and 1 <= region <= total_regions:
            analysis_frame = self.get_region(frame, region)
            logger.debug("Analyzing region %d (%dx%d)", region, analysis_frame.shape[1], analysis_frame.shape[0])
        
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
        
        # Detect respawn screen - only check configured respawn_region where RESPAWN text appears
        if region is None:
            # Full frame: pass full frame to OCR (it will extract regions internally)
            respawn_detected, confidence, method = self._detect_respawn(frame)
        elif region == self.respawn_region:
            # Already in respawn region: check for respawn
            respawn_detected, confidence, method = self._detect_respawn(analysis_frame)
        else:
            # Other regions: skip respawn detection (not present there)
            respawn_detected, confidence, method = False, 0.0, None
        
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
        
        # Save highlighted grid if enabled (every frame)
        if self.show_grid_highlighted:
            try:
                highlight = self.respawn_region if respawn_detected else None
                output_dir = Path("tests") / "test-output"
                output_dir.mkdir(parents=True, exist_ok=True)
                output_path = str(output_dir / "output_grid_highlighted.png")
                self.draw_grid(frame, highlight_region=highlight, 
                              output_path=output_path)
                logger.debug("Saved highlighted grid to %s (respawn: %s, region: %s)", 
                           output_path, respawn_detected, highlight)
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
        with self._background_ocr_lock:
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
        
        # Return cached result (may be stale) while background OCR runs
        return cached_result
    
    def _run_ocr_in_background(self):
        """Run OCR in background using multiprocessing for parallel region processing."""
        import time
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

                pool = self.ocr_pool
                if pool is None:
                    logger.warning("OCR pool not initialized")
                    return

                # Extract respawn and incoming regions (click_to has its own thread)
                respawn_frame = self.get_region(full_frame, self.respawn_region)
                incoming_frame = self.get_region(full_frame, self.incoming_region)

                # Static frame detection: two consecutive identical incoming frames → force GAME_END
                if (self._prev_incoming_frame is not None
                        and self._prev_incoming_frame.shape == incoming_frame.shape):
                    diff = np.mean(np.abs(incoming_frame.astype(np.int16)
                                         - self._prev_incoming_frame.astype(np.int16)))
                    if diff < self._static_frame_threshold:
                        self._static_incoming_count += 1
                        logger.debug("Analyzer: incoming_region static frame count=%d (diff=%.2f)",
                                     self._static_incoming_count, diff)
                        if self._static_incoming_count >= 2:
                            self._last_battle_event_ts = 0.0  # force GAME_END immediately
                            logger.debug("Analyzer: incoming_region static for 2 frames → GAME_END")
                    else:
                        self._static_incoming_count = 0
                else:
                    self._static_incoming_count = 0
                self._prev_incoming_frame = incoming_frame.copy()

                t1 = time.time()

                # Convert frames to bytes for pickling (multiprocessing requirement)
                respawn_bytes = respawn_frame.tobytes()
                respawn_shape = respawn_frame.shape
                respawn_dtype = str(respawn_frame.dtype)

                incoming_bytes = incoming_frame.tobytes()
                incoming_shape = incoming_frame.shape
                incoming_dtype = str(incoming_frame.dtype)
                t2 = time.time()

                # Submit both tasks to pool for parallel processing
                respawn_async = pool.apply_async(
                    _process_respawn_region,
                    (respawn_bytes, respawn_shape, respawn_dtype)
                )
                incoming_async = pool.apply_async(
                    _process_incoming_region,
                    (incoming_bytes, incoming_shape, incoming_dtype)
                )
                t3 = time.time()

                # Wait for results (blocks until both complete).
                # Timeout must cover cold-start pool worker init (~10s per worker × 3 workers)
                # in addition to actual OCR time (~4s). 120s is safe; normal runs complete in <10s.
                respawn_detected, respawn_ocr_time, respawn_text = respawn_async.get(timeout=120)
                incoming_detected, incoming_ocr_time, variant_name, incoming_text = incoming_async.get(timeout=120)
                t4 = time.time()

                # Log results
                if respawn_detected:
                    logger.debug("Analyzer: detected 'RESPAWN' text (matched: '%s')", respawn_text)

                if incoming_detected:
                    logger.info("\033[95m🚀 INCOMING MISSILE DETECTED (variant=%s) - text='%s'\033[0m", variant_name, incoming_text)
                else:
                    logger.debug("Analyzer: No text detected in incoming region %s", self.incoming_region)

                # Keep battle timestamp fresh whenever a battle event is active.
                # Use time.time() here (not current_time/t0) because OCR runs can
                # take 4-12s; using t0 would make the timestamp already expired by
                # battle_quiet_period at the moment it is written.
                if respawn_detected or incoming_detected:
                    self._last_battle_event_ts = time.time()

                # Update caches
                respawn_result = (True, 1.0, "ocr") if respawn_detected else (False, 0.0, None)
                with self._ocr_cache_lock:
                    self._ocr_cache['result'] = respawn_result
                    self._ocr_cache['timestamp'] = current_time

                incoming_result = (True, 1.0, "ocr") if incoming_detected else (False, 0.0, None)
                with self._incoming_cache_lock:
                    self._incoming_cache['result'] = incoming_result
                    self._incoming_cache['timestamp'] = current_time

                # Log timing
                logger.debug(
                    "Analyzer: Parallel OCR Timings - Extract: %.2fs, Serialize: %.2fs, Submit: %.2fs | "
                    "Respawn OCR: %.2fs | Incoming OCR: %.2fs | Total: %.2fs",
                    t1-t0, t2-t1, t3-t2, respawn_ocr_time, incoming_ocr_time, t4-t0
                )
            except mp.TimeoutError:
                logger.warning("Analyzer: OCR pool timeout")
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
        import time
        interval = 5.0
        while True:
            time.sleep(interval)
            if self.game_state == GameState.GAME_BATTLE:
                logger.debug("Click-to OCR skipped: GAME_BATTLE state active")
                continue
            with self._click_to_frame_lock:
                frame = self._click_to_latest_frame
            if frame is None:
                continue
            pool = self.ocr_pool
            if pool is None:
                continue
            try:
                click_to_frame = self.get_region(frame, self.click_to_region)
                click_to_bytes = click_to_frame.tobytes()
                click_to_shape = click_to_frame.shape
                click_to_dtype = str(click_to_frame.dtype)
                click_to_detected, _, click_to_text = pool.apply_async(
                    _process_click_to_region,
                    (click_to_bytes, click_to_shape, click_to_dtype)
                ).get(timeout=120)
                result = (True, 1.0, "ocr") if click_to_detected else (False, 0.0, None)
                with self._click_to_cache_lock:
                    self._click_to_cache['result'] = result
                    self._click_to_cache['timestamp'] = time.time()
                if click_to_detected:
                    logger.debug("Analyzer: detected 'Click to' text (matched: '%s')", click_to_text)
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
            'game_state': GameState.GAME_END,
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
    
    def get_region(self, frame, region_num):
        """
        Extract a grid region from the frame (left-to-right, top-to-bottom).
        Grid size is NxN where N is respawn_detection.grid_size.
        
        Args:
            frame: numpy array
            region_num: int from 1 to N*N
            
        Returns:
            numpy array: Cropped region
        """
        total_regions = self.grid_rows * self.grid_cols
        if not 1 <= region_num <= total_regions:
            logger.warning("Invalid region %d, returning full frame", region_num)
            return frame
        
        h, w = frame.shape[:2]
        region_h = h // self.grid_rows
        region_w = w // self.grid_cols
        
        # Convert region number to row/col (0-indexed)
        row = (region_num - 1) // self.grid_cols
        col = (region_num - 1) % self.grid_cols
        
        y1 = row * region_h
        y2 = y1 + region_h
        x1 = col * region_w
        x2 = x1 + region_w
        
        return frame[y1:y2, x1:x2]
    
    def draw_grid(self, frame, highlight_region=None, output_path=None, grid_size=None):
        """
        Draw a grid with region numbers on frame.
        
        Args:
            frame: numpy array
            highlight_region: int 1..N*N to highlight a specific region (green border)
            output_path: if provided, save annotated frame to this path
            grid_size: optional NxN override for drawing (e.g., 6 or 8)
            
        Returns:
            numpy array: Frame with grid overlay
        """
        frame_copy = frame.copy()
        h, w = frame.shape[:2]

        rows = self.grid_rows
        cols = self.grid_cols
        if grid_size is not None:
            try:
                size = max(2, int(grid_size))
                rows = size
                cols = size
            except (TypeError, ValueError):
                logger.warning("Invalid grid_size=%r; using default %dx%d", grid_size, rows, cols)
        
        region_h = h // rows
        region_w = w // cols
        
        # Draw grid lines (cyan dotted lines)
        for i in range(1, cols):
            x = w * i // cols
            for y in range(0, h, 10):
                cv2.line(frame_copy, (x, y), (x, min(y + 5, h)), (255, 255, 0), 1)
        
        for i in range(1, rows):
            y = h * i // rows
            for x in range(0, w, 10):
                cv2.line(frame_copy, (x, y), (min(x + 5, w), y), (255, 255, 0), 1)
        
        # Add region numbers
        total_regions = rows * cols
        for region in range(1, total_regions + 1):
            row = (region - 1) // cols
            col = (region - 1) % cols
            x = col * region_w + region_w // 2 - 15
            y = row * region_h + region_h // 2 + 10
            # Smaller text for more regions
            cv2.putText(frame_copy, str(region), (x, y), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
        
        # Highlight specific region if requested
        if highlight_region and 1 <= highlight_region <= total_regions:
            row = (highlight_region - 1) // cols
            col = (highlight_region - 1) % cols
            x1 = col * region_w
            y1 = row * region_h
            x2 = x1 + region_w
            y2 = y1 + region_h
            cv2.rectangle(frame_copy, (x1, y1), (x2, y2), (0, 255, 0), 4)
        
        # Save if path provided
        if output_path:
            cv2.imwrite(output_path, frame_copy)
            logger.info("Saved grid visualization to %s", output_path)
        
        return frame_copy
    
    def calibrate_respawn_detection(self, frame):
        """
        Debug method to help calibrate respawn detection thresholds.
        Shows HSV masks and pixel ratios for manual tuning.
        
        Args:
            frame: numpy array of respawn screen capture
            
        Returns:
            dict: Statistics about detected colors
        """
        h, w = frame.shape[:2]
        total_pixels = h * w
        
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        
        # Analyze text region
        center_y_start = int(h * 0.3)
        center_y_end = int(h * 0.6)
        center_x_start = int(w * 0.3)
        center_x_end = int(w * 0.7)
        center_region = hsv[center_y_start:center_y_end, center_x_start:center_x_end]
        
        text_mask = cv2.inRange(center_region, self.respawn_text_hsv_lower, self.respawn_text_hsv_upper)
        text_pixels = cv2.countNonZero(text_mask)
        text_ratio = text_pixels / total_pixels
        
        bar_mask = cv2.inRange(hsv, self.respawn_bar_hsv_lower, self.respawn_bar_hsv_upper)
        bar_pixels = cv2.countNonZero(bar_mask)
        bar_ratio = bar_pixels / total_pixels
        
        stats = {
            'text_pixels': text_pixels,
            'text_ratio': text_ratio,
            'text_threshold': self.respawn_text_threshold,
            'text_detected': text_ratio > self.respawn_text_threshold,
            'bar_pixels': bar_pixels,
            'bar_ratio': bar_ratio,
            'bar_threshold': self.respawn_bar_threshold,
            'bar_detected': bar_ratio > self.respawn_bar_threshold,
        }
        
        # Display debug windows
        cv2.imshow("Original Frame", frame)
        cv2.imshow("Text Mask (White)", cv2.cvtColor(text_mask, cv2.COLOR_GRAY2BGR))
        cv2.imshow("Bar Mask (Cyan)", cv2.cvtColor(bar_mask, cv2.COLOR_GRAY2BGR))
        
        # Combined visualization
        combined = frame.copy()
        combined[center_y_start:center_y_end, center_x_start:center_x_end][text_mask > 0] = [0, 255, 0]
        combined[bar_mask > 0] = [0, 255, 255]
        cv2.imshow("Combined Detection", combined)
        
        cv2.waitKey(0)
        cv2.destroyAllWindows()
        
        return stats
