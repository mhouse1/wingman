# ADR 188 — Pair firing is pressed whenever it is available

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-11-17 | 1.9.5           |

## Decision

Operator, 2026-11-17: "implement new special ability for pair firing, it should press
the 'q' key and activate whenever available".

- **D1. Pair firing is a row in `special_weapon.abilities`** (ADR 186 D1):
  `pair_fire: {glyph: test_screenshots/special_weapon/glyphs/pair_fire.png, trigger: ready}`.
  Any jet whose special-weapon icon draws the glyph (a red arrow with a + over a white
  missile) gets the rule. A jet's own `jet_profile.<jet>.special_weapon` still wins.
- **D2. The trigger is `ready`**, the rule the JAS 39 cloak already uses. The battle tick
  presses `q` once the icon reads READY on two reads in a row. If the icon still reads READY
  after `press_retry_s`, the press is repeated, up to `max_presses`. The count re-arms when
  the icon leaves READY, so every recharge is pressed again.
- No new code: the glyph template and the row are the whole change.

## Consequences

- With the glyph known, `q` goes out within about one battle tick (1.5 s) of the icon
  lighting, in GAME_BATTLE and GAME_BATTLE_EJECT, outside the post-respawn grace.
- Before this, the glyph read as `other` (`21:11:01 Special weapon glyph: other (best match
  0.12)`), and wingman never pressed it.

## Evidence

The operator's six `v` screenshots of 21:07 to 21:19, panel weapon "RB 27 Super Falcon"
(saved as `tests/fixtures/pair_fire/*.jpg`):

| Screenshot | Icon | Ring lit (of 16) | Pair-fire glyph overlap |
|---|---|---|---|
| 210714 | ready | 16 | 1.00 (the template) |
| 210717 | dim | 0 | 0.30 |
| 210722 | ready | 16 | 1.00 |
| 210725 | countdown "9" | 1 | 0.10 |
| 211903 | ready, on snow | 9 | 1.00 |
| 211935 | countdown "9" | 0 | 0.10 |

- Every other glyph crop: 0.15 or less (a match needs `glyph_min_iou` 0.85).
- 1,120 saved icon crops in `test_screenshots/special_weapon/`: the 4 at 0.54 or more are
  all this jet's, from tonight; the other 1,116 score 0.3 or less.
- On 211903 the ring reads 9/16 lit against bright snow. The ring reader's covered-sector
  rule (ADR 173) decides READY there; this ADR does not change it.

## Validation

- [ ] Live: `Special weapon glyph: pair_fire` within a few seconds of the icon lighting.
- [ ] Live: `✨ Special weapon pair_fire: trigger ready, icon READY` followed by the icon
      leaving READY (the countdown), on each recharge.
