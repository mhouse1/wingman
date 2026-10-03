# ADR 152 — Escalating Resupply Priority in Pursuit Mode

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-29 | 1.9.0           |

## Decision

**D1. Pursuit mode recognizes the yellow resupply icon as a distinct target.**
Add a detector for the yellow icon shown in
`tests/test-output/MISSILE_EMPTY_RESUPPLY.png` and
`tests/test-output/MISSILE_EMPTY_RESUPPLY2.png`. The source captures are in the
ignored test-output directory; committed copies in `tests/fixtures/` are the
detector tests' inputs. The resupply icon is a distinct target; do not feed it
through the red/orange `IconPoints` detector or assume it shares that
detector's ring geometry. Use its detected position to guide pursuit steering
toward resupply.

**D2. Resupply urgency is cumulative and monotonic.** Keep a confirmed
remaining-ammo count for each rack across weapon switches. During the existing
post-switch grace period, ignore zero readings that may still belong to the
previous rack, but accept stable positive readings as the new rack's baseline:
the previous rack was confirmed empty before switching. Use
`empty_confirm_reads` consecutive readings to confirm an ammo-count change. A
reading is one OCR read: the pursuit loop polls the cached count several times
per OCR cycle, and repeated polls of one read count once.
Each confirmed missile spent adds one unit to the resupply urgency. When the
pursuit confirms a rack empty and switches away from it, what that rack still
held is credited at once, because the switch (about a second of zero polls) is
faster than three OCR reads. A rack
switch, unreadable reading, or transient OCR increase must not reduce urgency.
Do not add a configurable urgency threshold. A visible resupply marker takes
priority once at least two missiles have been confirmed spent; one spent
missile alone does not interrupt normal pursuit. Confirmed zero remains the
maximum-priority state.

**D3. Resupply priority arbitrates pursuit steering.** The sequence is the
operator's of 2026-10-02. Once at least two missiles have been confirmed spent
and weapons remain, a marker inside the tracker's acquisition region
(`tracking.acquisition_region_pct`) competes with a visible target: the pursuit
steers at whichever is nearer the screen centre, and keeps firing the selected
weapon either way. With no target visible the marker takes priority over
opponent-icon steering. If a later scan misses the marker,
continue steering toward its last accepted position for at most half a second
while scanning for it again. Then return to normal pursuit until it is detected.
Small icons rejected by the detector remain ignored. Confirmed rearm ends the
focus, clears the held position, resets urgency, and resumes normal pursuit. At
confirmed zero missiles on every rack the pursuit is in resupply mode: it
searches for resupply instead of targets. The marker always wins, firing and
the gun are off, and nameplates and opponent icons are not steered at. Without
a marker in view it steers by the resupply pin: the small yellow icon the game
draws on the indicator ring, pointing toward the resupply point, read by the
same points law that flies toward an opponent's ring icon. With neither in
view the existing search manoeuvre flies. A confirmed rearm ends the mode.
Existing
hard safety ownership still takes precedence, and urgency is retained while
steering yields. The selected objective is shown by the same magenta HUD
steering vector and ring used for target pursuit, labeled `RESUPPLYING` (or
`RESUPPLYING (lost)` while using the bounded hold). The enemy tracker remains
unchanged; the HUD receives the active steering objective separately.

**D4. Resupply focus is gated and not limited to zero missiles.** When
`pursuit_mode.resupply_priority.actuate` is true, a visible or briefly held
marker can interrupt normal pursuit once two missiles are spent, before zero,
when it is nearer the screen centre than any visible target (D3).
At confirmed zero the pursuit enters resupply mode (D3), firing stops, and the
current ammo-exhaustion handoff to `eject_and_dive` is suppressed. Until actuation is enabled, shadow mode logs
the same proposals and preserves existing control and handoff behavior. Keep
the configured pursuit duration cap and external cancellation behavior
(respawn, manual takeover, shutdown) unchanged. If a positive cap expires
before rearm, retain the existing fall-through to `eject_and_dive`. The shipped
cap is `0` (unbounded), so with no marker or confirmed rearm the actuated
pursuit can continue until respawn or external stop; this remains an explicit
operational risk.

**D5. Confirmed rearm or respawn starts a fresh priority cycle.** While the
resupply proposal is active, a confirmed positive increase in the same rack's
ammo count indicates rearm even if the rack never reached zero. Record the new
baseline, clear the spent count, end resupply focus, and resume ordinary target
pursuit. Ignore increases outside an active resupply focus as OCR noise, with
one exception: at confirmed zero, a confirmed positive count ends the empty
state without a focus, because the game also rearms on a timer
([ADR 088](088-eject-rearm-abort-and-incoming-priority-afterburner.md)) and
firing must come back when it does.
In actuated mode a confirmed rearm is followed at once by a nose-up hold with
the roll released (`pursuit_mode.resupply_priority.rearm_climb_s`), because the
resupply point sits near terrain; steering resumes when it ends.
Respawn always ends the current pursuit and clears all rack counts and urgency;
the next pursuit starts at its initial priority. No state carries across lives.

## Consequences

- The pursuit loop gains yellow-icon detection and a resupply steering
  objective, while opponent icon detection remains unchanged. HUD annotation
  follows whichever objective currently owns steering.
- Confirmed ammunition readings must be attributed to the correct weapon rack
  so a weapon switch neither resets urgency nor falsely declares the inventory
  empty.
- After two confirmed missiles have been spent, a resupply marker in the
  acquisition region takes priority over opponent-icon steering, and over a
  visible target only when it is nearer the screen centre.
- With every rack empty the aircraft does not chase or shoot at targets: it
  turns toward the resupply point by the pin on the indicator ring and flies
  to the marker once it is in view. The icon law's own limits on a nose-down
  push apply; nothing else guards the descent to a low beacon.
- At zero missiles the aircraft may remain in pursuit until it reaches
  resupply, is stopped externally, or reaches its configured duration cap.
  The shipped cap is currently unbounded, so failure to detect a resupply icon
  can leave a zero-ammo pursuit running until an external stop or respawn.
- The named captures are currently ignored under `tests/test-output/`; tests
  remain there as operator references; detector tests use the committed copies
  in `tests/fixtures/` and include negative yellow lookalikes.

## Implementation Status

Implemented 2026-09-29 in `wingman/resupply.py` and
`Controller.pursue_and_engage`. Resupply detection and urgency telemetry are
enabled in shipped config. Shadow-session coverage supports advancing to a
live actuation trial; `pursuit_mode.resupply_priority.actuate` is enabled for
that trial.
Ammo changes are confirmed by consecutive reads, tracked per rack, and stale
post-switch zeroes are ignored during the existing grace period. Since
2026-10-02 the reads must be distinct OCR reads: the analyzer numbers each
stored missile count (`get_ammo_missiles_read_seq`) and `MissileUrgency` does
not advance a confirmation run on a repeated poll of the same read. With
`empty_confirm_reads: 3` an urgency change now takes three OCR cycles (about
three to five seconds) instead of about one second; the weapon-switch
confirmation in the same loop is unchanged and still counts polls. Resupply
steering begins when a marker is visible or recently detected and
either two missiles are confirmed spent or ammo is confirmed zero. Since
2026-10-02 (operator's sequence, D3): the emptied primary rack is credited to
the tally at the switch (`MissileUrgency.rack_emptied`); the marker is looked
for in the tracker's acquisition region; while weapons remain the nearer of
marker and visible target is steered at (`marker_nearer_than_target`,
`target_nearer=` in the `RESUPPLY:` debug line); and at confirmed zero the
pursuit ignores targets and opponent icons and flies the search manoeuvre
until a marker appears (`search=True`). After a confirmed rearm the other
rack counts as loaded, so an empty selected rack is switched away from and
exhaustion needs both empty (V5n). In resupply mode the search steers by the
resupply pin on the indicator ring (`find_resupply_ring_icons`, fed to a
second `IconPoints` through `_icon_rung`; `pin=` in the `RESUPPLY:` debug
line), after the undirected search lost every episode (V5p). The recently
detected position is retained for at most half a second during focus. Since
2026-10-02 the detector finds the icon's disc instead of its ring: one
saturated yellow (hue 24 to 30), a square component 24 to 69 px across on a
1920 px frame that fills 12% to 28% of its box, spread over all four quarters
of it, that is a bare circle outline with the crossed-missiles glyph inside
(V5j). The ring grows as the aircraft closes, so the ring-sized gate it
replaces matched terrain and exhaust more often than the icon (V5f). The
minimap's 20 px copies of the icon and the small hollow direction icon near
the screen centre are below the size floor and are not markers. The shared magenta HUD selected-target annotation
is labeled `RESUPPLYING` while this objective controls the axes. During resupply
focus, a confirmed same-rack ammo increase resets urgency and resumes target
pursuit. At terminal zero, actuated mode stops firing; without a fresh or
briefly held marker the search manoeuvre flies (until 2026-10-02 ordinary
pursuit steering resumed here). Every new pursuit after respawn resets urgency.

## Evidence and Assumptions

- **Measured:** the latest actuated session ran 2026-10-01 06:20 to 19:34
  (13h13m, 136 missions). It logged 201 resupply-focus starts, all at
  `spent>=2`, and four `RESUPPLY: confirmed ammo` resets (`7`, `3`, `2`, and
  `44`). None is evidence of a beacon rearm: three are misreads and one is
  ambiguous (V5e).
- **Measured:** each of those resets fired 0.8 to 1.4 seconds after the count
  first appeared, while one OCR cycle took 1.0 to 1.7 seconds. The three-read
  confirmation was being met by repeated polls of a single OCR read.
- **Measured:** both source captures exist in ignored `tests/test-output/`;
  byte-for-byte copies are committed as `tests/fixtures/` inputs.
- **Inferred:** the resupply target can be used to steer by its detected
  on-screen position. It is a different visual signal from the red/orange
  enemy direction markers.
- **Assumed:** detecting and approaching the yellow marker leads to a rearm
  that can be confirmed by a positive ammo reading. The shadow run below
  supports trying actuation, but only live evidence can establish marker
  precision and confirm rearm.

## Validation

- **V1. Passed.** `tests/test_resupply.py` recognizes both committed captures
  and rejects synthetic solid-yellow and small-glyph lookalikes.
- **V2. Passed.** Unit tests cover confirmed per-rack decreases, monotonic
  urgency, switch preservation, transient increases, terminal-zero
  confirmation, and rearm reset.
- **V3. Passed.** Pursuit tests cover the two-missile marker threshold,
  bounded hold through a brief detector dropout and expiry, confirmed rearm and
  target resumption, HUD selected-target replacement and label, no firing at
  zero, no-marker fallback, and legacy shadow behavior.
- **V4. Passed.** Policy and pursuit tests cover respawn/external stop, rearm
  reset, duration-cap fall-through, and deferred rack switching.
- **V5a. Shadow coverage (2026-09-29).** The 13h54m session in
  `logs/wingman_20260929_222852.log` covered 135 missions and 543 pursuit
  episodes. It recorded 53,657 resupply telemetry lines, including 8,969
  marker-positive scans and 2,678 ticks where resupply would have won priority.
  No resupply errors were logged. The shadow run did not save candidate frames
  or record confirmed rearm resets, so it is enough to advance to an actuated
  trial, not to validate marker precision or close V5.
- **V5b. Partial live evidence (2026-09-29 canary).** One `make r1` session used
  a local 30-second pursuit cap and `actuate: true`; shipped config remained
  unchanged. Across 10 pursuit episodes, two transient marker candidates were
  logged and one priority crossover occurred at `spent=2` (marker positions
  `(211,551)` and `(177,153)`). The pursuit then reached its cap at 30.4s with
  ammo `4->2`; no positive ammo increase or rearm was observed. The marker was
  absent on intervening scans, so sustained resupply steering was not
  demonstrated. No resupply scan or pursuit-loop errors occurred. V5 remains
  open pending a frame-verified marker and a confirmed ammo increase.
- **V5c. Actuated trial (2026-09-30).** The current live log recorded two
  pursuits with resupply actuation enabled. One candidate frame visibly shows
  the yellow marker; a second candidate appears to be an explosion/fire
  false positive. Priority crossed at `spent=1`, but both pursuits ended
  externally with ammo `2->2`; no confirmed rearm was observed. This supports
  that the live path can detect and prioritize a marker, while marker precision
  and rearm remain unverified. V5 remains open.
- **V5d. Actuated session after bounded marker hold (2026-10-01).** The
  04:57-06:05 session ran with the half-second last-seen position hold. It
  recorded 26 focus starts, but no confirmed ammo increase or rearm. The log
  shows a marker dropout with `marker_stale=True`, followed by `marker=-` and
  `marker_stale=False`, consistent with the hold expiring rather than persisting
  indefinitely. Fresh marker coordinates also shifted substantially between
  detections (for example `(1019,810)` to `(948,619)` in about 0.85s); the log
  alone cannot establish whether this is beacon motion or the detector selecting
  a different yellow component. V5 remains open pending frame-verified approach
  and confirmed rearm.
- **V5e. Second actuated session (2026-10-01).** A separate run, 06:20 to
  19:34 in `wingman.log`, logged 201 focus starts, 639 held-marker ticks (never
  more than two in a row, each followed by `marker=-`), eight
  `missiles exhausted` events and no resupply errors. The priority threshold
  and the bounded hold behaved as decided. Its four confirmed-ammo resets do
  not show a rearm at the beacon:
  - `ammo=7` at 07:19:58: a death mark was set three seconds earlier, the
    respawn OCR returned `FEEFPAWL`, and the count read 2 again at 07:20:02.
  - `ammo=3` at 12:20:07: a 2 to 3 change two seconds before
    `RESPAWN DETECTED`.
  - `ammo=2` at 15:42:43: a 1 to 2 change about one second after a sustained
    marker, back to 1 ten seconds later. A single added missile also matches
    the game's timer rearm
    ([ADR 088](088-eject-rearm-abort-and-incoming-priority-afterburner.md)),
    so the log cannot attribute it to the beacon.
  - `ammo=44` at 15:47:23: flares and missiles both read 44 in one OCR cycle,
    the count read 0 again 1.4 seconds later, and `missiles exhausted`
    re-fired at 15:47:25. It reset urgency with `seeking=False marker=-`.

  The session summary shows no `Missiles empty` outcomes. V5 remains open
  pending a frame-verified approach and a confirmed ammo increase, in a
  session run after the distinct-read fix of 2026-10-02.
- **V5f. Session after the distinct-read fix (2026-10-02):
  the focus steered at false markers.** Details and frames are in
  [Anomaly 011](../anomaly/011-resupply-focus-steers-at-terrain-and-exhaust.md).
  Of the 41 candidate frames saved, 38 showed terrain, the aircraft's own
  exhaust, an explosion, flares or the minimap rim at the reported position,
  and 3 showed the icon. Consecutive fresh detections were a median 345 px
  apart, so roll and pitch reversed every 150 to 300 ms. Of 13 focus starts,
  11 ended in a respawn, 1 at match end and 1 in `confirmed ammo=2`, 23 s
  after exhaustion and not attributable to the beacon. All 13 began after
  `missiles exhausted`; the state "two spent, ammo remaining" was logged on 3
  ticks, so the pre-zero priority of D2 and D4 did not run (cause not traced).
  The detector was replaced the same day (Implementation Status). On the same
  saved frames it reports 7 markers in the 41 candidates and 29 in 187 other
  pursuit frames, each on the icon's disc. V5 remains open pending a live
  session on the new detector.
- **V5g. First focus on the disc detector (2026-10-02, one episode).** Focus
  began 7 s after `missiles exhausted`, on a marker at `(980,805)`. The saved frame shows the aircraft's exhaust
  behind an indicator icon there, not the beacon (V5j); the frames of the
  approach itself were not saved. The marker was then reported on 49 of the next 53 ticks (21 s), with x
  between 891 and 1048 on a 1920 px frame (centre 960) and y swinging 220 to
  1010. It was then lost, the missile count read 2 two seconds later, and
  `RESUPPLY: confirmed ammo=2` followed a second after that. This is the first
  sustained approach followed by a confirmed ammo increase. One episode is
  not a rate, and the log alone does not exclude the timer rearm of
  [ADR 088](088-eject-rearm-abort-and-incoming-priority-afterburner.md); the
  cause of the vertical swing is not established. V5 remains open until the
  session's totals are recorded.
- **V5h. Second focus on the disc detector (2026-10-02, same
  session).** Focus began on a marker at `(1234,190)`; both saved
  frames show the icon. Within 1.5 s the marker was at `(951,612)` and it
  stayed within 60 px of the screen centre on 12 of the next 17 ticks, until
  it was lost 8 s after the focus began. The aircraft descended from 1655 m to 373 m over
  those 12 s (nose down to 29 degrees) and was level at 375 m 3 s later,
  when the missile count read 2. Health became unreadable about a second
  later and a respawn followed 4 s after the count read 2, before three distinct reads could
  log `confirmed ammo`. The rearm is measured; why the aircraft died at 375 m
  (terrain or enemy fire) is not established. The beacon sits low, and
  `pursuit_mode.dive_safety` is off, so an approach is a descent to terrain
  height with no climb guard.
- **V5i. Third focus (2026-10-02, same session).** Marker within 60 px of the
  screen centre for most of 14 s, descent from 1035 m to 313 m, missile count
  2 at 15 s after the focus began, `confirmed ammo=2` at 17 s, and the
  aircraft climbing at 20 s. Running total for the session at that point: 3
  focus starts, 3 rearms on reaching the marker (2
  confirmed, 1 followed by a death before confirmation).
- **V5j. The disc rule alone still accepted icon-colored lookalikes
  (2026-10-02, same session).** Five focus starts: three approaches
  that ended in a rearm (V5g to V5i), one on the crown indicator icon
  that pitched the aircraft down from 2270 m before it was shot down,
  and one tick on the aircraft's own explosion-lit airframe. Of 19
  saved candidates, 12 showed the beacon and 7 did not
  ([Anomaly 011](../anomaly/011-resupply-focus-steers-at-terrain-and-exhaust.md)).
  The detector now also requires a bare circle outline with the
  crossed-missiles glyph inside it. On the 260 frames saved by both sessions
  it reports 34 markers and drops the 10 false ones, at the cost of 5 real
  beacons whose outline had merged with the ring. Session total at its end
  (36m 39s, 6 missions): 7 focus starts, 4 rearms at the beacon (3
  logged `confirmed ammo=2`), 3 focus episodes on something else, all ending
  in a respawn.
- **V5k. First 16 minutes on the outline and glyph checks (2026-10-02).** Six focus starts, all on the beacon: the 10
  saved candidates all show it. Five reached it and the missile count went
  from 0 to 2 (three logged `confirmed ammo=2`; a respawn followed the other two before
  confirmation); one ended in a respawn before the beacon. No
  focus on a false marker, against 3 of 7 in the session before it. Across both
  sessions 3 of 9 rearms were followed by a death within seconds; the cause
  is not classified in the log. Six episodes is a small sample; V5 stays open
  until a longer session is tallied.
- **V5l. That session's totals (2026-10-02).** 45m 05s, 8 missions, 43 pursuits, 16
  `missiles exhausted`, 11 focus starts. Nine reached the beacon and the
  missile count went from 0 to 2 (6 logged `confirmed ammo=2`; a respawn
  followed the other 3 before confirmation); 2 ended in a respawn before the
  beacon. All 29 saved candidates show the resupply icon; none shows a
  lookalike. Every focus still began after `missiles exhausted`, and 5 of the
  16 exhaustions never got a focus: the tally and the missing search were the
  remaining gaps, and the operator's sequence (D3) was implemented on them the
  same day.
- **V5m. First rearm before running dry (2026-10-02, first session on the
  operator's sequence).** Primary read 0, the switch followed 2 s later and
  `spent=2 empty=False` on the next tick. The marker was in view with
  `target_nearer=True` for the next 5 s while the secondary fired once (count
  2 to 1). Focus then began with weapons left, the marker sat within 40 px of
  the screen centre from 3 s to 5 s into it during a descent from 1532 m to
  905 m, the count read 2 at 7 s and `confirmed ammo=2` followed at 10 s with
  the aircraft level at 931 m. In the pursuit before it the steering changed between marker and target
  four times in five seconds and the aircraft died with the secondary loaded.
  Two episodes; rates wait for the session's totals.
- **V5n. "Exhausted" was declared with a full rack aboard (2026-10-02,
  same session).** The weapon panel in the frames saved around the V5m rearm
  reads 1/2 and 0/2 before it, 2/2 and 2/2 five seconds later, and 0/2
  selected with 2/2 on the other rack 12 s after that: a resupply refills
  both racks. The pursuit never switched back, so `missiles exhausted` was
  logged 2 s later with two missiles unused, and resupply mode then ignored
  targets. After a confirmed rearm the pursuit now treats the other rack as
  loaded: when the selected rack reads empty it switches to it, and
  exhaustion needs both empty. A misread that logs a false rearm costs one
  switch to an empty rack and the post-switch grace before exhaustion.
- **V5o. Totals of the first session on the operator's sequence (2026-10-02).** 17m 42s, 3 missions,
  19 pursuits. "Two spent, weapons left" was logged in 15 of the 19 (3 ticks
  in the whole V5f session). Nine focus starts, 8 before any exhaustion; one
  `confirmed ammo` (V5m); `missiles exhausted` once (V5n, the false one). All
  21 saved candidates show the resupply icon. Against V5l (43 pursuits: 16
  exhaustions, 9 rearms) the aircraft now rarely empties the secondary and
  rarely completes a rearm, but 19 pursuits over 3 missions is too few to
  call either a rate. The both-racks fix (V5n) passed `make lint` and
  `make test` (2,583 passed, 35 skipped) and its session followed.
- **V5p. The undirected resupply-mode search lost every episode (2026-10-02).** 54m 23s, 9 missions,
  53 pursuits, 45 respawns. "Two spent, weapons left" in 33 pursuits; 20
  focus starts, 17 before exhaustion; 9 `missiles exhausted`; one
  `confirmed ammo`, followed 16 s later by `switching to the
  primary, reloaded by the rearm` (the V5n fix, exercised once). Each of the
  first 8 resupply-mode episodes ended in a respawn 7 to 22 s after
  exhaustion; the marker came into view in 3 of them. Against V5l (43
  pursuits, 6 confirmed rearms) the operator's sequence gave 2 confirmed
  rearms in 72 pursuits over two sessions. Resupply mode now steers by the
  resupply pin; `make lint` passed, `make test` passed with 2,590 passed and
  35 skipped, and the session on it followed.
- **V5q. First 30 minutes on the pin-directed search (2026-10-02).** 20 pursuits, 15 focus starts (all with weapons
  left), 3 `missiles exhausted`, 6 `confirmed ammo`. The three resupply-mode
  episodes: the pin was read, the aircraft banked toward it and the marker
  came into view in all three (34, 39 and 14 ticks); one rearmed
  (`confirmed ammo=6` 34 s after exhaustion) and two ran to the
  end of the match 48 s and 32 s after exhaustion. None ended in a respawn
  during the search, against 8 of 8 on the undirected search (V5p). The other
  five rearms came from the pre-exhaustion focus, in matches flown with a
  6-missile secondary rack, which the earlier sessions' 2-missile racks were
  not; the rates are not comparable across that. Five of the six rearms were
  followed by a respawn within 30 s, at 238 to 390 m. No errors logged.
- **V5r. Two and a half hours on the pin-directed search (2026-10-02, the
  same session, still running).** 26 missions, 124 pursuits, 62
  focus starts (54 with weapons left), 13 `missiles exhausted`, 21
  `confirmed ammo`, no errors. The 13 resupply-mode episodes: 6 rearmed, 3 ran
  to the end of the match, 4 ended in a respawn; on the undirected search 8
  of 8 ended in a respawn (V5p). `switching to the primary, reloaded by the
  rearm` fired twice. Of the first 9 rearms, 8 were followed by a respawn: 2
  seconds after a rearm taken in a 56 to 58 degree dive, 2 with health
  already at 4 and 12, and 4 after the aircraft had climbed away. Steering at
  the marker has no nose-down limit; applying the icon law's 45 degree limit
  to it is proposed and not made.
- **V5s. Totals of the session on the pin-directed search (2026-10-02).** 3h 47m, 38 missions, 184
  pursuits, 150 respawns. 87 focus starts (76 with weapons left), 20
  `missiles exhausted`, 28 `confirmed ammo`. The 20 resupply-mode episodes: 8
  rearmed, 3 ran to the end of the match, 9 ended in a respawn. The session
  summary counts 54 deaths with missiles aboard: 24 enemy fire, 21 terrain
  crash, 9 unclassified; the session before it (V5p, undirected search)
  counted 16: 12, 2 and 2. Terrain crashes per mission rose from 0.2 to 0.6
  as the aircraft began flying to low beacons; steering at the marker still
  has no nose-down limit (V5r).
- **V5t. No nose-down limit on marker steering (operator, 2026-10-02).** On
  the limit proposed in V5r: "this is overhead that clutters the code,
  currently crashes are due to rearm being close to terrain and we dont have
  good terrain avoidance." None is added (the icon law's own path-angle limit
  is off too, `icon_min_path_deg: null`). In the V5s session 20 of the 28
  rearms were followed by a respawn within 30 s; the log calls 6 of those
  terrain (each without an altitude reading) and 1 unclassified, and has no
  `DIED ARMED` line for the other 13. Deaths after a low rearm are left to
  terrain avoidance, which this ADR does not provide.
- **V5u. Nose-up on rearm (operator, 2026-10-02).** "crashes can be avoided
  by immediately applying nose up manuver on rearm." On `confirmed ammo` the
  pursuit holds nose-up for `rearm_climb_s` (3.0 s, a named guess) with the
  roll released, logs `RESUPPLY: rearm climb-out`, and steers at nothing else
  until it ends (D5). The trigger is the confirmation, which in the V5s
  session came 3.0 to 3.4 s after the count first read positive; the aircraft
  was descending at 4 of the 8 confirmations that followed exhaustion (last
  logged rate 11 to 222 m/s). Not yet run live: the measure is the share of rearms
  followed by a respawn within 30 s, 20 of 28 before this change. Two tests
  added; `make lint` passed and `make test` passed with 2,597 passed and 35
  skipped.
- **The resupply pin (operator, 2026-10-02).** "The small resupply yellow icon
  near the center of the screen behaves similar to the aircraft indicator
  icon, pointing to the general direction of the resupply icon." Measured on
  the frames of 2026-10-02: a yellow pin (a circle around the crossed
  missiles with a solid pointer, about 18 by 24 px) whose centroid sits 199
  to 208 px from the screen centre, on the ring the red aircraft icons use.
  It was found in 91 of 304 saved frames. The crown objective has the same
  pin with a crown inside (7 seen) and is rejected by its glyph.

Final automated gates on 2026-09-29: `make lint` passed; `make test` passed
with 2,471 passed and 75 skipped. ADR status remains Draft; V5 remains open
pending a reliable marker and confirmed ammo increase during resupply focus.
Post-change checks on 2026-10-01: `make lint` passed; all 102 tests in
`tests/test_resupply.py` and `tests/test_pursuit_mode.py` passed.
The distinct-read change of 2026-10-02 added two tests to
`tests/test_resupply.py`. The disc detector of the same day added 13 tests on
crops of the V5f session's frames (4 icons, 9 lookalikes); with both changes
`make lint` passed and `make test` passed with 2,562 passed and 35 skipped.
The first live session on the disc detector ran 2026-10-02 on
the working tree above `d43dba8`. The outline and glyph checks (V5j) added 5
crops of that session's lookalikes; `make lint` passed and `make test` passed
with 2,567 passed and 35 skipped, and the session on them followed.
The operator's sequence (D3: rack credit, acquisition region, nearest to
centre, resupply mode) added 15 tests across `tests/test_resupply.py` and
`tests/test_pursuit_mode.py`; `make lint` passed and `make test` passed with
2,582 passed and 35 skipped, and the session on it followed.

## References

- [Design 015 — Target-Tracking Pursuit Mode](../hldd/015-target-tracking-pursuit-mode-hldd.md)
- [ADR 144 — Mission SU-30 scripted sequence](144-mission-su30-scripted-sequence.md)
- `wingman/controller.py`: `pursue_and_engage`, `_icon_rung`
- Operator reference screenshots: [MISSILE_EMPTY_RESUPPLY.png](../../tests/test-output/MISSILE_EMPTY_RESUPPLY.png),
  [MISSILE_EMPTY_RESUPPLY2.png](../../tests/test-output/MISSILE_EMPTY_RESUPPLY2.png)
- Committed detector fixtures: [MISSILE_EMPTY_RESUPPLY.png](../../tests/fixtures/MISSILE_EMPTY_RESUPPLY.png),
  [MISSILE_EMPTY_RESUPPLY2.png](../../tests/fixtures/MISSILE_EMPTY_RESUPPLY2.png)