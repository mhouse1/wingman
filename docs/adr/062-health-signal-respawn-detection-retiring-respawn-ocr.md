# ADR 062 — Health-Signal Respawn Detection, Retiring Respawn OCR

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Rejected | 2026-08-02 | 1.6.29          |

**Rejected by its own Phase A data (2026-08-02)** — see the Phase A results
section at the end. Superseded by
[ADR 064](064-dual-sensor-respawn-detection.md), which abandons the
OCR-retirement goal and redirects the health signal to an active fallback.

Generalizes [ADR 061](061-eject-termination-via-observed-death-health-signal.md)
(Draft): the observed-death health signal, introduced there as an
eject-specific fallback, becomes the *primary* respawn detector for all
states, and the respawn-overlay OCR crop is retired after a measured
shadow-mode rollout. Refines the input signals of
[ADR 059](059-health-gated-immediate-mission-restart.md) (Draft) without
changing its restart flow. Phase C touches the
[ADR 044](044-deterministic-runtime-replay-gate.md)/ADR 045 replay-gate
assertions. ADR 061 should still be implemented first — it fixes a live
uncommanded-flight bug and builds the death-provenance machinery this ADR
reuses.

## Context

### Motivation

1. **Resource usage.** Respawn OCR runs as one of ~6 parallel crops every
   battle OCR round (mean 0.18-0.20 s of OCR work per ~1.5 s round —
   roughly 13% of battle-state OCR load). Removing it saves CPU and
   thread-pool pressure. Honest framing: it does **not** materially reduce
   round latency, which is bounded by the slowest parallel crop (telemetry,
   ~0.32 s); the win is CPU headroom and thermals, not responsiveness.
2. **Reliability at the moment that matters.** In the 2026-08-01 07:52
   incident (ADR 061), respawn OCR returned junk (`FS`, `LABBE`, `KM`)
   across the entire 8 s respawn window while the health signal identified
   the death (explicit `Health: 0`) and the respawn (`Health: 250`,
   missiles refilled) exactly. The replay screenshot
   `P1_050_RESPAWN_VISIBLE_NO_HEALTH` confirms the overlay blanks the health
   region — so "health digits return with a value of at least 1" is
   simultaneously evidence of *overlay gone* and *aircraft alive*, the two
   facts respawn OCR plus health currently establish separately.

### The trade-off, measured

The proposal accepts two costs: no independent fallback sensor, and possible
false triggers from health-reading dropouts. Session data (2026-08-01 03:33,
37 min, 17 real respawns, 18 ejects, rotated log
`logs/wingman_20260801_041134.log`) sizes the second cost:

| Signal | Count |
|--------|-------|
| Distinct health-dropout episodes (no-digits grace started) | 43 |
| Real respawns (OCR-detected, ground truth) | 17 |
| Explicit `Health: 0` reads | 11 |
| Alive transitions total | 47 |
| Alive transitions explained (17 respawns + 18 eject synthetic resets) | ~35 |
| **Unexplained dropout-recovery transitions** | **~12 (approx one per 3 min)** |

Two conclusions:

- The naive rule "readings missing → death, readings return → respawn" at
  the current 3 s no-digits threshold would fire a **phantom death/respawn
  roughly every 3 minutes of combat**. The health *spike* filter is
  irrelevant here — it rejects wrong values (464, 2250 were both caught);
  dropouts are the *absence* of values, which the filter never sees.
- The blast radius of a false trigger is bounded and self-healing: a phantom
  respawn causes cancel-mission plus restart — the same actions every real
  respawn already causes. Costs are a ~10 s combat-effectiveness gap, a
  missile-ignore window, and stats pollution — not an uncommanded-flight
  class of failure. False triggers are acceptable at *low* frequency; the
  design below is about making them rare, not impossible.

## Decision

### 1. Death marking — two evidence tiers, no synthetic sources

The analyzer marks the aircraft dead when either:

- **Strong:** health OCR reads an explicit numeric value below 1, confirmed
  by the next reading being sub-1 or no-digits (ADR 061's observed-death
  signal, as amended 2026-08-01 — a lone bounced 0 is an OCR misread, the
  dominant false-fire source in the 11:01 shadow session), or
- **Weak:** health digits have been continuously absent for
  `health.death_no_digits_s` (new config key, default **6.0 s** — raised
  from the hardcoded 3.0 s that produced the false-trigger rate above;
  tuned to sit clearly above routine combat dropouts and clearly below the
  ~8 s respawn-overlay duration).

The 6.0 s value is **one shared window** replacing the hardcoded 3.0 s
everywhere (resolved 2026-08-01 review): the existing no-digits path that
clears `_game_battle_alive` uses the same config value. Side effects are
benign and desirable — the alive flag persists ~3 s longer through HUD
dropouts, suppressing most of the ~12-per-session spurious alive
transitions measured above; restart timing is unaffected because it keys
off digits *returning*, not off when dead was marked.

The eject sequence's synthetic health-dead reset (`controller.py:1526`)
never creates a death mark (ADR 061 decision 1 carries over verbatim).

The shadow detector owns a **private no-digits clock**, driven purely by OCR
readings (added 2026-08-01 after the 11:01 shadow session): the shared
`_health_no_digits_since` is zeroed by `reset_health_for_respawn()` when the
OCR path detects a respawn, which wiped the weak tier's evidence during
every overlay and produced 5 structural misses in that session. In shadow
mode the detector must be measurable independently of the OCR plumbing it is
being compared against; in Phase B the same decoupling keeps the detector
correct when it is itself the trigger for that reset.

### 2. Respawn inference replaces respawn OCR

The first alive-valued health reading after a death mark **is** the respawn
event. It fires the exact plumbing respawn OCR fires today — the
`respawn_detected` event name, the main-loop respawn block (stop eject,
cancel mission, manual-mode exit, `reset_health_for_respawn`,
missile-ignore window), `MissionStatsTracker` respawn counts, and the
ADR 045 capture hook (called from the same background OCR thread with the
frame that produced the alive reading) — so every consumer is unchanged.

```mermaid
flowchart TD
    A[health OCR reading] --> B{explicit value below 1}
    B -->|yes| C[death mark - strong]
    A --> D{digits absent past threshold}
    D -->|yes| E[death mark - weak]
    C --> F{next reading has value at least 1}
    E --> F
    F -->|yes| G[respawn event fires existing plumbing]
```

### 3. Derived-signal replacements

Consumers that read the respawn *screen state* rather than the respawn
*event* switch to digits-presence:

- **Respawn-clear stability** (ADR 059's `respawn_clear_since` gate): keyed
  on health digits being continuously present instead of overlay OCR being
  continuously negative.
- **Ammo-update suppression during the overlay** (analyzer): ammo values are
  distrusted while health digits are missing, instead of while respawn OCR
  is positive.
- **`_handle_no_missiles` respawn suppression**: checks the death-mark state
  instead of `get_respawn_cache_result()`.

### 4. Staged rollout — measure before removing the net

Phases are switched by a config key (resolved 2026-08-01 review), so any
phase can be entered or rolled back with a one-line `config.yaml` edit:

```yaml
respawn_detection:
  mode: shadow    # ocr | shadow | health | health_only
```

- `ocr` — pre-062 behavior; health detector off.
- `shadow` — **Phase A.** The health-based detector runs alongside respawn
  OCR, acting on nothing, logging every would-fire decision. A shadow fire
  with no OCR-detected respawn within 15 s is classified a false fire.
  Exit criteria, evaluated over at least 3 sessions and 30 OCR-detected
  respawns: the shadow detector fires within **15 s** of at least 95% of
  OCR-detected respawns (the `matched` field in the summary), with at most
  1 false fire per session. *(Amended 2026-08-01 from 5 s: OCR fires at
  overlay start while health cannot return until the overlay clears ~8 s
  later, so the shadow fire structurally trails the OCR edge — live matches
  landed at +4 to +8 s and the deterministic replay lane at +7.3 s. A 5 s
  window would fail a perfect detector. The `matched_within_5s` field
  remains in the summary as informational data.)*
- `health` — **Phase B.** The health detector drives the plumbing; respawn
  OCR still runs but only logs disagreement (cheap insurance while
  confidence accumulates).
- `health_only` — **Phase C.** The respawn crop is dropped from battle OCR
  rounds; the ADR 044 replay-gate assertions and P1_050/P2_040 expectations
  are updated in the same change. The crop calibration stays in
  `config.yaml` (dormant) so re-enabling for diagnosis is a one-line change.

The Phase A commit implements `ocr` and `shadow`; `health` and
`health_only` are accepted but warn and fall back to `shadow` until their
phases land.

Each phase is a separate commit and a valid stopping point; failing Phase A
criteria means this ADR is rejected by its own data at near-zero cost.

## Consequences

- Battle-round OCR load drops ~13% (one of six parallel crops); round
  latency essentially unchanged (bounded by telemetry OCR).
- Health OCR becomes the single sensor for death and respawn. It already
  was the sole gate for mission restart (ADR 059 removed the
  health-guard-timeout override), so this concentrates existing risk rather
  than creating new risk — and Phase A quantifies it before commitment.
- The false-trigger profile changes from "OCR misreads overlay text"
  (~never fires falsely, but missed the 08:00 respawn entirely) to
  "sustained health dropout mimics death" (bounded, self-healing, rate
  measured in Phase A with the 6 s threshold).
- The ADR 061 eject fallback ceases to be a special case: eject termination
  rides the same respawn event as every other state.
- Replay assets: P1_050/P2_040 keep their screenshots (the overlay still
  blanks health, which is now the *asserted* signal); checkpoint names in
  the ADR 044 validator need a one-time update in Phase C.
- Mission stats respawn counts remain continuous across the cutover (same
  event, same counter).

## Validation

- Unit tests: two-tier death marking (explicit zero vs no-digits window vs
  synthetic reset), respawn event firing on first alive read after each
  death-mark tier, no event without a preceding mark, ammo suppression
  keyed on digits-absence, 6 s threshold honored from config.
- Phase A exit criteria as specified in decision 4, logged per session and
  summarized in the session stats JSON.
- `make tp` green at every phase; Phase C additionally requires
  `make tp-full` (real-OCR lane) with the updated assertions.
- Live acceptance for Accepted status: one full session in Phase C with
  respawn count matching observed gameplay and zero unexplained mission
  cancels.

## Phase A results — rejection record (2026-08-02)

Five post-ADR-063 shadow sessions (all of 2026-08-01/02, filtered input):

| Session | OCR respawns | Matched | False fires | Missed |
|---------|--------------|---------|-------------|--------|
| 18:40   | 3            | 2       | 0           | 1      |
| 19:03   | 0            | 0       | 0           | 0      |
| 19:11   | 5            | 4       | 0           | 1      |
| 02:51   | 5            | 3       | 0           | 2      |
| 03:11   | 12           | 9       | 0           | 3      |
| **Total** | **25**     | **18 (72%)** | **0** | **7** |

The 95%-matched exit criterion was mathematically unreachable at 25/30
samples (best case 77%) and the miss causes are structural, not tunable:

- ~2 misses were round-end artifacts (no recovery phase existed to detect).
- The rest split between overlays shorter than the 6 s no-digits window and
  the decisive failure: during degraded-OCR sessions the sensor
  **hallucinates digits on the respawn overlay itself**, resetting the
  absence clock exactly when absence is the signal. Absence cannot be
  measured by a sensor that fabricates presence.

The false-fire criterion passed perfectly (0 in five sessions), and the
shadow detector caught two real respawns that respawn OCR missed (the 08:00
uncommanded-flight incident and the 17:41 event) — evidence that the health
signal is a high-precision *corroborating* sensor even though it cannot be
the *primary* one. ADR 064 builds on exactly that.
