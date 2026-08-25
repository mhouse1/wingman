# ADR 071 — Single Gate-Corpus Screenshot Set

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Accepted | 2026-08-13 | 1.7.1           |

Builds on [ADR 037](037-timed-screenshot-replay-integration-testing.md)
(replay paths), [ADR 041](041-live-replay-auto-capture-for-integration-screenshots.md)
(live auto-capture), [ADR 044](044-runtime-screenshot-driven-automation-lane.md) /
[ADR 045](045-dual-lane-runtime-validation-replay-and-live-screen.md) (runtime
gates). Does not supersede any of them: paths, capture, and gates keep their
designs — this ADR decides what *images* the replay/gate lanes consume and
where CV ground truth lives. Companion:
[ADR 072](072-calibration-screenshot-consolidation.md) owns the
calibration-reference consolidation and the respawn-variant retirement; where
this document's early drafts overlapped it (root-screenshot retirement,
recapture-slot mechanics), ADR 072 is authoritative. Code review
[CR-015](../code-review/015-2026-08.md) supplies the evidence trail.

## Context

The August 2026 game-UI update moved HUD elements (~24 px), invalidating every
archived screenshot at once and exposing how the corpus had grown: three
parallel screenshot populations, each with its own refresh story.

1. **The gate corpus** (`test_screenshots/integration_test/`, P1_*/P2_*):
   consumed by the ADR 037/044/045 replay and live lanes.
2. **Root-level variants** (`RESPAWN.png`, `RESPAWNB/C/D.png`, `continue.png`,
   `continue1.png`, `AMMO_*.png`, `PLAY1.png`, `CANCEL.png`, `game_*.png`…):
   consumed by unit/OCR tests and the calibration map — each captured once,
   by hand, and never refreshed since.
3. **CV ground-truth frames**: minimap tests pinning hand-verified blob
   positions to whatever gate frame happened to contain them.

Investigation during the update recovery established three facts:

- **The P2 set was largely literal copies.** `P2_000 ≡ P1_030`,
  `P2_010 ≡ P1_040`, `P2_050 ≡ P1_060` — byte-identical git blobs. The
  genuinely distinct P2 frames (takeover moment, manual HUD) are visually
  indistinguishable from ordinary battle HUDs: MetalStorm renders no "manual
  mode" indicator, because manual mode is wingman's FSM concept, not a game
  screen.
- **What PATH2 uniquely tests is not pixels.** The manual-takeover lane
  exercises `manual_mode`, `manual_mode_entered`, and respawn handling from
  `GAME_BATTLE_MANUAL` — all FSM states and injected triggers. The frames only
  need to OCR consistently with the step's expectations (a respawn overlay
  where `respawn_detected` is asserted, a neutral battle HUD elsewhere).
- **Root-level variants duplicate gate frames at lower quality.** The gate
  corpus already contains a respawn overlay (P1_050), a click-to screen
  (P1_070), a lobby with PLAY (P1_000) and battle HUDs with both ammo counters
  (P1_030) — and unlike the root variants, those frames are refreshed
  unattended by `make p1` (ADR 041 lane, repaired 2026-08-13 — see
  Consequences).

## Decision

**One screenshot population: the P1 gate corpus, refreshed unattended by
`make p1`, consumed by every lane and test. Sequences carry coverage; files do
not.**

1. **The P2 files are retired.** PATH2 and PATH3 keep their full FSM coverage
   as *sequences over shared P1 frames*: the takeover steps inject their
   triggers over P1_030, the in-manual respawn step asserts OCR over P1_050,
   the end screens use P1_070/P1_080. The manual-takeover FSM test is
   unchanged — it never depended on P2 pixels.
2. **Root-level duplicates are retired** *(detailed ownership of the
   calibration-map consolidation now rests with ADR 072; summary kept here for
   the corpus picture)*. Unit/OCR tests and the calibration
   map point at the gate frames: `TEST_SCREENSHOT` → P1_050,
   `TEST_SCREENSHOT_B` → P1_030, `TEST_SCREENSHOT_D` → P1_060,
   `TEST_SCREENSHOT_CONTINUE` → P1_070; calibration entries for respawn,
   click_to, good_luck, PLAY and both ammo crops reference gate frames
   (`GOODLUCK.png` retired with the rest — P1_020 is the same screen).

   Root-level frames survive only when they show something no gate frame
   contains: lobby popups the mission cycle does not visit (`UNREADY`,
   `INVITED`, `REVEAL_ALL`, `UNLOCK_CLOSE`, `INSPECT`, `SILVER`,
   `CREATION_FAILED`, `TAP_HERE_TO_CONTINUE`, `event_refresh_*`), the
   `INCOMING.png` missile-warning frame (also the ADR 046 template source —
   no PATH1 step reliably shows the warning), HUD-region references
   (`HEALTH`, `ENEMY_CLOSE_BY`), the telemetry corpus, and the dedicated CV
   fixtures of decision 3. These are irreducible, not duplicates.
3. **CV ground truth lives on dedicated, recapture-immune fixtures.**
   Hand-verified minimap truths were moved out of the gate corpus to
   `MINIMAP_DESERT_3RINGS.png` and `MINIMAP_RIM_MERGED.png` (preserved from
   git history at the moment their truths were verified). Rule: **never pin
   hand-verified CV assertions to a gate frame** — the gate corpus is
   recaptured on every UI update, which silently invalidates pinned pixel
   truths. This happened twice in one day (P1_040, then P1_060, 2026-08-13)
   before the rule was adopted.
4. **`make p1` is the corpus refresh.** One unattended run — lobby through
   mission end — rewrites all nine frames on the current UI layout. `make p2`
   / `make p3` become optional FSM-sequence capture checks, no longer required
   for fixture freshness, and no longer need a human at the keyboard for the
   takeover moment.

## What was given up, deliberately

- **The discolored-respawn OCR case** (`RESPAWNC`): its frame showed the
  pre-update layout, unreadable by the recalibrated crop for the right
  reasons. Open recapture item: a discolored *new-layout* frame saved as
  `test_screenshots/RESPAWNC.png`.
- **The Levenshtein-distractor negative** (`RESPAWND`, "natethegreat"): already
  vacuous after the crop recalibration — the distractor text sat outside the
  new crop (CR-015-03), so the test passed by reading empty terrain. Open
  recapture item: near-miss text *inside* the current respawn crop, saved as
  `test_screenshots/RESPAWND.png`. P1_060 stands in as a plain negative
  meanwhile.
- **A second click-to variant** (`continue1.png`): old layout; the single gate
  frame carries the case.

**Superseded by ADR 072 decision 3 (2026-08-14):** the variant set is retired
outright rather than held open for recapture — only the crop *location*
changed in the game update, so per-layout variant maintenance is cost without
coverage. The self-activating recapture slots this section originally
prescribed were implemented and then removed when ADR 072 landed; the accepted
losses (discolored-frame OCR robustness, the Levenshtein-distractor negative)
and their revisit condition (a future update changing overlay *rendering*,
not position) are recorded in ADR 072's consequences.

## Consequences

- Every screenshot consumer now shares one refresh path, so a game-UI update
  is one `make p1` run plus recapture of the two dedicated OCR gap items —
  not an archaeology session across three populations. The 2026-08-13 update
  cost days precisely because the old corpus had no single refresh story.
- The `make p1` lane this decision leans on was broken in three stacked ways
  and repaired the same day (all with regression tests): capture pinned to the
  config region while the game window sat at +66+69 (2026-08-09 regression
  from the ADR 045 presenter-lane fix; now `--capture-pin-region`, presenter
  lane only); the ADR 056 eject transition making `missiles_empty` steps
  unobservable under the trigger+state readiness rule (state sightings within
  the freshness window now count); and the out-of-order deadline sized for
  ~24 s replay pacing rather than a ~6 min real mission
  (`CAPTURE_TIMEOUT_S` 120 → 600).
- Shared frames mean shared blast radius: a bad P1 recapture now breaks every
  lane at once instead of one. This is judged acceptable because the failure
  is loud (gates + unit tests together) and the fix is one rerun of `make p1`,
  whereas the old corpus failed *quietly and partially* — the stale-crop gate
  fixtures sat broken for four days with `make test` green (CR-015-01).
- `git ls-tree` blob identity is the cheap dedup test for screenshot
  consolidation; it proved three "distinct" P2 files were copies before any
  behavioral analysis was needed.
- ADR 037's example configs and step lists still name P2 files; that document
  is Accepted history and stays unmodified. The live configs
  (`tests/replay_paths/adr037_paths.yaml`, the inline OCR clones) are the
  source of truth for current step definitions.
