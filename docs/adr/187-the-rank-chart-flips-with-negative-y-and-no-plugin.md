# ADR 187 — The rank chart flips with negative-Y, and no plugin

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-11-17 | 1.9.5           |

## Decision

Operator, 2026-11-17: "change the 'Squadron rank per reading' chart y axis to go from high to
low instead of low to high reversing the y axis direction, this allows the chart to align with
'Squadron trophies per reading'". Then, once the cost was known: "the decision is to not
install extra plugins and complexity, we'll keep the chart as is for now with the negative y
axis display".

- **D1. The rank chart is flipped with Grafana's own `negative-Y` transform.** The panel sets
  `fieldConfig.defaults.custom.transform: "negative-Y"` in
  `grafana/dashboards/score-per-round.json`. The line climbs as the squadron climbs, as the
  trophies line beside it does. The query is unchanged.
- **D2. The axis labels keep their minus sign** (-615 to -665). The tooltip shows the real
  rank (616), and the panel's description says why the axis reads negative.
- **D3. No Grafana plugin is installed for this.** The dashboard stays on core Grafana panels.

## Consequences

- The two squadron charts read the same way: up is better on both.
- A reader of the rank axis drops the sign in their head. The real figure is one hover away.
- The stack keeps no plugin to download, pin, or update with each Grafana upgrade. Every panel
  is still plain JSON settings, with no panel-side script.
- `tests/test_round_scores.py` holds the flip: the rank panel has `negative-Y` and the trophies
  panel does not.

## Context

Grafana 12.2's time-series panel has no reversed-axis setting. `negative-Y` is its only flip:
it multiplies each value by -1 before plotting and keeps the real value for the tooltip. The
axis labels are formatted from the plotted numbers, so they carry the sign.

Each way to strip the sign was tried on the live dashboard (rendered in headless Chromium,
2026-11-17) and failed:

| Tried | Result |
|-------|--------|
| A regex value mapping `^-(.*)$` to `$1` | The axis ignores value mappings; the labels kept the minus |
| A unit format | No format in `getValueFormat` (`prefix:`, `suffix:`, `si:`, `count:`, `currency:`, `bool:`, `time:`) drops a sign |
| Per-point labels (`showValues`) with the axis hidden | The labels showed -616, and hiding the axis in the defaults also hid the time axis |
| `min` above `max` (665 and 610) with no transform | Grafana swaps them: positive labels, but the axis is not reversed |

A CSV or other data source would not help either. The labels come from the renderer, whatever
the data source.

## Alternatives considered

- **Business Charts (`volkovlabs-echarts-panel`)**: Apache ECharts with a panel-side JavaScript
  function, where `yAxis.inverse: true` gives a truly reversed axis with positive labels.
  Rejected (D3). It adds a downloaded plugin to `grafana/compose.yaml`, a script to maintain in
  the panel, and a check that the change markers still draw.
- **Hide the rank axis numbers** with an override on the rank field only, and read the rank by
  hovering. Not chosen: the chart would lose its scale.
- **Undo the flip.** Rejected: the two squadron charts would move in opposite directions on a
  good day.

Revisit if Grafana adds a reversed axis to the time-series panel, or if the dashboard takes on
a plugin for another reason.

## Validation

- [x] 2026-11-17: the rank chart, rendered, climbs where the trophies chart climbs (10/31 to
  11/02, the 11/10 dip, the 11/16 peak).
- [x] 2026-11-17: hovering the 11/16 09:46 reading shows `rank 616`.
- [x] `tests/test_round_scores.py` passes (91 tests).

## References

- Design 018: [`docs/hldd/018-score-per-round-in-grafana-hldd.md`](../hldd/018-score-per-round-in-grafana-hldd.md)
- ADR 166: [`docs/adr/166-read-the-squadron-leaderboard-from-the-lobby.md`](166-read-the-squadron-leaderboard-from-the-lobby.md)
