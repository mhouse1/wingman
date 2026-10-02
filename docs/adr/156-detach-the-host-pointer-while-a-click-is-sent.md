# ADR 156 — Detach the Host Pointer Only While a Click Is Sent

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-10-02 | 1.9.0           |

Extends [ADR 099](099-nested-display-lane-for-unattended-operation.md). Evidence:
[Anomaly 009](../anomaly/009-click-to-continue-ignored-after-ubuntu-26-04.md).

## Decision

**D1. A click detaches the nested server's host-pointer devices only while it is
being sent, and only when it would otherwise miss.** `_linux_click` aims and
reads the pointer back before every click. It detaches in two cases:

- the server reports the pointer over no window, which means the operator's
  mouse is somewhere else;
- the pointer is not where it was just sent, which means the operator's mouse
  is over the window and moving, and is steering it.

It then sends one XInput2 `XIChangeHierarchy` request that detaches
`xwayland-pointer`, `xwayland-relative-pointer` and `xwayland-pointer-gestures`
from the Virtual core pointer (`wingman/pointer_isolation.py`, through libXi),
re-aims, clicks, and reattaches them in a `finally` block. With the operator's
mouse at rest over the window, nothing is touched.

**D2. The operator's mouse keeps working in the game window.** It is the
behaviour before the Ubuntu 26.04 upgrade and the operator requires it
(2026-10-02). A first version of this ADR detached the devices for the whole
session; the operator's clicks then did nothing on the podium screen, and it
was withdrawn the same day.

**D3. Detach, never disable.** Disabling those devices also restores click
delivery, and it crashes the nested server the next time the operator's mouse
crosses its window. The module contains no "Device Enabled" write, and a test
pins that.

**D4. Overlapping clicks share one detach.** The lobby scanner clicks from its
own thread while the main loop can be mid-burst. A counted hold detaches on the
first click that needs it and reattaches when the last one finishes. The lock
uses `acquire(timeout=2.0)`; on timeout the click goes out without detaching.

**D5. `main` reattaches at startup and at shutdown.** A session killed
mid-click can leave the devices detached on a server that outlives it.

**D6. `nested.isolate_pointer` (default true) turns D1 off.** Each click then
logs a WARNING when the pointer is over no window, and lands only while the
operator's mouse is over the nested window.

## Consequences

- Wingman's clicks reach the game wherever the operator's mouse is.
- The operator's mouse works in the game window, except during the moments a
  click is being sent while their mouse is elsewhere: about 0.2 s for a single
  click and about 3.5 s for the seven-click end-of-match burst. A mouse that
  enters the window inside that span does nothing until the click finishes.
- Each such click costs two short libXi connections, about 0.2 s in total
  measured on VEDA.
- Nothing changes on the on-screen lane (`NESTED=0`), on Windows, or under
  Xwayland builds without the check: there the pointer is over a window and no
  device is touched.

## Why

After the Ubuntu 26.04 upgrade (Xwayland 24.1.10), "Click to Continue" bursts
that cleared the podium in about 10 s (about 2,570 episodes, 2026-09-13 to
09-28) stopped working: 1 of 11 episodes cleared on the first burst and three
never cleared. Clicks were on target and reached no window.

The rule is in `hw/xwayland/xwayland-input.c` (24.1.10). `xwl_xy_to_window`
returns the root window when `sprite_check_lost_focus` is true, and that
function now ends its non-Wayland-device branches with:

```c
pointer_crossing = (xwl_seat->pointer_enter_count > 0);
...
if (master->lastSlave != get_pointer_device(xwl_seat))
    return !pointer_crossing;
```

An XTest event's last slave is the XTEST pointer, so the pointer window is the
root unless the host pointer is inside an Xwayland surface. The function returns
FALSE earlier when no `xwayland-pointer` device shares the sprite being checked,
and a floating slave has a sprite of its own (`AttachDevice`: "If device is set
to floating, we need to create a sprite for it").

Controlled A/B/A on a fresh `Xwayland :9` with one test window, 2026-10-02 05:26:

| Host pointer devices | `QueryPointer` child | XTest click delivered |
|---|---|---|
| attached (as started) | none | nothing |
| detached | the window | ButtonPress, ButtonRelease |
| reattached | none | nothing |

**Why not disable (D2).** The first version of this fix set "Device Enabled" to
0. It cleared the live podium at 05:10:28 after 81 failed bursts, and at
05:21:48 the nested server died:

```
(EE) Segmentation fault at address 0x28
(EE) 8: libwayland-client.so.0 ... 4: Xwayland ... 3: Xwayland
Fatal server error: Caught signal 11 (Segmentation fault). Server aborting
```

`DisableDevice` calls `FreeSprite(dev)`, and `pointer_handle_enter` then runs
`CheckMotion(&enter, GetMaster(dev, POINTER_OR_FLOAT))` on the device when the
host pointer enters. The game and the session went down with the server.

Other alternatives measured and rejected: XTest relative moves and
`XWarpPointer` (pointer window stays the root), `XSendEvent` button events to
the game's windows (Wine ignores them), and Return or Space on the podium.

## Validation

- [x] Mechanism: the A/B/A table above, and the 24.1.10 source.
- [x] The real `_linux_click` on a scratch `Xwayland :9`, 2026-10-02: isolation
      off, 0 of 2 clicks delivered; per-click isolation, 20 of 20 single clicks
      and 7 of 7 in a burst delivered, the three devices attached again after
      every click, no server error.
- [x] `tests/test_pointer_isolation.py` and the click-path tests in
      `tests/test_input_linux.py`: mouse elsewhere, mouse over the window, mouse
      leaving mid-burst, a click failing part-way, overlapping clicks, isolation off.
- [x] Whole-session detach (the withdrawn version), operator's session
      2026-10-02 05:30: four match ends, each cleared by its first burst.
- [x] Per-click version live, VEDA 2026-10-02 06:09 (12m 34s, 2 missions): both
      match ends cleared by their first burst; the first PLAY click, the
      FINAL_CONTINUE popups and two REVEAL_ALL popups all landed, each logged
      `(host pointer detached for this click)`; no `pointer is over no window`
      warning, no lock timeout; devices attached between clicks.
- [x] The operator's mouse works in the game window on the per-click version
      (operator, 2026-10-02), and the nested server stayed up with their mouse
      in the window.
- [x] The second detach case was added after the operator's 06:25 session: a
      PLAY click read back at (210, 382), then (360, 486) after a re-aim, and
      went out there with their mouse moving in the window. Wingman then sat in
      `GAME_WAITING` with PLAY still on screen and refused `m` seven times.
      `test_click_wins_over_the_operators_mouse_moving_in_the_window` reproduces it.
- [x] Live, VEDA 2026-10-02: a READY click at 07:10:30 logged `detached for this
      click: operator's mouse had it at (1320,39)`, landed on target and started
      matchmaking; a seven-click end-of-match burst at 07:15:51 logged the same
      reason with all seven clicks at (939,1094).
- [ ] At least 10 match ends on the per-click version, each cleared by its
      first burst (2 so far). Move this ADR to `Accepted` when recorded.
