"""Declarative schema and startup validation for `config.yaml`.

Closes Future 002 finding A-03. The failure this exists to prevent has already
happened in production once (code review 015): every value under
`behavior_tree.missile_evade` silently equalled its in-code default, because the
config is read through `cfg.get(key, default)` chains that cannot distinguish
"key absent" from "key misspelled". The British/American pair
`max_manoeuvre_s` / `max_maneuver_s` is the exact shape of the trap — the edit
appears to land, the gate passes, and the aircraft flies the old value.

The fix is deliberately narrow. This module does **not** replace the dict
interface or inject defaults, because either would change runtime behaviour
across ~200 call sites for no correctness gain. It validates the loaded mapping
against a declared shape and refuses to start on:

- an **unknown key** at any depth (with a did-you-mean suggestion),
- a **wrong type** for a known key,
- a value outside a **declared range**, and
- a **missing required key**.

Keeping the declared shape in one file also makes the config self-documenting:
the schema is the only place that lists every key the program actually reads.

Adding a config key means adding it here in the same change. That coupling is
the point — a key that is not in the schema is a key that fails at startup, so
the schema cannot silently fall behind the YAML.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field

# `bool` is a subclass of `int` in Python, so INT/NUMBER checks must reject it
# explicitly or `enabled: true` would satisfy a numeric leaf.
NUMBER = (int, float)


class ConfigError(ValueError):
    """Raised when config.yaml does not match the declared schema."""


@dataclass(frozen=True)
class Leaf:
    """A scalar or list value."""

    types: tuple = (object,)
    choices: tuple | None = None
    minimum: float | None = None
    maximum: float | None = None
    item_types: tuple | None = None   # for lists: allowed element types
    length: int | None = None         # for lists: exact required length
    allow_none: bool = False


@dataclass(frozen=True)
class Section:
    """A mapping with a fixed, known set of child keys."""

    children: dict = field(default_factory=dict)
    required: frozenset = frozenset()


@dataclass(frozen=True)
class MapOf:
    """A mapping with caller-defined keys, all sharing one value schema.

    Used for `crops:`, where the key names are calibration targets rather than
    program constants, so unknown-key rejection does not apply to the keys —
    only to the shape of each value.
    """

    value: object


def _num(minimum=None, maximum=None) -> Leaf:
    return Leaf(types=NUMBER, minimum=minimum, maximum=maximum)


def _int(minimum=None, maximum=None) -> Leaf:
    return Leaf(types=(int,), minimum=minimum, maximum=maximum)


BOOL = Leaf(types=(bool,))
STR = Leaf(types=(str,))
FRACTION = _num(0.0, 1.0)
SECONDS = _num(0.0)
_STR_LIST = Leaf(types=(list,), item_types=(str,))
_NUM_LIST = Leaf(types=(list,), item_types=NUMBER)
_HSV = Leaf(types=(list,), item_types=(int,), length=3)


_CROP = Section(
    children={
        "coords": Leaf(types=(list,), item_types=(list,), length=2),
        "text": _STR_LIST,
    },
    required=frozenset({"coords"}),
)


SCHEMA = Section(
    # "Required" means the program cannot construct itself without the key, not
    # merely that a full config would normally carry it. `region` and `monitor`
    # are needed to build Capture in every lane. `crops` deliberately is NOT
    # required: the replay smoke lane substitutes the analyzer and runs without
    # any, and a schema that rejects legitimate partial configs is a schema
    # people disable.
    required=frozenset({"region", "monitor"}),
    children={
        "unattended_mode": BOOL,
        "accept_invite": BOOL,
        # Upper bound is a sanity guard (a tick slower than a minute is a typo);
        # no lower bound beyond non-negative — the replay lanes tick at 0.01.
        "loop_interval_sec": _num(0.0, 60.0),
        "monitor": _int(0),
        "region": Section(
            children={
                "left": _int(),
                "top": _int(),
                "width": _int(1),
                "height": _int(1),
            },
            required=frozenset({"left", "top", "width", "height"}),
        ),
        "game_window_offset": Section(
            children={
                "x": Leaf(types=(int,), allow_none=True),
                "y": Leaf(types=(int,), allow_none=True),
            },
        ),
        "enemy_hsv": Section(children={"lower": _HSV, "upper": _HSV}),
        "crops": MapOf(_CROP),

        # ADR 084
        "stall_recovery": Section(children={
            "action_after_s": SECONDS,
            "unready_dwell_s": SECONDS,
            "scan_interval_s": SECONDS,
            "play_click_delay_s": SECONDS,
            "cooldown_s": SECONDS,
        }),

        # ADR 074
        "unknown_anomaly": Section(children={
            "screenshot_after_s": SECONDS,
            "recapture_interval_s": SECONDS,
            "max_per_episode": _int(0),
            "dismiss_grace_s": SECONDS,
            "dir": STR,
        }),

        "respawn_detection": Section(children={
            "use_ocr": BOOL,
            "use_gpu": BOOL,
            "ocr_cooldown": SECONDS,
            "text_hsv_lower": _HSV,
            "text_hsv_upper": _HSV,
            "mode": Leaf(types=(str,), choices=("ocr", "template", "dual")),
        }),

        # ADR 080 / SAF-004
        "health": Section(children={
            "death_no_digits_s": SECONDS,
            "max_plausible": _int(1),
            "value_confirm_window": _int(1),
            "value_confirm_tolerance": _num(0),
            "death_no_confirmed_s": SECONDS,
            "decline_evidence_drop": _num(0),
            "decline_evidence_window_s": SECONDS,
            "dropout_capture": Section(children={
                "enabled": BOOL,
                "capture_after_s": SECONDS,
                "recapture_interval_s": SECONDS,
                "max_per_session": _int(0),
                "dir": STR,
            }),
        }),

        "fuel": Section(children={
            "stale_after_s": SECONDS,
            "rearm_margin_pct": _num(0, 100),
        }),

        "incoming_detection": Section(children={
            "incoming_template_matching_enabled": BOOL,
            "incoming_template_threshold": FRACTION,
            "incoming_template_near_threshold_low": FRACTION,
            "incoming_template_near_threshold_high": FRACTION,
            "incoming_template_fallback_to_ocr": BOOL,
            "incoming_fallback_tokens": _STR_LIST,
            "incoming_template_telemetry_info": BOOL,
            "incoming_debounce_ms": _num(0),
            "incoming_template_scales": _NUM_LIST,
            "incoming_template_sources": _STR_LIST,
        }),

        "mission": Section(children={
            "weapon_loop_interval": SECONDS,
            "no_missiles_consecutive_required": _int(1),
            "no_missiles_abort_grace_s": SECONDS,
            "starting_stalled_reclassify_after_s": SECONDS,
            "respawn_clear_stability_s": SECONDS,
            "starting_max_wait_s": SECONDS,
            "startup_stall_exit_after_s": SECONDS,
            "good_luck_wait_s": SECONDS,
            "good_luck_bypass_on_alive": BOOL,
            "starting_health_probe_interval_s": SECONDS,
            "capture_stale_inject_s": SECONDS,
            "padlock_spread_missiles": _int(0),
            # ADR 047 waiting-state fallback (read in tick_handlers.py)
            "waiting_fallback_enabled": BOOL,
            "waiting_fallback_diff_threshold": FRACTION,
            "waiting_fallback_score_threshold": _int(0),
            "waiting_fallback_consecutive_required": _int(1),
            "waiting_fallback_min_elapsed_s": SECONDS,
            "play_reclick_interval": SECONDS,
            "play_reclick_missed_interval": SECONDS,
        }),

        "startup_state_detection": Section(children={
            "unknown_max_wait_s": SECONDS,
            "debounce_consecutive_required": _int(1),
        }),

        "debug": Section(children={
            "show_window": BOOL,
            "show_grid_highlighted": BOOL,
            "draw_markers": BOOL,
            "capture_with_overlay": BOOL,
            "debug_output_dir": STR,
        }),

        "j20_mission": Section(children={
            "target_painting_mode": BOOL,
            "attack_mode_dry_run": BOOL,
            "min_safe_altitude": _num(0),
            "bearing_deadzone_deg": _num(0, 180),
            "short_ring_min_count": _int(0),
            "ring_debounce_ticks": _int(0),
            "ema_reseed_angle_deg": _num(0, 360),
            "rear_commit_deg": _num(0, 360),
            "rear_release_deg": _num(0, 360),
            "orbit_direction": Leaf(types=(str,), choices=("left", "right")),
            "orbit_roll_hold_s": SECONDS,
            "orbit_roll_interval_s": SECONDS,
            "coarse_kp": _num(0),
            "coarse_min_hold_s": SECONDS,
            "coarse_max_hold_s": SECONDS,
            "coarse_cooldown_s": SECONDS,
        }),

        # ADR 024 / 070 / 073 / 076 / 081 / 083
        "behavior_tree": Section(children={
            "mode": Leaf(types=(str,), choices=("off", "shadow", "active")),
            "disengage_after_s": SECONDS,
            "disengage_hold_s": SECONDS,
            "evade_hold_s": SECONDS,
            "evade_health_threshold": Leaf(types=NUMBER, allow_none=True),
            "missile_evade": Section(children={
                "enabled": BOOL,
                "clear_seconds": SECONDS,
                "min_clear_samples": _int(1),
                # British spelling is load-bearing: the American variant was the
                # silent-default trap this whole module exists to catch.
                "max_manoeuvre_s": SECONDS,
                "max_hold_s": SECONDS,
                "pitch_down": BOOL,
            }),
            "climb": Section(children={
                "enabled": BOOL,
                "enter_below_alt": _num(0),
                "exit_above_alt": _num(0),
                "confirm_reads": _int(1),
                "max_climb_s": SECONDS,
                "pitch_pulse_s": SECONDS,
                "pulse_observe_s": SECONDS,
                "min_climb_rate": _num(0),
                "max_climb_rate": _num(0),
                "max_pitch_deg": _num(0, 90),
                "pitch_lead_s": SECONDS,
                "recover_below_time_s": Leaf(types=NUMBER, allow_none=True),
                "confirm_bypass_time_s": Leaf(types=NUMBER, allow_none=True),
                "descent_memory_s": SECONDS,
                "exit_pitch_deg": Leaf(types=NUMBER, allow_none=True),
                "exit_push_pulse_s": SECONDS,
                "exit_push_max_pulses": _int(1),
                "exit_lead_s": SECONDS,
                "fuel_reserve_pct": _num(0, 100),
                "spawn_guard": Section(children={
                    "enabled": BOOL,
                    "max_hold_s": SECONDS,
                    "release_overlap_s": SECONDS,
                    "pulse_s": SECONDS,
                    "observe_s": SECONDS,
                }),
                "sustain": Section(children={
                    "enabled": BOOL,
                    "enter_below_alt": _num(0),
                    "exit_above_alt": _num(0),
                    "max_climb_s": SECONDS,
                }),
            }),
        }),

        "minimap": Section(children={
            "mask_radius_frac": FRACTION,
            "min_blob_px": _int(0),
            "max_blob_px": _int(0),
            "ema_alpha": FRACTION,
            "ema_reset_after_s": SECONDS,
        }),

        "tracking": Section(children={
            "enabled": BOOL,
            "acquisition_region_pct": Leaf(types=(list,), item_types=NUMBER, length=4),
            "deadband": FRACTION,
            "kp": _num(0),
            "min_hold_sec": SECONDS,
            "max_hold_sec": SECONDS,
            "command_cooldown_sec": SECONDS,
            "lost_timeout_sec": SECONDS,
            "prefer_red_lock": BOOL,
            "local_roi_enabled": BOOL,
            "local_roi_scale": FRACTION,
            "local_roi_min_px": Leaf(types=(list,), item_types=(int,), length=2),
            "local_roi_expand_factor": _num(1.0),
            "local_roi_max_scale": FRACTION,
            "local_roi_reacquire_cycles": _int(0),
        }),

        "tracking_hsv": Section(children={
            "red_lower": _HSV,
            "red_upper": _HSV,
            "green_lower": _HSV,
            "green_upper": _HSV,
            "min_contour_area": _num(0),
            "min_aspect_ratio": _num(0),
        }),

        "hud": Section(children={
            "enabled": BOOL,
            "output_path": STR,
            "interval_sec": SECONDS,
        }),

        # ADR 038 / 067 / 069
        "telemetry": Section(children={
            "max_speed_mph": _num(0),
            "max_altitude_ft": _num(0),
            "max_speed_change_mph_s": _num(0),
            "max_altitude_change_fps": _num(0),
            "plausibility_margin": _num(0),
            "max_gate_dt_s": SECONDS,
            "reseed_after_rejections": _int(1),
            "smoothing_window": _int(1),
            "stale_after_s": SECONDS,
            "trend_min_alt_rate_fps": _num(0),
            "trend_min_speed_rate_mph_s": _num(0),
            "steep_dive_min_sin": FRACTION,
            "level_max_sin": FRACTION,
            "ocr_every_n_ticks": _int(1),
            "eject_closed_loop": Section(children={
                "enabled": BOOL,
                "check_interval_s": SECONDS,
                "confirm_consecutive": _int(1),
                "legacy_nose_hold_s": SECONDS,
                "over_rotation_after_s": SECONDS,
                "target_dive_angle_deg": _num(0, 90),
                "dive_angle_floor_deg": _num(0, 90),
                "descent_target_mps": _num(0),
                "descent_floor_mps": _num(0),
                "rotation_pulse_s": SECONDS,
                "observe_after_pulse_s": SECONDS,
                "max_rotation_pulses": _int(1),
                "eject_max_s": SECONDS,
                    "abort_on_rearm": BOOL,
            }),
        }),

        # Performance 008 — periodic RESOURCE line for long-session leak diagnosis.
    "memory_guard": Section({          # ADR 090
        "enabled": BOOL,
        "soft_limit_mb": _int(1),
        "hard_limit_mb": _int(1),
    }),
        "resource_monitor": Section(children={
            "enabled": BOOL,
            "interval_s": SECONDS,
            "warmup_s": SECONDS,
            "game_process_name": STR,
        }),
        # Performance 008 — heap census, the Python-vs-native discriminator.
        "heap_census": Section(children={
            "enabled": BOOL,
            "interval_s": SECONDS,
            "top_n": _int(1),
            "tracemalloc": BOOL,
            "tracemalloc_depth": _int(1),
            "gc_census": BOOL,
            "max_census_ms": _int(0),
        }),
        "performance": Section(children={
            "round_histogram": Section(children={
                "enabled": BOOL,
                "png_enabled": BOOL,
                "png_every_n_rounds": _int(0),
                "output_dir": STR,
            }),
            "regression": Section(children={
                "min_sessions": _int(0),
                "min_cycles": _int(0),
                "min_crop_samples": _int(0),
                "min_reaction_events": _int(0),
                "threshold_pct": _num(0),
            }),
        }),
    },
)


def _type_name(types: tuple) -> str:
    return " or ".join(t.__name__ for t in types)


def _suggest(key: str, known) -> str:
    match = difflib.get_close_matches(key, sorted(known), n=1, cutoff=0.6)
    return f" — did you mean {match[0]!r}?" if match else ""


def _check_leaf(spec: Leaf, value, path: str, errors: list) -> None:
    if value is None:
        if not spec.allow_none:
            errors.append(f"{path}: must not be null")
        return

    if spec.types != (object,):
        # bool-before-int: `enabled: true` must not satisfy a numeric leaf.
        if bool not in spec.types and isinstance(value, bool):
            errors.append(f"{path}: expected {_type_name(spec.types)}, got bool ({value!r})")
            return
        if not isinstance(value, spec.types):
            errors.append(
                f"{path}: expected {_type_name(spec.types)}, "
                f"got {type(value).__name__} ({value!r})"
            )
            return

    if spec.choices is not None and value not in spec.choices:
        errors.append(f"{path}: {value!r} is not one of {list(spec.choices)}")
        return

    if isinstance(value, list):
        if spec.length is not None and len(value) != spec.length:
            errors.append(f"{path}: expected {spec.length} items, got {len(value)}")
        if spec.item_types is not None:
            for i, item in enumerate(value):
                if bool not in spec.item_types and isinstance(item, bool):
                    errors.append(f"{path}[{i}]: expected {_type_name(spec.item_types)}, got bool")
                elif not isinstance(item, spec.item_types):
                    errors.append(
                        f"{path}[{i}]: expected {_type_name(spec.item_types)}, "
                        f"got {type(item).__name__} ({item!r})"
                    )
        return

    if isinstance(value, NUMBER):
        if spec.minimum is not None and value < spec.minimum:
            errors.append(f"{path}: {value!r} is below the minimum {spec.minimum}")
        if spec.maximum is not None and value > spec.maximum:
            errors.append(f"{path}: {value!r} is above the maximum {spec.maximum}")


def _check(spec, value, path: str, errors: list) -> None:
    if isinstance(spec, Section):
        if not isinstance(value, dict):
            errors.append(f"{path or '<root>'}: expected a mapping, got {type(value).__name__}")
            return
        for missing in sorted(spec.required - set(value)):
            errors.append(f"{path}.{missing}".lstrip(".") + ": required key is missing")
        for key, child in value.items():
            child_path = f"{path}.{key}".lstrip(".")
            if key not in spec.children:
                errors.append(
                    f"{child_path}: unknown key{_suggest(key, spec.children)}"
                )
                continue
            _check(spec.children[key], child, child_path, errors)
        return

    if isinstance(spec, MapOf):
        if not isinstance(value, dict):
            errors.append(f"{path}: expected a mapping, got {type(value).__name__}")
            return
        for key, child in value.items():
            _check(spec.value, child, f"{path}.{key}".lstrip("."), errors)
        return

    _check_leaf(spec, value, path, errors)


def validate_config(cfg, *, schema: Section = SCHEMA) -> list[str]:
    """Return a list of human-readable problems; empty means the config is valid."""
    errors: list[str] = []
    _check(schema, cfg, "", errors)
    return sorted(errors)


def assert_valid_config(cfg, *, source: str = "config", schema: Section = SCHEMA) -> None:
    """Raise `ConfigError` listing every problem, or return silently.

    All problems are reported at once rather than one per run — a config with
    three misspellings should cost one startup, not three.
    """
    errors = validate_config(cfg, schema=schema)
    if errors:
        listed = "\n".join(f"  - {e}" for e in errors)
        raise ConfigError(
            f"{source} failed schema validation ({len(errors)} problem"
            f"{'s' if len(errors) != 1 else ''}):\n{listed}\n"
            "Every key the program reads is declared in wingman/config_schema.py; "
            "an unknown key here is a key that would silently keep its code default."
        )
