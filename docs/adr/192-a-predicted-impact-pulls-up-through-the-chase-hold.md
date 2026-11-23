# ADR 192 — A predicted impact pulls up through the chase hold

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-11-18 | 1.9.5           |

Amends [ADR 181](181-the-chase-stays-on-its-target-through-missile-and-terrain-warnings.md)
(Draft). ADR 181's other holds are unchanged.

## Decision

Operator, 2026-11-18, choosing from the options: "pull up when impact
predicted". With the target in view, a severe pull-out alarm still starts the
crash recovery, a pull-up with no turn. The terrain turn, the missile evade and
the two-agreeing-warnings recovery stay held, as ADR 181 says.

- **D1. Start.** In a `hold` round, a pull-out alarm predicting a lowest point
  under `behavior_tree.climb.pullout.bypass_holdoff_below_m` (300 m) starts the
  recovery with the target in view. This alarm already broke the hold-off for
  every other objective (Code Review 019 cycle 36); the target was the only
  exception. It logs
  `crash recovery breaks the chase hold — a pull-out would bottom out at N m (ADR 192)`.
- **D2. No hand-back while it stands.** ADR 181 handed a flying recovery back
  to the chase as soon as the target came into view. That no longer happens
  while the severe alarm stands. Once the alarm clears, the hand-back works as
  before.

## Consequences

- Fewer terrain deaths in the chase, at the cost of breaking off some chases
  that would have survived. Code Review 019 cycle 36 measured alarms under
  300 m as followed by a crash 24% of the time during a hold-off.
- This is what lets ADR 191's window stand. That window sends lives that see a
  target into the pursuit 350 to 500 m lower, and they died on terrain 5 times
  in 30 under the hold.

## Evidence

The hold withheld the recovery, and the emergency climb got through too late
(2026-11-18, Stormbreak):
```
14:18:39,190 PURSUIT HOLD: crash recovery (a pull-out would bottom out at -646 m) withheld — the chase stays on its target (ADR 181)
14:18:40,632 CLIMB — holding nose up + afterburner (EMERGENCY ...) (target alt 1000, cap 22s)
14:18:45,730 DIED ARMED — cause=terrain
```

Terrain deaths with crash recovery held off in the 15 s before them, all five
sessions of 2026-11-18:

| Session | Terrain deaths | Held off just before | Holds logged |
|---------|----------------|----------------------|--------------|
| 07:53 | 3 | 2 | 96 |
| 10:30 | 3 | 2 | 84 |
| 12:08 | 1 | 1 | 13 |
| 12:42 | 9 | 6 | 53 |
| 13:58 | 5 | 4 | 42 |
| All | 21 | 15 | 288 |

The predicted lowest points the hold withheld, in the last two sessions, ran
from +300 m down to -646 m.

### First live session (operator's, acct1, from 16:08)

ADR 192 does what it says. In the first 13 lives, 6 recoveries broke the chase
hold and none was withheld for a predicted impact. One recovered cleanly at
16:34:08: a -49° dive from 1,241 m bottomed out at 1,049 m and was handed back
after 4.3 s. But 5 of the 13 lives still died on terrain. In two of them the
recovery started early, and the dive got steeper while NOSE_UP was held:
```
16:17:33,789 crash recovery breaks the chase hold — a pull-out would bottom out at -65 m (ADR 192)
16:17:35,214 PADLOCK: OFF | Altitude: 1075 | Speed: 1169 | Nose: -75° (steep_dive)
16:17:38,069 PADLOCK: OFF | Altitude: 367 | Speed: 712 | Nose: -90° (steep_dive)
16:18:32,184 crash recovery breaks the chase hold — a pull-out would bottom out at 113 m (ADR 192)
16:18:33,614 PADLOCK: OFF | Altitude: 748 | Speed: 909 | Nose: -62° (steep_dive)
```

What might cause it (inferred): the pursuit left the aircraft rolled or
inverted, and NOSE_UP pulled it further toward the ground. Wingman reads no
roll angle, so nothing levels the wings before the pull. That is the next
blocker after the hold. Research 020 found pull-out strength was not the gap,
which fits: the pull goes the wrong way. Other cases in the same session: a
recovery stopped after 1.4 s (16:12:38), and an aircraft stuck in terrain at
4 to 28 kph (16:24).

**Not a verdict on ADR 192.** This session flew the **SU-34 Fullback**, and no
November log has it. By 16:51, 10 of its 13 deaths were terrain. In November,
every jet with 15 or more deaths ran 16 to 36% terrain (F-100 Super Sabre
17% of 637, J35 Draken 21% of 304, F-15 Eagle 24% of 251). Compared on the
same maps, Crucible had 3 terrain deaths in 17 and Countdown 1 in 11 on other
jets earlier the same day. The pull-out prediction uses nose-up rates
measured on other jets, so for this heavy strike jet it is likely
optimistic (inferred). Judge ADR 192 on a jet with history.

## Validation

- [ ] Live: `breaks the chase hold` logged, and terrain deaths per life with
      the target in view fall from 15 of 21 deaths held off.
- [ ] Score per round against the ADR 181 arm. Rounds are noisy (an SD of about
      200), so judge it over randomized rounds within a session.
