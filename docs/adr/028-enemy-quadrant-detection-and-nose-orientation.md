# ADR 028 — Minimap Ring-Engage Navigation

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-08-08 | 1.7.1           |

> **Revision note:** third in-place revision of this Draft (permitted: never
> `Accepted`; the filename keeps its original slug). Revision 1 specified a
> 3×3 quadrant split of `ENEMY_CLOSE_BY` (never implemented). Revision 2
> replaced it with a single `MINIMAP` crop and an overhead-attack phase
> machine gated on `attack_altitude` — implemented and flown on 2026-08-08 in
> one dry-run and one live session. Revision 3 (this text) replaces the
> overhead phase machine with mission-agnostic **ring-engage navigation**,
> motivated by those sessions' measurements. The sensor decision (single
> minimap crop, HSV component scan, vector-EMA smoothing) is unchanged and
> carries its evidence forward. Companion HLDD:
> [Design 003](../hldd/003-enemy-quadrant-detection-hldd.md), revised
> together with this ADR.

## Context

The revision-2 tactic — climb to `attack_altitude`, then position directly
above the enemy cluster — was implemented and flown. Its sensor pipeline
worked (bearing available on every battle tick of both sessions; the field-
proven `enemy_hsv` mask and component area band rejected the locked-target
ring and route-line overlays). The tactic layer did not survive contact with
the data:

1. **The altitude gate starved the tactic.** Live session 2026-08-08 09:00:
   74% of battle ticks idled in Climb; mean altitude 4291 against an 8000
   gate because the J20 mission's eject-dive cycle keeps the aircraft low.
   Lowering the gate to 7000 would have added only 6 eligible ticks of 97 —
   the gate, not the threshold value, is the flaw.
2. **The eject cycle preempts positioning.** Both at-altitude engagement
   windows ended in a mission eject within seconds (09:04:55, 09:15:09). A
   tactic that needs long dwell at a precise station fights the mission
   profile.
3. **A precise radius latch fights the signal.** The overhead entry radius
   (0.12) sat inside the centroid jitter band: the dry run showed a raw
   glitch ejecting a genuine hold (radius 0.11 → 0.49 in one tick), and after
   EMA smoothing was added, the live run showed the inverse — a genuine
   overhead pass (raw radius 0.048) filtered out by the smoothing lag
   (smoothed 0.19). Thresholds of that precision are the wrong quantization
   for this signal.
4. **Whole-map centroid allows identity capture.** With one surviving blob,
   the steering reference hopped between icons (raw radius 0.58 → 0.99 at
   bearing ≈ 0°), dragging navigation toward a receding straggler.
5. **The preprogrammed path leaves the arena.** Independently of the tactic,
   the scripted mission path sometimes carries the aircraft outside the
   battle area. Enemies only render inside the arena, so navigation that
   continuously steers toward detected enemies bounds the excursion — now
   stated as requirement **FR-005** (`docs/requirements/002-functional.sdoc`).
6. **Phase 3 needs a mission-agnostic primitive.** The ADR 024 behavior tree
   will invoke engage behavior from a tree node; the policy must not be
   welded to the J20 mission script.

## Decision

### 1. Three equal-width range rings

The minimap disc is divided into three rings of equal radial width on the
normalised radius: **short** (0–1/3), **mid** (1/3–2/3), **long** (2/3–1).
Equal *width*, not equal *area*, is deliberate: what matters is travel
distance to the contact, and the long ring covering 56% of map area is
irrelevant to that. Ring membership (~0.33-wide bands) is coarse enough to
sit outside the measured jitter band that broke the 0.12 overhead latch.

### 2. Ring-engage policy

Evaluated every battle tick from per-ring red-icon counts:

| Priority | Condition                                        | Behaviour                              |
|----------|--------------------------------------------------|----------------------------------------|
| 1        | short-ring count at or above `short_ring_min_count` | **Orbit**: open-loop periodic roll (fixed direction) |
| 2        | mid ring occupied                                | **Engage mid**: steer toward the mid-ring centroid |
| 3        | long ring occupied                               | **Engage long**: steer toward the long-ring centroid |
| 4        | no enemies detected                              | Idle — no command                      |

Mid beats long because it is reachable soonest; short beats both because the
fight is already here (nearest-first doctrine — `short_ring_min_count` is a
config threshold, not a hardcoded rule, so the Phase 3 tree can tune
aggression). Orbit replaces the overhead hold: it needs no precise station,
tolerates the own-ship marker occluding icons at map centre, and constant
turning is itself a defensive posture (a measurable hypothesis, like the
original AOA claim).

### 3. Per-ring bearing, smoothed within a selection

The steering reference is the area-weighted centroid of the **selected ring
only** — a long-range straggler cannot capture navigation while the mid ring
is occupied (fixes context item 4). The centroid vector is smoothed by the
existing `MinimapEma` (vector-space; bearing angles cannot be averaged
across the ±180° wrap), and the EMA is **reseeded only when the selection
jumps to a genuinely different target** — a bearing change beyond
`ema_reseed_angle_deg`. Averaging across a target switch would blend two
different objects, but a single contact crossing a ring boundary keeps its
smoothing: the first live session (2026-08-08) showed that
reseed-on-every-ring-change turned mid↔long boundary flaps into raw-sample
steering reversals. Mode changes into and out of Orbit are debounced
(`ring_debounce_ticks`) instead of radius-hysteresis-latched.

### 4. No altitude precondition; safety floors retained

Navigation is active at any altitude (`attack_altitude` and the
`overhead_*` latch parameters are retired). Two gates carry forward
unchanged, both exercised in the live session: steering is suppressed when
the telemetry snapshot is missing/stale (5 ticks) and below
`min_safe_altitude` (3 ticks, CFIT floor). Altitude management itself
belongs to the mission profile today and to the behavior tree in Phase 3 —
this policy only commands the roll axis.

### 5. Mission-agnostic intent API — not an FSM state

The policy is a pure object (`EngageNavigator` in `wingman/engage_nav.py`):
`update(components, altitude, now) → Intent`, where an Intent is `steer`
(normalised error), `orbit` (direction), or `none` (reason). The tick
handler translates intents into controller calls; the J20 mission invokes it
today, and the ADR 024 behavior tree's engage node (working name
`GAME_BATTLE_ENGAGE`) invokes the same object later. **`GAME_BATTLE_ENGAGE`
is a tree node, not a `transitions` FSM state**: the FSM states are
screen-derived facts, and mixing agent intent into them would force every
screen-detection path to enumerate intent states.

### 6. Actuation

Steer intents reuse `Controller.orient_nose_to_target` with the coarse gains
(`error_norm = clamp(bearing/90, −1, 1)`); the shared cooldown timestamp
still arbitrates against the Design 005 fine-tracking loop (terminal loop
wins). Orbit intents issue an open-loop `roll_<direction>` hold every
`orbit_roll_interval_s` — the one genuinely new actuation pattern, to be
validated in dry-run before live flight.

### 7. Instrumentation (unchanged, still the acceptance evidence)

Per-tick DEBUG (mode, reason, ring counts, error, altitude), INFO on mode
change, per-mission phase-uptime summary, and the MissionStatsTracker /
incoming-rate A/B against `attack_mode` off. This ADR cannot go `Accepted`
without those live measurements. FR-005 adds an observable: excursions
outside the arena under ring-engage versus the preprogrammed path.

## Consequences

**Positive**

- Engagement is no longer starved: every battle tick with detection is
  actionable (25% eligibility → ~100% of GAME_BATTLE time).
- Ring quantization matches the signal's noise; the two measured
  latch-failure modes (false eject, missed arrival) are structurally gone.
- Ring-restricted centroids end identity capture by stragglers.
- Continuous enemy-directed steering bounds arena excursions (FR-005) —
  enemies only exist inside the arena.
- The policy object slots directly under the Phase 3 tree; sensor, EMA,
  safety gates, dry-run mode, and controller arbitration all carry forward.

**Negative / Trade-offs**

- Orbit-roll flight dynamics are unvalidated (open-loop roll cadence vs the
  game's bank/turn model) — dry-run first.
- A single short-ring straggler overrules a mid-ring furball at the default
  threshold of 1 — consistent with nearest-first, revisitable via config.
- Arena containment is achieved *indirectly*: there is no arena-boundary
  sensor, so FR-005 is verified by outcome (excursion observations), not by
  a boundary check. A map-edge detector is deferred future work.
- Ring semantics inherit the unresolved minimap zoom/world-scale question.

## Alternatives Considered

**Overhead phase machine with altitude gate and radius latch** (revision 2)
— superseded by measurement: 25% engagement, latch failures in both
directions, eject-cycle preemption.

**3×3 quadrant split of `ENEMY_CLOSE_BY`** (revision 1) — superseded before
implementation: range-limited to the map centre, coarse, duplicated the
Design 005 controller.

**Equal-area rings** — rejected: travel time scales with radial distance,
not covered area; equal-area boundaries (0.58/0.82) would make "short"
misleadingly wide.

**`GAME_BATTLE_ENGAGE` as an FSM state** — rejected: the `transitions` FSM
encodes screen-derived facts; intent lives in the mission script today and
the ADR 024 tree tomorrow. The name survives as the tree node.

**Arena-boundary detection from minimap terrain pixels** — deferred: no
reliable boundary rendering is known; enemy-directed steering achieves the
containment outcome without a new sensor. Revisit if FR-005 observations
show excursions despite engagement.

**Health drops as an altitude proxy** — still rejected (damage arrives after
the crash).
