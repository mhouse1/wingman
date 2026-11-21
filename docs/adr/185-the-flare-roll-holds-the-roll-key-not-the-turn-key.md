# ADR 185 — The flare roll holds the roll key, not the turn key

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-11-16 | 1.9.5           |

## Decision

Operator, 2026-11-16, after two `v` screenshots showed a held flare roll that did not roll:
"the l key turns nose right the roll key is ;".

- **D1. The flare roll holds `;` (`BARREL_ROLL_KEY`).** This supersedes the key choice in
  [ADR 164](164-roll-right-through-the-flares.md) (`ROLL_RIGHT_KEY`, `l`). The hold rules
  from ADR 164 and [ADR 184](184-the-roll-holds-until-the-lock-warning-has-been-gone-2-s.md)
  (when the roll starts, holds and ends) are unchanged.
- **D2. `;` keeps both names.** `YAW_LEFT`, ADR 070's name for it, is now an alias of
  `BARREL_ROLL_KEY`. The game names that binding `Flight/yaw`, but in the Cursor control scheme
  this account uses (`liveControlScheme-KeyboardMouse` = `Cursor`, simulator controls off) it
  rolls the aircraft. `j`/`l` (`Flight/roll`) bank the jet into a turn.
- **D3. `;` is a leased key.** The missile evade (ADR 070) also holds `;`, so the Actuator
  leases it as it already leases `j`/`l`. Whichever of the two lets go first leaves the key
  down for the other.

## Consequences

- The flare roll now rolls instead of turning right, so the chase loses less ground while
  it holds. It still yields pitch and turn while the roll holds (FR-011).
- `;` is not a watched manoeuvre key, so the hold needs no programmatic-key bracket or
  release grace.
- Not changed: ADR 070's evade holds `l` and `;` together, described there as "roll right
  plus left rudder". With these bindings that is a right turn plus a roll. Whether that is
  still what the evade should fly is a separate decision.

## Evidence

On the 2026-11-16 15:58 session (`make r1`, ADR 184 code), I grabbed frames from display
`:3` every 0.2 s through the flare roll that began at `16:29:38,193`, without sending any
input. With `l` held, the wings were level at 0 s, banked about 60° at 0.6 s, and sat at
knife-edge (about 90°) from 1.2 s to 4.9 s, with the horizon level and no further rotation.
The operator's screenshots `screenshot_20261116_162641.png` and `…162643.png`, taken 1.4 s
and 3.1 s into the 16:26:40 roll, show the same knife-edge.

The game's bindings for acct1 (`inputBindingOverrides2` in the Wine prefix registry):
`Flight/roll` is `j`/`l`, `Flight/pitch` is `i`/`k`, and `Flight/yaw` is `;`.

## Implementation

- `wingman/keybindings.py`: `BARREL_ROLL_KEY = ';'`, `YAW_LEFT = BARREL_ROLL_KEY`.
- `wingman/controller.py`: `_run_flare_roll` holds `BARREL_ROLL_KEY`, and the bracket is
  applied only to a watched key. `;` is added to the Actuator's leased keys.
- `tests/test_flare_roll.py`, `tests/test_actuator.py`.
- `docs/requirements/002-functional.sdoc`: FR-011 names the roll control.

## Validation

- [ ] Live: frames through a flare roll show the aircraft rotating past 90° (a full turn
      about its own axis), not holding at knife-edge.
- [ ] Live: no manual takeover and no stuck `;` after a roll.
