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


# Design 011 (ACS Mode) step 1. Profile names are caller-defined (like
# `crops:`), not fixed schema keys — MapOf, not Section, for the same reason.
_JET_PROFILE = Section(children={"has_padlock": BOOL})

# capture_budget: 0 = unlimited for that dimension.
_CAPTURE_BUDGET = Section(children={"max_files": _int(0), "max_mb": _num(0)})


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
        "jet_profile": Section(children={
            "active": STR,
            "profiles": MapOf(_JET_PROFILE),
        }),
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
        # mission_loiter: the survival hold
        "loiter_mission": Section(children={
            "target_alt": _num(0),
            "hysteresis_m": _num(0),
            "orbit_direction": STR,
            "orbit_roll_interval_s": SECONDS,
            "orbit_roll_hold_s": SECONDS,
            "orbit_pitch_hold_s": _num(0),
            "orbit_deadband_m": _num(0),   # ADR 112
            "level_band_deg": _num(0),     # ADR 114
            "recover_hold_s": _num(0),
            "entry_pullup_s": _num(0),     # ADR 123
                "lock_timeout_s": _num(0),
                "boundary_avoid_frac": FRACTION,
                "orbit_window_s": _num(0),
                "closing_margin": FRACTION,
                "boundary_max_age_s": _num(0),
            "tick_s": SECONDS,
        }),
        "enemy_hsv": Section(children={"lower": _HSV, "upper": _HSV}),
        # Design 010 instrumentation
        "return_to_battle": Section(children={
            # x1, y1, x2, y2 as screen fractions
            "region": Leaf(types=(list,), item_types=NUMBER, length=4),
            "min_red_frac": FRACTION,
            "ocr_region": Leaf(types=(list,), item_types=NUMBER, length=4),
            "text": Leaf(types=(list,), item_types=(str,)),
        }),
        "crops": MapOf(_CROP),

        # ADR 084
        "stall_recovery": Section(children={
            "action_after_s": SECONDS,
            "unready_dwell_s": SECONDS,
            "scan_interval_s": SECONDS,
            "play_click_delay_s": SECONDS,
            "leave_click_delay_s": SECONDS,
            "cooldown_s": SECONDS,
        }),

        # ADR 074
        "unknown_anomaly": Section(children={
            "screenshot_after_s": SECONDS,
            "recapture_interval_s": SECONDS,
            "max_per_episode": _int(0),
            "dismiss_grace_s": SECONDS,
            "dir": STR,
            "stuck_warn_interval_s": SECONDS,
            "stuck_warn_max_interval_s": SECONDS,
        }),

        # ADR 146 (2026-09-24) — see config.yaml's own comment on this block.
        "game_unknown_close": Section(children={
            "enabled": BOOL,
            "min_stuck_s": SECONDS,
            "retry_interval_s": SECONDS,
            "max_clicks": _int(0),
            "min_score": FRACTION,
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
            # ADR 137 D7: the dropout_capture episode above explicitly
            # excludes this window (telemetry_hud_live() gates it out as a
            # "death/menu gap, not a dropout") — this is that excluded case,
            # captured on purpose instead of skipped.
            "respawn_stall_capture": Section(children={
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
            # SAF-001
            "manual_takeover": Section(children={"persist_through_respawn": BOOL}),
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
            "j20_turn_guard_s": SECONDS,   # ADR 132
            # ADR 144: which mission battle entry launches; ADR 145 adds jas39
            # and makes the 'u' hotkey launch it too; ADR 149 adds f111
            "default_mission": Leaf(types=(str,),
                                    choices=("j20", "su30", "jas39", "f111")),
            "padlock_spread_missiles": _int(0),
            # ADR 137 D5, pre_crash_buffer_s/pre_crash_freshness_s/
            # pre_crash_lookback_s added D8
            "crash_capture": Section(children={
                "enabled": BOOL,
                "max_per_session": _int(0),
                "pre_crash_buffer_s": SECONDS,
                "pre_crash_freshness_s": SECONDS,
                "pre_crash_lookback_s": SECONDS,
                # ADR 143: enemy_fire_lookback_s/terrain_lookback_s
                "enemy_fire_lookback_s": SECONDS,
                "terrain_lookback_s": SECONDS,
                "dir": STR,
            }),
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

        # ADR 144: mission_su30, the scripted Su-30 sequence
        "su30_mission": Section(children={
            "climb_alt_m": _num(0),
            # ADR 147: the hard altitude floor while su30 is in play; absent
            # leaves the tree's own floor and sustain band in charge.
            "alt_floor_m": _num(0),
            "climb_max_s": SECONDS,
            "nose_angle_deg": _num(-90, 90),
            "angle_tolerance_deg": _num(0, 90),
            "angle_confirm_reads": _int(1),
            "angle_pulse_s": SECONDS,
            "angle_max_s": SECONDS,
            "tick_s": SECONDS,
            "lock_timeout_s": SECONDS,
        }),

        # ADR 149: mission_f111, mission_su30 plus the wing sweep
        "f111_mission": Section(children={
            "climb_alt_m": _num(0),
            # ADR 147 extended by ADR 149: the hard altitude floor while f111
            # is in play; absent leaves the tree's own floor and sustain band.
            "alt_floor_m": _num(0),
            "climb_max_s": SECONDS,
            "nose_angle_deg": _num(-90, 90),
            "angle_tolerance_deg": _num(0, 90),
            "angle_confirm_reads": _int(1),
            "angle_pulse_s": SECONDS,
            "angle_max_s": SECONDS,
            "tick_s": SECONDS,
            "unsweep_alt_m": _num(0),
            "unsweep_timeout_s": SECONDS,
            "wingsweep_tap_s": SECONDS,
        }),

        # ADR 145: mission_jas39, J20 plus the cloak loop
        "jas39_mission": Section(children={
            "turn_guard_s": SECONDS,
            # Floor of 0.5 s: with the 0.1 s tap, anything shorter is holding
            # the key down rather than retrying it.
            "cloak_press_interval_s": _num(0.5),
        }),

        # ADR 024 / 070 / 073 / 076 / 081 / 083
        "behavior_tree": Section(children={
            "mode": Leaf(types=(str,), choices=("off", "shadow", "active")),
            "disengage_after_s": SECONDS,
            "disengage_hold_s": SECONDS,
            "evade_hold_s": SECONDS,
            "evade_health_threshold": Leaf(types=NUMBER, allow_none=True),
            "missile_evade": Section(children={
            "afterburner_clear_s": _num(0),   # ADR 128 / FR-008
            "afterburner_max_s": _num(0),
                "enabled": BOOL,
                "clear_seconds": SECONDS,
                "min_clear_samples": _int(1),
                # British spelling is load-bearing: the American variant was the
                # silent-default trap this whole module exists to catch.
                "max_manoeuvre_s": SECONDS,
                "max_hold_s": SECONDS,
                "pitch_down": BOOL,
            }),
            # ADR 134: hold the afterburner whenever fuel is above the floor
            # and nothing higher-priority (eject/evade/climb) needs the key.
            "afterburner_cruise": Section(children={
                "enabled": BOOL,
                "min_fuel_pct": _num(0, 100),
                "rearm_fuel_pct": _num(0, 100),
                "confirm_reads": _int(1),
            }),
            # Phase 1 (operator directive): release the airbrake and hold
            # the afterburner whenever speed drops below the floor.
            "stall_prevention": Section(children={
                "enabled": BOOL,
                "min_speed_kph": _num(0),
                "confirm_reads": _int(1),
            }),
            "boundary": Section(children={
                "turn_frac": FRACTION,
                "recede_frac": FRACTION,
                "hold_s": _num(0),
                "release_frac": FRACTION,
                "min_clear_frac": FRACTION,
                "blind_ticks": _int(0),
                "entry_ratio": FRACTION,
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
                "boundary_turn_max_s": _num(0),
                "exit_lead_s": SECONDS,
                "fuel_reserve_pct": _num(0, 100),
                "alt_floor_m": Leaf(types=NUMBER, allow_none=True),
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
                # HLDD 001 Phase 1 — trigger/debounce tuning for the forward
                # sky-occlusion detector. Detection config (crop, sky HSV)
                # lives in the top-level terrain_avoidance section instead,
                # mirroring the minimap.boundary_hsv / behavior_tree.boundary.*
                # split.
                "terrain_avoidance": Section(children={
                    "enabled": BOOL,
                    "shadow": BOOL,
                    "sky_min_frac": FRACTION,
                    "confirm_reads": _int(1),
                    "capture_max": _int(0),
                    "capture_cooldown_s": _num(0),
                    "capture_dir": STR,
                }),
            }),
            # HLDD 013 Phase 1: TACTIC_ATTACK_SUPPORT's fallback roll — own
            # gain/hold-time knobs, independent of j20_mission's coarse_kp/
            # coarse_min_hold_s/etc (see the HLDD's Actuation section for why
            # this must not resolve to self._ctl_cfg's combat tuning).
            "attack_support": Section(children={
                "seek_center_enabled": BOOL,
                "seek_center_trigger_frac": FRACTION,       # matches boundary_near_frac
                "seek_center_deadzone_deg": _num(0, 180),   # matches bearing_deadzone_deg's range
                "seek_center_kp": _num(0),
                "seek_center_min_hold_s": SECONDS,
                "seek_center_max_hold_s": SECONDS,
                "seek_center_cooldown_s": SECONDS,
            }),
        }),

        "minimap": Section(children={
            "boundary_median_age_s": _num(0),   # ADR 113
            "blind_capture_max": _int(0),       # ADR 117
            "blind_capture_interval_s": _num(0),
            "minimap_present_min_px": _int(0),  # ADR 117: minimap drawn at all
            "mask_radius_frac": FRACTION,
            "min_blob_px": _int(0),
            "max_blob_px": _int(0),
            "ema_alpha": FRACTION,
            "ema_reset_after_s": SECONDS,
            # ADR 028 revision 4
            "regroup_enabled": BOOL,
            "friendly_hsv": Section(children={"lower": _HSV, "upper": _HSV}),
            # Design 010 instrumentation
            "boundary_hsv": Section(children={"lower": _HSV, "upper": _HSV}),
            "boundary_min_px": _int(0),
            "boundary_min_span_frac": FRACTION,
            "boundary_thin_component_max_radial_frac": FRACTION,  # ADR 117 D9
            "boundary_thin_component_min_elongation": _num(1),  # ADR 117 D9
            "boundary_thin_component_max_arc_residual_frac": _num(0),  # ADR 117 D10
            "boundary_thin_component_min_arc_radius_frac": _num(0),  # ADR 117 D11
            "boundary_relaxed_span_frac": _num(0),   # ADR 133
            "boundary_void_min_frac": _num(0),
            "boundary_void_v_max": _int(0),
            "boundary_void_s_max": _int(0),
            "boundary_void_radius_frac": _num(0),
            "boundary_close_iters": _int(0),
            "boundary_max_thickness_frac": FRACTION,
            "boundary_near_frac": FRACTION,
            "boundary_trace_ticks": _int(1),
            "boundary_respawn_settle_s": _num(0),
            "boundary_capture_dir": STR,
            "boundary_capture_max": _int(0),
            "boundary_approach_capture_max": _int(0),
        }),

        # HLDD 001 Phase 1 — forward sky-occlusion terrain-ahead detector.
        # Detection config only; the trigger/debounce tuning that consumes
        # this reading lives under behavior_tree.climb.terrain_avoidance.
        "terrain_avoidance": Section(children={
            "crop": STR,
            "sky_hsv": Section(children={"lower": _HSV, "upper": _HSV}),
        }),

        "tracking": Section(children={
            "enabled": BOOL,
            "actuate": BOOL,
            "acquisition_region_pct": Leaf(types=(list,), item_types=NUMBER, length=4),
            "deadband": FRACTION,
            "kp": _num(0),
            "min_hold_sec": SECONDS,
            "max_hold_sec": SECONDS,
            "command_cooldown_sec": SECONDS,
            # Pitch axis (HLDD 005, 2026-09-21) — own flag, own gains,
            # deliberately not reusing actuate/deadband/kp/etc above. See
            # HLDD 005 Safety and Gating Rules for why pitch needs an
            # independent actuation gate from roll's.
            "actuate_pitch": BOOL,
            "pitch_deadband": FRACTION,
            "pitch_kp": _num(0),
            "pitch_min_hold_sec": SECONDS,
            "pitch_max_hold_sec": SECONDS,
            "pitch_command_cooldown_sec": SECONDS,
            # Review 018 CR-018-04 (2026-09-25): the tall-bar detector's keys
            # (prefer_red_lock, red_mass_steering, red_mass_cluster_select,
            # red_mass_nameplate_gate_enabled, red_mass_tallbar_fallback,
            # ranked_lock_priority, ranked_priority_tolerance_px) are gone;
            # an old config carrying them fails here rather than silently.
            # See config.yaml's comment on this key for what it targets.
            "red_mass_exclude_pct": Leaf(types=(list,), item_types=NUMBER, length=4),
            # Action item 001, Cycle 12 (2026-09-24) — see config.yaml's own
            # comments on these keys.
            "red_mass_exclude_zones_pct": Leaf(types=(list,), item_types=(list,)),
            "red_mass_cluster_glyph_window_px": Leaf(types=(list,), item_types=(int,), length=2),
            # 2026-09-24: the steering point (the aircraft, below its nameplate) and
            # the acquire/keep split — see config.yaml's own comments on these keys.
            "red_mass_aim_offset_px": _int(-300, 300),
            "red_mass_keep_min_glyphs": _int(0),
            "red_mass_keep_box_pct": Leaf(types=(list,), item_types=NUMBER, length=2),
            # null means "same as tracking_hsv.red_upper's hue". See
            # config.yaml's own comment on this key.
            "red_mass_hue_max": _int(0, 179),
            # null means "same as tracking_hsv.red_lower's value". See
            # config.yaml's own comment.
            "red_mass_value_min": _int(0, 255),
            # HLDD 005 nameplate gate (2026-09-23) — see config.yaml's own
            # comment on these keys for what they target and how measured.
            "red_mass_nameplate_min_glyphs": _int(0),
            "red_mass_nameplate_glyph_area": Leaf(types=(list,), item_types=(int,), length=2),
            "red_mass_nameplate_glyph_max_dim": _int(0),
            # HLDD 005 Sustained-Hold Actuation (2026-09-23) — see
            # config.yaml's own comment on this key for the phased rollout.
            "sustained_hold_enabled": BOOL,
            "pitch_lead_s": SECONDS,   # CR-018-01
        }),

        # CR-018-04: only the red range remains (the nameplate mask).
        "tracking_hsv": Section(children={
            "red_lower": _HSV,
            "red_upper": _HSV,
        }),

        "padlock_indicator": Section(children={
            "region_pct": Leaf(types=(list,), item_types=NUMBER, length=4),
            "green_lower": _HSV,
            "green_upper": _HSV,
            "min_contour_area": _num(0),
            "max_contour_area": _num(0),
            "min_dashes": _int(1),
        }),

        # ADR 140: fixed-screen-center padlock-off dot — distinct element
        # and distinct config block from padlock_indicator above (ADR 136's
        # dashed ring, which moves with flight attitude and is NOT reused).
        "padlock_center_indicator": Section(children={
            "region_pct": Leaf(types=(list,), item_types=NUMBER, length=4),
            "green_lower": _HSV,
            "green_upper": _HSV,
            "min_pixels": _int(1),
            "confirm_seconds": _num(0),
            "max_correction_attempts": _int(1),   # ADR 140 D6
        }),

        # HLDD 015: missiles-empty alternative to eject_and_dive — switch to
        # secondary weapons and pursue with both tracking axes instead of
        # diving. Hard-gated: enabled must stay false until Design 005's
        # Two-Axis Rollout has a live-validated pitch channel on the ambient
        # path (see pursue_and_engage's own docstring, controller.py).
        "pursuit_mode": Section(children={
            "enabled": BOOL,
            "pursuit_max_duration_s": SECONDS,
            "recovery_max_s": SECONDS,   # ADR 148
            "pursuit_padlock_verify": BOOL,
            "ammo_zero_grace_s": SECONDS,
            "search_resume_delay_s": SECONDS,
            "search_resume_centre_err": FRACTION,
            "search_resume_centre_delay_s": SECONDS,
            "empty_confirm_reads": _int(1),
            "steer_interval_s": SECONDS,    # CR-018-01
            "engage_interval_s": SECONDS,   # CR-018-01
            "dive_guard_margin_m": _num(0),   # review 018 dive guard
            "dive_guard_ttg_s": SECONDS,      # review 018 dive guard
            "dive_guard_pullout_pulse_s": SECONDS,
            "dive_guard_pullout_interval_s": SECONDS,
            "dive_guard_level_rate_mps": _num(0),
            "search_floor_m": _num(0),                  # look-down search
            "search_look_down_pulse_s": SECONDS,
            "search_look_down_interval_s": SECONDS,
            "search_look_down_min_deg": _num(-90, 0),
        }),

        "hud": Section(children={
            "enabled": BOOL,
            "output_path": STR,
            "interval_sec": SECONDS,
            # Timestamped archive of the annotated frame while target
            # tracking runs during a secondary-missile encounter (ADR 136
            # heatdive loop) — live_hud.png itself is overwritten every
            # render, so this is what lets a saved frame be lined up
            # against a wingman.log timestamp for debugging.
            "target_tracking_archive": Section(children={
                "enabled": BOOL,
                "dir": STR,
                "max_files": _int(0),
                # Action item 001: also save the exact, unannotated crop the
                # tracker scanned beside each archived frame (the annotated
                # PNG's PURSUING marker overwrites the very pixels that
                # produced the lock, so it cannot be replayed faithfully).
                "save_raw_scan": BOOL,
                # Seconds between archived frames, and a cap per contiguous
                # encounter — spreads the per-session budget across the
                # session instead of spending it in the first minutes.
                "min_interval_s": SECONDS,
                "max_per_encounter": _int(0),
            }),
        }),

        # Cross-session disk safety net shared by every capture write site
        # (wingman/capture_budget.py). Directory keys are paths.
        "capture_budget": Section(children={
            "min_free_gb": _num(0),
            "default": _CAPTURE_BUDGET,
            "dirs": MapOf(_CAPTURE_BUDGET),
            "rotated_logs": _CAPTURE_BUDGET,
            "session_video": _CAPTURE_BUDGET,
        }),

        # Design 012: opt-in session video, paired with the BT JSONL trace.
        "session_recording": Section(children={
            "fps": _num(0),
            "scale": FRACTION,
        }),

        # ADR 099: nested display lane
        "nested": Section(children={
            "enabled": BOOL,
            "display": STR,
            "size": STR,
        }),

        # ADR 098: focus guard for key injection
        "focus_guard": Section(children={
            "enabled": BOOL,
            "ttl_s": SECONDS,
            "session_ttl_s": SECONDS,
            "on_unknown": STR,
            "display": STR,
            "process_name": STR,
        }),

        # ADR 038 / 067 / 069
        "telemetry": Section(children={
            "nose_direction_deadband_mps": _num(0),   # ADR 123
            "max_speed_mph": _num(0),
            "max_altitude_ft": _num(0),
            "max_speed_change_mph_s": _num(0),
            "max_altitude_change_fps": _num(0),
            "plausibility_margin": _num(0),
            "max_gate_dt_s": SECONDS,
            "reseed_after_rejections": _int(1),
            # ADR 097: the altitude gate is an absolute vertical-rate ceiling
            # (D2) plus agreement-based anchor reseeding (D3).
            "max_alt_rate_mps": _num(0),
            "reseed_agreement_m": _num(0),
            "digit_drop_ratio": _num(0, 1),   # ADR 150
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
                "heatdive_enabled": BOOL,
                "heatdive_padlock_verify": BOOL,
                "telemetry_confirm_polls": _int(0),
            }),
        }),

        # Performance 008 — periodic RESOURCE line for long-session leak diagnosis.
    "memory_guard": Section({          # ADR 090
        "enabled": BOOL,
        "soft_limit_mb": _int(1),
        "hard_limit_mb": _int(1),
    }),
        # ADR 094 — finish-round-then-exit hotkey.
        "finish_round_then_exit": Section(children={
            "close_game": BOOL,
            "game_term_grace_s": SECONDS,
        }),
        # ADR 092 — leak gate thresholds.
        "leak_gate": Section(children={
            "log_dir": STR,
            "warmup_s": SECONDS,
            "min_window_h": _num(0),
            "min_samples": _int(1),
            "mi_use_pass_mb_h": _num(0),
            "mi_use_fail_mb_h": _num(0),
            "rss_pass_mb_h": _num(0),
            "rss_fail_mb_h": _num(0),
            "history_sessions": _int(1),
        }),
        # ADR 093 — progress watchdog.
        "liveness_guard": Section(children={
            "enabled": BOOL,
            "stall_limit_s": SECONDS,
            "hard_limit_s": SECONDS,
        }),
        # Anomaly 003 — diagnostic-only, gated on --record-session at the
        # call site (see docs/anomaly/003-eject-no-telemetry-...).
        "eject_stuck_detector": Section(children={
            "enabled": BOOL,
            "eject_stuck_after_s": SECONDS,
        }),
        # ADR 093 — ceiling on the ADR 087 blackout ESC suppression.
        "lobby_blackout": Section(children={
            "blackout_esc_ceiling_s": SECONDS,
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
