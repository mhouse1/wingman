# ADR 028 — Minimap Ring-Engage Navigation

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-08-08 | 1.7.1           |

> **Revision note:** fourth in-place revision of this Draft (permitted: never
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

> **On minimap rotation (revision 4).** The minimap is *heading-up*: the
> own-ship icon is fixed at the centre pointing up and the compass letters
> rotate around the rim. `_scan_minimap_components` already measures
> `bearing_deg` from the up-axis, so what it returns is the bearing relative to
> the nose. Rotation is not an error source for this policy — it is what makes
> the bearing directly usable. A suspicion that rotation was breaking
> orientation detection prompted revision 4's investigation; the measurement did
> not support it, and the real gap was the row-4 silence above.

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
| 4        | no enemies, friendly or objective icons present  | **Regroup**: steer toward the aggregate friendly centroid |
| 5        | nothing detected at all                          | Idle — no command                      |

Mid beats long because it is reachable soonest; short beats both because the
fight is already here (nearest-first doctrine — `short_ring_min_count` is a
config threshold, not a hardcoded rule, so the Phase 3 tree can tune
aggression). Orbit replaces the overhead hold: it needs no precise station,
tolerates the own-ship marker occluding icons at map centre, and constant
turning is itself a defensive posture (a measurable hypothesis, like the
original AOA claim).

### 2a. Regroup — the silence this policy used to leave (revision 4)

Rows 1–3 only fire while something red is on the minimap. Row 4 previously
issued **no command at all**, and the aircraft simply held its heading. Measured
over an 11,383-tick session:

| Ring state | ticks | share |
|------------|------:|------:|
| `rings=0/0/0` — nothing to steer by | 6,582 | **57%** |
| any ring occupied | 4,801 | 43% |

So the navigator was silent for the majority of battle. That is not a defect in
its logic — it is the scope of rows 1–3 — but it is how the aircraft reaches the
map edge, because nothing else in the behavior tree takes horizontal position as
an input either.

The premise of this ADR is that steering at enemies keeps the aircraft in the
arena because *enemies only render inside it*. The same is true of friendlies
and objectives, and they are visible when enemies are not. Measured on the
Design 010 frames, the two are cleanly complementary:

| Frame | enemy icons | friendly icons | friendly centroid |
|-------|------------:|---------------:|-------------------|
| Step0, flying away from the edge | 0 | 5 | **+4.6°** — dead ahead |
| Step1, about to cross the edge | 0 | 2 | **+179.8°** — dead astern |
| Step2, already outside | 4 | 0 | n/a |

Step1 is the frame that mattered, and the friendly centroid points exactly the
way the aircraft needed to turn. Regroup therefore steers toward the
**aggregate** friendly centroid — all icons, area-weighted, not ring-selected:
the icons are a proxy for where the battle is, not targets to intercept.

Three properties carry over deliberately:

- **Rear-sector commitment applies.** Step1's centroid is at 179.8°, precisely
  the unstable-sign case that commitment exists for. Regroup shares that code
  rather than reimplementing it.
- **Its own EMA.** Sharing the enemy EMA would blend an enemy bearing with a
  friendly one across a mode change and steer at neither.
- **No hue wrap.** Red straddles the hue origin and the enemy scan takes the
  170–180 band; folding that into the friendly scan would count enemies as
  friendlies and steer the aircraft at the thing it is meant to avoid.

Any enemy contact still outranks Regroup — it fills the silence, it does not
compete.

#### It needs a behavior-tree leaf, not just a navigator mode

The first implementation put Regroup inside the navigator alone. That was
unreachable, and the live data said so: **5 selections in a two-hour session.**

The navigator only runs from `_actuate_engage`, which the tick handler calls
when the tree selects **Engage** — and Engage's condition is `has_contacts`.
So a mode whose entire purpose is the no-contact case sat behind a condition
requiring contacts. The 5 firings were the narrow window where the tree saw a
contact but the navigator's own ring binning did not.

Regroup is therefore a leaf in the selector, between Engage and AttackSupport:

| Position | Why |
|----------|-----|
| Below Engage | A real target always outranks regrouping |
| Above AttackSupport | That leaf is `always` and flies the mission script with nothing steering toward the battle — the state the aircraft drifts out of the map in |

Its condition, `has_friendlies`, requires friendly icons **and** no enemies.
The enemy half is redundant given the ordering and is stated anyway, so the
condition stays true to its name if the order is ever changed.

Measured after the leaf: **6 Regroup selections against 2 Engage** in a
six-minute window, against 5 in two hours before. Reachable.

#### The insert that broke Climb

Adding the leaf silently inverted ADR 073's priority. Climb was placed with:

```python
children.insert(len(children) - 2, climb_leaf)   # above Engage
```

The comment was true only while exactly two leaves followed it. A third pushed
Climb *below* Engage, and two ADR 073 tests failed. The insert is now by name,
with a regression test asserting the ordering survives further additions.

A positional index into a list whose length is a design variable is a latent
defect regardless of this change; it happened to be this change that found it.

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
