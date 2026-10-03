"""Shared FSM vocabulary: game states, orchestration events, the transition table.

A leaf module (standard library only). It exists so that code needing only these
names does not import analyzer.py, which loads EasyOCR and torch at import time,
about 2.2 s per process (CR-018-15). analyzer.py re-exports every name here, so
existing imports keep working.
"""

from enum import Enum, auto


class GameState(Enum):
    GAME_UNKNOWN         = auto()  # Startup state; classify current frame before normal runtime flow
    GAME_BATTLE          = auto()  # Active gameplay (default); respawn/incoming scanning active
    GAME_END_B           = auto()  # "Click to Continue" detected; clicking in progress
    GAME_LOBBY           = auto()  # Final continue (region 64) clicked; waiting in lobby
    GAME_WAITING         = auto()  # PLAY clicked; waiting for CANCEL crop to confirm matchmaking
    GAME_STARTING        = auto()  # Matchmaking confirmed; waiting for "Good Luck" before launching mission
    GAME_STARTING_STALLED = auto() # GAME_STARTING timed out without "Good Luck" detection
    GAME_BATTLE_MANUAL   = auto()  # Player took manual control; auto-mission restart suppressed
    GAME_BATTLE_EJECT    = auto()  # Eject sequence active (missiles empty); respawn detection only


# States where a round is genuinely under way and stopping would abandon an
# aircraft in flight. ADR 094's deferred exit waits these out; everything else
# — including GAME_UNKNOWN before the first classification, and GAME_END_B once
# the round is scored — is a safe moment to stop.
BATTLE_STATES = frozenset({
    GameState.GAME_BATTLE,
    GameState.GAME_BATTLE_MANUAL,
    GameState.GAME_BATTLE_EJECT,
})


class GameEvent(Enum):
    """Orchestration events the analyzer publishes (ADR 060 Phase 1).

    Replaces ADR 039's single-slot `set_on_*` setters: subscribing to a
    nonexistent event is an AttributeError at wiring time rather than a silent
    runtime no-op, and every event fans out to any number of subscribers.
    Payloads are documented per event; `emit()` passes them through verbatim.
    """
    CANCEL_MISSION = auto()            # ()          — transition requires mission cancel
    START_GAME_STARTING_LOOP = auto()  # ()          — entered GAME_STARTING
    LOBBY_PLAY_CLICK = auto()          # (crop, frame)
    MANUAL_TAKEOVER = auto()           # SAF-001: operator has the aircraft
    LOBBY_POPUP_CLICK = auto()         # (crop,)
    LOBBY_POPUP_ABSENT = auto()        # ()          — popup batch completed, none detected
    STALL_RECOVERY_ACTION = auto()     # (crop,)     — stall-recovery screen detected (ADR 084)
    LOBBY_STALL = auto()               # ()          — no lobby crops detected for the stall window
    FSM_TRANSITION = auto()            # (trigger, prev_state_name, next_state_name, ts)
    RESPAWN_DETECTED = auto()          # (frame,)    — fired from the background OCR thread


# ============================================================================
# FSM Transition Table (ADR 025)
# ============================================================================

FSM_TRANSITIONS = [
    {"trigger": "unknown_to_end_detected",    "source": "GAME_UNKNOWN",          "dest": "GAME_END_B"},
    {"trigger": "unknown_to_lobby_detected",  "source": "GAME_UNKNOWN",          "dest": "GAME_LOBBY"},
    {"trigger": "unknown_to_battle_detected", "source": "GAME_UNKNOWN",          "dest": "GAME_BATTLE"},
    {"trigger": "play_clicked",        "source": "GAME_LOBBY",            "dest": "GAME_WAITING"},
    {"trigger": "cancel_detected",    "source": "GAME_LOBBY",            "dest": "GAME_STARTING"},
    {"trigger": "cancel_detected",    "source": "GAME_WAITING",          "dest": "GAME_STARTING"},
    {"trigger": "waiting_timeout",    "source": "GAME_WAITING",          "dest": "GAME_LOBBY"},
    {"trigger": "good_luck_detected", "source": "GAME_STARTING",         "dest": "GAME_BATTLE"},
    {"trigger": "starting_timeout",   "source": "GAME_STARTING",         "dest": "GAME_STARTING_STALLED"},
    # ADR 102: the match never began — PLAY is still on screen.
    {"trigger": "starting_play_visible", "source": "GAME_STARTING",      "dest": "GAME_LOBBY"},
    {"trigger": "starting_stalled_reclassify", "source": "GAME_STARTING_STALLED", "dest": "GAME_UNKNOWN"},
    {"trigger": "starting_recovery",  "source": "GAME_STARTING_STALLED", "dest": "GAME_STARTING"},
    {"trigger": "starting_give_up",   "source": "GAME_STARTING_STALLED", "dest": "GAME_LOBBY"},
    {"trigger": "click_to_detected",  "source": ["GAME_BATTLE", "GAME_BATTLE_MANUAL", "GAME_BATTLE_EJECT"], "dest": "GAME_END_B"},
    {"trigger": "manual_takeover",    "source": ["GAME_BATTLE", "GAME_BATTLE_EJECT"], "dest": "GAME_BATTLE_MANUAL"},
    {"trigger": "respawn_reset",      "source": "GAME_BATTLE_MANUAL",     "dest": "GAME_BATTLE"},
    # SAF-001: the operator hands the aircraft back explicitly. Without
    # this, takeover survived only until the next death — measured
    # 2026-08-30 at 15 s and 85 s, both ended by respawn detection.
    {"trigger": "manual_release",     "source": "GAME_BATTLE_MANUAL",     "dest": "GAME_BATTLE"},
    {"trigger": "eject_started",      "source": "GAME_BATTLE",            "dest": "GAME_BATTLE_EJECT"},
    {"trigger": "eject_complete",     "source": "GAME_BATTLE_EJECT",      "dest": "GAME_BATTLE"},
    # CR-018-14: operator and recovery overrides for an FSM that has the screen
    # wrong ('u'/'o' force battle; 'm', the GAME_END_B stall guard and the
    # ready-button click force the lobby). Written out rather than "*": "*" also
    # made each a self-transition that re-ran the entry hook, and would have
    # made any future state a valid source without anyone deciding it should be.
    {"trigger": "manual_force_battle",
     "source": ["GAME_UNKNOWN", "GAME_END_B", "GAME_LOBBY", "GAME_WAITING",
                "GAME_STARTING", "GAME_STARTING_STALLED", "GAME_BATTLE_MANUAL",
                "GAME_BATTLE_EJECT"],
     "dest": "GAME_BATTLE"},
    {"trigger": "manual_reset",
     "source": ["GAME_UNKNOWN", "GAME_BATTLE", "GAME_END_B", "GAME_WAITING",
                "GAME_STARTING", "GAME_STARTING_STALLED", "GAME_BATTLE_MANUAL",
                "GAME_BATTLE_EJECT"],
     "dest": "GAME_LOBBY"},
    {"trigger": "continue_clicked",   "source": ["GAME_END_B", "GAME_BATTLE_MANUAL"], "dest": "GAME_LOBBY"},
    {"trigger": "respawn_detected",   "source": "GAME_END_B",            "dest": "GAME_BATTLE"},
]


# --- ADR 123: nose direction ------------------------------------------------
# A continuously maintained answer to "is the nose up or down", derived from the
# altitude rate. Kept as STATE rather than recomputed on demand because the
# consumer needs it at an instant the telemetry may not have refreshed on.
NOSE_UP = "up"
NOSE_DOWN = "down"
NOSE_UNKNOWN = "unknown"
