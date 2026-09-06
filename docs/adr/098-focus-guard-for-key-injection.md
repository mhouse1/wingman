# ADR 098 — Focus Guard for Key Injection

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-08-28 | 1.8.7           |

## Context

Wingman injects keystrokes into whatever window has focus. Nothing checks that
the window is the game.

The only existing protection is a capture-staleness check: if no frame has
arrived for `capture_stale_inject_s`, injection is suppressed on the assumption
that the display was lost. The code comments at `controller.py:174` and
`capture.py:589` both name the hazard directly — "presses land in whatever
window is focused" — but that guard cannot fire for the case that matters,
because `mss` keeps grabbing the monitor perfectly well when the operator
alt-tabs away. Frames keep arriving; wingman keeps typing.

### The hazard, observed

On 2026-08-28 at 10:26, while a session was running, the operator switched to
their editor to type a message. Their text arrived as `tryi auganw` — "try
again" with a stray `i` and a trailing `w`. The session log for that window:

| Time     | Key injected | Binding          |
|----------|--------------|------------------|
| 10:26:07 | `i`          | `NOSE_UP_KEY`    |
| 10:26:09 | `w`          | `WINGSWEEP_KEY`  |
| 10:26:09 | `e`          | `AFTERBURNER_KEY`|

The focus probe was sampling at the same moment and recorded 12 consecutive
samples with Visual Studio Code as the focused window. An earlier message in the
same session arrived as `fprobfi falked`.

A 2.8-hour session injects roughly 2500 keystrokes, dominated by `f`, `p` and
`space`. Every one of them lands in the operator's editor if the game does not
have focus. This is a data-integrity hazard, not only an annoyance.

## The mechanism is verified, not assumed

The session is Wayland with a rootless Xwayland, so it was not obvious that an
X11 focus query could see focus move to a non-X window at all. A guard that is
confidently wrong exactly when focus leaves the game would be worse than none.
`scripts/focus-probe.py` was built to settle this before any guard was designed.

Result over 231 samples spanning several alt-tabs:

| Observation | Samples |
|-------------|--------:|
| Both signals report the game | 219 |
| Both signals report another app | 12 |
| **Signals disagreeing** | **0** |

Both `_NET_ACTIVE_WINDOW` and `XGetInputFocus` tracked focus leaving for Visual
Studio Code and returning, in complete agreement. The Wayland concern does not
materialise for this case.

The probe also caught three ways a naive guard would have failed, each of which
would have shipped into the injection path:

1. **The game window is not titled "Metalstorm".** It is "Wine Desktop" — the
   Proton virtual desktop container.
2. **Titles cannot identify anything.** With the game shut down, a Visual Studio
   Code window titled "Metalstorm config GitHub... - wingman - Visual Studio
   Code" satisfied a `metalstorm` substring test. A guard using that rule keeps
   typing into the editor — the exact failure it exists to prevent.
3. **The managed window does not belong to the game process.** "Wine Desktop" is
   owned by `explorer.exe` (pid 3241639), a *sibling* of `Metalstorm.exe` (pid
   3241663) under the Proton launcher `pv-adverb`. Matching only the game binary
   sees "not the game" whenever the desktop is focused, suppresses every
   keypress, and silently stops wingman working.

Trap 3 is the important one: it fails closed and looks like nothing is wrong.

## Decision

**D1. Suppress injection when the focused window does not belong to the game.**
Both keyboard and mouse injection, since a click lands in the focused window too.

**D2. Identify the game by its Wine session, never by window title.** The
session is every process descended from the game binary's parent, which covers
`explorer.exe` and any Wine helper while excluding unrelated applications — the
editor shares only `systemd` with the game, far above that root. Window
ownership comes from `_NET_WM_PID`.

**D3. Accept either focus signal.** They agreed on all 231 samples, so requiring
both adds no safety and doubles the ways the check can fail. The guard treats
the game as focused if either names a session process.

**D4. On an unresolvable check, inject.** A guard that suppresses when it cannot
tell converts a transient X hiccup into a silently dead session — trap 3's
failure mode, arrived at by a different route. Unknown results are logged and
counted. `focus_guard.on_unknown` may be set to `suppress` where protecting the
operator's files matters more than completing the run.

**D5. Check at most once per `ttl_s`, not per keystroke.** A burst of presses
inside one tick shares a single answer. Session PIDs are cached separately and
refreshed less often, since they change only when the game restarts.

**D6. The guard is off by default** (`focus_guard.enabled: false`) until it has
run alongside a full session without suppressing legitimate injection. Turning
it on is a one-line config change once that evidence exists.

> **Amended 2026-09-05.** That evidence now exists and the guard ships enabled:
> `wingman/config.yaml` carries `focus_guard: {enabled: true, ...}`. The
> condition D6 set was met by the ADR 099 nested lane, where the guard follows
> injection to `:3` and the game is the only client — so it reports the game
> focused and has nothing to suppress. Measured over the 2026-09-05 21:58
> session: **zero** `FocusGuard` suppressions. D6's default is kept here as the
> decision that was made, not as a description of the shipped config; ADR 099 D7
> and D8 govern the guard's current role.

## Consequences

Injection stops when the operator alt-tabs, which is the point. The cost is one
focus query per tick and the risk that a mis-scoped session definition
suppresses legitimate presses — bounded by D4 and D6, and detectable because
every suppression is logged with the window that caused it.

This does not make wingman safe to leave running while working in another
window. It removes the keystrokes, not the screen capture or the mouse warp
paths that ADR 044 and ADR 045 exercise. The honest summary is that it closes
the specific hazard that corrupted the operator's text on 2026-08-28.

## Validation

- V1. Unit: a window owned by a session process passes; the editor, a window titled like the game, and an unidentifiable window are all suppressed.
- V2. Unit: with the game not running, no window is treated as the game.
- V3. Unit: the session walk terminates on a malformed or cyclic process tree.
- V4. Unit: an unresolvable check injects by default and suppresses under `on_unknown: suppress`, and both paths are logged.
- V5. Unit: hotkey registration and observation paths are never gated — only injection.
- V6. Live: run a session with the guard enabled, alt-tab away, and confirm the log shows suppression while focus is elsewhere and normal injection when it returns, with the round count unaffected.

## References

- ADR 091 — the shared XTest display, the other consumer of X state in the injection path
- ADR 094 — `find_game_pids`, the `/proc` scan this reuses to identify the game
- `scripts/focus-probe.py` — the experiment that verified the mechanism
- Evidence: `wingman.log` and `focus-probe.log`, 2026-08-28 10:24 through 10:28
