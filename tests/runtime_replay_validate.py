"""Validate ADR044 PATH1 runtime replay artifacts.

This script enforces machine-checkable pass/fail rules against:
- Runtime log signatures
- replay_assertions output schema/status

Exit code is nonzero on any validation failure.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

NEGATIVE_LOG_PATTERNS = [
    "Traceback",
    "[ERROR]",
    "Replay assertion failure",
    "Replay: injecting FSM trigger 'good_luck_detected'",
]

POSITIVE_LOG_PATTERNS_REQUIRED = [
    "MISSILES EMPTY — cancelling mission and ejecting",
    "Controller: eject_and_dive — descent control engaged",
    "Analyzer: 'Good Luck' detected in good_luck crop",
    "Controller: 'Good Luck' detected",
]

TERMINAL_PATTERNS = [
    "Controller: eject_and_dive complete",
    "Controller: eject_and_dive — cancelled during descent",
]

MANUAL_TAKEOVER_PATTERN = "GAME_BATTLE → GAME_BATTLE_MANUAL"


def _load_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


def _count_patterns(text: str, patterns: list[str]) -> dict[str, int]:
    return {pattern: text.count(pattern) for pattern in patterns}


def _validate_assertions_payload(payload: dict, failures: list[str]) -> dict:
    assertions = payload.get("assertions")
    if assertions is None:
        failures.append("assertions payload missing root key: assertions")
        return {}

    if not isinstance(assertions, dict):
        failures.append("assertions payload key 'assertions' must be an object")
        return {}

    has_failures = assertions.get("has_failures")
    is_complete = assertions.get("is_complete")
    checkpoints = assertions.get("checkpoints")

    if has_failures is not False:
        failures.append("assertions.has_failures must be false")

    if is_complete is not True:
        failures.append("assertions.is_complete must be true")

    if not isinstance(checkpoints, list):
        failures.append("assertions.checkpoints must be a list")
        checkpoints = []

    failed_indexes = []
    for idx, checkpoint in enumerate(checkpoints):
        if not isinstance(checkpoint, dict):
            failures.append(f"assertions.checkpoints[{idx}] must be an object")
            continue
        if checkpoint.get("status") == "failed":
            failed_indexes.append(idx)

    if failed_indexes:
        failures.append(
            "assertions.checkpoints contains failed statuses at indexes: "
            + ", ".join(str(i) for i in failed_indexes)
        )

    return assertions


def _validate_log(log_text: str, failures: list[str]) -> dict:
    negative_counts = _count_patterns(log_text, NEGATIVE_LOG_PATTERNS)
    required_positive_counts = _count_patterns(log_text, POSITIVE_LOG_PATTERNS_REQUIRED)
    terminal_counts = _count_patterns(log_text, TERMINAL_PATTERNS)
    manual_takeover_count = log_text.count(MANUAL_TAKEOVER_PATTERN)

    for pattern, count in negative_counts.items():
        if count > 0:
            failures.append(f"forbidden log pattern present ({count}): {pattern}")

    for pattern, count in required_positive_counts.items():
        if count <= 0:
            failures.append(f"required log pattern missing: {pattern}")

    terminal_total = sum(terminal_counts.values())
    if terminal_total <= 0:
        failures.append("missing terminal eject outcome: neither complete nor cancelled marker found")

    # A respawn-triggered stop during the nose phase is a successful eject —
    # and under ADR 068 d6 the nose phase spans nearly the whole eject, so
    # system-legitimate stops (match ended, app shutdown) now land in-phase
    # too (CR-014-11). Only cancellations with any other reason still require
    # the manual takeover marker to explain them.
    cancellation_count = terminal_counts["Controller: eject_and_dive — cancelled during descent"]
    respawn_cancel_count = sum(
        log_text.count(f"cancelled during descent (reason={reason})")
        for reason in ("respawn_detected", "match_ended", "shutdown")
    )
    if cancellation_count - respawn_cancel_count > 0 and manual_takeover_count <= 0:
        failures.append(
            "cancellation observed but missing required manual takeover marker: "
            + MANUAL_TAKEOVER_PATTERN
        )

    return {
        "negative_counts": negative_counts,
        "required_positive_counts": required_positive_counts,
        "terminal_counts": terminal_counts,
        "manual_takeover_count": manual_takeover_count,
    }


def main() -> int:
    # Windows consoles default to cp1252, which cannot encode the em-dash
    # and arrow characters in the log markers — a failed print must never
    # mask the validation verdict.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Validate ADR044 runtime replay artifacts")
    parser.add_argument("--log-file", default="wingman.log", help="Path to runtime log file")
    parser.add_argument("--assertions-file", required=True, help="Path to replay assertions JSON file")
    parser.add_argument("--intents-file", default=None, help="Optional path to replay intents JSON file")
    parser.add_argument(
        "--summary-out",
        default="tests/test-output/runtime_replay_validation.path1.json",
        help="Path to write validation summary JSON",
    )
    args = parser.parse_args()

    failures: list[str] = []

    log_path = Path(args.log_file)
    assertions_path = Path(args.assertions_file)
    intents_path = Path(args.intents_file) if args.intents_file else None
    summary_path = Path(args.summary_out)

    if not log_path.exists():
        failures.append(f"log file not found: {log_path}")
        log_text = ""
    else:
        log_text = _load_text(log_path)

    assertions_payload = {}
    if not assertions_path.exists():
        failures.append(f"assertions file not found: {assertions_path}")
    else:
        try:
            assertions_payload = json.loads(assertions_path.read_text(encoding="utf-8"))
        except Exception as exc:
            failures.append(f"failed to parse assertions file {assertions_path}: {exc}")

    intents_exists = None
    if intents_path is not None:
        intents_exists = intents_path.exists()
        if not intents_exists:
            failures.append(f"intents file not found: {intents_path}")

    assertions_summary = {}
    if assertions_payload:
        assertions_summary = _validate_assertions_payload(assertions_payload, failures)

    log_summary = _validate_log(log_text, failures)

    passed = len(failures) == 0
    summary = {
        "generated_at": time.time(),
        "passed": passed,
        "failure_count": len(failures),
        "failures": failures,
        "inputs": {
            "log_file": str(log_path),
            "assertions_file": str(assertions_path),
            "intents_file": str(intents_path) if intents_path is not None else None,
            "intents_exists": intents_exists,
        },
        "assertions": {
            "path_name": assertions_summary.get("path_name"),
            "has_failures": assertions_summary.get("has_failures"),
            "is_complete": assertions_summary.get("is_complete"),
            "checkpoint_total": len(assertions_summary.get("checkpoints", []))
            if isinstance(assertions_summary.get("checkpoints"), list)
            else None,
            "failures": assertions_summary.get("failures"),
        },
        "log_markers": log_summary,
    }

    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    if passed:
        print("PASS: runtime replay validation succeeded")
        return 0

    print("FAIL: runtime replay validation failed")
    for reason in failures:
        print(f" - {reason}")
    print(f"Summary: {summary_path}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
