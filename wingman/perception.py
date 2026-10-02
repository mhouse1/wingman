"""What the controller may read from perception, and the few commands it may send. CR-018-15.

`Controller` reached into `GameStateAnalyzer` through 21 members at 61 call sites,
three of them private (`_ammo_missiles` under `_ammo_lock`, `_last_battle_event_ts`,
`_last_lobby_play_click_ts`). Nothing said which parts of the 4,500-line analyzer a
tactic may depend on, and 35 analyzer-shaped stubs in the tests each re-implemented
whatever subset their test happened to touch.

`Perception` is that list. `tests/test_perception_port.py` fails when the controller
or the hotkey handlers use an analyzer member not listed here, or any private one,
and checks that the real analyzer and the shared test fake
(`tests/perception_fake.py`) both provide every member. A new read is therefore a
decision made here, not a reach made at a call site.

A leaf module: it imports only the state vocabulary and typing.
"""

from typing import Protocol, runtime_checkable

from .state import GameState


@runtime_checkable
class Perception(Protocol):
    # --- reads: the latest perceived values ------------------------------------
    @property
    def game_state(self) -> GameState: ...

    @property
    def game_battle_alive(self) -> bool: ...

    def get_telemetry(self): ...

    def get_ammo_missiles(self): ...

    def get_ammo_missiles_read_seq(self) -> "int | None": ...

    def get_ammo_flares(self): ...

    def get_health(self): ...

    def get_incoming_cache_timestamp(self) -> float: ...

    def get_incoming_cache_result(self): ...

    def get_afterburner_fuel_pct(self) -> "int | None": ...

    def nose_direction(self) -> str: ...

    def scan_region_for_good_luck(self, frame) -> bool: ...

    def crops_for_state(self, state: "GameState | None" = None) -> dict: ...

    # --- commands: the controller tells perception something happened ----------
    def trigger_event(self, name: str) -> bool: ...

    def arm_starting_health_scan(self): ...

    def disarm_starting_health_scan(self): ...

    def mark_health_dead_synthetic(self): ...

    def inject_respawn_ocr_result(self, detected: bool, confidence: float,
                                  method: str = "ocr") -> None: ...

    def note_lobby_click(self) -> None: ...

    def note_battle_event(self) -> None: ...


# The members above, by name, for the structural test.
PERCEPTION_MEMBERS = frozenset(
    name for name in vars(Perception)
    if not name.startswith("_")
)
