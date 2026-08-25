"""PROFILE overlay recovery (ADR 093).

On 2026-08-24 a full-screen PROFILE overlay opened over the lobby from wingman's
own click and never closed. It is not the lobby, not a calibrated popup, and not
the exit dialog, so all three recovery paths found nothing to act on and the
session sat inert for 110 minutes — zero OCR, zero control actions, zero state
transitions, while still logging.

These tests pin the two halves of the fix: that PROFILE is eligible on the
GAME_LOBBY blackout gate (the state the livelock actually happened in, which is
NOT in STALL_ACTION_STATES), and that the crop reads the real captured frames.
"""

import time
from pathlib import Path

import yaml

from wingman import analyzer as analyzer_module

CONFIG = yaml.safe_load(Path("wingman/config.yaml").read_text())
CROPS = CONFIG["crops"]
# The tracked reference frame, saved beside the other STALL_* crops. The
# original anomaly captures live in a gitignored directory that gets swept, so
# pinning the regression to them made it silently skip.
REFERENCE = Path("test_screenshots/STALL_PROFILE.png")


def test_profile_crop_is_calibrated():
    assert "STALL_PROFILE" in CROPS, "PROFILE crop missing — ADR 093"
    assert "PROFILE" in (CROPS["STALL_PROFILE"].get("text") or []), \
        "PROFILE crop needs its text list or it can never match"


def test_profile_dismiss_is_a_separate_click_target():
    """The title is a label, not a button. Clicking it does not close anything."""
    assert "STALL_PROFILE_DISMISS" in CROPS, "STALL_PROFILE_DISMISS crop missing — ADR 093"
    title = CROPS["STALL_PROFILE"]["coords"]
    close = CROPS["STALL_PROFILE_DISMISS"]["coords"]
    assert close[0][0] > 0.5 > title[1][0], \
        "close control must be in the opposite corner from the title"


def test_profile_is_in_the_stall_recovery_batch():
    assert "STALL_PROFILE" in analyzer_module.STALL_RECOVERY_CROPS


class _Stub:
    """Minimal stand-in exposing only what _stall_recovery_targets touches."""
    def __init__(self, crops, blackout_since=0.0, stall_since=0.0, after_s=15.0):
        self.crops = crops
        self._lobby_blackout_since = blackout_since
        self._stall_state_since = stall_since
        self._stall_action_after_s = after_s
        self._unready_since = 0.0
        self._unready_dwell_s = 30.0


def _targets(stub, state):
    return analyzer_module.GameStateAnalyzer._stall_recovery_targets(stub, state)


ALL_CROPS = {c: {} for c in ("STALL_PROFILE", "STALL_RETRY", "STALL_EXIT_TO_DESKTOP",
                             "STALL_AIRCRAFT", "STALL_MULTI_PLAYER")}


def test_profile_eligible_on_a_sustained_lobby_blackout():
    """The state the livelock happened in. GAME_LOBBY is not in
    STALL_ACTION_STATES, so only the ADR 087 blackout gate can reach it."""
    stub = _Stub(ALL_CROPS, blackout_since=time.time() - 60)
    assert "STALL_PROFILE" in _targets(stub, analyzer_module.GameState.GAME_LOBBY)


def test_profile_not_eligible_before_the_blackout_matures():
    stub = _Stub(ALL_CROPS, blackout_since=time.time() - 2)
    assert "STALL_PROFILE" not in _targets(stub, analyzer_module.GameState.GAME_LOBBY)


def test_profile_not_eligible_during_healthy_lobby():
    """No blackout at all — recovery actions must stay out of normal operation."""
    stub = _Stub(ALL_CROPS, blackout_since=0.0)
    assert _targets(stub, analyzer_module.GameState.GAME_LOBBY) == []


def test_profile_is_scanned_before_the_generic_targets():
    """Scan order: the batch stops at the first hit, so the specific screen
    must not be pre-empted by a generic one."""
    stub = _Stub(ALL_CROPS, blackout_since=time.time() - 60)
    targets = _targets(stub, analyzer_module.GameState.GAME_LOBBY)
    assert targets[0] == "STALL_PROFILE", targets


def test_absent_crop_is_simply_not_offered():
    """An uncalibrated PROFILE must degrade, never raise."""
    stub = _Stub({"STALL_EXIT_TO_DESKTOP": {}}, blackout_since=time.time() - 60)
    assert "STALL_PROFILE" not in _targets(stub, analyzer_module.GameState.GAME_LOBBY)


def test_reference_frame_is_present_and_real():
    """Tracked, so this cannot silently start skipping."""
    assert REFERENCE.is_file(), f"{REFERENCE} missing — ADR 093 reference frame"
    assert REFERENCE.stat().st_size > 10_000, "reference frame looks like a placeholder"


# The real-OCR regression for this crop lives in tests/test_stall_crops_ocr.py
# alongside the other STALL_* crops. That harness is strictly better than a
# hand-rolled easyocr call here: it goes through the production
# _process_crop_region path, and it also asserts the crop does NOT fire on an
# ordinary lobby frame — a false positive would click the screen corner during
# healthy play. This file keeps the gating and wiring tests; the pixels are
# tested there.
