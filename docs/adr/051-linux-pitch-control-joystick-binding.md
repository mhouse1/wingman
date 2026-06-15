# ADR 051 — Linux Pitch Control: Joystick Binding Required

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Accepted | 2026-06-14 | 1.6.19          |

## Context

After migrating to Linux (ADR 049), the MetalStorm in-game pitch controls (nose-up /
nose-down) did not respond to keyboard input even though roll controls worked correctly.
The issue persisted regardless of which keyboard keys were assigned to pitch.

The same keyboard bindings worked correctly on Windows.

## Investigation

### Symptoms

- Roll (left/right) responded correctly to keyboard keys on Linux.
- Pitch (up/down) did not respond to any keyboard key on Linux.
- Mapping the same key to both roll and pitch confirmed the issue: the key worked for
  roll but not pitch, ruling out key-specific interference (input method, ibus, etc.).

### Attempted fixes that did not help

| Fix | Result |
|---|---|
| `GTK_IM_MODULE=none QT_IM_MODULE=none XMODIFIERS=""` | No effect |
| `SDL_VIDEODRIVER=x11` | No effect |
| Reassigning pitch to different keys (O, L, etc.) | No effect |

### Root cause

MetalStorm uses different input APIs for the two control axes:

- **Roll**: `WM_KEYDOWN` / `GetKeyState` event-based input. Wine forwards these
  correctly through XWayland.
- **Pitch**: DirectInput joystick Y-axis polling (`GetAsyncKeyState` or DirectInput
  analog axis). Under Wine on XWayland, `GetAsyncKeyState` uses `XQueryKeymap` to
  snapshot key state; this snapshot is frequently stale or zeroed out under XWayland,
  so pitch reads as "not pressed" even when a key is held.

The keyboard pitch binding is effectively non-functional on Linux because Wine's async
key-state path through XWayland does not reliably reflect held keys for axes the game
polls continuously.

## Decision

Configure MetalStorm's pitch control using the **joystick/controller binding** rather
than keyboard binding. MetalStorm exposes a separate joystick input path for pitch that
goes through DirectInput device enumeration rather than `XQueryKeymap`, which Wine
handles correctly on Linux.

## Implementation

In MetalStorm's in-game control settings:

1. Open **Settings → Controls**.
2. Switch the pitch axis binding from **Keyboard** to **Joystick / Controller**.
3. Assign the desired keys or axes under the joystick binding section.

No changes to Wingman, Proton, or system configuration are required.

The environment variables added during investigation
(`GTK_IM_MODULE`, `QT_IM_MODULE`, `XMODIFIERS`, `SDL_VIDEODRIVER`) were removed from
the Heroic game config once the root cause was confirmed — they had no effect and are
not needed.

## Consequences

- Pitch control works correctly on Linux via the joystick binding path.
- Roll control continues to use keyboard binding (no change needed).
- This configuration difference (joystick pitch / keyboard roll) is Linux-only; the
  Windows setup is unaffected.
- If the Heroic prefix is recreated, the in-game control configuration must be
  re-applied (it is stored in the game's own save/config, not in the Wine prefix).

## References

- ADR 049 — Linux migration: game launcher and automation layer
- ADR 050 — Wayland screen capture
- MetalStorm controls configuration: in-game Settings → Controls
