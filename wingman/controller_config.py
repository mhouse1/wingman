"""Typed construction parameters for `Controller`.

Closes Future 002 finding A-02. `Controller.__init__` had grown to 21
positional-or-keyword parameters, four of them raw `*_cfg: dict | None` blocks
appended one per feature (telemetry, missile evade, climb, fuel). That shape has
two costs: every new tactic widens the signature, and the `cfg.get(...)`
extraction for each block was duplicated at the call site in `main.py` rather
than living in one place.

The split here is **collaborators vs configuration**. Objects the controller
talks to — analyzer, capture, exit event, callbacks — stay explicit constructor
arguments, because they are wiring and a reader needs to see them. Everything
that is a tuned value moves into this frozen dataclass.

`from_config` is the single place that knows which `config.yaml` block feeds
which controller setting; the schema in `config_schema.py` guarantees those keys
exist and are the right type before this runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class ControllerConfig:
    """Tuning and mode flags for `Controller`.

    Frozen so a tactic thread cannot retune the controller mid-flight; use
    `dataclasses.replace` (or the `**overrides` argument of `from_config`) to
    derive a variant.
    """

    # --- Fire control ---
    fire_button: str = "left"
    fire_hold_seconds: float = 0.0
    weapon_loop_interval: float = 0.5

    # --- Match-entry timing (mission: block) ---
    starting_max_wait_s: float = 90.0
    good_luck_wait_s: float = 13.0
    good_luck_bypass_on_alive: bool = True
    capture_stale_inject_s: float = 10.0

    # --- Run-mode flags. Not pure config: replay and capture lanes override
    #     these, which is why they are `replace`-able rather than read-only
    #     properties of the YAML.
    target_painting_mode: bool = False
    simulate_os_input: bool = False
    disable_hotkeys: bool = False
    capture_with_overlay: bool = True

    # --- Subsystem blocks, passed through to the owning helpers ---
    telemetry: dict = field(default_factory=dict)
    missile_evade: dict = field(default_factory=dict)
    climb: dict = field(default_factory=dict)
    fuel: dict = field(default_factory=dict)

    @classmethod
    def from_config(cls, cfg: dict | None, **overrides) -> "ControllerConfig":
        """Build from a validated `config.yaml` mapping.

        `overrides` applies run-mode flags the YAML cannot know about (replay
        mode, capture mode). Unknown override names raise `TypeError` here
        rather than being silently ignored — the same fail-fast contract the
        config schema enforces on the YAML side.
        """
        cfg = cfg or {}
        mission = cfg.get("mission", {}) or {}
        bt = cfg.get("behavior_tree", {}) or {}
        j20 = cfg.get("j20_mission", {}) or {}
        debug = cfg.get("debug", {}) or {}

        base = cls(
            weapon_loop_interval=float(mission.get("weapon_loop_interval", 0.5)),
            starting_max_wait_s=float(mission.get("starting_max_wait_s", 90.0)),
            good_luck_wait_s=float(mission.get("good_luck_wait_s", 13.0)),
            good_luck_bypass_on_alive=bool(mission.get("good_luck_bypass_on_alive", True)),
            capture_stale_inject_s=float(mission.get("capture_stale_inject_s", 10.0)),
            target_painting_mode=bool(j20.get("target_painting_mode", False)),
            capture_with_overlay=bool(debug.get("capture_with_overlay", True)),
            telemetry=cfg.get("telemetry", {}) or {},
            missile_evade=bt.get("missile_evade", {}) or {},
            climb=bt.get("climb", {}) or {},
            fuel=cfg.get("fuel", {}) or {},
        )
        if not overrides:
            return base
        known = {f for f in base.__dataclass_fields__}
        unknown = sorted(set(overrides) - known)
        if unknown:
            raise TypeError(
                f"ControllerConfig.from_config got unknown override(s): {unknown}. "
                f"Known fields: {sorted(known)}"
            )
        return replace(base, **overrides)
