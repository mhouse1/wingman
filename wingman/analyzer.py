"""Game state analyzer for detecting respawn, enemies, and other game conditions."""

import logging
import cv2
import numpy as np
import threading
import time
from pathlib import Path
import os

try:
    import easyocr
except ImportError:
    easyocr = None

logger = logging.getLogger(__name__)


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
        
        # OCR-based respawn detection (looks for "RESPAWN" text)
        self.use_ocr = respawn_cfg.get("use_ocr", True)
        self.respawn_region = respawn_cfg.get("region", 32)  # Region 32 is bottom row, center-left (6x6 grid)
        self.incoming_region = respawn_cfg.get("incoming_region", 10)  # Region 10 for INCOMING missile warning
        
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
        
        # Background OCR thread for non-blocking analysis (scans both respawn and incoming)
        self._background_ocr_frame = None
        self._background_ocr_running = False
        self._background_ocr_thread = None
        
        # Fallback HSV detection (if OCR unavailable)
        self.respawn_text_hsv_lower = np.array(
            respawn_cfg.get("text_hsv_lower", [0, 0, 180]), 
            dtype=np.uint8
        )
        self.respawn_text_hsv_upper = np.array(
            respawn_cfg.get("text_hsv_upper", [180, 50, 255]), 
            dtype=np.uint8
        )
        
        self.debug = config.get("debug", {}).get("show_window", False)
        self.show_grid_highlighted = config.get("debug", {}).get("show_grid_highlighted", False)
        
        # Debug output directory for OCR preprocessing images
        debug_output_dir = config.get("debug", {}).get("debug_output_dir", "tests/test-output")
        self.debug_output_dir = Path(debug_output_dir)
        if not self.debug_output_dir.exists():
            self.debug_output_dir.mkdir(parents=True, exist_ok=True)
        
        # Grid configuration (6x6 = 36 regions)
        self.grid_rows = 6
        self.grid_cols = 6

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
    
    def analyze_frame(self, frame, region=None):
        """
        Analyze a single frame and return game state.
        
        Args:
            frame: numpy array (BGR image from screen capture)
            region: Optional grid region 1-36 to analyze (6x6 grid):
                     1  2  3  4  5  6
                     7  8  9 10 11 12
                    13 14 15 16 17 18
                    19 20 21 22 23 24
                    25 26 27 28 29 30
                    31 32 33 34 35 36
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
        
        # Extract region if specified
        analysis_frame = frame
        if region and 1 <= region <= 36:
            analysis_frame = self.get_region(frame, region)
            logger.debug("Analyzing region %d (%dx%d)", region, analysis_frame.shape[1], analysis_frame.shape[0])
        
        state = {
            'is_respawning': False,
            'respawn_confidence': 0.0,
            'respawn_method': None,
            'is_incoming': False,
            'incoming_confidence': 0.0,
            'incoming_method': None,
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
        
        # Cache expired - schedule background OCR (non-blocking)
        if not self._background_ocr_running:
            self._background_ocr_frame = frame
            self._background_ocr_thread = threading.Thread(
                target=self._run_ocr_in_background,
                daemon=True
            )
            self._background_ocr_thread.start()
            logger.debug("Background OCR scheduled")
        
        # Return cached result (may be stale) while background OCR runs
        return cached_result
    
    def _run_ocr_in_background(self):
        """Run OCR in background thread and update both respawn and incoming caches."""
        import time
        try:
            self._background_ocr_running = True
            t0 = time.time()
            current_time = t0
            full_frame = self._background_ocr_frame
            if full_frame is None:
                return
            
            reader = self.ocr_reader
            if reader is None:
                logger.warning("OCR reader not initialized")
                return
            
            try:
                # === RESPAWN DETECTION (Region configured in config) ===
                respawn_frame = self.get_region(full_frame, self.respawn_region)
                t1 = time.time()
                
                # Preprocess respawn region
                gray_respawn = cv2.cvtColor(respawn_frame, cv2.COLOR_BGR2GRAY)
                _, binary_respawn = cv2.threshold(gray_respawn, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                small_respawn = cv2.resize(binary_respawn, None, fx=0.7, fy=0.7, interpolation=cv2.INTER_AREA)
                t2 = time.time()
                
                # Run OCR on respawn region
                results_respawn = reader.readtext(small_respawn, detail=1, paragraph=False)
                t3 = time.time()
                
                # Check for RESPAWN text
                respawn_detected = False
                for (bbox, text, conf) in results_respawn:
                    text_clean = ''.join(c for c in text.strip().upper() if c.isalpha())
                    if self.debug:
                        logger.debug("Analyzer: Respawn OCR - clean: %s, original: %s", text_clean, text)
                    if self._is_respawn_text(text_clean):
                        logger.debug("Analyzer: detected 'RESPAWN' text (matched: '%s' from OCR: '%s')", text_clean, text)
                        respawn_detected = True
                        break
                
                # Update respawn cache
                respawn_result = (True, 1.0, "ocr") if respawn_detected else (False, 0.0, None)
                with self._ocr_cache_lock:
                    self._ocr_cache['result'] = respawn_result
                    self._ocr_cache['timestamp'] = current_time
                
                # === INCOMING MISSILE DETECTION (Region 10) ===
                incoming_frame = self.get_region(full_frame, self.incoming_region)
                t4 = time.time()
                
                # Preprocess incoming region and try the same variants used in tests.
                gray_incoming = cv2.cvtColor(incoming_frame, cv2.COLOR_BGR2GRAY)
                _, binary_incoming = cv2.threshold(gray_incoming, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                variants = {
                    "binary_otsu_1p0": binary_incoming,
                    "binary_otsu_up_1p4": cv2.resize(binary_incoming, None, fx=1.4, fy=1.4, interpolation=cv2.INTER_CUBIC),
                    "binary_otsu_inv_1p4": cv2.bitwise_not(cv2.resize(binary_incoming, None, fx=1.4, fy=1.4, interpolation=cv2.INTER_CUBIC)),
                    "gray_up_1p4": cv2.resize(gray_incoming, None, fx=1.4, fy=1.4, interpolation=cv2.INTER_CUBIC),
                }
                t5 = time.time()

                # Run OCR on incoming region with test-proven settings.
                incoming_detected = False
                matched_variant = None
                last_results_incoming = []
                for variant_name, variant_img in variants.items():
                    results_incoming = reader.readtext(variant_img, detail=0, paragraph=True)
                    last_results_incoming = results_incoming
                    extracted_text = " ".join(str(result) for result in results_incoming)
                    normalized = " ".join(extracted_text.upper().split())
                    logger.debug("Analyzer: Incoming OCR variant=%s normalized='%s' raw=%s", variant_name, normalized, results_incoming)
                    if self._is_incoming_text(normalized.replace(" ", "")):
                        logger.info("\033[95m🚀 INCOMING MISSILE DETECTED (variant=%s) - text='%s'\033[0m", variant_name, normalized)
                        incoming_detected = True
                        matched_variant = variant_name
                        break

                t6 = time.time()
                if not last_results_incoming:
                    logger.debug("Analyzer: No text detected in incoming region 10")
                
                # Update incoming cache
                incoming_result = (True, 1.0, "ocr") if incoming_detected else (False, 0.0, None)
                with self._incoming_cache_lock:
                    self._incoming_cache['result'] = incoming_result
                    self._incoming_cache['timestamp'] = current_time
                
                # Debug: save preprocessed images if enabled
                if self.debug:
                    cv2.imwrite(str(self.debug_output_dir / "debug_ocr_respawn_region.png"), respawn_frame)
                    cv2.imwrite(str(self.debug_output_dir / "debug_ocr_respawn_preprocessed.png"), small_respawn)
                    cv2.imwrite(str(self.debug_output_dir / "debug_ocr_incoming_region.png"), incoming_frame)
                    if matched_variant:
                        cv2.imwrite(str(self.debug_output_dir / "debug_ocr_incoming_preprocessed.png"), variants[matched_variant])
                    else:
                        cv2.imwrite(str(self.debug_output_dir / "debug_ocr_incoming_preprocessed.png"), variants["binary_otsu_up_1p4"])
                
                # Log timing
                logger.debug(
                    "Analyzer: Dual OCR Timings - Respawn extract: %.2fs, preprocess: %.2fs, OCR: %.2fs | "
                    "Incoming extract: %.2fs, preprocess: %.2fs, OCR: %.2fs | Total: %.2fs",
                    t1-t0, t2-t1, t3-t2, t4-t3, t5-t4, t6-t5, t6-t0
                )
            except Exception as e:
                logger.warning("Analyzer: OCR detection failed: %s", e)
        finally:
            self._background_ocr_running = False
    
    def _empty_state(self):
        """Return empty game state for error cases."""
        return {
            'is_respawning': False,
            'respawn_confidence': 0.0,
            'respawn_method': None,
            'is_incoming': False,
            'incoming_confidence': 0.0,
            'incoming_method': None,
        }
    
    def reset_cache(self):
        """Reset OCR caches - useful when switching between different images/scenes."""
        self._ocr_cache['timestamp'] = 0.0
        self._ocr_cache['result'] = (False, 0.0, None)
        self._incoming_cache['timestamp'] = 0.0
        self._incoming_cache['result'] = (False, 0.0, None)
        logger.debug("OCR caches reset")
    
    def get_region(self, frame, region_num):
        """
        Extract a grid region from the frame (1-36, left-to-right, top-to-bottom).
        
        Grid layout (6x6):
             1  2  3  4  5  6
             7  8  9 10 11 12
            13 14 15 16 17 18
            19 20 21 22 23 24
            25 26 27 28 29 30
            31 32 33 34 35 36
        
        Args:
            frame: numpy array
            region_num: int from 1 to 36
            
        Returns:
            numpy array: Cropped region
        """
        if not 1 <= region_num <= 36:
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
    
    def draw_grid(self, frame, highlight_region=None, output_path=None):
        """
        Draw 6x6 grid with region numbers on frame.
        
        Args:
            frame: numpy array
            highlight_region: int 1-36 to highlight a specific region (green border)
            output_path: if provided, save annotated frame to this path
            
        Returns:
            numpy array: Frame with grid overlay
        """
        frame_copy = frame.copy()
        h, w = frame.shape[:2]
        
        region_h = h // self.grid_rows
        region_w = w // self.grid_cols
        
        # Draw grid lines (cyan dotted lines)
        for i in range(1, self.grid_cols):
            x = w * i // self.grid_cols
            for y in range(0, h, 10):
                cv2.line(frame_copy, (x, y), (x, min(y + 5, h)), (255, 255, 0), 1)
        
        for i in range(1, self.grid_rows):
            y = h * i // self.grid_rows
            for x in range(0, w, 10):
                cv2.line(frame_copy, (x, y), (min(x + 5, w), y), (255, 255, 0), 1)
        
        # Add region numbers
        for region in range(1, 37):
            row = (region - 1) // self.grid_cols
            col = (region - 1) % self.grid_cols
            x = col * region_w + region_w // 2 - 15
            y = row * region_h + region_h // 2 + 10
            # Smaller text for more regions
            cv2.putText(frame_copy, str(region), (x, y), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
        
        # Highlight specific region if requested
        if highlight_region and 1 <= highlight_region <= 36:
            row = (highlight_region - 1) // self.grid_cols
            col = (highlight_region - 1) % self.grid_cols
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
