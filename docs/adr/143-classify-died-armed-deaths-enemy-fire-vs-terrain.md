# ADR 143 — Classify "Died Armed" Deaths as Enemy-Fire or Terrain, Instead of Calling Them All a Crash

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-20 | 1.8.9           |

## Context

[ADR 137](137-emergency-climb-airbrake-and-crash-instrument.md) D2 added
`crash_with_missiles`: fired from `RespawnHandler.tick_detect`
(`tick_handlers.py`) whenever a respawn is confirmed, the aircraft was not
mid-eject, did not have the ADR 136 secondary loadout active, and
`Analyzer.get_ammo_missiles()` read `None` or `> 0`. `MissionStatsTracker`
counts it in `_total_crashes_with_missiles` and `print_summary()` prints it
unconditionally as:

```
Crash w/ missiles : 5  (died with missiles unused — mostly enemy fire, not
necessarily terrain; ADR 137 seventh trial)
```

The parenthetical is itself the problem this ADR exists to fix: the label
says "crash," the summary line's own hedge admits most of what it counts
probably isn't one, and there has been no cheaper way to tell which — zero,
some, or all — without a manual log-and-screenshot investigation each time,
exactly like the one below. That defeats the instrument's own stated purpose
(ADR 137 D2: give the operator a number to watch "toward zero" for genuine
crashes) when the number conflates two failure modes with different causes,
different fixes, and different owners (missile/flare-evasion tuning vs.
dive-recovery tuning, ADR 137 D1/D4/D9).

**Investigation, 2026-09-20, `wingman.log` 13:46:31–14:14:18 (5 missions, 16
respawns, 5 `crash_with_missiles`).** Every one of the 5 was checked by hand
against the surrounding log and its ADR 137 D5 pre-crash screenshot
(`test_screenshots/crash_with_missiles/crash_20260920_*.png`):

| # | Time | Missiles | Last `INCOMING MISSILE DETECTED` | Gap to death | Last known altitude / rate | Screenshot shows |
|---|---|---|---|---|---|---|
| 1 | 13:48:10 | 2 | 13:48:05.250 | 5.7s | 6705m, +181 m/s (1.5s before) | mid-air fireball, open sky, no ground |
| 2 | 13:53:22 | 2 | 13:53:15.826 | 7.0s | 3898m, +415 m/s (3.0s before) | kill-feed banner (`DESTROYED`), night sky |
| 3 | 13:53:49 | 3 | 13:53:43.699 | 5.7s | 4423m, +442 m/s (1.5s before) | aircraft breaking apart, starfield, no ground |
| 4 | 13:56:05 | 2 | 13:55:59.950 | 5.7s | 3948m, +386 m/s (3.0s before) | full-frame fireball, dark sky |
| 5 | 13:59:36 | 2 | 13:59:30.539 | 5.8s | 3387m, +267 m/s (3.0s before) | overexposed blast, radar shows open water nearby |

All 5 were preceded by a confirmed enemy missile launch (flares deployed)
5.7–7.0s before the respawn was detected. All 5 had a last-known altitude of
3,387–6,705m while **climbing** at +181 to +442 m/s — not descending, not
low. All 5 screenshots show an airborne explosion against open sky, not
ground filling the frame. None correlate with the `TERRAIN AHEAD` shadow
detector (HLDD 001 phase 1, `[SHADOW - not actuating]`) or with an ADR 086/137
`DIVE RECOVERY` / `climb.emergency_active` event anywhere nearby — that
detector fires constantly regardless of outcome this build, so its absence or
presence near a given death isn't informative on its own, but its complete
absence near an emergency-climb trigger specifically (which *would* be
informative — see Decision below) is notable. By every signal available,
this session's `crash_with_missiles: 5` was 5-for-5 enemy fire and 0-for-5
terrain — yet the summary printed the word "crash" five times.

Two signals already exist in the codebase that make this distinction
possible automatically, cheaply, at the same tick the metric already fires,
without adding new sensing:

1. **Recent enemy-fire evidence.** `AmmoEventsHandler` (`tick_handlers.py`)
   already tracks `_last_incoming_alert_ts`, updated every time
   `deploy_flares_on_new_incoming()` sees a new `Analyzer` incoming-template
   detection. This is exactly the signal that produced the 5.7–7.0s gaps
   above — it just isn't exposed or consulted anywhere near the
   `crash_with_missiles` emit site today.
2. **Recent terrain-emergency evidence.** The `Climb` condition object built
   by `make_climb_condition` (`behavior_tree.py`) already exposes
   `emergency_active` / `hard_emergency_active` — the exact ADR 086/137 D4
   verdict for "an uncontrolled, fast-approaching-ground dive is in
   progress," already proven live across ADR 137's six trials (e.g. the
   2026-09-10 07:24:42 case: emergency fired on schedule, descent rate
   worsened anyway, aircraft hit the ground with 1 missile aboard — the
   archetype case this classifier should tag as terrain). Today this is a
   live boolean with no timestamp, so nothing can ask "was this true
   recently" after the fact the way `_last_incoming_alert_ts` allows for
   incoming alerts.

Explicitly not proposed as a signal: the ADR 137 D8 pre-crash buffer's own
`alt`/`rate` values. This session is itself a counter-example to trusting
that field for classification — all 5 events logged `alt=None rate=None`
from the buffer's closest-to-5.0s-old sample (D8 rev 4's fixed-lookback
selection, "closest to target age," not "closest valid reading") even though
a real altitude/rate was demonstrably available from the BT tactic's own
telemetry snapshots 1.5–3.0s earlier in every single case. The buffer is a
single-tick OCR sample and inherits OCR's ordinary miss rate; the two signals
above are not — `_last_incoming_alert_ts` is set from a confirmed detection
event, and `emergency_active` is a computed verdict already validated live
across ADR 137, not a per-tick OCR read.

## Decision

**D1. `AmmoEventsHandler` gains a public `last_incoming_alert_ts` property**,
mirroring the existing `battle_started_ts` property — no behavior change,
just exposing the field `deploy_flares_on_new_incoming()` already maintains.

**D2. The `Climb` condition (`behavior_tree.py`,
`ConditionTactic`/`make_climb_condition`'s backing object) gains a
`last_emergency_active_ts` timestamp**, set every time `update()` computes
`emergency_active = True` (mirrors the existing `_emergency_active` /
`_hard_emergency_active` assignment sites at lines ~755/792, adding one
`self._last_emergency_active_ts = now` alongside each). Exposed as a
read-only property the same way `emergency_active` already is. This is the
one new piece of state this ADR adds — everything else is wiring together
signals that already exist.

**D3. `RespawnHandler.tick_detect` classifies each `crash_with_missiles`
occurrence at the same site D2 (ADR 137) already computes it**, using both
timestamps captured above (via the existing `self._behavior_tree` and
`self._ammo_events` references `RespawnHandler` already holds):

```python
now = self._clock()
enemy_fire_recent = (now - self._ammo_events.last_incoming_alert_ts) <= self._enemy_fire_lookback_s
terrain_recent = (now - climb.last_emergency_active_ts) <= self._terrain_lookback_s

if terrain_recent:
    cause = "terrain"
elif enemy_fire_recent:
    cause = "enemy_fire"
else:
    cause = "unclassified"
```

`terrain_recent` is checked first: an active dive-recovery emergency is
direct, mechanism-level evidence a crash was already in progress, which is
stronger than inferring enemy fire from the mere absence of a recent missile
alert. The two are not mutually exclusive in principle (a missile alert
during an emergency dive is plausible), and this ADR does not have a live
example of that overlap to design against — `terrain` wins ties as the more
specific, higher-confidence signal, matching ADR 137 D2's own precedent of
excluding the more-specific eject/secondary-weapon cases before falling
through to the general classification.

`enemy_fire_lookback_s` defaults to **10.0** — comfortably above this
session's observed 5.7–7.0s range (the same "comfortably above the measured
range" reasoning ADR 137 D4/D8 already used for their own thresholds), while
still tight enough that an unrelated missile alert from early in a long life
won't falsely tag an unrelated later death. `terrain_lookback_s` defaults to
**10.0** as well, pending live data (Non-Goal 3) — no terrain example
occurred this session to tune it against.

Config: new `mission.crash_capture.enemy_fire_lookback_s: 10.0` and
`mission.crash_capture.terrain_lookback_s: 10.0` keys, alongside the
existing `pre_crash_buffer_s`/`pre_crash_freshness_s`/`pre_crash_lookback_s`
in the same block (`config.yaml`, `config_schema.py`).

**D4. Three new event names, emitted alongside the existing
`crash_with_missiles`** (kept unchanged as the umbrella "died armed" event —
no churn to its existing consumers): `died_armed_enemy_fire`,
`died_armed_terrain`, `died_armed_unclassified`, one of which fires every
time `crash_with_missiles` does, through the same `_emit_capture_event`
funnel.

**D5. `MissionStatsTracker` adds three new counters**
(`_total_died_armed_enemy_fire`, `_total_died_armed_terrain`,
`_total_died_armed_unclassified`) handled in `_on_event_locked`
(`mission_stats.py`) the same way `_total_crashes_with_missiles` already is,
folded into `finalize()`'s dict.

**D6. `print_summary()`'s "Crash w/ missiles" line is replaced with a
breakdown**, dropping the misleading word "crash" from the umbrella line
entirely — the word is now reserved for the sub-count that has actual
supporting evidence:

```
Died armed        : 5   (missiles unused at death; ADR 143)
  enemy fire      : 5
  terrain crash   : 0
  unclassified    : 0
```

The `RTB w/ missiles` line (a different, already-unambiguous metric —
confirmed map-boundary crossings) is untouched.

**D7. The per-occurrence `💥 CRASH WITH MISSILES` WARNING line
(`tick_handlers.py`, ADR 137 D4/D8) gains the classification**, so a future
by-hand log read is immediate rather than requiring this ADR's own
investigation to be repeated:

```
💥 DIED ARMED — 2 missile(s), cause=enemy_fire (incoming 5.7s ago),
alt=None rate=None (pre-crash frame, 4.5s old)
```

## Non-Goals

1. **Not a change to what counts as "died armed" in the first place.** The
   existing `was_ejecting` / `had_secondary_weapon_active` exclusions (ADR
   137 D2/D6) are untouched — this ADR only classifies occurrences that
   already pass those filters.
2. **Not a fix to the ADR 137 D8 pre-crash buffer's per-tick OCR
   reliability.** This session's 0/5 buffer hit rate for alt/rate is a
   pre-existing, separately-documented gap (D8 rev 4's own "closest to
   target age, not closest valid reading" tradeoff) — worth a future look,
   but this ADR deliberately routes around it rather than depending on it,
   per Context above.
3. **Not tuned against a real terrain example yet.** No `terrain_recent`
   case occurred in the session that motivated this ADR — `terrain_lookback_s`
   is a starting default (Decision D3), not a measured one, and the first
   live trial (Validation below) is what will confirm or correct it.
4. **Not a retroactive reclassification of past sessions.** The three new
   counters start from the session this ships in, same posture as ADR 137
   Non-Goal 4.
5. **Not a claim that "unclassified" will be rare.** A death with neither
   signal recent (e.g., a mid-air collision, an unusual kill mechanism, or a
   genuine sensing gap) should land there rather than being forced into one
   of the two buckets — matching the project's existing fail-open posture for
   this instrument (ADR 137 D2's own `None`-ammo handling).

## Validation

- **Unit (planned).** `tests/test_mission_stats.py`: the three new counters
  increment correctly per emitted event name and appear in `finalize()`/
  `print_summary()`'s new breakdown, including the zero-of-each case (summary
  always shows all three, matching `total_respawns`'s always-shown
  precedent). `tests/test_behavior_tree.py`: `last_emergency_active_ts`
  updates only on a `True` transition of `emergency_active`, not every tick.
  `tests/test_tick_handlers.py`: `RespawnHandler` picks `terrain` over
  `enemy_fire` when both lookbacks are within window; picks `enemy_fire`
  when only that one is; falls back to `unclassified` when neither is;
  `AmmoEventsHandler.last_incoming_alert_ts` is readable and matches the
  private field.
- **V1 — live, open.** Next session: spot-check every `died_armed_*`
  occurrence against a manual log/screenshot review (the same method this
  ADR's own investigation used) to confirm the classifier's verdict matches.
  Precision on `enemy_fire` should be high given the tight, consistent
  5.7–7.0s gap measured here; precision on `terrain` is the open question
  (Non-Goal 3) since it has no live example yet — the first session with a
  genuine `DIVE RECOVERY`-preceded death is the real test.
- **V2 — live, open.** Watch the `unclassified` rate. A high rate would mean
  the two lookback windows are too tight, or that a third, undiscovered
  cause (e.g., collision, out-of-bounds kill) is common enough to deserve its
  own signal — not assumed here, to be read from data.
