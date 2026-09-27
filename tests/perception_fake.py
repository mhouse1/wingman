"""The one test double for the Perception port. CR-018-15.

Thirty-five analyzer-shaped stubs had each re-implemented whatever subset of the
analyzer their test touched, so a new read or command on the controller side broke
them one by one. Controller-facing stubs now inherit from this class and override
only what their test is about.

Defaults are the real analyzer's starting values (GAME_UNKNOWN, not alive, no
incoming, nose unknown) and the values its getters return when there is no reading
(None). `trigger_event` applies the real transition table, so a fake FSM cannot
accept a transition the real one refuses. `tests/test_perception_port.py` checks
that this class and GameStateAnalyzer both provide every member of the port.
"""

import time

from wingman.state import FSM_TRANSITIONS, NOSE_UNKNOWN, GameState


def _sources(transition):
    source = transition["source"]
    return source if isinstance(source, list) else [source]


class PerceptionFake:
    # Plain attributes rather than properties, so a subclass or a test can set them.
    game_state = GameState.GAME_UNKNOWN
    game_battle_alive = False

    # --- reads -------------------------------------------------------------------
    def get_telemetry(self):
        return None

    def get_ammo_missiles(self):
        return None

    def get_ammo_flares(self):
        return None

    def get_health(self):
        return None

    def get_incoming_cache_timestamp(self) -> float:
        return 0.0

    def get_incoming_cache_result(self):
        return (False, 0.0, None)

    def get_afterburner_fuel_pct(self):
        return None

    def nose_direction(self) -> str:
        return NOSE_UNKNOWN

    def scan_region_for_good_luck(self, frame) -> bool:
        return False

    def crops_for_state(self, state=None) -> dict:
        return {}

    # --- commands ----------------------------------------------------------------
    def trigger_event(self, name: str) -> bool:
        self.__dict__.setdefault("trigger_calls", []).append(name)
        for t in FSM_TRANSITIONS:
            if t["trigger"] == name and self.game_state.name in _sources(t):
                self.game_state = GameState[t["dest"]]
                return True
        return False

    def arm_starting_health_scan(self):
        pass

    def disarm_starting_health_scan(self):
        pass

    def mark_health_dead_synthetic(self):
        pass

    def inject_respawn_ocr_result(self, detected, confidence, method="ocr") -> None:
        self.__dict__.setdefault("injected_respawn", []).append((detected, confidence, method))

    def note_lobby_click(self) -> None:
        self._last_lobby_play_click_ts = time.time()

    def note_battle_event(self) -> None:
        self._last_battle_event_ts = time.time()
