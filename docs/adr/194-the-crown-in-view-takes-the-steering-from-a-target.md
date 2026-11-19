# ADR 194 — The crown in view takes the steering from a target

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-11-19 | 1.9.5           |

Replaces, for the priority target's crown only, the rule that the marker and a visible
target are weighed by distance from the screen centre (operator, 2026-10-02). The
resupply marker and the air superiority control points keep that rule.

## Decision

Operator, 2026-11-19, on a `v` screenshot of the crown flown past: "why didnt it pursuit
the priority target?"

- **D1. The crown's marker in view has the steering over an ordinary target**, whatever
  their distances from the centre. Firing carries on: the target still gets the gun and
  the missiles when it crosses the crosshair.
- **D2. Unchanged:** the resupply marker still comes first when it is proposed. With every
  rack empty the crown is still flown at. A control point is still weighed against a
  target by distance from the centre.

## Consequences

- The comparison could not recover once the target won it. The chase keeps its target
  centred, so the target always measured nearer than the marker, and the crown slid off
  the screen.
- A target near the crosshair is no longer chased while the crown is in view. It is fired
  at as it passes.

## Evidence

`wingman.log`, 2026-11-19 (the operator's `v` screenshot at 04:54:18):

```
04:53:58,359 PRIORITY TARGET: marker in view at (872,652) (steering to it)
04:54:01,730 TARGETPROJ: at=(915,160) marker=(986,556) v=(-12,-79)px/s km=13.60
04:54:01,772 PRIORITY: marker=(936,548) stale=True ... proposed=False control=False
04:54:02,485 PRIORITY: marker=(934,626) stale=False ... proposed=False control=False size=16x16
04:54:18,271 PRIORITY: marker=(454,1034) stale=False ... proposed=False control=False size=49x49
04:54:19,474 OBJECTIVE: the priority target lost at 50 px, 737 px off the centre, largest 50 px, after 103 sightings in 17.0s — passed beside it
```

- 04:54:02: the F-14 13 km out was about 30 px from the centre, the crown 37 px. The
  target won by 7 px.
- The crown stayed in view on every picture for 17 s and grew from 16 to 49 px, with
  `control=False` each time.

## Validation

- [x] `tests/test_pursuit_mode.py::test_the_crown_beside_a_centred_target_takes_the_steering`
      replays 04:54:02: crown 37 px off the centre, target 30 px, the crown is steered at.
- [x] `test_the_crown_in_view_takes_the_steering_from_a_target_nearer_the_centre`: a
      target 190 px off the centre, the crown 740 px, the crown is steered at and firing
      goes on.
- [ ] Live: with a target in view, `PRIORITY: marker=… control=True` while the crown is in
      view, and the `OBJECTIVE` line names it flown through rather than passed beside.
