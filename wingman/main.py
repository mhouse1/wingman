import argparse
import yaml
import time
import logging
import threading
import re
from datetime import datetime
try:
    import keyboard as keyboard_module
except Exception:
    keyboard_module = None

WINGMAN_VERSION = "1.0.1"
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
    monitor_index = cfg["region"].get("monitor", 1)

    hsv_lower = cfg["enemy_hsv"]["lower"]
    hsv_upper = cfg["enemy_hsv"]["upper"]
    # Toggle start/pause of the main loop with the 'm' key.
    # Uses `keyboard` if available, otherwise falls back to OS-specific listeners.
    running = threading.Event()
    running.set()  # start running immediately with analyzer active
    exit_requested = threading.Event()

    def toggle_running():
        if running.is_set():
            running.clear()
            logger.info("Paused — press '%s' to resume", BEGIN_MISSION_KEY)
        else:
            running.set()
            logger.info("Resumed — press '%s' to pause", BEGIN_MISSION_KEY)

    # Setup input listeners (keyboard or fallback)
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
                                logger.info("Mission cancelled")
                            except Exception:
                                logger.debug("Controller not ready to cancel mission")
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
                        logger.info("Mission cancelled")
                    except Exception:
                        logger.debug("Controller not ready to cancel mission")
                elif v == EXIT_KEY:
                    logger.info("Exiting...")
                    exit_requested.set()
        t = threading.Thread(target=input_listener, daemon=True)
        t.start()
        logger.info("Analyzer ACTIVE - Hotkeys: U=J20 | Y=Loiter | X=Weapon loop")

    # Initialize main components
    cap = Capture(region, monitor_index)
    analyzer = GameStateAnalyzer(cfg)
    ctrl = Controller(cfg, logger)

    # Load loop interval from config
    loop_interval_sec = cfg.get("loop_interval_sec", 0.5)

    # Robust mission restart logic
    was_respawning = False
    mission_active = False
    mission_started_at = None
    pending_mission_restart = False
    restart_retry_interval = 2.0  # seconds between restart attempts
    last_restart_attempt = 0.0

    try:
        while True:
            loop_start = time.time()
            if exit_requested.is_set():
                logger.info("Exit requested, shutting down")
                break
            if not running.is_set():
                time.sleep(0.05)
                continue

            # Capture and analyze frame
            frame = cap.get_frame()
            game_state = analyzer.analyze_frame(frame)

            # Detect respawn
            if game_state.get('is_respawning'):
                if not was_respawning:
                    logger.info("\033[91m⚠ RESPAWN DETECTED - Cancelling active missions\033[0m")
                    ctrl.cancel_mission()
                    # Wait for mission to fully complete (lock released)
                    if hasattr(ctrl, '_mission_complete'):
                        logger.info("Waiting for mission to fully cancel before restart...")
                        # Wait up to 5 seconds for mission to complete
                        for _ in range(50):
                            if ctrl._mission_complete.is_set():
                                break
                            time.sleep(0.1)
                        else:
                            logger.warning("Timeout waiting for mission to complete; will attempt restart anyway.")
                    pending_mission_restart = True
                    was_respawning = True
                    mission_active = False

                logger.info("\033[91mRESPAWN ACTIVE (%.0f%% confidence)\033[0m", game_state.get('respawn_confidence', 0) * 100)

                # Try to restart mission if needed
                if pending_mission_restart and (time.time() - last_restart_attempt > restart_retry_interval):
                    logger.info("Attempting to restart mission after respawn...")
                    if ctrl.restart_last_mission():
                        logger.info("Restarted last mission after respawn")
                        mission_active = True
                        mission_started_at = time.time()
                        pending_mission_restart = False
                    else:
                        logger.info("Mission restart attempt failed, will retry")
                    last_restart_attempt = time.time()

                time.sleep(1)
                continue

            # Gameplay resumed after respawn
            if was_respawning:
                logger.info("\033[92m✓ Gameplay resumed - ready for missions\033[0m")
                was_respawning = False
                # Immediately restart the last mission when gameplay resumes
                logger.info("Attempting to restart mission after gameplay resumes...")
                if ctrl.restart_last_mission():
                    logger.info("Restarted last mission after respawn (on resume)")
                    mission_active = True
                    mission_started_at = time.time()
                else:
                    logger.info("Mission restart attempt failed after resume; will retry on next loop if needed.")

            # Enforce configurable loop interval
            elapsed = time.time() - loop_start
            if elapsed < loop_interval_sec:
                time.sleep(loop_interval_sec - elapsed)
    except KeyboardInterrupt:
        logger.info("Exiting")
    except Exception:
        logger.exception("Unhandled exception in main loop")


if __name__ == "__main__":
    main()
