# ADR 184 — The roll holds until the lock warning has been gone 2 s

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-11-16 | 1.9.5           |

## Decision

Operator, 2026-11-16, on `tests/test-output/screenshot_20261113_112801.png` (LOCK WARNING on
screen): "wingman is supposed to barrel roll while incoming missile is detected, this is not
happening, fix it so that as soon as LOCK WARNING is detected it holds the roll until no more
incoming missile detected for 2 seconds".

- **D0. The incoming crop reads LOCK WARNING.** The crop was x 0.4521 to 0.5486 of the frame,
  and it cut the warning to `OCKWARNIN` / `DCKWARNIN` on the operator's frame. That text holds
  neither fallback token (`MING`, `ARNING`), and the template is INCOMING's, so LOCK WARNING
  never raised an alert, and no flares or roll followed. The crop is now x 0.4380 to 0.5620, which
  reads `LOCKWARNING`; INCOMING still matches its template (score 1.000). As `analyzer.py`'s
  comment on the tokens asks, the crop was widened and the tokens left alone.
- **D1. The roll starts on every incoming alert, with a target in view too.** This supersedes the
  flare-roll row of [ADR 181](181-the-chase-stays-on-its-target-through-missile-and-terrain-warnings.md)
  D1. A held roll no longer ends when the chase gets its target. The rest of ADR 181 (crash
  recovery, terrain turn and the missile evade, withheld with a target in view) is unchanged.
- **D2. It holds until the warning has been gone `flare_roll_after_s` (2 s).** Before, the
  [ADR 164](164-roll-right-through-the-flares.md) roll let go 2 s after the burst's last flare,
  even with the warning still on screen. Now it holds until 2 s after whichever is later: the
  last flare, or the last detection pass that saw the incoming warning. That pass counts even when
  the alert debounce reports no new alert (`GameStateAnalyzer.incoming_last_seen_ts`).
- **D3. The cap only catches a stuck burst.** `flare_roll_max_s` (15 s) now ends the roll only
  when a burst never reports its last flare and the warning is no longer seen. A warning that
  stays on screen keeps the roll held.
- **D4. These still end the roll, or stop it starting:** manual takeover, leaving the battle,
  an eject, and a hard-emergency climb (ADR 163 D5, where a roll would turn the pull-up
  sideways).

FR-011 is amended to match.

## Consequences

- The chase yields pitch and roll while the roll holds (FR-011), so it loses its aim for as long
  as the warning shows, plus 2 s. ADR 181 found that a roll begun with the target in view lost
  it almost every time. The operator has chosen the roll.
- Rolls last longer: they now follow the warning, not the flares.

## Evidence

The detector on the operator's frame, `_process_incoming_region` with the shipped settings:

| Crop (x) | Template score | OCR fallback read | Alert |
|---|---|---|---|
| 0.4521 to 0.5486 (before) | 0.057 | `OCKWARNIN`, `DCKWARNIN` | no |
| 0.4380 to 0.5620 (now) | -0.011 | `LOCKWARNING` | yes |

Every alert of the 12:12 session read `text=INCOMING` (343, plus one `INCOMINGJE`); none was a
lock warning.

The live session of 2026-11-16 12:12 to 14:10 (`make r1`, ADR 181 code) had 351 alerts that
deployed flares. Only 45 started a roll:

| Outcome on an alert | Count |
|---|---|
| `flare roll not started — the chase stays on its target (ADR 181)` | 252 |
| `flare roll not started — emergency climb` | 12 |
| `FLARE ROLL — holding roll right` | 45 |

The rest arrived while a roll was already held. The rolls that did fly lasted a median of 2.9 s.

```
12:14:58,893 [DEBUG] Controller: deploy_flares - pressing 'space' key for 0.05 seconds
12:14:58,893 [INFO] Controller: flare roll not started — the chase stays on its target (ADR 181)
12:15:00,355 [INFO] Controller: flare roll not started — the chase stays on its target (ADR 181)
```

## Implementation

- `wingman/config.yaml`: `crops.incoming` widened. `tests/test_incoming_lock_warning.py`
  (real OCR on `tests/fixtures/incoming/lock_warning_20261113_112801.png`, which needs
  `git add -f`).
- `wingman/analyzer.py`: `_incoming_last_seen_ts`, set by every pass that sees the warning;
  `incoming_last_seen_ts()`. Added to the `Perception` port and its fake.
- `wingman/controller.py`: `_flare_roll_refusal` drops the ADR 181 check; `_run_flare_roll`
  holds to `incoming_last_seen_ts + flare_roll_after_s`, and the cap applies only to an
  unreported burst.
- `tests/test_flare_roll.py`: the warning tests. `tests/test_pursuit_hold.py`: the two ADR 181
  roll tests are reversed.
- `docs/requirements/002-functional.sdoc`: FR-011.

## Validation

- [ ] Live: `INCOMING MISSILE DETECTED (source=ocr_fallback … text=LOCKWARNING)` lines.
- [ ] Live: no `flare roll not started — the chase stays on its target` lines; a
      `FLARE ROLL — holding` line for each alert that is not already inside a roll.
- [ ] Live: each `flare roll released (flares done, N s)` comes 2 s after the last
      `incoming_template … detected=True` pass.
- [ ] Deaths per round by kill card (shot down), against the 2026-11-16 12:12 session.
