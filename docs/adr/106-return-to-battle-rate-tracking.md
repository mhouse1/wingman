# ADR 106 — RETURN TO BATTLE Rate Tracking

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-03 | 1.8.8           |

## Context

Design 010 instrumented the map boundary; ADR 101 added the first thing that
acts on it. Both changes are judged by one outcome: how often the aircraft
leaves the arena and the game shows its RETURN TO BATTLE banner.

That number has been measured ad hoc, per session, in conversation. The
measurements are not comparable unless the counting rule is fixed, and they are
not durable — `wingman.log` opens with `mode="w"` and is destroyed by the next
run, so a session's figures exist only if someone wrote them down. Two of the
three sessions below can no longer be re-derived from a log.

This ADR fixes the counting rule, records the series, and states what would
count as done.

## Decision

**D1. The tracked metric is confirmed crossings per mission.**

Per mission, not per hour or per session: sessions run from 3 to 10 hours and
missions vary from 4 to 30 minutes, so raw counts are not comparable. Missions
are the unit of exposure — each one is an opportunity to fly out of the arena.

**Confirmed** means OCR read the banner. The colour trigger alone is 94% false
positives (125 of 133 in the 2026-09-02 session), so counting triggers would
measure the detector's noise rather than the aircraft's behaviour. ADR 103 D8
already moved the count behind the OCR arbiter for this reason.

**D2. The figures come from a fixed set of commands**, so entries added months
apart stay comparable:

```bash
grep -c 'RETURN TO BATTLE (confirmed crossing' wingman.log   # crossings
grep -c 'colour trigger not confirmed'          wingman.log   # unconfirmed triggers
grep -c 'MAP BOUNDARY: turn requested'          wingman.log   # ADR 101 turns
grep -c 'turn budget spent'                     wingman.log   # turns that gave up
grep -A6 'Wingman Session Summary'              wingman.log   # missions started
```

**D3. Record the code state with every row.** The point of the series is to
attribute movement to a change. A row without the ADR revision that was live is
a number nobody can act on.

**D4. Copy the row out of the log before the next run.** `wingman.log` is
truncated on every start. A session not recorded here is a session lost.

**D6. Capture the frame on every confirmed crossing.** The counts say how often;
they say nothing about where. The suspicion worth testing first is that
crossings cluster on particular maps — a map whose arena edge sits close to the
action, or whose terrain reads differently to the boundary detector, would
produce a rate that has nothing to do with wingman's code.

The whole frame is saved, not the minimap crop: a map is identifiable from its
terrain and scoreboard, not from a 320 px disc. Frames land in
`test_screenshots/unknown_anomalies/` as `rtb_<timestamp>_crossing<n>.png`,
alongside the ADR 074 anomaly captures, and the folder is gitignored.

**Confirmed only, and capped.** Capturing on the colour trigger would bury the
eight frames that matter under a hundred that do not, at 94% false positives.
`boundary_capture_max` (20) bounds a bad night: the folder is gitignored, but the
frames are ~2 MB each and the disk is not. The cap suppresses the capture, never
the count — the count is the metric.

Losing a frame must never lose a crossing. The writer runs on an OCR pool
thread, where an exception is swallowed, so it never raises.

## The series

Crossings per mission is the column that matters; the rest are there to explain
its movement.

| Date | Dur | Missions | Crossings | **per mission** | Turns | Budget spent | Unconf. triggers | Code state | Game UI |
|------|----:|---------:|----------:|----------------:|------:|-------------:|-----------------:|------------|---------|
| 2026-09-01 (day) | 4h30m | 40 | 4 | **0.10** | 36 | 0 | 91 | ADR 101 rev 1 | pre-update |
| 2026-09-01 (night) | 9h42m | 95 | 32 | **0.34** | 201 | 19 | 262 | ADR 101 rev 1 | pre-update |
| 2026-09-02 | 7h21m | 77 | 8 | **0.10** | 58 | 11 | 125 | ADR 101 rev 2 | **post-update** |

*Provenance: the two 2026-09-01 rows were measured live at the time. Those logs
have since been overwritten and the figures cannot be re-derived — which is what
D4 exists to prevent.*

**D5. Record the game UI version too.** MetalStorm shipped a minimap change
shortly before the 2026-09-02 session — `test_screenshots/AMMO_MISSILE.png` and
`AMMO_MISSILE_1.png`, captured at 19:14 and 19:21, thirteen minutes before that
session started at 19:27. The game is an input to this measurement and it moves
without warning, so a row that names only wingman's code state is not
attributable.

### Reading the series so far

**Three points are not a trend, and the third one is confounded.**

The rev 2 session came in at 0.10, a 69% drop from the night session that
exposed the rev 1 defect. Two separate reasons not to bank that:

**The pre-fix day session was also 0.10.** Under identical rev 1 code the rate
varied 0.10 to 0.34, so between-session variance is at least as large as the
effect being claimed. Map, opponent mix and session length all differ.

**The game UI changed in the same gap.** ADR 101 rev 2 and MetalStorm's minimap
update both landed between the night session and 2026-09-02, so the two cannot
be separated by these rows. The minimap is the detector's only input, which
makes this a change to the measurement instrument at the same moment as a change
to the thing being measured.

What *is* direct evidence for rev 2 is the 64% fall in turn **requests** (2.12 to
0.75 per mission). That is the chattering hold being fixed, and it is measured on
wingman's own behaviour rather than on an outcome the game shares. The crossing
rate is not yet demonstrated either way.

### What changed in the minimap

From the two screenshots, against the archived `MINIMAP.png`:

| | before | after |
|---|---|---|
| terrain | dark monochrome grey | coloured — tan land, blue water |
| field-of-view wedge | pale grey | green |
| rim | plain | coloured arcs |
| rotates with heading | yes | yes (unchanged) |

Enemy-contact detection survives: the archived frame still reproduces its
hand-verified 2/0/3 ring occupancy, and both new frames return plausible counts
(5/0/0 and 0/1/1). Ring binning keys on the red icons, which did not change.

**The boundary detector is the exposure.** It keys on hue 8-28 with saturation
and value at or above 120, and its docstring justifies that as map-independent
because "the boundary is a HUD overlay drawn at a constant colour - hue 16.9-18.5
while the map background ranged V 66.6-118.4". That premise was written against a
dark grey background. The new background contains tan terrain in the same hue
family.

On these two frames the saturation and value thresholds still exclude it — the
mask covers 0.3% of the crop, largest component about 100 px — so the detector is
not obviously broken. But neither frame is near an arena edge, so `detect_map_boundary`
returns None for both AND for the old frame, and nothing here exercises the case
that matters. The margin that made the mask map-independent is smaller than it
was, and untested.

### What the residual looks like

Six of the eight 2026-09-02 crossings were under Climb — the case ADR 101
targets. In five of six budget exhaustions the distance at give-up equalled the
closest approach of that turn: after a full 8 s of rolling, the aircraft was no
nearer to escaping than when it started.

Two readings fit that equally, and the current logging cannot separate them:

- the roll is not reaching the aircraft, or rolling during a climb does not turn
  the flight path enough;
- the roll IS turning it, but `dist` is the range to the nearest point on a
  boundary *line*, so heading changes first and distance only follows once the
  velocity vector points away — and 8 s expires in between.

Raising `boundary_turn_max_s` is the obvious next move and would be a guess
until that is settled. The discriminator is cheap: the trace buffer dumps only
on a **confirmed crossing**, so the ~50 turns per session that succeed are
invisible. Logging `dist` and `fwd` at turn release would make them visible.

## Consequences

Every session that runs the boundary instrumentation owes a row here. That is a
small manual cost, and it is the price of the log being destroyed on each run.

The series will read poorly for a while. Three points cannot separate a 3x
change from ordinary variance, and pretending otherwise is how a tuning change
gets adopted on noise.

### The map question

Untested. The hypothesis is that the residual crossings concentrate on a subset
of maps, in which case the next move is per-map tuning or a per-map turn
threshold rather than more work on the turn itself. D6 exists to answer it, and
the answer needs a handful of sessions' worth of frames before it means
anything — eight per session is not a sample.

Two things to look for once frames accumulate: whether the same terrain recurs,
and whether the crossings on a given map share an approach geometry (the trace
is in the log beside each capture).

## Target

**Under 0.05 crossings per mission, sustained across at least five sessions on
the same code.**

Five because the observed spread under fixed code was already 3.4x; fewer rows
cannot distinguish a real improvement from a quiet night. 0.05 is half the
current best-case rate, chosen as a step rather than an aspiration — zero is not
the target, because an aircraft chasing a contact to the edge is sometimes right
to.

This ADR stays Draft until the series supports a conclusion either way.

## References

- [Design 010](../hldd/010-mini-map-detection/010-mini-map-detection-hldd.md) —
  HLDD 010, the boundary instrumentation these figures come from
- ADR 101 — the boundary-aware climb; rev 1 and rev 2 are the code states in the
  table
- ADR 103 D8 — why the count is gated on OCR confirmation rather than the colour
  trigger
- ADR 028 — Regroup, which steers inward but sits below Climb in the selector
- `test_screenshots/AMMO_MISSILE.png`, `AMMO_MISSILE_1.png` — the post-update
  minimap, captured 2026-09-02 19:14 and 19:21
- `test_screenshots/MINIMAP.png` — the pre-update minimap the crops and the
  boundary hue range were calibrated against
