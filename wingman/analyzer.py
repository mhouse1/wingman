"""Game state analyzer for detecting respawn, enemies, and other game conditions."""

import logging
import cv2
import numpy as np

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
        # Enemy detection (existing functionality)
        self.enemy_hsv_lower = np.array(config["enemy_hsv"]["lower"], dtype=np.uint8)
        self.enemy_hsv_upper = np.array(config["enemy_hsv"]["upper"], dtype=np.uint8)
        
        # Respawn detection config
        respawn_cfg = config.get("respawn_detection", {})
        
        # OCR-based respawn detection (looks for "RESPAWN" text)
        self.use_ocr = respawn_cfg.get("use_ocr", True)
        self.respawn_region = respawn_cfg.get("region", 32)  # Region 32 is bottom row, center-left (6x6 grid)
        
        # EasyOCR reader (lazy initialization on first use)
        self._ocr_reader = None
        
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
        
        # Grid configuration (6x6 = 36 regions)
        self.grid_rows = 6
        self.grid_cols = 6
    
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
                - enemies: list of (x, y, area) tuples
                - enemy_count: int
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
            'enemies': [],
            'enemy_count': 0,
        }
        
        # Detect respawn screen - only check configured respawn_region where RESPAWN text appears
        if region is None:
            # Full frame: extract respawn region and check for respawn
            respawn_region_frame = self.get_region(frame, self.respawn_region)
            respawn_detected, confidence, method = self._detect_respawn(respawn_region_frame)
        elif region == self.respawn_region:
            # Already in respawn region: check for respawn
            respawn_detected, confidence, method = self._detect_respawn(analysis_frame)
        else:
            # Other regions: skip respawn detection (not present there)
            respawn_detected, confidence, method = False, 0.0, None
        
        state['is_respawning'] = respawn_detected
        state['respawn_confidence'] = confidence
        state['respawn_method'] = method
        
        # Find enemies (skip if respawning to save processing)
        if not respawn_detected:
            state['enemies'] = self._find_enemies(analysis_frame)
            state['enemy_count'] = len(state['enemies'])
        
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
        
        Returns:
            tuple: (is_respawning: bool, confidence: float, method: str)
        """
        reader = self.ocr_reader
        if reader is None:
            logger.warning("OCR reader not initialized")
            return False, 0.0, None
        
        try:
            # Convert to grayscale for better OCR
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            
            # Try binary thresholding for clearer text (works better than CLAHE for clean text)
            # Otsu's method automatically finds optimal threshold
            _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            
            # Downscale for faster OCR (OCR works better on smaller images)
            small = cv2.resize(binary, None, fx=0.8, fy=0.8, interpolation=cv2.INTER_AREA)
            
            # Debug: save preprocessed images
            if self.debug:
                cv2.imwrite("debug_ocr_grayscale.png", gray)
                cv2.imwrite("debug_ocr_binary.png", binary)
                cv2.imwrite("debug_ocr_downscaled.png", small)
                logger.debug("Saved OCR preprocessing debug images")
            
            # Run EasyOCR - returns list of (bbox, text, confidence)
            results = reader.readtext(small, detail=1, paragraph=False)
            
            # Search for "RESPAWN" in detected text
            for (bbox, text, conf) in results:
                # Clean text: uppercase, remove spaces and non-alphabetic characters
                text_clean = ''.join(c for c in text.strip().upper() if c.isalpha())
                
                # Debug: always print what OCR detected
                if self.debug:
                    print('text clean:', text_clean, '(original:', text, ')')
                
                # Match "RESPA" (lenient - allows OCR misreads like RE$PA! → RESPA)
                if 'RESPA' in text_clean:
                    logger.debug("Analyzer: detected 'RESPAWN' text (matched text: '%s' from OCR: '%s')", text_clean, text)
                    return True, 1.0, "ocr"  # 100% confidence when found
            
            return False, 0.0, None
            
        except Exception as e:
            logger.warning("Analyzer: OCR detection failed: %s", e)
            return False, 0.0, None
    
    def _find_enemies(self, frame):
        """
        Find enemy positions in frame using HSV color detection.
        
        Returns:
            list: List of (x, y, area) tuples for each detected enemy
        """
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, self.enemy_hsv_lower, self.enemy_hsv_upper)
        
        # Morphological operations to reduce noise
        kernel = np.ones((3, 3), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        enemies = []
        
        for c in contours:
            area = cv2.contourArea(c)
            if area < 20:  # Filter out noise
                continue
            
            M = cv2.moments(c)
            if M["m00"] == 0:
                continue
            
            cx = int(M["m10"] / M["m00"])
            cy = int(M["m01"] / M["m00"])
            enemies.append((cx, cy, area))
        
        logger.debug("Analyzer: found %d enemies", len(enemies))
        return enemies
    
    def _empty_state(self):
        """Return empty game state for error cases."""
        return {
            'is_respawning': False,
            'respawn_confidence': 0.0,
            'respawn_method': None,
            'enemies': [],
            'enemy_count': 0,
        }
    
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
