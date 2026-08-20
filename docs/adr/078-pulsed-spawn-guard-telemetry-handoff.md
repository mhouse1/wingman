# ADR 078 — Pulsed Spawn Guard with Telemetry Handoff

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Accepted | 2026-08-17 | 1.8.4           |

## Context

The ADR 076 spawn-attitude guard holds NOSE_UP **continuously** from death
detection until the alive handoff (+2.5 s overlap). The design assumed the
hold acts almost entirely on the respawn screen, where input is inert, with
only the short overlap touching the live aircraft.

The 2026-08-17 13:05 session (1 h 48 min, 75 respawns) falsified that
assumption twice, counted by the new spawn-crash instrument and confirmed
by direct operator observation: **the aircraft over-rotated at spawn — a
full 180 — and flew out of the map inverted.**

The mechanism, from the 14:22 episode:

```
14:22:24  guard starts (death latched)               — hold begins
14:22:3x  aircraft SPAWNS (exact instant invisible)  — hold now acts on a live airframe
14:22:38  HEALTH ALIVE confirmed → restart           — hold still on (+2.5s overlap)
14:22:39  telemetry: Alt 1182  Speed 1258  Nose +3° (level)   ← inverted, heading reversed
14:22:40  guard releases (alive_handoff, 14.0s total)
14:22:45  RESPAWN DETECTED — death 7.5s after restart (out of map)
```

The gap: **alive detection lags the spawn instant by several seconds**
(health digits must appear, pass the SAF-004 confirm window, then the
respawn-clear stability window). During that lag the live aircraft receives
continuous full nose-up at spawn speed (~1100+ km/h) — precisely the input
the 2026-08-15 finding showed loops the aircraft (ADR 073: 60 s of held
nose-up, altitude oscillating, zero net gain). The ADR 076 d3 rate ceiling
cannot help: it lives in the Climb hold, which only takes the pitch axis
after the restart — after the loop has already happened.

Telemetry, not health, is the earliest sign of life: in the same episode a
fresh altitude/speed read appeared ~1.5 s **before** HEALTH ALIVE (the HUD
renders before health OCR confirms). The guard already runs while the FSM
is in GAME_BATTLE, where telemetry OCR is live.

(The session's second counted spawn crash, 0.3 s after restart, was not a
crash at all: an ADR 064 weak-tier health fallback false-fired during a
healthy steep climb — health OCR dropout read as death — and its queued
respawn event landed just after the restart. Separate instrument noise,
noted for the record; the fix here does not address it.)

## Decision

Supersedes the **hold mechanics** of ADR 076 d1/d2. The guard's purpose,
triggers, ownership-aware release, priority behavior, and SAF-009 bounds
are unchanged; ADR 076 remains Accepted for all of that.

### d1 — The guard pulses instead of holding

NOSE_UP is applied in `pulse_s` pulses (default 1.5 s) separated by
`observe_s` gaps (default 1.0 s) — the climb hold's pulse-and-observe
pattern, open-loop because a dead aircraft provides no rate feedback.
On the respawn screen the duty cycle is irrelevant (input is inert); at
the spawn instant the worst case is one bounded pulse of rotation instead
of unbounded held rotation. A pulse cannot loop the aircraft; a hold
demonstrably does.

### d2 — Fresh telemetry releases the guard

At guard start, the current telemetry stable-value timestamp is recorded
as a baseline. Any fresh sample with an advanced timestamp means the HUD
is rendering — the aircraft exists — and the guard releases immediately
(`telemetry_handoff`). This beats the health-confirm path by ~1.5–2 s,
cutting the live-aircraft exposure to at most one pulse plus selection
latency, after which the Climb tactic owns pitch with its rate ceiling and
confirm-reads debounce.

The alive handoff (+overlap) remains as the fallback release when
telemetry never freshens (HUD unreadable), and every other release trigger
(state exit, tactic preempt, takeover, max-hold backstop, cleanup) is
unchanged.

## Consequences

- Terrain-clearance bias at spawn is retained (the aircraft still spawns
  with pitch-up input in its first frames) but rotation authority is
  bounded — the 180-and-out-of-map failure mode is structurally removed.
- The guard ends seconds earlier on normal spawns (telemetry handoff),
  shrinking the window where guard and climb interact; the ownership rule
  matters less often but stays for the overlap cases.
- New config keys `climb.spawn_guard.pulse_s` / `observe_s`; existing keys
  unchanged.
- The spawn-crash instrument stays as-is, including its known
  false-positive class (queued ADR 064 fallback events landing
  post-restart). If weak-tier false fires recur, that is an ADR 064
  tuning follow-up, not a guard concern.

## Verification

- Unit tests: guard emits multiple press/release cycles on the pulse
  cadence; fresh telemetry (advancing timestamp) releases with
  `telemetry_handoff`; stale/frozen telemetry does not release early; all
  ADR 076 release paths and the ownership rule unchanged (`test_spawn_guard.py`).
- `make test` green; replay gates unaffected.
- Live validation (2026-08-17 15:02 and 15:33 sessions, 32 respawns):
  (a) **32 of 32 guard exits via `telemetry_handoff`** (3.6–5.6 s
  typical) — the alive-handoff fallback was never needed; (b) zero
  over-rotation deaths — the only counted spawn crash across both
  sessions was the known ADR 064 false positive (`died_after_s: 0.0`,
  pre-ADR 079 session), and the 15:33 session with the full stack counted
  **0 spawn crashes over 18 respawns**; (c) no looping or inverted flight
  observed after spawns.

## References

- ADR 076 — spawn-attitude guard (Accepted; hold mechanics superseded here)
- ADR 073 — pulse-and-observe pitch pattern and the 2026-08-15 held
  nose-up looping evidence
- ADR 064 — dual respawn detection (weak-tier false fire noted in context)
- ADR 065 — GAME_STARTING health probes (alive-detection latency context)
- 2026-08-17 13:05 session log — 14:22 over-rotation episode, operator
  observation of the 180-and-out-of-map failure
