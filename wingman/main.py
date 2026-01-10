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

# Note: enabling this will slow down startup by 10seconds due to easyocr/tensorflow init
# try:
#     import easyocr
# except Exception:
#     easyocr = None

from .capture import Capture
from .vision import Vision
from .controller import Controller
from .ai import SimpleAI


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
    # Determine fire control: prefer boolean `left_mouse_button`, fall back to `fire_button` string
    controls_cfg = cfg.get("controls", {})
    if controls_cfg.get("left_mouse_button") is True:
        fire_button = "left"
    else:
        fire_button = controls_cfg.get("fire_button", "left")
    ctrl = Controller(region, fire_button=fire_button)
    ai = SimpleAI(region, smoothing=cfg.get("aim", {}).get("smoothing", 0.25), fire_cooldown=cfg.get("aim", {}).get("fire_cooldown", 0.2))

    try:
        # Toggle start/pause of the main loop with the 'm' key.
        # Uses `keyboard` if available, otherwise falls back to OS-specific listeners.
        running = threading.Event()
        running.clear()  # start paused until first 'm'

        def toggle_running():
            if running.is_set():
                running.clear()
                logger.info("Paused — press 'm' to resume")
            else:
                running.set()
                logger.info("Running — press 'm' to pause")

        # Try keyboard global hook first
        keyboard_avail = keyboard_module is not None
        if keyboard_avail:
            logger.info("Press 'm' to toggle start/pause of main loop")
            try:
                keyboard_module.on_press_key('m', lambda e: toggle_running())
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
                                if ch.lower() == 'm':
                                    toggle_running()
                        except Exception:
                            pass
                        time.sleep(0.05)

                t = threading.Thread(target=msvcrt_listener, daemon=True)
                t.start()
                logger.info("Press 'm' in the console to toggle start/pause")
            except Exception:
                def input_listener():
                    while True:
                        try:
                            s = input()
                        except EOFError:
                            break
                        if s.strip().lower() == 'm':
                            toggle_running()

                t = threading.Thread(target=input_listener, daemon=True)
                t.start()
                logger.info("Type 'm' + Enter to toggle start/pause")

        while True:
            if not running.is_set():
                time.sleep(0.05)
                continue
            frame = cap.get_frame()
            enemies = vis.find_enemies(frame)
            logger.debug("Detected %d enemies", len(enemies))
            # action = ai.decide(enemies)
            # logger.debug("AI action: %s", action)
            # target = action.get("target")
            # if action.get("fire"):
            #     logger.info("Firing")
            #     ctrl.fire()

            #screen_numbers = scan_screen_for_numbers(frame)
            #print("Detected numbers:", screen_numbers)
            logger.info("Firing")
            ctrl.fire()
            ctrl.loiter()
            time.sleep(3)

            # 
            
    except KeyboardInterrupt:
        logger.info("Exiting")
    except Exception:
        logger.exception("Unhandled exception in main loop")


if __name__ == "__main__":
    main()
