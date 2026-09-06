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
grep -c 'map boundary ahead, rolling away'     wingman.log   # turns that ACTUATED
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

> **2026-09-04: the metric is input-limited.** In `GAME_BATTLE` the detector
> produced a reading on 385 of 682 ticks — **56%**. (An earlier note here said
> 23%; that averaged in lobby and loading ticks, where there is no minimap to
> read and None is correct. See ADR 117.) Every row below measures a tactic that is
> blind on nearly half its battle ticks, so differences between rows are influenced by
> when the detector happened to see something. ADR 117 captures the failure
> case; until those frames are read, no row here should be attributed to a
> tactic or detector change.

Crossings per mission is the column that matters; the rest are there to explain
its movement.

| Date | Dur | Missions | Crossings | **per mission** | Turns | Actuated | Budget spent | Unconf. | Code state | Game UI |
|------|----:|---------:|----------:|----------------:|------:|---------:|-------------:|--------:|------------|---------|
| 2026-09-01 (day) | 4h30m | 40 | 4 | **0.10** | 36 | 18 | 0 | 91 | ADR 101 rev 1 | pre-update |
| 2026-09-01 (night) | 9h42m | 95 | 32 | **0.34** | 201 | — | 19 | 262 | ADR 101 rev 1 | pre-update |
| 2026-09-02 | 7h21m | 77 | 8 | **0.10** | 58 | — | 11 | 125 | ADR 101 rev 2 | **post-update** |
| 2026-09-03 | 43m | 7 | 1 | **0.14** | 14 | **4** | 3 | 59 | ADR 101 rev 2 | post-update |
| 2026-09-03 (pm) | 7h10m | 74 | 8 | **0.11** | 65 | **65** | — | 232 | **ADR 107** | post-update |
| 2026-09-03 (eve) | 1h29m | 15 | 0 | **0.00** | 38 | 38 | — | — | ADR 107 + **108** | post-update |
| 2026-09-03 (night) | 4h22m | 46 | 12 | **0.26** | 150 | 150 | — | — | ADR 107 + 108 | post-update |
| 2026-09-04 | 4h26m | 44 | 6 | **0.14** | 129 | 129 | — | 92 | ADR 107 + 108 rev 2 | post-update |
| 2026-09-04 (eve) | 56m | 9 | 2 | **0.22** | 19 | 19 | — | — | ADR 111 + **112** | post-update |
| 2026-09-04 (late) | 49m | 10 | 1 | **0.10** | — | — | — | — | ADR **113** + 114-116 | post-update |
| 2026-09-04 (night) | 2h21m | 21 | 2 | **0.10** | 83 | 83 | — | — | ADR 113-117 | post-update |
| 2026-09-05 (am) | 1h55m | 18 | 3 | **0.17** | 32 | 32 | — | — | ADR 118-121 | post-update |
| 2026-09-05 (mid) | 24m | 3 | 2 | **0.67** | 17 | 17 | — | — | ADR **122** + 123 | post-update |
| 2026-09-05 (soak) | **6h16m** | **64** | 6 | **0.094** | 169 | 169 | — | — | ADR 122-125 | post-update |
| 2026-09-05 (pm) | 2h28m | 25 | 4 | **0.160** | 167 | 167 | — | — | ADR 126 (5s cap, **reverted**) | post-update |

**Actuated** counts turns that reached the aircraft — `grep -c 'map boundary
ahead, rolling away'`. Added 2026-09-03, when the gap became visible: 14 requests
produced 4 rolls, because ADR 101 D7 scoped the turn to a *running climb* and no
climb was running for the other ten. That is the column ADR 107 exists to close,
and it is measured on wingman's own behaviour rather than on an outcome the game
shares.

*Provenance: the two 2026-09-01 rows were measured live at the time. Those logs
have since been overwritten and the figures cannot be re-derived — which is what
D4 exists to prevent. Their **Actuated** cells are blank for the same reason: the
count was not taken while the log existed.*

**The 2026-09-03 row is 7 missions and one crossing.** At that size the rate is
one event divided by a small number, and it belongs in the table as the last
pre-ADR-107 reading rather than as evidence of anything. It is listed because a
row not written before the next run is a row lost, not because 0.14 means
something.

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

### ADR 107, first full session

The two claims came apart exactly as the section below anticipated.

**The mechanical claim held.** 65 turns engaged, 65 actuated — 100%, against 29%
and 50% on the rows above. The 10-of-14 delivery gap ADR 107 was written to close
is closed.

**The outcome claim did not.** 0.11 crossings per mission against a 0.10
baseline: no movement at all.

So the tactic now fires every time and does not work. Two measurements say why
it is the geometry and not the timing:

- **46% of turns still ran to the 12 s cap** — the aircraft does not recede.
- Where a turn was running, range went **0.45R to 0.06R** and **0.27R to 0.03R**.
  Bank *and* pull, and it closed anyway. ADR 107 D2 argued adding pitch was what
  would make the turn effective; on this evidence it is not.

A second and separate gap: only **3 of 8** crossings had a BoundaryTurn anywhere
in the preceding 20 ticks. Five had no turn at all in the last ~30 seconds —
crossed too fast to catch, no reading, or suppressed by the D4 emergency yield.
That is a different question from why a running turn fails, and worth keeping
apart from it.

### ADR 108, and why the 0.00 row means nothing

The 1h29m row read **zero crossings in 15 missions** and was tempting. The 4h22m
row that followed, on identical code, read **0.26** — inside the pre-existing
0.10-0.34 band.

At a 0.10 baseline, 15 missions expects about 1.5 crossings, so zero is well
within chance. This is the third time the table has shown a small sample
flattering a change, and it is why the target below asks for five sessions
rather than one good one.

What the larger session did establish is where the remaining failure is. Nine of
its twelve crossings happened with **BoundaryTurn actively running**, and all
twelve had a turn in the preceding 20 ticks — so the tactic is present and
firing. The release rule was the defect: over 105 turns the median release was
0.34R, 40% released inside 0.30R and 27% inside 0.20R, one of them at 0.06R.
Recession alone was letting go of aircraft still on the edge. ADR 107 D5 now
carries a hysteresis band.

Detection itself is healthy after ADR 108 — 31% readable with a median reading
of 0.42R, against 19% and a median of 0.07R when the detector was reading desert
terrain.

### 2026-09-04 — the tactic is break-even, on 61 measured turns

0.14 crossings per mission, inside the 0.10-0.34 band the metric has occupied
since before ADR 107 existed. Six sessions of work on this tactic have not moved
it out of that band.

With the sampler corrected, the turn could finally be measured. Pairing the
controller's attitude summary with the handler's range line — adjacent in the
log, no new instrumentation needed — over 61 turns:

| | |
|---|---:|
| median swing | 27 deg |
| median range gained | **+0.00R** |
| turns gaining 0.10R or more | 13 |
| turns losing 0.10R or more | 15 |

Near-symmetric. The aircraft rotates hard and the range does not respond. See
ADR 107's negative-result section, including two readings retracted there.

**Crossings clustered rather than arriving steadily:**

```
02:13  02:30  02:52  02:58  ·········(2h13m)·········  05:11  05:36
```

Four in 45 minutes, then two hours clean, then two in 25. That shape argues the
rate is driven by circumstance — map, opponents, where the fighting happens —
more than by per-tick tactics, and it is the strongest support yet for the map
question below.

**The evidence needed to test it was not captured.** All six crossings were
suppressed by the shared capture cap, which had already filled with approach
frames: 18 approach frames saved, 0 crossing frames, 125 suppressed. ADR 108 D4
named this risk and then implemented a single FIFO counter, which gives the
frequent event priority — the opposite of what it argued for. Fixed by giving
crossings a reserved budget.

### After ADR 107

The rows above are the pre-107 baseline. ADR 107 replaces the roll-during-climb
with a first-class tactic that owns pitch and roll, so two columns should move
together if it works:

- **Actuated** should approach **Turns**. A tactic is selected on its own
  condition, so the ten requests that reached no aircraft on 2026-09-03 have no
  equivalent — that is the mechanical claim, and it is the one to check first.
- **Crossings per mission** should fall. That is the outcome claim, and it needs
  the five sessions the target below asks for, because the observed spread under
  fixed code was already 3.4x.

If Actuated tracks Turns and the crossing rate does not move, the tactic is
firing and not working — which would point at the turn geometry rather than at
when it fires, and is worth knowing separately.

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
