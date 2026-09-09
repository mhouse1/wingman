# ADR 104 — Nested Display: Placement on Launch

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-09-02 | 1.8.8           |

> **Revised 2026-09-02.** The first version hardcoded `-fullscreen`, which is
> right on Impulse and wrong on VEDA. Placement is now derived from the host
> output. See *Decision — revised*.

## Context

ADR 099 put the game on its own rootful Xwayland server (`:3` by default) so it
is always focused and gives Wingman a real framebuffer to capture. `scripts/
nested-display.py` brings that server up with:

```
Xwayland :3 -geometry 1920x1200 -decorate
```

`-geometry` fixes the server's own internal virtual-screen size — the pixel
buffer Wingman actually captures, and what every crop coordinate in
`wingman/config.yaml` is calibrated against. `-decorate` only affects how the
*host* compositor frames the outer window; it has no effect on the captured
pixels.

Nothing in `start()` ever asked the host compositor to place that outer window
fullscreen. On VEDA, opening a new 1920x1200 window happened to read as
filling its external KVM monitor, so this was never noticed. Deploying to
Impulse (docs/job-aids/010, foundry job-aid 001) surfaced it: on Impulse's
laptop panel the same un-requested placement left the window floating and
visibly cut off (reported 2026-09-01).

Xwayland has a first-class flag for exactly this — `-fullscreen`, documented
in `man Xwayland`: "Set the Xwayland window fullscreen when running rootful."
Adding it was not a drop-in change: Xwayland refuses to start at all with both
`-decorate` and `-fullscreen` present —

```
error, cannot use the decorate option when running fullscreen
(EE) Couldn't add screen
```

— confirmed by actually trying the combination and watching the server die
before answering on the display, rather than assuming from the man page alone.
`-decorate` was never discussed or justified in ADR 099 or HLDD 009; nothing
in this codebase relies on the outer window showing a title bar, so dropping
it costs nothing — a fullscreen window has no decorations to draw regardless.

## Decision — revised

Hardcoding `-fullscreen` traded one machine's problem for another's. VEDA runs a
3840x1600 desktop and is where the operator works; a fullscreen nested window
covers the screen they are using, which is precisely what ADR 099 and HLDD 009
exist to prevent. Impulse's laptop panel is no larger than the framebuffer and
has nowhere to put a window, so the default placement left it floating and cut
off.

**Choose the placement from the host output, not from a flag.**

```
fullscreen  iff  host_output fits within the requested -geometry
```

- **VEDA** — host 3840x1600, geometry 1920x1200 → `-decorate` (windowed). There
  is room beside the operator's work.
- **Impulse** — host 1920x1080, geometry 1920x1200 → `-fullscreen`. There is not.
- **Host unknown** → `-decorate`.

The rule is not "how big is the monitor" but "is there room to put this window
beside the operator's work", which is the question that actually differs between
the two machines. Both axes must fit, so an ultrawide that is shorter than the
framebuffer still counts as having room.

**Unknown host means windowed, deliberately.** The costs are not symmetric: a
missed fullscreen is a mispositioned window on a machine nobody is watching,
while a wrong fullscreen takes over the screen of someone who is working.

**The host size is read from `/sys/class/drm`, not from X.** `start()` runs from
a make recipe with no `XAUTHORITY`, where querying the operator's display fails
outright with *"Authorization required, but no authorization protocol
specified"* — confirmed on VEDA rather than assumed. sysfs needs no auth and is
indifferent to whether the session is X or Wayland. The largest connected output
wins: on a docked laptop the question is whether ANY screen has room, and the
external monitor is the one that does.

`-geometry` is unchanged — the server's internal framebuffer size, and therefore
capture resolution and crop calibration, are unaffected by how the compositor
visually presents the window. A wrong placement decision costs window placement,
never captured pixels.

`-decorate` and `-fullscreen` remain mutually exclusive: Xwayland exits with
"cannot use the decorate option when running fullscreen" and never answers on
the display. The two are selected as alternatives, never combined.

## Consequences

**Positive:**
- The nested display window now requests fullscreen placement on launch
  instead of relying on incidental default window placement, which should
  make behavior consistent across machines with different monitor
  configurations rather than depending on what a given compositor happens to
  do with a freshly-opened window.
- Confirmed the server starts cleanly with the new flags (previously fatal
  with `-decorate` + `-fullscreen` together); `tests/test_nested_display.py`
  still passes (no test pinned the exact argument list).

**Negative / open questions:**
- The *decision* is verified against real hardware — VEDA reports 3840x1600 from
  sysfs and resolves to windowed, and the laptop case resolves to fullscreen —
  but the *result* is still not visually confirmed. Nothing here has watched
  Impulse's panel actually fill. Needs a human check on the next live run there.
- `modes` line one is the preferred mode, which is the active one on every
  normal setup but not guaranteed if the resolution was changed by hand.
- Multi-monitor VEDA-like setups are decided by the largest output. A machine
  whose largest screen is big but whose game window opens on a small secondary
  would be windowed when fullscreen might suit it better. Not observed; no
  machine in use has that shape.
- If the host compositor ever declines the fullscreen request for some
  reason, there is no fallback placement logic — the window would be left
  wherever the compositor defaults to, same as before this change, just
  without `-decorate`'s title bar to manually drag/resize it by.

## Files Changed

| File | Change |
|---|---|
| `scripts/nested-display.py` | `host_output_size()`, `should_fullscreen()`; `start()` selects `-fullscreen` or `-decorate` from the host output |
| `tests/test_nested_display.py` | 9 tests: both machines' geometries, exact match, ultrawide, unknown host, unparseable geometry, sysfs parsing, largest-output, unreadable sysfs |

## References

- [ADR 099](099-nested-display-lane-for-unattended-operation.md) — nested display lane, original design
- [HLDD 009](../hldd/009-nested-display-isolation-hldd.md) — nested display isolation architecture
- Wingman `docs/job-aids/010-run-metalstorm-on-linux.md` — general Linux provisioning guide
- foundry `metalstorm-config/docs/job-aid/001-deploy-on-impulse.md` — the deployment that surfaced this
- `man Xwayland` — `-fullscreen` and `-decorate` flag documentation, including their mutual incompatibility
