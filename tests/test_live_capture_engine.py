import numpy as np
import pytest
from pathlib import Path

from wingman.replay import LivePathCaptureEngine, ReplayStep


@pytest.fixture
def capture_dir(tmp_path: Path) -> Path:
    out = tmp_path / "captures"
    out.mkdir()
    return out


def _frame(width: int = 10, height: int = 10, value: int = 1) -> np.ndarray:
    return np.full((height, width, 3), value, dtype=np.uint8)


def test_live_capture_engine_quality_gate_rejects_black_frame(capture_dir: Path):
    steps = [ReplayStep(screenshot_name="step.png", injection_time_s=0.0, expected_state="GAME_BATTLE")]
    engine = LivePathCaptureEngine(
        path_name="PATH1",
        steps=steps,
        screenshot_dir=capture_dir,
        region=(0, 0, 10, 10),
        overwrite=True,
        timeout_s=5.0,
        allow_inject=False,
    )

    engine.evaluate(np.zeros((10, 10, 3), dtype=np.uint8), "game_battle", 0.0)

    assert not (capture_dir / "step.png").exists()
    assert not engine.is_complete()


def test_live_capture_engine_captures_after_debounce(capture_dir: Path):
    steps = [ReplayStep(screenshot_name="step.png", injection_time_s=0.0, expected_state="GAME_BATTLE")]
    engine = LivePathCaptureEngine(
        path_name="PATH1",
        steps=steps,
        screenshot_dir=capture_dir,
        region=(0, 0, 10, 10),
        overwrite=True,
        timeout_s=5.0,
        allow_inject=False,
    )

    frame = _frame()
    engine.evaluate(frame, "game_battle", 0.0)
    assert not (capture_dir / "step.png").exists()
    engine.evaluate(frame, "game_battle", 0.1)

    assert (capture_dir / "step.png").exists()
    assert engine.is_complete()


def test_live_capture_engine_existing_file_is_terminal_failure(capture_dir: Path):
    (capture_dir / "step.png").write_bytes(b"existing")
    steps = [ReplayStep(screenshot_name="step.png", injection_time_s=0.0, expected_state="GAME_BATTLE")]
    engine = LivePathCaptureEngine(
        path_name="PATH1",
        steps=steps,
        screenshot_dir=capture_dir,
        region=(0, 0, 10, 10),
        overwrite=False,
        timeout_s=5.0,
        allow_inject=False,
    )

    frame = _frame()
    engine.evaluate(frame, "game_battle", 0.0)
    engine.evaluate(frame, "game_battle", 0.1)

    assert engine.has_failures()
    assert engine.is_complete()
    assert (capture_dir / "step.png").read_bytes() == b"existing"


def test_live_capture_engine_summary_records_absolute_timestamps(capture_dir: Path):
    ticks = iter([100.0, 100.0, 101.0, 101.0])

    def fake_time():
        return next(ticks)

    steps = [ReplayStep(screenshot_name="step.png", injection_time_s=0.0, expected_state="GAME_BATTLE")]
    engine = LivePathCaptureEngine(
        path_name="PATH1",
        steps=steps,
        screenshot_dir=capture_dir,
        region=(0, 0, 10, 10),
        overwrite=True,
        timeout_s=5.0,
        allow_inject=False,
        time_fn=fake_time,
    )

    frame = _frame()
    engine.evaluate(frame, "game_battle", 100.0)
    engine.evaluate(frame, "game_battle", 101.0)

    payload = engine.to_dict()
    assert payload["started_at_s"] == 100.0
    assert payload["ended_at_s"] == 101.0
    assert payload["steps"][0]["status"] == "captured"
    assert payload["steps"][0]["capture_time_s"] == 101.0


def test_live_capture_engine_state_enter_trigger_ready_from_current_state(capture_dir: Path):
    steps = [
        ReplayStep(
            screenshot_name="step.png",
            injection_time_s=0.0,
            expected_trigger="battle_started",
        )
    ]
    engine = LivePathCaptureEngine(
        path_name="PATH1",
        steps=steps,
        screenshot_dir=capture_dir,
        region=(0, 0, 10, 10),
        overwrite=True,
        timeout_s=5.0,
        allow_inject=False,
    )

    frame = _frame()
    engine.evaluate(frame, "game_battle", 0.0)
    engine.evaluate(frame, "game_battle", 0.1)

    assert (capture_dir / "step.png").exists()
    assert engine.is_complete()


def test_live_capture_engine_auto_resumes_to_matching_state(capture_dir: Path):
    steps = [
        ReplayStep(screenshot_name="lobby.png", injection_time_s=0.0, expected_state="GAME_LOBBY"),
        ReplayStep(screenshot_name="waiting.png", injection_time_s=0.0, expected_state="GAME_WAITING"),
        ReplayStep(screenshot_name="battle.png", injection_time_s=0.0, expected_state="GAME_BATTLE"),
    ]
    engine = LivePathCaptureEngine(
        path_name="PATH1",
        steps=steps,
        screenshot_dir=capture_dir,
        region=(0, 0, 10, 10),
        overwrite=True,
        timeout_s=5.0,
        allow_inject=False,
    )

    frame = _frame()
    engine.evaluate(frame, "game_battle", 0.0)
    engine.evaluate(frame, "game_battle", 0.1)

    payload = engine.to_dict()
    assert payload["steps"][0]["status"] == "skipped"
    assert payload["steps"][0]["notes"] == "skipped_before_resume"
    assert payload["steps"][1]["status"] == "skipped"
    assert payload["steps"][1]["notes"] == "skipped_before_resume"
    assert payload["steps"][2]["status"] == "captured"


def test_live_capture_engine_auto_resume_waits_for_known_state(capture_dir: Path):
    steps = [
        ReplayStep(screenshot_name="lobby.png", injection_time_s=0.0, expected_state="GAME_LOBBY"),
        ReplayStep(screenshot_name="battle.png", injection_time_s=0.0, expected_state="GAME_BATTLE"),
    ]
    engine = LivePathCaptureEngine(
        path_name="PATH1",
        steps=steps,
        screenshot_dir=capture_dir,
        region=(0, 0, 10, 10),
        overwrite=True,
        timeout_s=5.0,
        allow_inject=False,
    )

    frame = _frame()
    engine.evaluate(frame, "GAME_UNKNOWN", 0.0)
    engine.evaluate(frame, "GAME_BATTLE", 0.1)
    engine.evaluate(frame, "GAME_BATTLE", 0.2)

    payload = engine.to_dict()
    assert payload["steps"][0]["status"] == "skipped"
    assert payload["steps"][0]["notes"] == "skipped_before_resume"
    assert payload["steps"][1]["status"] == "captured"


def test_live_capture_engine_lookahead_buffer_captures_next_step(capture_dir: Path):
    """Trigger for step N+1 fires while step N is current; step N+1 must still be captured."""
    steps = [
        ReplayStep(
            screenshot_name="p060.png",
            injection_time_s=0.0,
            expected_state="GAME_BATTLE",
            expected_trigger="restart_last_mission",
        ),
        ReplayStep(
            screenshot_name="p070.png",
            injection_time_s=0.0,
            expected_state="GAME_END_B",
            expected_trigger="click_to_detected",
        ),
    ]
    engine = LivePathCaptureEngine(
        path_name="PATH1",
        steps=steps,
        screenshot_dir=capture_dir,
        region=(0, 0, 10, 10),
        overwrite=True,
        timeout_s=5.0,
        allow_inject=False,
    )

    frame = _frame()
    # click_to_detected fires while p060 is still current (should be buffered)
    engine.on_event("click_to_detected", 0.0)
    assert not engine.is_complete()

    # restart_last_mission fires → p060 captured (debounce=2)
    engine.on_event("restart_last_mission", 0.1)
    engine.evaluate(frame, "GAME_BATTLE", 0.1)
    engine.evaluate(frame, "GAME_BATTLE", 0.2)  # p060 captured, advance; _trigger_ready=True for p070

    # p070 should now be ready immediately (buffer applied in _advance)
    engine.evaluate(frame, "GAME_END_B", 0.3)   # ready_count=1
    engine.evaluate(frame, "GAME_END_B", 0.4)   # ready_count=2 → capture

    payload = engine.to_dict()
    assert payload["steps"][0]["status"] == "captured", payload["steps"][0]
    assert payload["steps"][1]["status"] == "captured", payload["steps"][1]


def test_live_capture_engine_lookahead_buffer_chains_two_steps(capture_dir: Path):
    """Both click_to_detected and continue_clicked fire early; both next steps must be captured."""
    steps = [
        ReplayStep(
            screenshot_name="p060.png",
            injection_time_s=0.0,
            expected_state="GAME_BATTLE",
            expected_trigger="restart_last_mission",
        ),
        ReplayStep(
            screenshot_name="p070.png",
            injection_time_s=0.0,
            expected_state="GAME_END_B",
            expected_trigger="click_to_detected",
        ),
        ReplayStep(
            screenshot_name="p080.png",
            injection_time_s=0.0,
            expected_state="GAME_LOBBY",
            expected_trigger="continue_clicked",
        ),
    ]
    engine = LivePathCaptureEngine(
        path_name="PATH1",
        steps=steps,
        screenshot_dir=capture_dir,
        region=(0, 0, 10, 10),
        overwrite=True,
        timeout_s=5.0,
        allow_inject=False,
    )

    frame = _frame()
    # Both future triggers fire while p060 is current.
    # Only the immediate next step (p070) is buffered at this point.
    engine.on_event("click_to_detected", 0.0)

    # Capture p060
    engine.on_event("restart_last_mission", 0.1)
    engine.evaluate(frame, "GAME_BATTLE", 0.1)
    engine.evaluate(frame, "GAME_BATTLE", 0.2)  # p060 captured; p070 now current with _trigger_ready=True

    # continue_clicked fires while p070 is current (buffered for p080)
    engine.on_event("continue_clicked", 0.25)

    # Capture p070
    engine.evaluate(frame, "GAME_END_B", 0.3)
    engine.evaluate(frame, "GAME_END_B", 0.4)   # p070 captured; p080 now current with _trigger_ready=True

    # Capture p080
    engine.evaluate(frame, "GAME_LOBBY", 0.5)
    engine.evaluate(frame, "GAME_LOBBY", 0.6)   # p080 captured

    payload = engine.to_dict()
    assert payload["steps"][0]["status"] == "captured", payload["steps"][0]
    assert payload["steps"][1]["status"] == "captured", payload["steps"][1]
    assert payload["steps"][2]["status"] == "captured", payload["steps"][2]


def test_live_capture_engine_timeout_retry_mode_does_not_fail_or_advance(capture_dir: Path):
    steps = [
        ReplayStep(
            screenshot_name="step.png",
            injection_time_s=0.0,
            expected_state="GAME_BATTLE",
            expected_trigger="restart_last_mission",
        )
    ]
    engine = LivePathCaptureEngine(
        path_name="PATH1",
        steps=steps,
        screenshot_dir=capture_dir,
        region=(0, 0, 10, 10),
        overwrite=True,
        timeout_s=1.0,
        allow_inject=False,
        timeout_advances=False,
    )

    frame = _frame()
    engine.evaluate(frame, "GAME_BATTLE", 0.0)
    engine.evaluate(frame, "GAME_BATTLE", 2.0)  # timeout, should retry same step

    payload = engine.to_dict()
    assert payload["steps"][0]["status"] == "pending"
    assert payload["steps"][0]["notes"] == "timeout_retry_1_after_1.0s"
    assert payload["has_failures"] is False
    assert engine.is_complete() is False


def test_live_capture_engine_out_of_order_mode_captures_later_step_first(capture_dir: Path):
    steps = [
        ReplayStep(screenshot_name="lobby.png", injection_time_s=0.0, expected_state="GAME_LOBBY"),
        ReplayStep(
            screenshot_name="battle.png",
            injection_time_s=0.0,
            expected_state="GAME_BATTLE",
            expected_trigger="battle_started",
        ),
    ]
    engine = LivePathCaptureEngine(
        path_name="PATH1",
        steps=steps,
        screenshot_dir=capture_dir,
        region=(0, 0, 10, 10),
        overwrite=True,
        timeout_s=5.0,
        allow_inject=False,
        auto_resume=False,
        timeout_advances=False,
        out_of_order=True,
    )

    frame = _frame()

    # Observe battle_started while in battle; out-of-order mode should capture battle step
    # even though lobby step is still pending.
    engine.on_event("battle_started", 0.0)
    engine.evaluate(frame, "GAME_BATTLE", 0.0)
    engine.evaluate(frame, "GAME_BATTLE", 0.1)

    mid = engine.to_dict()
    assert mid["steps"][0]["status"] == "pending"
    assert mid["steps"][1]["status"] == "captured"

    # Later, lobby appears and the earlier pending step is captured.
    engine.evaluate(frame, "GAME_LOBBY", 0.2)
    engine.evaluate(frame, "GAME_LOBBY", 0.3)

    final = engine.to_dict()
    assert final["steps"][0]["status"] == "captured"
    assert final["steps"][1]["status"] == "captured"
    assert engine.is_complete() is True


def test_live_capture_engine_reuse_from_copies_existing_file(capture_dir: Path):
    steps = [
        ReplayStep(
            screenshot_name="target.png",
            injection_time_s=0.0,
            expected_state="GAME_LOBBY",
            reuse_from="source.png",
        )
    ]
    engine = LivePathCaptureEngine(
        path_name="PATH1",
        steps=steps,
        screenshot_dir=capture_dir,
        region=(0, 0, 10, 10),
        overwrite=True,
        timeout_s=5.0,
        allow_inject=False,
        out_of_order=True,
    )

    source = capture_dir / "source.png"
    source.write_bytes(b"copied-by-reuse")

    frame = _frame()
    engine.evaluate(frame, "GAME_LOBBY", 0.0)
    engine.evaluate(frame, "GAME_LOBBY", 0.1)

    target = capture_dir / "target.png"
    assert target.exists()
    assert target.read_bytes() == b"copied-by-reuse"

    payload = engine.to_dict()
    assert payload["steps"][0]["status"] == "captured"
    assert payload["steps"][0]["readiness_source"] == "reused"
    assert payload["steps"][0]["notes"] == "captured_reused_from:source.png"


def test_live_capture_engine_out_of_order_requires_fresh_trigger(capture_dir: Path):
    steps = [
        ReplayStep(
            screenshot_name="respawn.png",
            injection_time_s=0.0,
            expected_state="GAME_BATTLE",
            expected_trigger="respawn_detected",
        )
    ]
    engine = LivePathCaptureEngine(
        path_name="PATH1",
        steps=steps,
        screenshot_dir=capture_dir,
        region=(0, 0, 10, 10),
        overwrite=True,
        timeout_s=5.0,
        allow_inject=False,
        out_of_order=True,
        trigger_freshness_s=1.0,
    )

    frame = _frame()
    engine.on_event("respawn_detected", 0.0)
    engine.evaluate(frame, "GAME_BATTLE", 5.0)
    engine.evaluate(frame, "GAME_BATTLE", 5.1)

    payload = engine.to_dict()
    assert payload["steps"][0]["status"] == "pending"
    assert not (capture_dir / "respawn.png").exists()


def test_live_capture_engine_out_of_order_overall_timeout_fails_pending_steps(capture_dir: Path):
    steps = [
        ReplayStep(screenshot_name="lobby.png", injection_time_s=0.0, expected_state="GAME_LOBBY"),
        ReplayStep(
            screenshot_name="battle.png",
            injection_time_s=0.0,
            expected_state="GAME_BATTLE",
            expected_trigger="missiles_empty",
        ),
    ]
    engine = LivePathCaptureEngine(
        path_name="PATH1",
        steps=steps,
        screenshot_dir=capture_dir,
        region=(0, 0, 10, 10),
        overwrite=True,
        timeout_s=5.0,
        allow_inject=False,
        auto_resume=False,
        timeout_advances=False,
        out_of_order=True,
    )

    frame = _frame()
    engine.evaluate(frame, "GAME_LOBBY", 0.0)
    engine.evaluate(frame, "GAME_LOBBY", 0.1)  # lobby captured

    # Trigger fires while the state never matches within the freshness window,
    # so the battle step can never become ready (the ADR045 hang scenario).
    engine.on_event("missiles_empty", 1.0)
    engine.evaluate(frame, "GAME_BATTLE_EJECT", 1.0)
    engine.evaluate(frame, "GAME_BATTLE_EJECT", 1.1)
    assert not engine.is_complete()

    # Past the lane-wide timeout the pending step fails and the lane completes
    # instead of looping forever.
    engine.evaluate(frame, "GAME_BATTLE_EJECT", 5.2)

    assert engine.is_complete()
    assert engine.has_failures()
    payload = engine.to_dict()
    assert payload["steps"][0]["status"] == "captured"
    assert payload["steps"][1]["status"] == "failed"
    assert payload["steps"][1]["timeout"] is True
    assert payload["steps"][1]["notes"] == "timeout_after_5.0s"


def test_oo_trigger_captures_when_state_exits_same_tick(capture_dir: Path):
    """Regression (2026-08-13 21:55 make p1 run): since ADR 056, missiles_empty
    atomically exits GAME_BATTLE for GAME_BATTLE_EJECT, so by the next
    evaluate() the state no longer matches and 'trigger fresh AND state
    matches now' was unobservable by construction — P1_040 timed out on every
    real-game capture run. The state half must accept the expected state
    having been OBSERVED within the freshness window."""
    steps = [ReplayStep(
        screenshot_name="p1_040.png", injection_time_s=0.0,
        expected_state="GAME_BATTLE", expected_trigger="missiles_empty",
    )]
    engine = LivePathCaptureEngine(
        path_name="PATH1", steps=steps, screenshot_dir=capture_dir,
        region=(0, 0, 10, 10), overwrite=True, timeout_s=5.0,
        allow_inject=False, out_of_order=True,
    )
    # Battle ticks observed, then the one-shot trigger fires and the FSM
    # leaves GAME_BATTLE on the same tick.
    engine.evaluate(_frame(), "game_battle", 0.0)
    engine.on_event("missiles_empty", 1.4)
    engine.on_state("game_battle_eject", 1.5)
    # Next evaluate is already in the eject state.
    engine.evaluate(_frame(), "game_battle_eject", 1.6)

    assert (capture_dir / "p1_040.png").exists(), (
        "same-tick state exit made the trigger step uncapturable"
    )


def test_oo_one_shot_trigger_captures_on_first_ready_evaluate(capture_dir: Path):
    """One-shot FSM triggers cannot survive debounce_required consecutive
    evaluates inside the 2.0 s freshness window at the 1.5 s loop tick —
    trigger-sourced readiness must capture on the first ready evaluate."""
    steps = [ReplayStep(
        screenshot_name="one_shot.png", injection_time_s=0.0,
        expected_state="GAME_BATTLE", expected_trigger="respawn_detected",
    )]
    engine = LivePathCaptureEngine(
        path_name="PATH1", steps=steps, screenshot_dir=capture_dir,
        region=(0, 0, 10, 10), overwrite=True, timeout_s=5.0,
        allow_inject=False, out_of_order=True, debounce_required=2,
    )
    engine.evaluate(_frame(), "game_battle", 0.0)
    engine.on_event("respawn_detected", 1.0)
    # Only ONE evaluate lands inside the freshness window.
    engine.evaluate(_frame(), "game_battle", 1.9)

    assert (capture_dir / "one_shot.png").exists(), (
        "one-shot trigger needed debounce ticks it can never get"
    )


def test_oo_stale_state_does_not_satisfy_trigger_step(capture_dir: Path):
    """The anti-stale guard survives the fix: expected state seen long BEFORE
    the trigger (outside the freshness window) must not capture."""
    steps = [ReplayStep(
        screenshot_name="stale.png", injection_time_s=0.0,
        expected_state="GAME_BATTLE", expected_trigger="missiles_empty",
    )]
    engine = LivePathCaptureEngine(
        path_name="PATH1", steps=steps, screenshot_dir=capture_dir,
        region=(0, 0, 10, 10), overwrite=True, timeout_s=60.0,
        allow_inject=False, out_of_order=True,
    )
    engine.evaluate(_frame(), "game_battle", 0.0)     # battle seen at t=0
    engine.on_state("game_lobby", 5.0)                # long gone from battle
    engine.on_event("missiles_empty", 30.0)           # trigger fires much later
    engine.evaluate(_frame(), "game_lobby", 30.5)     # state stale by 30 s

    assert not (capture_dir / "stale.png").exists(), (
        "a 30s-stale state sighting satisfied the both-fields rule"
    )
