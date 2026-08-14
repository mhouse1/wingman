from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEST_SCREENSHOTS_DIR = PROJECT_ROOT / "test_screenshots"
CONFIG_PATH = PROJECT_ROOT / "wingman" / "config.yaml"
# The gate corpus (integration_test/, refreshed unattended by make p1) is the
# single screenshot set (ADR 071). Standalone respawn/continue variants were
# retired 2026-08-13 with the old-layout purge.
TEST_SCREENSHOT = TEST_SCREENSHOTS_DIR / "integration_test" / "P1_050_RESPAWN_VISIBLE_NO_HEALTH.png"
TEST_SCREENSHOT_B = TEST_SCREENSHOTS_DIR / "integration_test" / "P1_030_BATTLE_HUD_MISSILES_4.png"
TEST_SCREENSHOT_D = TEST_SCREENSHOTS_DIR / "integration_test" / "P1_060_BATTLE_HUD_HEALTH_ALIVE_MISSILES_4.png"
# Open recapture items (ADR 071): tests referencing these are skip-marked and
# SELF-ACTIVATE the moment the file appears — drop the capture in, done.
# RESPAWNC.png: discolored NEW-layout respawn frame (OCR robustness for the
#   ADR 021 preprocessing pipeline).
# RESPAWND.png: near-miss text INSIDE the current respawn crop (Levenshtein
#   rejection; the old distractor sat outside the recalibrated crop and the
#   test passed vacuously — CR-015-03).
TEST_SCREENSHOT_C = TEST_SCREENSHOTS_DIR / "RESPAWNC.png"
TEST_SCREENSHOT_DISTRACTOR = TEST_SCREENSHOTS_DIR / "RESPAWND.png"
# continue.png/continue1.png retired 2026-08-13 (ADR 071): the gate corpus
# frame is the single click-to fixture, refreshed unattended by make p1.
TEST_SCREENSHOT_CONTINUE = TEST_SCREENSHOTS_DIR / "integration_test" / "P1_070_CLICK_TO_CONTINUE.png"
TEST_SCREENSHOT_INCOMING = TEST_SCREENSHOTS_DIR / "INCOMING.png"
SCRIPT_PATH = Path(__file__).resolve().parent / "analyzer_cli.py"