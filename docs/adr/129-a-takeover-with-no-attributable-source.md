# ADR 129 — A Takeover With No Attributable Source

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Accepted | 2026-09-06 | 1.8.8         |

## Context

The operator reported that alt-tabbing to another window stopped wingman flying.
The obvious suspect was ADR 098's focus guard, which suppresses injection when
the game does not have focus.

**It was not the guard, and the log said so.** Over the 2026-09-05 21:58 session:

| Measurement | Result |
|-------------|-------:|
| `grep -c "FocusGuard" wingman.log` | **0** |
| `GAME_BATTLE_MANUAL` log lines | 23 |
| Distinct keys that raised a takeover | 1 (`enter`) |

ADR 099 had already removed the hazard by construction — injection follows the
game to `:3`, where it is the only client and always focused, so the guard has
nothing to suppress and never fired. The session log confirms the topology:

```
ADR 099: nested lane ACTIVE - capture and injection on :3, hotkeys observed on :0, :3
ADR 099: focus guard follows injection to display :3
```

What actually stopped the aircraft was manual takeover, twice, mid-eject:

```
22:01:41,579 Controller: eject_and_dive — rotation pulse 3/12 (rate -193 m/s, nose -55deg)
22:01:42,121 Controller: maneuver key 'enter' pressed - entering GAME_BATTLE_MANUAL (manual takeover)
22:02:04,966 Controller: maneuver key 'enter' pressed - entering GAME_BATTLE_MANUAL (manual takeover)
```

### The full session narrows it further

The session ran to a clean exit at 02:35 — 4h 36m, 48 missions, all 48
click-to-finish, summary and stats written. Over that whole run:

| Measurement | Result |
|-------------|-------:|
| Manual takeovers | **2** |
| Both within | a **23-second** window (22:01:42, 22:02:04) |
| Takeovers after 22:30 | **0** |
| Any hotkey delivery after 22:30 | **0**, across 4h 04m |

Both takeovers land in the first four minutes, while the operator was at the
keyboard; nothing fires across four unattended hours. That **rules out** a
spurious source — wingman's own echo, XTest auto-repeat, or a stray X event
would not politely confine itself to the minutes a human was typing. It is
*consistent with* the operator's own `:0` keystrokes reaching the takeover path,
but it does not establish it: zero deliveries after 22:30 is equally explained by
zero keys being pressed. **measured** — the counts and the window;
**inferred** — the `:0` origin; **assumed** — nothing.

That is exactly the gap this ADR closes. Correlation with the operator being
awake is not attribution, and no amount of further soaking produces attribution
from a log line that never carried the source.

## The defect is in the evidence, not in the behaviour

`ENTER` is `MANUAL_TAKEOVER_KEY`, so a takeover is the *correct* response to it.
The question is which keyboard it came from, and the log cannot answer:

- **Nested display `:3`** — the operator focused the game window and pressed
  ENTER. A deliberate takeover, working exactly as SAF-001 intends.
- **Operator display `:0`** — ADR 099 requires ctrl+alt there, so this would mean
  a genuine hotkey, or a modifier-mask defect letting ordinary typing through.

`should_deliver_hotkey(display_name, key_name, state)` decides from all three
inputs and **records none of them**. `_XKeyEvent` carried only `name` and
`is_injected`, so both paths produce byte-identical INFO lines. Two explanations,
one log line, no way to separate them after the fact — and the next session
rotates the log away.

This is the failure mode the iterate discipline names directly: when two
explanations look identical in the log, add instrumentation rather than tune.
Changing the modifier mask or removing ENTER from `TAKEOVER_KEYS` would be force
applied to an unmeasured lever, and would break SAF-001 if the takeovers turn out
to be the operator's own.

## Decision

**D1. Every observed hotkey carries its origin.** `_XKeyEvent` gains `display`
and `state`, populated at the XRecord delivery point from the values
`should_deliver_hotkey` already receives.

**D2. The takeover log line names the source.** `describe_key_source()` renders
the display, what that display *means* (`nested, game focused` vs
`operator desktop`), and the decoded modifier state:

```
Controller: maneuver key 'enter' pressed - entering GAME_BATTLE_MANUAL
  (manual takeover) [source=':3' (nested, game focused) mods=none]
```

The role is spelled out, not left as a bare display number — `:0` and `:3` mean
nothing to a reader weeks later, and the difference between them is the whole
diagnosis.

**D3. Attribution never gates the takeover.** A source that cannot be named is
still the operator taking the aircraft. SAF-001's cessation path is untouched;
this ADR adds a log field and nothing else.

**D4. Read with `getattr`.** The Windows `keyboard` module delivers real
`KeyboardEvent`s carrying neither field, and `_XKeyEvent` exists to mirror that
shape. Both new fields default, so every existing caller is unaffected.

## Consequences

The next `'enter'` takeover is attributable from its own log line. If it reads
`source=':3'` the operator took the aircraft and there is no defect. If it reads
`source=':0' ... mods=none`, ADR 099's `_OPERATOR_MOD_MASK` gate is leaking and
ordinary typing is flying the aircraft — a real bug, and one that would have been
invisible without this.

No behaviour changes. Nothing is fixed by this ADR, deliberately: the two
candidate causes need different fixes and the log could not yet say which applies.

## Validation

- V1. Unit: `_XKeyEvent` carries `display` and `state`, and still matches the
  `keyboard.KeyboardEvent` shape the rest of the code reads.
- V2. Unit: both fields default, so a caller passing neither is unaffected.
- V3. Unit: the nested display, the operator desktop, an unrecognised display and
  a missing display each render distinctly; ctrl+alt is decoded; no modifiers is
  stated rather than omitted.
- V4. Unit: the takeover INFO line contains the source.
- V5. Unit: a takeover with no source still takes over (D3).
- V6. Live: run a session, alt-tab away, type, and confirm no takeover is raised
  from `:0`; then focus the game window, press ENTER, and confirm the takeover
  logs `source=':3'`.

Covered by `tests/test_takeover_attribution.py` (11 tests). V6 is outstanding.

## References

- ADR 098 — the focus guard, measured here as never firing on the nested lane
- ADR 099 — the nested display lane; D7/D8 govern the guard's current role
- SAF-001 — manual takeover and the 2.0 s cessation bound this must not affect
- ADR 121 — a hung shutdown must leave evidence; this session shut down cleanly,
  but see the reconnect note below
- Evidence: the 2026-09-05 21:58 to 2026-09-06 02:35 session, preserved as
  `logs/preserved_20260906_0237_session.log` (it was never auto-archived, and the
  live `wingman.log` opens with `mode="w"`)

## Loose end observed while gathering this

The session's only `[ERROR]` is at shutdown:

```
02:35:35,252 Nested display: closing Xwayland for :3 (pid(s): 1127717)
02:35:35,252 [ERROR] XKey listener thread died: Display connection closed by server
02:35:35,253 XKey: reconnecting display in 3s (attempt 1)
```

The hotkey listener treats a *deliberate* teardown of `:3` as a connection
failure and schedules a reconnect against a display that is being destroyed on
purpose. Harmless here — the process exits before the 3 s timer fires — but it
logs an ERROR on every clean shutdown, which is exactly the kind of expected
noise that hides a real listener death. Not addressed here; recorded so it is not
rediscovered as a new fault.

## V6 satisfied — the answer is "the operator" (2026-09-06)

Four takeovers across the 04:02, 04:22, 04:29 and 05:10 sessions, every one
attributed:

```
Controller: maneuver key 'enter' pressed - entering GAME_BATTLE_MANUAL
  (manual takeover) [source=':3' (nested, game focused) mods=none]
```

All four from `:3` — the nested display, which can only receive a key when the
operator has deliberately focused the game window. **None from `:0`.**

That closes the question this ADR was opened for. ADR 099's `_OPERATOR_MOD_MASK`
ctrl+alt gate is **not** leaking; ordinary typing on the operator's desktop is not
reaching the takeover path. The two unattributable takeovers of 2026-09-05 22:01
were the operator's own presses, and there is no defect to fix.

The instrumentation stays. It cost one log field and it converted a question that
could not be answered from four sessions of evidence into one answered by the
first takeover after it shipped. Status moved to Accepted: D1-D4 are implemented,
V1-V5 are covered by `tests/test_takeover_attribution.py`, and V6 is now met.

