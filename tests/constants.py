from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEST_SCREENSHOTS_DIR = PROJECT_ROOT / "test_screenshots"
CONFIG_PATH = PROJECT_ROOT / "wingman" / "config.yaml"
# The gate corpus (integration_test/, refreshed unattended by make p1) is the
# single screenshot set (ADR 071). Standalone respawn/continue variants were
# retired 2026-08-13 with the old-layout purge.
TEST_SCREENSHOT = TEST_SCREENSHOTS_DIR / "integration_test" / "P1_050_RESPAWN_VISIBLE_NO_HEALTH.png"
TEST_SCREENSHOT_B = TEST_SCREENSHOTS_DIR / "integration_test" / "P1_030_BATTLE_HUD_MISSILES_4.png"
# RESPAWNC (discolored positive) and RESPAWND ('natethegreat' Levenshtein
# distractor) are open recapture items (ADR 071): a discolored new-layout
# frame, and a frame with near-miss text INSIDE the current respawn crop —
# the old RESPAWND was already vacuous after the crop recalibration
# (code review CR-015-03). P1_060 stands in as a second negative meanwhile.
TEST_SCREENSHOT_D = TEST_SCREENSHOTS_DIR / "integration_test" / "P1_060_BATTLE_HUD_HEALTH_ALIVE_MISSILES_4.png"
# continue.png/continue1.png retired 2026-08-13 (ADR 071): the gate corpus
# frame is the single click-to fixture, refreshed unattended by make p1.
TEST_SCREENSHOT_CONTINUE = TEST_SCREENSHOTS_DIR / "integration_test" / "P1_070_CLICK_TO_CONTINUE.png"
TEST_SCREENSHOT_INCOMING = TEST_SCREENSHOTS_DIR / "INCOMING.png"
SCRIPT_PATH = Path(__file__).resolve().parent / "analyzer_cli.py"