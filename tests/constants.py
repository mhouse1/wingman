from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEST_SCREENSHOTS_DIR = PROJECT_ROOT / "test_screenshots"
CONFIG_PATH = PROJECT_ROOT / "wingman" / "config.yaml"
TEST_SCREENSHOT = TEST_SCREENSHOTS_DIR / "RESPAWN.png"
TEST_SCREENSHOT_B = TEST_SCREENSHOTS_DIR / "RESPAWNB.png"
TEST_SCREENSHOT_C = TEST_SCREENSHOTS_DIR / "RESPAWNC.png"
TEST_SCREENSHOT_D = TEST_SCREENSHOTS_DIR / "RESPAWND.png"
SCRIPT_PATH = Path(__file__).resolve().parent / "analyzer_cli.py"