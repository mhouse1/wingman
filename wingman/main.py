import argparse
import yaml
import time
import logging
import threading
import re
try:
    import keyboard as keyboard_module
except Exception:
    keyboard_module = None

# Key controls (change these to remap start/pause and cancel)
BEGIN_MISSION_KEY = 'enter'
CANCEL_MISSION_KEY = 'end'
EXIT_KEY = 'backspace'

# Note: enabling this will slow down startup by 10seconds due to easyocr/tensorflow init
# try:
#     import easyocr
# except Exception:
#     easyocr = None

from .capture import Capture
from .vision import Vision
from .controller import Controller
from .ai import SimpleAI
from .analyzer import GameStateAnalyzer


def load_config(path):
    with open(path, "r") as f:
        return yaml.safe_load(f)


def scan_screen_for_numbers(frame, reader=None):
    """
    Scan a screen frame for numbers using EasyOCR.
    
    Args:
        frame: numpy array (BGR image) from screen capture
        reader: optional EasyOCR Reader instance (will create if None)
    
    Returns:
        dict: Dictionary with detected text as keys and extracted numbers as values.
              Format: {"label_text": "123", "position_x_y": "456", ...}
    """
    if easyocr is None:
        return {"error": "easyocr not installed"}
    
    # Initialize reader if not provided
    if reader is None:
        try:
            reader = easyocr.Reader(['en'], gpu=True)
        except Exception as e:
            return {"error": f"Failed to initialize EasyOCR: {e}"}
    
    try:
        # Detect all text with bounding boxes and confidence
        results = reader.readtext(frame, detail=1, paragraph=False)
    except Exception as e:
        return {"error": f"EasyOCR read error: {e}"}
    
    # Extract numbers and associated text
    number_dict = {}
    
    for bbox, text, confidence in results:
        # Extract numbers from the detected text
        numbers = re.findall(r'\d+', text)
        
        if numbers:
            # Get position for labeling
            x_center = int(sum([p[0] for p in bbox]) / 4)
            y_center = int(sum([p[1] for p in bbox]) / 4)
            
            # Create key: use the full text if it contains non-digits, otherwise use position
            if re.search(r'[^\d\s]', text):
                # Text contains letters/labels
                key = text.strip()
            else:
                # Pure numbers, use position as key
                key = f"pos_{x_center}_{y_center}"
            
            # Join multiple numbers found in the same text region
            value = ' '.join(numbers)
            number_dict[key] = value
    
    return number_dict

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="wingman/config.yaml")
    parser.add_argument("--log-level", default="INFO", help="Logging level (DEBUG, INFO, WARNING, ERROR)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(asctime)s [%(levelname)s] %(message)s")
    logger = logging.getLogger("wingman")

    cfg = load_config(args.config)
    logger.info("Configuration loaded from %s", args.config)
    
    region = (
        cfg["region"]["left"],
        cfg["region"]["top"],
        cfg["region"]["width"],
        cfg["region"]["height"],
    )

    hsv_lower = cfg["enemy_hsv"]["lower"]
    hsv_upper = cfg["enemy_hsv"]["upper"]

    if args.dry_run:
        logger.info("Config loaded. Region: %s", region)
        logger.info("HSV lower/upper: %s %s", hsv_lower, hsv_upper)
        return

    cap = Capture(region)
    vis = Vision(hsv_lower, hsv_upper, debug=cfg.get("debug", {}).get("show_window", False))
    analyzer = GameStateAnalyzer(cfg)
    logger.info("GameStateAnalyzer initialized - respawn detection enabled")
    
    # Determine fire control: prefer boolean `left_mouse_button`, fall back to `fire_button` string
    controls_cfg = cfg.get("controls", {})
    if controls_cfg.get("left_mouse_button") is True:
        fire_button = "left"
    else:
        fire_button = controls_cfg.get("fire_button", "left")
    
    # Create exit event before controller
    exit_requested = threading.Event()
    exit_requested.clear()
    
    ctrl = Controller(region, fire_button=fire_button, exit_event=exit_requested)
    ai = SimpleAI(region, smoothing=cfg.get("aim", {}).get("smoothing", 0.25), fire_cooldown=cfg.get("aim", {}).get("fire_cooldown", 0.2))

    try:
        # Toggle start/pause of the main loop with the 'm' key.
        # Uses `keyboard` if available, otherwise falls back to OS-specific listeners.
        running = threading.Event()
        running.set()  # start running immediately with analyzer active

        def toggle_running():
            if running.is_set():
                running.clear()
                logger.info("Paused — press '%s' to resume", BEGIN_MISSION_KEY)
            else:
                running.set()
                logger.info("Resumed — press '%s' to pause", BEGIN_MISSION_KEY)

        # Try keyboard global hook first
        keyboard_avail = keyboard_module is not None
        if keyboard_avail:
            logger.info("Analyzer ACTIVE - Monitoring respawn state")
            logger.info("Hotkeys: U=J20 mission | Y=Loiter mission | X=Toggle weapon loop | '%s'=Pause | '%s'=Cancel | '%s'=Exit", BEGIN_MISSION_KEY, CANCEL_MISSION_KEY, EXIT_KEY)
            try:
                keyboard_module.on_press_key(BEGIN_MISSION_KEY, lambda e: toggle_running())
                def _on_cancel(e):
                    try:
                        ctrl.cancel_mission()
                    except Exception:
                        logger.debug("Controller not ready to cancel mission")
                    if running.is_set():
                        running.clear()
                        logger.info("Mission cancelled and paused")

                keyboard_module.on_press_key(CANCEL_MISSION_KEY, _on_cancel)
                
                def _on_exit(e):
                    logger.info("Exiting...")
                    exit_requested.set()
                
                keyboard_module.on_press_key(EXIT_KEY, _on_exit)
            except Exception:
                logger.warning("keyboard.on_press_key failed; falling back to console listener")
                keyboard_avail = False

        # Fallbacks: Windows console listener via msvcrt, otherwise input()
        if not keyboard_avail:
            try:
                import msvcrt

                def msvcrt_listener():
                    while True:
                        try:
                            if msvcrt.kbhit():
                                ch = msvcrt.getwch()
                                if ch.lower() == BEGIN_MISSION_KEY:
                                    toggle_running()
                                elif ch.lower() == CANCEL_MISSION_KEY:
                                    try:
                                        ctrl.cancel_mission()
                                    except Exception:
                                        logger.debug("Controller not ready to cancel mission")
                                    if running.is_set():
                                        running.clear()
                                        logger.info("Mission cancelled and paused")
                                elif ch == '\x08':  # backspace character
                                    logger.info("Exiting...")
                                    exit_requested.set()
                        except Exception:
                            pass
                        time.sleep(0.05)

                t = threading.Thread(target=msvcrt_listener, daemon=True)
                t.start()
                logger.info("Analyzer ACTIVE - Hotkeys: U=J20 | Y=Loiter | X=Weapon loop")
            except Exception:
                def input_listener():
                    while True:
                        try:
                            s = input()
                        except EOFError:
                            break
                        v = s.strip().lower()
                        if v == BEGIN_MISSION_KEY:
                            toggle_running()
                        elif v == CANCEL_MISSION_KEY:
                            try:
                                ctrl.cancel_mission()
                            except Exception:
                                logger.debug("Controller not ready to cancel mission")
                            if running.is_set():
                                running.clear()
                                logger.info("Mission cancelled and paused")
                        elif v == EXIT_KEY:
                            logger.info("Exiting...")
                            exit_requested.set()

                t = threading.Thread(target=input_listener, daemon=True)
                t.start()
                logger.info("Analyzer ACTIVE - Hotkeys: U=J20 | Y=Loiter | X=Weapon loop")

        # Track previous game state to detect respawn transitions
        was_respawning = False

        while True:
            if exit_requested.is_set():
                logger.info("Exit requested, shutting down")
                break
            if not running.is_set():
                time.sleep(0.05)
                continue
            
            # Capture and analyze frame
            frame = cap.get_frame()
            game_state = analyzer.analyze_frame(frame)
            logger.info("\033[91m Analyzing ... \033[0m")
            
            # Check if respawning - cancel missions and wait
            if game_state['is_respawning']:
                # Cancel mission on first detection of respawn (transition from gameplay to respawn)
                if not was_respawning:
                    logger.info("\033[91m⚠ RESPAWN DETECTED - Cancelling active missions\033[0m")
                    ctrl.cancel_mission()
                    was_respawning = True
                
                logger.info("\033[91mRESPAWN ACTIVE (%.0f%% confidence)\033[0m", 
                           game_state['respawn_confidence'] * 100)
                time.sleep(1)  # Wait while respawning
                continue
            
            # Gameplay resumed after respawn
            if was_respawning:
                logger.info("\033[92m✓ Gameplay resumed - ready for missions\033[0m")
                was_respawning = False
            
            # Normal gameplay - ready for missions
            time.sleep(1)  # Check every second

             
            
    except KeyboardInterrupt:
        logger.info("Exiting")
    except Exception:
        logger.exception("Unhandled exception in main loop")


if __name__ == "__main__":
    main()
