from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEST_SCREENSHOTS_DIR = PROJECT_ROOT / "test_screenshots"
CONFIG_PATH = PROJECT_ROOT / "wingman" / "config.yaml"
# The gate corpus (integration_test/, refreshed unattended by make p1) is the
# single screenshot set (ADR 072). Standalone respawn/continue variants were
# retired 2026-08-13 with the old-layout purge.
TEST_SCREENSHOT = TEST_SCREENSHOTS_DIR / "integration_test" / "P1_050_RESPAWN_VISIBLE_NO_HEALTH.png"
TEST_SCREENSHOT_B = TEST_SCREENSHOTS_DIR / "integration_test" / "P1_030_BATTLE_HUD_MISSILES_4.png"
TEST_SCREENSHOT_D = TEST_SCREENSHOTS_DIR / "integration_test" / "P1_060_BATTLE_HUD_HEALTH_ALIVE_MISSILES_4.png"
# Respawn variant set (RESPAWNB/C/D) retired outright — ADR 072 decision 3:
# only the crop location changed in the game update, so per-layout variant
# maintenance is cost without coverage. Accepted losses (revisit if a future
# update changes overlay RENDERING, not just position): discolored-frame OCR
# robustness (CR-015-04) and the Levenshtein-distractor negative (CR-015-03).
# continue.png/continue1.png retired 2026-08-13 (ADR 072): the gate corpus
# frame is the single click-to fixture, refreshed unattended by make p1.
TEST_SCREENSHOT_CONTINUE = TEST_SCREENSHOTS_DIR / "integration_test" / "P1_070_CLICK_TO_CONTINUE.png"
TEST_SCREENSHOT_INCOMING = TEST_SCREENSHOTS_DIR / "INCOMING.png"
SCRIPT_PATH = Path(__file__).resolve().parent / "analyzer_cli.py"