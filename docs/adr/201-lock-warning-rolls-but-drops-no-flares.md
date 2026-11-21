# ADR 201 — LOCK WARNING rolls but drops no flares

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-11-20 | 1.9.5           |

Narrows ADR 184 D0, which made LOCK WARNING raise the incoming alert in full, flares
included. The flare roll on LOCK WARNING (ADR 184) is unchanged.

## Decision

Operator, 2026-11-20: "flares should not deploy on lock warning, it should only deploy on
INCOMING detected".

- **D1. Only INCOMING drops flares.** An alert counts as INCOMING if:
  - the INCOMING template matched, or
  - the OCR fallback text contains `MING` (`INCOMING_TEXT_TOKENS`).

  An OCR read with only `ARNING` (`LOCKWARNING`) is LOCK WARNING.
- **D2. LOCK WARNING still raises the alert.**
  - It starts the flare roll and keeps it held (ADR 184), and still drives the missile
    evade (ADR 163) and the jet's incoming presses (ADR 173 D4, ADR 183 D4).
  - The flares are held, and the log reads
    `INCOMING MISSILE DETECTED - flares held: LOCK WARNING, not INCOMING (ADR 201)`.
- **D3. INCOMING right after LOCK WARNING is not debounced** (`incoming_debounced`). Before
  this, the 500 ms debounce could swallow the INCOMING read that follows a lock, and that is
  now the read that drops flares.

## Consequences

- Flares go out only on an INCOMING alert. In the 09:46 session this would have dropped 6 of
  17 bursts; the other 11 followed a LOCK WARNING read.
- The flare roll's timing does not change: `incoming_last_seen_ts` still counts LOCK WARNING.
- An INCOMING the OCR misreads with no `MING` (for example `INCOMIN`) is now treated as LOCK
  WARNING and drops no flares. The INCOMING template still catches a clean INCOMING.

## Evidence

`wingman.log`, 2026-11-20 09:46 session (KF-21):

```
10:04:29,354 INCOMING MISSILE DETECTED (source=ocr_fallback template=INCOMING_s100 score=0.225 text=INCOMING_)
10:04:29,923 INCOMING MISSILE DETECTED - Deploying flares
10:04:31,160 INCOMING MISSILE DETECTED (source=ocr_fallback template=INCOMING_s100 score=0.091 text=LOCKWARNING)
10:04:31,227 INCOMING MISSILE DETECTED - Deploying flares
```

- Detections by OCR text: `LOCKWARNING` 28, `INCOMING` 16, `INCOMING_` 1.
- Flare bursts: 11 after a `LOCKWARNING` read, 6 after an `INCOMING` read (28 more alerts
  were held by ADR 198).

## Implementation

- `wingman/analyzer.py`:
  - `INCOMING_TEXT_TOKENS`, `is_lock_warning_only`, `incoming_debounced`.
  - The cache's `lock_warning` flag and `incoming_is_lock_warning()`.
- `wingman/tick_handlers.py`: `deploy_flares_on_new_incoming` holds the flares when
  `_lock_warning_only()` says so.
- `tests/test_lock_warning_no_flares.py`.

## Validation

- [x] Tests:
  - `LOCKWARNING` is LOCK WARNING only; `INCOMING`, `INCOMING_` and a template hit are not.
  - A LOCK WARNING alert rolls and drops no flares; an INCOMING alert drops three.
  - INCOMING after LOCK WARNING inside 500 ms is not debounced.
- [ ] Live: no `Deploying flares` line whose detection text is `LOCKWARNING`, and a
      `flares held: LOCK WARNING` line in its place.
