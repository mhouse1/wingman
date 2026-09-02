# ADR 104 — Nested Display: Fullscreen on Launch

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-09-02 | 1.8.8           |

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

## Decision

Launch the nested Xwayland server with `-fullscreen` in place of `-decorate`:

```
Xwayland :3 -geometry 1920x1200 -fullscreen
```

`-geometry` is unchanged — the server's internal framebuffer size, and
therefore capture resolution and crop calibration, are unaffected by how the
compositor visually presents the window.

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
- Not visually confirmed end-to-end. The environment this fix was authored in
  has no screenshot tool or `wmctrl`/`xdotool` available, so verification
  stopped at "the server starts and Xwayland's own documentation says
  `-fullscreen` does this" rather than a screenshot showing the game actually
  filling Impulse's panel. Needs a human check on next live run.
- If the host compositor ever declines the fullscreen request for some
  reason, there is no fallback placement logic — the window would be left
  wherever the compositor defaults to, same as before this change, just
  without `-decorate`'s title bar to manually drag/resize it by.

## Files Changed

| File | Change |
|---|---|
| `scripts/nested-display.py` | `start()`: `-decorate` to `-fullscreen` in the `Xwayland` launch args |

## References

- [ADR 099](099-nested-display-lane-for-unattended-operation.md) — nested display lane, original design
- [HLDD 009](../hldd/009-nested-display-isolation-hldd.md) — nested display isolation architecture
- Wingman `docs/job-aids/010-run-metalstorm-on-linux.md` — general Linux provisioning guide
- foundry `metalstorm-config/docs/job-aid/001-deploy-on-impulse.md` — the deployment that surfaced this
- `man Xwayland` — `-fullscreen` and `-decorate` flag documentation, including their mutual incompatibility
