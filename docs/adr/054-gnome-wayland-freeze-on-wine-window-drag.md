# ADR 054 — GNOME Wayland Freeze on Wine Virtual Desktop Window Drag

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft | 2026-06-16 | 1.6.21          |

## Context

On the Linux host (GNOME Wayland, Ubuntu 24.04 — see [ADR 049](049-linux-migration-game-and-automation-layer.md) and [ADR 053](053-linux-one-command-launch.md) for the rest of the Linux porting work), manually dragging the MetalStorm Wine virtual desktop window by its GNOME title bar caused a full desktop freeze requiring a hard power-cycle. This is a host environment issue, not a Wingman code bug, but it directly affects anyone following the Linux setup in ADR 049/053, so it is documented here as a reference for future Linux porting work.

## Investigation

`journalctl` across the affected boot showed no GPU hang/reset, no OOM kill, and no kernel panic — the kernel log is clean. The only anomaly:

```
07:21:37 gnome-shell: Window manager warning: Window 0xe00009 sets an MWM hint indicating
         it isn't resizable, but sets min size 1 x 1 and max size 2147483647 x 2147483647;
         this doesn't make much sense.
07:21:37 gnome-shell: Window manager warning: Buggy client sent a _NET_ACTIVE_WINDOW
         message with a timestamp of 0 for 0xe00009
07:24:11 gnome-shell: libinput error: event3 - Logitech USB Receiver: client bug: event
         processing lagging behind by 1844ms, your system is too slow
[journal goes quiet except kernel-level UFW netfilter log lines, which keep logging
 even when userspace is fully hung]
07:26:29 kernel: usb 1-5: USB disconnect, device number 3
[boot ends abruptly — no shutdown-target reached]
07:27:22 [next boot starts, ~53s later — consistent with a hard power-cycle]
```

The MWM-hint warning is a red herring on its own — grepping the full journal across multiple boots (2026-06-13 through 2026-06-16) shows this exact warning firing every time the Wine virtual desktop window is created, with no freeze in most cases. It is routine noise from Wine's window hints, not the trigger by itself.

The real signal is the libinput lag warning, followed by a dead compositor and a non-graceful shutdown. Because GNOME Shell/Mutter is also the Wayland display server, a hang inside Mutter has no fallback — the entire desktop (mouse, keyboard, everything) freezes, which matches "freezes the computer" even though the underlying kernel may still be technically alive.

### Working hypothesis

An interactive move/resize grab in Mutter, triggered by dragging the Wine virtual desktop window's title bar, deadlocks when combined with that window's self-contradictory size hints (`min=1x1`, `max=unbounded`, flagged non-resizable). This is a known category of Mutter/Wayland compositor bug with certain XWayland clients; Wine's virtual desktop window is exactly this kind of client. No kernel-level evidence was found to support a GPU driver crash instead.

## Decision

Two complementary fixes, both implemented in `wingman/move_game_window.py` as direct X11-protocol-level operations that never go through Mutter's interactive move/resize grab path:

**1. Strip the title bar (primary fix — applied automatically).** The drag vector is removing entirely by clearing the window's `_MOTIF_WM_HINTS` decorations field, which removes the title bar Mutter draws around the XWayland surface. With no title bar there is no drag handle to grab.

```
make undecorate-game-window
```

`wait-game` calls this automatically after the game window appears, so every `make r` / `make rd` run is protected without a manual step. This does not block GNOME's default `Super`+drag-anywhere move gesture — true 100% prevention of all move-initiation paths would require a GNOME Shell extension hooking `grab-op-begin`, which was judged disproportionate effort for a session-specific safety net. Removing the title bar eliminates the vector that actually caused the freeze (manual title-bar drag).

**2. Reposition programmatically when actually needed.** If the window does need to move (e.g. multi-monitor layout changes), do it via a direct `ConfigureWindow` request instead of a drag:

```
make move-game-window X=100 Y=100
```

Both operations:

1. Locate the window via `xwininfo -root -tree` (same matching logic as `capture.py:_detect_via_x11`).
2. Connect via `python-xlib`, reusing `controller.py:_ensure_xauthority()` for the XWayland auth wildcard fix (see ADR 053, Issue 7).
3. Issue a single protocol-level request (`ConfigureWindow` or `ChangeProperty`) — never an interactive grab loop.

Repositioning is rarely needed in the first place: `make r` auto-detects the game window's position via `xwininfo` every run (ADR 053), so the window does not need to be in any particular screen location for Wingman to work.

## Consequences

Positive:

- The freeze vector (manual title-bar drag) is eliminated automatically on every launch, with no workflow change required.
- A safe, scriptable way to reposition the window still exists for the rare case it's needed.
- No new system dependency — reuses the existing `python-xlib` dependency already required for XTest/XRecord.

Trade-offs:

- Undecorating does not block GNOME's `Super`+drag-anywhere gesture, or any other compositor extension that might issue its own interactive grab on this window. The residual risk is small but not zero.
- Root cause is unconfirmed — no kernel-level reproduction was captured (no GPU hang/reset signature), so this is a documented avoidance strategy, not a verified fix of the underlying Mutter/Wayland bug.
- The window loses its title bar permanently while undecorated, which is fine for Wingman's automated workflow (no manual window interaction needed) but means the window can no longer be identified by title in the GNOME Activities overview.

## References

- [ADR 049](049-linux-migration-game-and-automation-layer.md) — Linux migration overview
- [ADR 053](053-linux-one-command-launch.md) — XWayland window detection, XAUTHORITY wildcard fix, full Linux input stack
- `wingman/move_game_window.py` — undecorate and reposition utility
- `wingman/controller.py:_ensure_xauthority()` — reused XAUTHORITY fix
- `wingman/capture.py:_detect_via_x11()` — reused window-matching logic
- `Makefile` — `undecorate-game-window`, `move-game-window`, `wait-game` targets
- `Makefile` — `move-game-window` target
