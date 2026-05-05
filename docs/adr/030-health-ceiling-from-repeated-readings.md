# ADR 030 — Health Ceiling from Repeated OCR Readings

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Accepted | 2026-05-04 | 1.6.5           |

## Context

`_process_health_region` concatenates all digit characters found in the health crop into a single integer (e.g., `"".join(c for c in results if c.isdigit())`).  When EasyOCR misreads a background pixel as a digit and prepends it to the real value, the result is garbage — a `4` prefix turning `224` into `4224` was observed in `wingman.log` at `05:58:13`:

```
2026-05-04 05:58:13,955 [INFO] Health: 4224 | alive=True
```

Because `4224 >= 1`, the `alive` check passes silently and the mission continues normally.  However the same mechanism could produce a `0`-leading read (e.g., `0224` → `224` is fine, but a stray `0` replacing the real digits entirely would return `0`) or a wildly large value.  A reading of `0` sets `game_battle_alive = False` and triggers a spurious mission restart after the 3 s grace window expires.

### Why a hard maximum is wrong

Aircraft in the game have different maximum health values.  The J20 reads ~224 at full health; other aircraft may have different ceilings.  Hard-coding `> 999` as a rejection threshold would need updating every time a new aircraft is added and would fail if a legitimate aircraft has health above that value.

### Health icons restore health to full

The game contains health icons that, when picked up during `GAME_BATTLE`, restore the aircraft's health to its full value instantly.  A health restore is a legitimate jump from a low reading (e.g. 2) back up to the aircraft's maximum (e.g. 224).  The spike filter must not reject these restores.

## Decision

Maintain a **rolling window of the last N accepted health readings** inside `GameStateAnalyzer`.  Once the window has accumulated enough readings, derive a **health ceiling** as the maximum value seen across the window.  Reject any incoming OCR reading that exceeds the ceiling by more than a configurable multiplier (`spike_factor`).

### Algorithm

```
window  = deque(maxlen=WINDOW_SIZE)   # e.g. maxlen=10
ceiling = None

on new OCR reading `v`:
    if ceiling is None:
        # Still calibrating — accept all readings to build the window.
        window.append(v)
        if len(window) == WINDOW_SIZE:
            ceiling = max(window)
    elif v <= ceiling:                  # health drain or restore back to full — always accept
        window.append(v)
        ceiling = max(window)           # ceiling slides with window (handles gradual drain)
        return v
    elif v <= ceiling * spike_factor:   # e.g. spike_factor = 1.5 — small overshoot, accept
        # Reading is above the current ceiling but within the plausible overshoot band.
        # This handles OCR rounding jitter at full health and aircraft changes mid-session.
        window.append(v)
        ceiling = max(window)           # ceiling rises to the new value
        return v
    else:
        # Spike — reading far exceeds ceiling; almost certainly a stray OCR digit.
        # Reject without updating window or ceiling.
        log WARNING: "Health OCR spike rejected: {v} (ceiling={ceiling})"
        return last accepted value
```

Key properties:
- **Self-calibrating** — the ceiling is derived from the aircraft's own readings; no hard-coded values.
- **Health restores accepted** — a restore from 2 → 224 is `v <= ceiling` (224 ≤ 224), so it is always accepted. The spike gate only fires on readings that exceed the established ceiling, never on recoveries back to it.
- **Ceiling can rise** — small overshoot up to `ceiling × spike_factor` is accepted, so a different aircraft with a higher max health after a mission restart will gradually recalibrate the ceiling upward.
- **Ceiling can fall** — as the window slides with lower readings (health draining), `max(window)` follows the real value downward, tightening the spike gate over time.
- **Reset on mission restart** — `window` and `ceiling` are cleared in `on_enter_GAME_LOBBY` so each new aircraft session recalibrates from scratch.

### Parameters

| Parameter      | Value | Rationale |
|----------------|-------|-----------|
| `WINDOW_SIZE`  | 10    | ~10 s of readings at 1 Hz; enough to smooth OCR noise while reacting to fast health changes |
| `spike_factor` | 1.5   | A true prefixed digit at minimum doubles the reading (e.g. `4224 / 224 ≈ 18.8×`). Health restores jump back to `ceiling` exactly (`v <= ceiling`), so they are accepted before the spike gate is even reached. 1.5 therefore only rejects readings >50% above ceiling — a band that no legitimate game value occupies. |

### During calibration (first N readings)

All readings are accepted unconditionally until the window is full.  This handles the boot period where health may oscillate slightly as OCR warms up.  Spurious spikes during calibration are unlikely because the 3 s grace window already suppresses brief zero reads, and a spike > 1 means `alive=True` which is the safe side.

## Consequences

### Positive
- Eliminates false health values caused by stray prefix digits without any aircraft-specific configuration.
- Ceiling self-adjusts when the aircraft changes between missions.
- Rejected readings return the last accepted value, so `game_battle_alive` continuity is preserved.
- Log line for rejected spikes gives a clear diagnostic signal.

### Negative / Risks
- `spike_factor = 1.5` will reject readings more than 50% above the current ceiling.  Health icon restores jump back to the aircraft's full health, which equals the established ceiling (`v <= ceiling`); these are accepted unconditionally before the spike gate is evaluated.  The only case where a legitimate reading could exceed the ceiling is if health icons can overheal beyond the aircraft's starting maximum — this is not observed in current gameplay.  If overhealing is added in future, raising `spike_factor` to 2.0 or higher handles it.
- The first `WINDOW_SIZE` readings are unfiltered.  In practice, OCR spikes at startup are harmless (alive stays True), but a spike to `0` during calibration could trigger a false restart.  Mitigated by the existing 3 s no-digits grace window (a real value of `0` is distinct from `None`/no-digits, but the same grace logic could be applied to the zero case if needed in future).

## Implementation Notes

The window and ceiling should live on `GameStateAnalyzer` as:

```python
# __init__
from collections import deque
HEALTH_WINDOW_SIZE = 10
HEALTH_SPIKE_FACTOR = 1.5

self._health_window: deque = deque(maxlen=HEALTH_WINDOW_SIZE)
self._health_ceiling: "int | None" = None
```

Reset in `on_enter_GAME_LOBBY`:

```python
self._health_window.clear()
self._health_ceiling = None
```

Filter applied inside the `health_future.result()` block in `analyze_frame` before updating `self._health`:

```python
health_value = _apply_health_ceiling_filter(
    health_value,
    self._health_window,
    self._health_ceiling,
    HEALTH_WINDOW_SIZE,
    HEALTH_SPIKE_FACTOR,
)
# health_value is None if spike rejected and no prior reading exists
```

## Clarifications Needed

The following implementation details must be decided before coding:

### 1. `_apply_health_ceiling_filter` function signature and return contract

Specify whether this is a module-level function or a static method. Define the exact signature:

```python
def _apply_health_ceiling_filter(
    value: int,
    window: deque,
    ceiling: "int | None",
    window_size: int,
    spike_factor: float,
    last_accepted: "int | None",
) -> "tuple[int, int | None]":
    """
    Returns (filtered_value, new_ceiling).
    
    filtered_value: the input value if accepted; last_accepted if rejected as a spike.
    new_ceiling: updated ceiling after window slide, or unchanged if spike rejected.
    
    Returns None for filtered_value only if rejected AND last_accepted is None 
    (still in calibration phase before window is full).
    """
```

### 2. Thread safety of `_health_window` and `_health_ceiling`

Clarify the locking strategy:
- Both `_health_window` and `_health_ceiling` are only written by the `analyze_frame()` call path (single writer).
- Protect both under the existing `_health_lock` for consistency with `_health` and `_game_battle_alive`.
- Acquire the lock before reading OR writing either variable.

### 3. Reset scope: `on_enter_GAME_BATTLE`

Add defensive reset in `on_enter_GAME_BATTLE` as well as `on_enter_GAME_LOBBY`:

```python
def on_enter_GAME_BATTLE(self):
    self._health_window.clear()
    self._health_ceiling = None
    self._health_no_digits_since = 0.0
    # ... existing code ...
```

This ensures calibration restarts even if the FSM transitions directly to GAME_BATTLE via `manual_reset`.

### 4. Handling rejected spikes in `analyze_frame`

When a spike is rejected and `last_accepted` is returned:
- Still reset `self._health_no_digits_since = 0.0` (digits were present, just invalid).
- Use the returned value in the `alive = value >= 1` check.
- Log the rejection with ceiling and received value for diagnostics.

### 5. Exact insertion point in `analyze_frame` health block

Apply the filter **after** OCR result is returned but **before** the `alive` check:

```python
if health_value is not None:
    # Apply ceiling filter
    with self._health_lock:
        health_value, self._health_ceiling = _apply_health_ceiling_filter(
            health_value,
            self._health_window,
            self._health_ceiling,
            HEALTH_WINDOW_SIZE,
            HEALTH_SPIKE_FACTOR,
            self._health,  # last accepted
        )
    
    self._health_no_digits_since = 0.0
    alive = health_value >= 1 if health_value is not None else False
    
    with self._health_lock:
        prev_alive = self._game_battle_alive
        self._health = health_value
        self._game_battle_alive = alive
    
    logger.info("Health: %d | alive=%s", health_value or 0, alive)
    # ... existing event handling ...
```

## Alternatives Considered

### Hard maximum (e.g., reject > 999)
Rejected: requires per-aircraft configuration; brittle when new aircraft types are added; would need updating if any legitimate aircraft has health > 999.

### Median filter
The median of a window always lags the true value by half the window length.  Health can legitimately drop to 1 HP in a single frame (missile hit); a median filter would delay the `alive=False` detection.  The ceiling approach has no downward lag — only upward spikes are filtered.

### EasyOCR confidence threshold
OCR confidence scores are unreliable at sub-pixel scales and on the binary-thresholded health crop; the confidence for a stray digit is often indistinguishable from a correct digit.  Not a useful discriminator here.

## References

- `wingman/analyzer.py` — `_process_health_region`, `analyze_frame` health block (~line 897)
- Log evidence: `wingman.log` line ~840 — `Health: 4224 | alive=True`
- ADR 020 — CPU-only OCR optimisations (context for why binary-threshold upscaling is used)
