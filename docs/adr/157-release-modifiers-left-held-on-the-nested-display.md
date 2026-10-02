# ADR 157 — Release Modifiers Left Held on the Nested Display

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-10-02 | 1.9.0           |

Extends [ADR 099](099-nested-display-lane-for-unattended-operation.md). Evidence:
[Anomaly 010](../anomaly/010-enter-frames-the-game-window-after-alt-tab.md).

## Decision

**D1. Wingman releases a modifier key that stays held on the nested display for
more than 1.5 s.** `StuckModifierGuard` (`wingman/input_linux.py`) polls the
nested server's key state every 0.5 s on its own daemon thread. When Alt, Meta,
Super, Control or Shift (either side) has been down for 1.5 s, it sends an XTest
`KeyRelease` for that key on the shared injection connection and logs
`XKey: released Alt_L on :3 — held 1.6s with no release`.

**D2. The release is a real key event, sent while the key is still down in X.**
That is the only window in which the game can be told. Once the operator
returns, Xwayland clears its own state without sending anything.

**D3. `nested.release_stuck_modifiers` (default true) turns it off.**

## Consequences

- The operator can Alt+Tab away from the game window and come back by any means
  without their next Enter toggling the game's window mode.
- Wingman's own injected keys are not sent with a stray modifier while the
  operator is away. Before this, every key it pressed in that time carried Alt.
- A modifier the operator deliberately holds in the game window for more than
  1.5 s is released under them. Wingman binds no modifier
  (`wingman/keybindings.py`), so this affects only bindings the operator added.
- Not covered: leaving and returning within 1.5 s. The key is then cleared
  silently before the guard acts, and Wine keeps it. A real press and release of
  the key in the game window clears that, as does Alt+Tab back in.

## Why

Rootful Xwayland 24.1.10 does not release held keys when keyboard focus leaves
its window: `keyboard_handle_leave` calls `xwl_seat_leave_kbd` only when
rootless. On the next `keyboard_handle_enter` it queues `LeaveNotify` key
events for the stale keys, which update the server's state without a
`KeyRelease` reaching any client. Wine therefore still believes the key is
down. The game toggles borderless and framed on Alt+Enter, and a framed window
puts its content at +4,+30, which shifts every OCR crop and click point.

Measured on VEDA, 2026-10-02 (watcher on `:3`, wingman log):

```
07:00:53  Alt_L held on :3 for 1.5s and counting (mask 0x18)
07:00:58  Alt_L came up on :3 after 6.3s
07:01:28  maneuver key 'enter' pressed … [source=':3' (nested, game focused) mods=none]
07:01:28  game window (1920, 1200) -> (1928, 1234) (FRAMED)
07:01:39  Alt_L held on :3 for 1.5s and counting
07:02:02  Alt_L came up on :3 after 24.1s        (an XTest release, sent by hand)
```

A bare Enter with `mods=none` framed the window in the same second, 30 s after
Alt had been left down for 6.3 s. An XTest Alt+Enter toggled it back
(1928x1234 to 1920x1200), and bare Enter had no effect once Wine had seen a
real Alt release.

The guard on a scratch `Xwayland :9`: Alt_L pressed and never released came up
after 2.0 s, the focused window received `KeyRelease` 64, and a held `e`
(keycode 26, as wingman holds afterburner) stayed down.

## Validation

- [x] Mechanism: the timeline above and the 24.1.10 source.
- [x] The real guard on a scratch Xwayland, as above.
- [x] `tests/test_stuck_modifier_guard.py`: release past the threshold, a normal
      chord untouched, the timer restarting per press, wingman's held keys
      untouched, every host-shortcut modifier covered, a failing display, a
      prompt stop.
- [ ] Live: the log shows `XKey: released Alt_L on :3` after the operator
      Alt+Tabs away, and an Enter in the game window afterwards does not frame it.
