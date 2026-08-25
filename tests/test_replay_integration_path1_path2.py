"""ADR037 — PATH1 and PATH2 OCR regression tests.

These tests run the full replay pipeline with the real GameStateAnalyzer (no
monkeypatching of OCR).  Screenshots in test_screenshots/integration_test/ must
be real game captures; all-black placeholder images cause the test to be skipped
automatically.

Run with:
    uv run --active pytest tests/test_replay_integration_path1_path2.py -v -s
or via the Makefile:
    make ocr

Marked @pytest.mark.slow so they are excluded from the default `make test` run.
"""

import json
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml

import wingman.main as wingman_main

# ---------------------------------------------------------------------------
# Skip guard: easyocr must be importable
# ---------------------------------------------------------------------------
try:
    import easyocr as _easyocr_check  # noqa: F401
    _EASYOCR_AVAILABLE = True
except ImportError:
    _EASYOCR_AVAILABLE = False

pytestmark = pytest.mark.slow

_REPO = Path(__file__).resolve().parent.parent
_REAL_CONFIG_PATH = _REPO / "wingman" / "config.yaml"
_SCREENSHOT_DIR = _REPO / "test_screenshots" / "integration_test"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_dummy_screenshot(path: Path) -> bool:
    """Return True when a screenshot is an all-black placeholder (all zeros)."""
    img = cv2.imread(str(path))
    if img is None:
        return True
    return not np.any(img)


def _check_screenshots_ready(names: list[str]) -> None:
    """Skip the test if any required screenshot is missing or still a placeholder."""
    if not _EASYOCR_AVAILABLE:
        pytest.skip("easyocr not installed — OCR integration tests require easyocr")
    for name in names:
        p = _SCREENSHOT_DIR / name
        if not p.exists():
            pytest.skip(f"Screenshot not found: {p}")
        if _is_dummy_screenshot(p):
            pytest.skip(
                f"Screenshot is an all-black placeholder: {name} — "
                "replace with a real game capture before running OCR tests"
            )


def _build_test_config(tmp_path: Path) -> Path:
    """Load the real config and apply test-friendly overrides."""
    cfg = yaml.safe_load(_REAL_CONFIG_PATH.read_text(encoding="utf-8"))

    cfg["loop_interval_sec"] = 0.5

    mission = cfg.setdefault("mission", {})
    mission["waiting_fallback_enabled"] = False

    debug = cfg.setdefault("debug", {})
    debug["show_window"] = False
    debug["show_grid_highlighted"] = False
    debug["draw_markers"] = False
    debug["debug_output_dir"] = str(tmp_path / "debug")

    out = tmp_path / "test_config.yaml"
    out.write_text(
        yaml.dump(cfg, default_flow_style=None, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return out


def _build_path1_ocr_yaml(tmp_path: Path) -> Path:
    """Write PATH1_OCR with generous settle windows for real OCR timing."""
    payload = {
        "PATH1_OCR": [
            {
                "screenshot_name": "P1_000_LOBBY_PLAY.png",
                "injection_time_s": 0.0,
                "expected_state": "GAME_LOBBY",
                "max_settle_time_s": 5.0,
                "inject_trigger": "manual_reset",
            },
            {
                "screenshot_name": "P1_010_WAITING_CANCEL_VISIBLE.png",
                "injection_time_s": 10.0,
                "expected_state": "GAME_WAITING",
                "expected_trigger": "cancel_detected",
                "max_settle_time_s": 8.0,
            },
            {
                "screenshot_name": "P1_020_GOOD_LUCK_VISIBLE.png",
                "injection_time_s": 20.0,
                "expected_state": "GAME_STARTING",
                "expected_trigger": "good_luck_detected",
                "max_settle_time_s": 8.0,
                "inject_trigger": "good_luck_detected",
            },
            {
                "screenshot_name": "P1_030_BATTLE_HUD_MISSILES_4.png",
                "injection_time_s": 30.0,
                "expected_state": "GAME_BATTLE",
                "expected_trigger": "battle_started",
                "max_settle_time_s": 5.0,
            },
            {
                "screenshot_name": "P1_040_BATTLE_HUD_MISSILES_0_HEALTH_ALIVE.png",
                "injection_time_s": 40.0,
                "expected_state": "GAME_BATTLE",
                "expected_trigger": "missiles_empty",
                "max_settle_time_s": 8.0,
            },
            {
                "screenshot_name": "P1_050_RESPAWN_VISIBLE_NO_HEALTH.png",
                "injection_time_s": 60.0,
                "expected_state": "GAME_BATTLE",
                "expected_trigger": "respawn_detected",
                "max_settle_time_s": 12.0,
            },
            {
                "screenshot_name": "P1_060_BATTLE_HUD_HEALTH_ALIVE_MISSILES_4.png",
                "injection_time_s": 75.0,
                "expected_state": "GAME_BATTLE",
                "expected_trigger": "restart_last_mission",
                "max_settle_time_s": 12.0,
            },
            {
                "screenshot_name": "P1_070_CLICK_TO_CONTINUE.png",
                "injection_time_s": 88.0,
                "expected_state": "GAME_END_B",
                "expected_trigger": "click_to_detected",
                "max_settle_time_s": 5.0,
                "inject_trigger": "click_to_detected",
            },
            {
                "screenshot_name": "P1_080_LOBBY_AFTER_MISSION.png",
                "injection_time_s": 89.0,
                "expected_state": "GAME_LOBBY",
                "expected_trigger": "continue_clicked",
                "max_settle_time_s": 5.0,
                "inject_trigger": "continue_clicked",
            },
        ]
    }
    out = tmp_path / "path1_ocr.yaml"
    out.write_text(
        yaml.dump(payload, default_flow_style=None, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return out


def _build_path2_ocr_yaml(tmp_path: Path) -> Path:
    """Write PATH2_OCR with generous settle windows for real OCR timing."""
    content = """\
# PATH2_OCR: real-OCR timing clone of PATH2.
# inject_trigger: manual_force_battle on step 0 seeds the FSM into GAME_BATTLE.
# inject_trigger: manual_takeover on the second P1_030 step fires the manual-mode transition.
PATH2_OCR:
  # Seed GAME_BATTLE immediately via inject_trigger; no assertion on this step.
  # P1_030 reused (P2_000 deleted 2026-08-13 — byte-identical copy).
  - screenshot_name: P1_030_BATTLE_HUD_MISSILES_4.png
    injection_time_s: 0.0
    inject_trigger: manual_force_battle
  # BATTLE (missiles=0) at t=8 s.  Ammo OCR detects 0 within 10 s.
  # P1_040 reused (P2_010 deleted 2026-08-13 — same battle HUD, missiles=0).
  - screenshot_name: P1_040_BATTLE_HUD_MISSILES_0_HEALTH_ALIVE.png
    injection_time_s: 8.0
    expected_state: GAME_BATTLE
    expected_trigger: missiles_empty
    max_settle_time_s: 10.0
  # Manual takeover: inject_trigger fires manual_takeover immediately.
  # missiles_empty at the previous step fires eject_started (ADR 056), so the
  # FSM sits in GAME_BATTLE_EJECT here — the old GAME_BATTLE expectation could
  # never be met and masked the real flow behind an out-of-order harness failure.
  - screenshot_name: P1_030_BATTLE_HUD_MISSILES_4.png
    injection_time_s: 20.0
    expected_state: GAME_BATTLE_EJECT
    expected_trigger: manual_mode
    max_settle_time_s: 5.0
    inject_trigger: manual_takeover
  # GAME_BATTLE_MANUAL confirmed immediately after inject.
  - screenshot_name: P1_030_BATTLE_HUD_MISSILES_4.png
    injection_time_s: 23.0
    expected_state: GAME_BATTLE_MANUAL
    expected_trigger: manual_mode_entered
    max_settle_time_s: 5.0
  # RESPAWN at t=35 s.  Respawn OCR runs in background; allow 12 s.
  - screenshot_name: P1_050_RESPAWN_VISIBLE_NO_HEALTH.png
    injection_time_s: 35.0
    expected_state: GAME_BATTLE_MANUAL
    expected_trigger: respawn_detected
    max_settle_time_s: 12.0
  # ALIVE at t=52 s.  Death ended manual takeover at the respawn step (respawn_reset),
  # so health returning restarts the mission immediately via the alive event.
  # P1_060 reused (P2_050 deleted 2026-08-13 — byte-identical copy).
  - screenshot_name: P1_060_BATTLE_HUD_HEALTH_ALIVE_MISSILES_4.png
    injection_time_s: 52.0
    expected_state: GAME_BATTLE
    expected_trigger: restart_last_mission
    max_settle_time_s: 12.0
  # End-of-mission screen at t=65 s.  inject_trigger forces FSM into GAME_END_B.
  # (previous step's deadline is t=64 s; 1 s buffer before this step activates.)
  - screenshot_name: P1_070_CLICK_TO_CONTINUE.png
    injection_time_s: 65.0
    expected_state: GAME_END_B
    expected_trigger: click_to_detected
    max_settle_time_s: 5.0
    inject_trigger: click_to_detected
  # Back to LOBBY at t=66 s.  inject_trigger fires continue_clicked.
  - screenshot_name: P1_080_LOBBY_AFTER_MISSION.png
    injection_time_s: 66.0
    expected_state: GAME_LOBBY
    expected_trigger: continue_clicked
    max_settle_time_s: 5.0
    inject_trigger: continue_clicked
"""
    out = tmp_path / "path2_ocr.yaml"
    out.write_text(content, encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# Fake infrastructure (no real OS input; real GameStateAnalyzer is kept)
# ---------------------------------------------------------------------------

class FakePerformanceTracker:
    """Stub tracker that records which OCR crops were actually scanned.

    The ``ocr_crops`` dict (crop_name → scan count) lets tests assert that OCR
    ran for every expected crop, which catches silent failures like a missing
    method that causes all OCR calls to be swallowed by the exception handler.
    """

    def __init__(self, *_args, **_kwargs):
        self.ocr_crops: dict[str, int] = {}  # crop_name → scan count
        self.reaction_count: int = 0

    def record_ocr_crop(self, crop_name: str, _seconds: float) -> None:
        self.ocr_crops[crop_name] = self.ocr_crops.get(crop_name, 0) + 1

    def record_reaction(self, _seconds: float) -> None:
        self.reaction_count += 1

    def on_enter_game_lobby(self) -> None:
        pass

    def on_session_end(self) -> None:
        pass


class FakeController:
    """Minimal controller stub: records intents, simulates mission-running state."""

    def __init__(self, *_args, **_kwargs):
        self._intents: list[dict] = []
        self._mission_running = True  # mission is "running" at battle start
        self._auto_respawn = True

    # --- Mission state ---
    def is_mission_running(self) -> bool:
        return self._mission_running

    def is_mission_teardown_in_progress(self) -> bool:
        return False  # stub teardown is instantaneous

    def is_auto_respawn_restart_enabled(self) -> bool:
        return self._auto_respawn

    def set_auto_respawn_restart(self, enabled: bool) -> None:
        self._auto_respawn = enabled

    def eject_and_dive(self, on_complete=None) -> None:
        self._mission_running = False
        self._intents.append({"action_type": "eject_and_dive"})
        # The real controller invokes on_complete when the eject SEQUENCE ends
        # (respawn stop / cancel), not synchronously. Ignoring it left the FSM
        # in GAME_BATTLE_EJECT forever — the true cause of PATH1_OCR's
        # "timeout waiting for state=game_battle" at P1_050.
        self._eject_on_complete = on_complete

    def _fire_eject_complete(self) -> None:
        cb, self._eject_on_complete = getattr(self, "_eject_on_complete", None), None
        if cb is not None:
            cb()

    def restart_last_mission(self) -> bool:
        self._mission_running = True
        self._intents.append({"action_type": "restart_last_mission"})
        return True

    # --- ADR 076 spawn-attitude guard ---
    def start_spawn_guard(self) -> None:
        self._intents.append({"action_type": "start_spawn_guard"})

    def notify_spawn_alive(self) -> None:
        self._intents.append({"action_type": "notify_spawn_alive"})

    # --- Behavior-tree tactic predicates (ADR 024 3.1b / ADR 070) ---
    # The active-mode tree wires actuators at construction and calls the
    # is_running predicates every tick (the MissileEvade leaf's sticky
    # condition calls is_missile_evading even with no incoming detection).
    # The stub never actuates, so these report "not running" and record any
    # start calls as intents.
    def is_ejecting(self) -> bool:
        return False

    def is_disengage_running(self) -> bool:
        return False

    def is_missile_evading(self) -> bool:
        return False

    def disengage_roll_right(self, duration: float = 10.0) -> None:
        self._intents.append({"action_type": "disengage_roll_right"})

    def missile_evade_mode(self) -> None:
        self._intents.append({"action_type": "missile_evade_mode"})

    def is_climbing(self) -> bool:
        return False

    def climb_mode(self, target_alt=None, max_s=None, fuel_floor_pct=0.0,
                   exit_lead_s=0.0) -> None:
        self._intents.append({"action_type": "climb_mode"})

    # Engage-geometry actuation (3.1a) — called when the Engage leaf selects
    # with contacts on the injected battle frames' minimaps.
    def orient_nose_to_target(self, error_norm, **_cfg) -> str | None:
        self._intents.append({"action_type": "orient_nose_to_target"})
        return None

    def roll_left(self, hold_seconds: float = 0.3, block: bool = True) -> None:
        self._intents.append({"action_type": "roll_left"})

    def roll_right(self, hold_seconds: float = 0.3, block: bool = True) -> None:
        self._intents.append({"action_type": "roll_right"})

    def stop_eject_sequence(self, reason: str = "respawn_detected") -> None:
        self._intents.append({"action_type": "stop_eject_sequence"})
        self._fire_eject_complete()

    # --- Clicks / crops ---
    def click_crop(self, _crop, block=False, count=1, region_name=None) -> None:
        self._intents.append(
            {"action_type": "click_crop", "region_name": region_name, "count": count}
        )

    def start_game_starting_loop(self) -> None:
        pass

    def cancel_mission(self) -> None:
        self._intents.append({"action_type": "cancel_mission"})
        self._fire_eject_complete()

    def deploy_flares(self, hold_seconds=0.05, block=True, ignore_cancel=True) -> None:
        self._intents.append({"action_type": "deploy_flares"})

    def reload_flares(self) -> None:
        self._intents.append({"action_type": "reload_flares"})

    def padlock_target_switch(self, presses: int = 2, delay_between: float = 0.35) -> None:
        self._intents.append({"action_type": "padlock_target_switch"})

    def popup_click_allowed(self, _popup) -> bool:
        return False

    def record_popup_click(self, _popup) -> None:
        pass

    def cleanup(self) -> None:
        pass

    def get_action_intents(self) -> list[dict]:
        return list(self._intents)


# ---------------------------------------------------------------------------
# Test runner helper
# ---------------------------------------------------------------------------

def _run_replay(
    *,
    tmp_path: Path,
    monkeypatch,
    config_path: Path,
    replay_yaml: Path,
    path_name: str,
    exit_after: float = 15.0,
) -> tuple[dict, object | None, object | None, FakePerformanceTracker | None]:
    """Run wingman_main.main() with the given config and return the assertions payload.

    Returns:
        (payload, engine, analyzer, tracker)
        - payload: parsed assertions JSON
        - engine: ReplayAssertionEngine instance (or None)
        - analyzer: GameStateAnalyzer instance (or None)
        - tracker: FakePerformanceTracker instance with ocr_crops counts (or None)
    """
    report = tmp_path / "report.json"
    intents = tmp_path / "intents.json"
    assertions_out = tmp_path / "assertions.json"

    # Capture live instances of key objects so tests can assert on post-run state.
    saved: dict = {}
    _orig_engine = wingman_main.ReplayAssertionEngine
    _orig_analyzer = wingman_main.GameStateAnalyzer

    class _TrackerEngine(wingman_main.ReplayAssertionEngine):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            saved["engine"] = self

    class _TrackerAnalyzer(wingman_main.GameStateAnalyzer):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            saved["analyzer"] = self

    class _TrackerPerf(FakePerformanceTracker):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            saved["tracker"] = self

    monkeypatch.setattr(wingman_main, "ReplayAssertionEngine", _TrackerEngine)
    monkeypatch.setattr(wingman_main, "GameStateAnalyzer", _TrackerAnalyzer)
    monkeypatch.setattr(wingman_main, "Controller", FakeController)
    monkeypatch.setattr(wingman_main, "PerformanceTracker", _TrackerPerf)

    old_argv = sys.argv
    sys.argv = [
        "wingman",
        "--config", str(config_path),
        "--replay-config", str(replay_yaml),
        "--replay-path", path_name,
        "--replay-screenshot-dir", str(_SCREENSHOT_DIR),
        "--replay-exit-after", str(exit_after),
        "--replay-report", str(report),
        "--replay-intents-output", str(intents),
        "--replay-assertions-output", str(assertions_out),
        "--log-level", "WARNING",
    ]
    try:
        wingman_main.main()
    finally:
        sys.argv = old_argv
        monkeypatch.setattr(wingman_main, "ReplayAssertionEngine", _orig_engine)
        monkeypatch.setattr(wingman_main, "GameStateAnalyzer", _orig_analyzer)

    assert assertions_out.exists(), "replay_assertions.json was not written"
    payload = json.loads(assertions_out.read_text(encoding="utf-8"))
    return payload, saved.get("engine"), saved.get("analyzer"), saved.get("tracker")


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------

# All five crops that background OCR scans while in GAME_BATTLE.
_BATTLE_OCR_CROPS = frozenset({"respawn", "incoming", "health", "ammo_flares", "ammo_missiles"})


def _checkpoint_table(assertions: dict) -> str:
    """Return a human-readable table of checkpoint status for failure messages."""
    rows = []
    for cp in assertions.get("checkpoints", []):
        status = cp.get("status", "?")
        name = cp.get("screenshot_name", "?")
        exp_state = cp.get("expected_state") or "-"
        exp_trig = cp.get("expected_trigger") or "-"
        state_ok = "ok" if cp.get("state_met_at_s") is not None else "MISS"
        trig_ok = "ok" if cp.get("trigger_met_at_s") is not None else "MISS"
        extra = f"  [{cp.get('failure_reason', '')}]" if cp.get("failure_reason") else ""
        rows.append(
            f"  {status:7}  {name:<45}"
            f"  state({state_ok})={exp_state:<25}"
            f"  trig({trig_ok})={exp_trig}{extra}"
        )
    return "\n".join(rows) if rows else "  (no checkpoints)"


def _assert_path_passed(assertions: dict, path_name: str) -> None:
    """Assert the replay path completed with no failures.

    Prints a full per-checkpoint table on failure so it is immediately clear
    which OCR detection or state transition caused the regression.
    """
    table = _checkpoint_table(assertions)
    assert not assertions.get("has_failures", True), (
        f"{path_name} assertion failures:\n"
        + "\n".join(assertions.get("failures", []))
        + "\n\nCheckpoint table:\n" + table
    )
    assert assertions.get("is_complete", False), (
        f"{path_name} did not complete all checkpoints.\n\nCheckpoint table:\n" + table
    )


def _assert_ocr_ran(tracker: FakePerformanceTracker, crops: frozenset, path_name: str) -> None:
    """Assert that every expected OCR crop was scanned at least once.

    This guards against silent OCR failures (e.g. a missing tracker method that
    causes the exception handler to swallow every OCR call).
    """
    assert tracker is not None, f"{path_name}: FakePerformanceTracker was not captured"
    not_scanned = [c for c in sorted(crops) if tracker.ocr_crops.get(c, 0) == 0]
    assert not not_scanned, (
        f"{path_name}: OCR crops never scanned: {not_scanned}. "
        f"Scanned counts: {dict(tracker.ocr_crops)}"
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_path1_ocr_regression(tmp_path, monkeypatch):
    """PATH1 full OCR regression: LOBBY → WAITING → STARTING → BATTLE → RESPAWN → ALIVE."""
    path1_screenshots = [
        "P1_000_LOBBY_PLAY.png",
        "P1_010_WAITING_CANCEL_VISIBLE.png",
        "P1_020_GOOD_LUCK_VISIBLE.png",
        "P1_030_BATTLE_HUD_MISSILES_4.png",
        "P1_040_BATTLE_HUD_MISSILES_0_HEALTH_ALIVE.png",
        "P1_050_RESPAWN_VISIBLE_NO_HEALTH.png",
        "P1_060_BATTLE_HUD_HEALTH_ALIVE_MISSILES_4.png",
        "P1_070_CLICK_TO_CONTINUE.png",
        "P1_080_LOBBY_AFTER_MISSION.png",
    ]
    _check_screenshots_ready(path1_screenshots)

    config_path = _build_test_config(tmp_path)
    replay_yaml = _build_path1_ocr_yaml(tmp_path)

    payload, _engine, analyzer, tracker = _run_replay(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        config_path=config_path,
        replay_yaml=replay_yaml,
        path_name="PATH1_OCR",
        exit_after=5.0,
    )

    _assert_path_passed(payload["assertions"], "PATH1_OCR")

    # Guard: all five GAME_BATTLE OCR crops must have been scanned at least once.
    # Catches silent OCR failures (swallowed exceptions, missing tracker methods, etc.).
    _assert_ocr_ran(tracker, _BATTLE_OCR_CROPS, "PATH1_OCR")

    # Final FSM state: PATH1 ends with P1_080 which injects continue_clicked → GAME_LOBBY.
    # OCR may then detect PLAY in the lobby screenshot and transition to GAME_WAITING
    # within the exit_after window — both are valid "back in lobby" states.
    assert analyzer is not None
    assert analyzer.game_state.name in ("GAME_LOBBY", "GAME_WAITING"), (
        f"PATH1_OCR: expected final state GAME_LOBBY or GAME_WAITING, got {analyzer.game_state.name}"
    )


def test_path2_ocr_regression(tmp_path, monkeypatch):
    """PATH2 full OCR regression: BATTLE (seeded) → missiles_empty → manual_takeover → RESPAWN → ALIVE."""
    path2_screenshots = [
        "P1_030_BATTLE_HUD_MISSILES_4.png",               # reused; P2_000 deleted
        "P1_040_BATTLE_HUD_MISSILES_0_HEALTH_ALIVE.png",  # reused; P2_010 deleted
        "P1_030_BATTLE_HUD_MISSILES_4.png",
        "P1_030_BATTLE_HUD_MISSILES_4.png",
        "P1_050_RESPAWN_VISIBLE_NO_HEALTH.png",
        "P1_060_BATTLE_HUD_HEALTH_ALIVE_MISSILES_4.png",  # reused; P2_050 deleted
        "P1_070_CLICK_TO_CONTINUE.png",
        "P1_080_LOBBY_AFTER_MISSION.png",
    ]
    _check_screenshots_ready(path2_screenshots)

    config_path = _build_test_config(tmp_path)
    replay_yaml = _build_path2_ocr_yaml(tmp_path)

    payload, _engine, analyzer, tracker = _run_replay(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        config_path=config_path,
        replay_yaml=replay_yaml,
        path_name="PATH2_OCR",
        exit_after=5.0,
    )

    _assert_path_passed(payload["assertions"], "PATH2_OCR")

    # Guard: all five GAME_BATTLE OCR crops must have been scanned at least once.
    _assert_ocr_ran(tracker, _BATTLE_OCR_CROPS, "PATH2_OCR")

    # Final FSM state: PATH2 ends with P1_080 which injects continue_clicked → GAME_LOBBY.
    # OCR may then detect PLAY in the lobby screenshot and transition to GAME_WAITING
    # within the exit_after window — both are valid "back in lobby" states.
    assert analyzer is not None
    assert analyzer.game_state.name in ("GAME_LOBBY", "GAME_WAITING"), (
        f"PATH2_OCR: expected final state GAME_LOBBY or GAME_WAITING, got {analyzer.game_state.name}"
    )
