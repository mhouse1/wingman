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

WINGMAN_VERSION = "1.2.0"
# Key controls (change these to remap start/pause and cancel)
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


    # Initialize main components
    cap = Capture(region, monitor_index)
    analyzer = GameStateAnalyzer(cfg)
    
    # Load mission restart timing from config
    mission_cfg = cfg.get("mission", {})
    weapon_loop_interval = mission_cfg.get("weapon_loop_interval", 0.5)
    restart_retry_interval = mission_cfg.get("restart_retry_interval", 2.0)
    restart_delay_after_unlock = mission_cfg.get("restart_delay_after_unlock", 4.0)
    
    # Initialize controller with config-driven weapon loop interval and exit event
    ctrl = Controller(region, analyzer=analyzer, weapon_loop_interval=weapon_loop_interval, exit_event=exit_requested)

    # Load loop interval from config
    loop_interval_sec = cfg.get("loop_interval_sec", 0.5)

    # Robust mission restart logic
    was_respawning = False
    mission_active = False
    mission_started_at = None
    pending_mission_restart = False
    last_restart_attempt = 0.0
    restart_not_before = 0.0

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
                    # Wait for mission lock to release before restart
                    logger.info("Waiting for mission lock to release before restart...")
                    for _ in range(50):
                        if not ctrl.is_mission_running():
                            break
                        time.sleep(0.1)
                    else:
                        logger.warning("Timeout waiting for mission lock release; will keep retrying restart.")
                    pending_mission_restart = True
                    restart_not_before = time.time() + restart_delay_after_unlock
                    logger.info("Mission lock released (or release pending); delaying restart by %.1f seconds", restart_delay_after_unlock)
                    was_respawning = True
                    mission_active = False

                logger.info("\033[91mRESPAWN ACTIVE (%.0f%% confidence)\033[0m", game_state.get('respawn_confidence', 0) * 100)

                # Try to restart mission if needed
                if pending_mission_restart and (time.time() - last_restart_attempt > restart_retry_interval):
                    now = time.time()
                    if ctrl.is_mission_running():
                        last_restart_attempt = now
                        time.sleep(1)
                        continue
                    if now < restart_not_before:
                        time.sleep(1)
                        continue
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
            
            # Retry mission restart if pending and delay has passed (persists across gameplay resume)
            if pending_mission_restart and time.time() >= restart_not_before:
                if not ctrl.is_mission_running():
                    logger.info("Attempting to restart mission (delay expired)...")
                    if ctrl.restart_last_mission():
                        logger.info("Restarted last mission after respawn")
                        mission_active = True
                        mission_started_at = time.time()
                        pending_mission_restart = False
                    else:
                        logger.info("Mission restart attempt failed; will retry on next loop if needed.")

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
