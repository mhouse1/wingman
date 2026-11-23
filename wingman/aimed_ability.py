"""A special ability fired on its lead diamond (ADR 186).

Operator, 2026-11-17, flying the F-106: the jet "has a single use nuke [that]
needs the boresight to align with the diamond before launching ... when nuke is
available it should select the target closest to the center of the screen". And:
"the special abilities should be built agnostic to jet type so same jets with the
same abilities uses the same implementation."

So an ability is a row in `special_weapon.abilities` (its glyph and its trigger),
and every jet whose HUD draws that glyph gets the same rule. This module is the
rule for the `lead_aligned` trigger, whatever the ability:

- While the special weapon's icon reads READY, the game draws a lead diamond on
  each enemy in reach (yellow for the nuke; `lead` in the row). On the operator's
  screenshots of 02:39-02:41 there were zero, one or two at a time.
- The diamond nearest the crosshair is the target: the one closest to the centre
  of the screen. It must be the nearest on two pictures in a row (RocketLead's
  confirmation), so a target change needs the new one to stay nearest.
- The pursuit steers the crosshair onto it, ahead of every other aim source.
- `q` goes out once it is within `fire_px` of the crosshair, through the same
  press policy as every other trigger (SpecialWeaponPresser: READY only, one retry
  after `press_retry_s` if the icon does not answer).

No key I/O here. The Controller owns the press and the steering; this module
answers "where is the aim" and "press now?" for one picture.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from .config_schema import schema_default
from .lead_ring import crosshair
from .rocket_lead import LEAD_COLOURS, Diamond, RocketLead
from .special_weapon import SpecialWeaponPresser

logger = logging.getLogger(__name__)

# Diamonds kept per picture: one per enemy in reach, and the screenshots showed two.
_MOST_DIAMONDS = 6


def _setting(cfg: dict, key: str):
    value = (cfg.get("aimed") or {}).get(key)
    return schema_default(f"special_weapon.aimed.{key}") if value is None else value


@dataclass(frozen=True)
class AimView:
    """One picture's aim: the diamond, its steering error in the tracker's
    half-frame units, and its distance from the crosshair in px at 1200 high."""

    diamond: Diamond
    err: float
    err_y: float
    px: float


class AimedAbility:
    """The lead_aligned rule for whichever ability the jet carries. Used on the
    pursuit's steering thread only."""

    def __init__(self, cfg: "dict | None", crosshair_pct):
        cfg = cfg or {}
        self.enabled = bool(_setting(cfg, "enabled"))
        self._search_px = float(_setting(cfg, "search_px"))
        self._min_score = float(_setting(cfg, "min_score"))
        self._memory_s = float(_setting(cfg, "memory_s"))
        self.fire_px = float(_setting(cfg, "fire_px"))
        self._crosshair_pct = tuple(crosshair_pct)
        self._presser = SpecialWeaponPresser.from_config("lead_aligned", cfg)
        self._leads: "dict[str, RocketLead]" = {}
        self._active: "RocketLead | None" = None

    def _lead(self, colour: str) -> RocketLead:
        lead = self._leads.get(colour)
        if lead is None:
            lead = RocketLead(self._min_score, self._search_px, self._memory_s,
                              colour=colour, most=_MOST_DIAMONDS)
            self._leads[colour] = lead
        return lead

    @staticmethod
    def applies(profile: "dict | None") -> bool:
        """True for a resolved special-weapon profile this rule answers."""
        return bool(profile) and profile.get("trigger") == "lead_aligned"

    def view(self, frame, picture_ts: float, profile: "dict | None",
             ready: "bool | None") -> "AimView | None":
        """The aim on this picture, or None: no lead_aligned ability, the icon not
        READY, or no confirmed diamond. Searches only while the icon is READY."""
        if not self.enabled or not self.applies(profile) or ready is not True:
            self.reset()
            return None
        colour = profile.get("lead") or "yellow"
        if colour not in LEAD_COLOURS:
            self.reset()
            return None
        lead = self._lead(colour)
        if lead is not self._active:
            self.reset()
            self._active = lead
        h, w = frame.shape[:2]
        cx, cy = crosshair(frame.shape, self._crosshair_pct)
        d = lead.observe(frame, (cx, cy), picture_ts)
        if d is None:
            return None
        return AimView(
            diamond=d,
            err=max(-1.0, min(1.0, (d.x - cx) / (w / 2.0))),
            err_y=max(-1.0, min(1.0, (d.y - cy) / (h / 2.0))),
            px=math.hypot(d.x - cx, d.y - cy) * 1200.0 / h)

    def press_now(self, now: float, ready: "bool | None",
                  view: "AimView | None") -> bool:
        """True when `q` should go out on this picture: the icon READY and the
        diamond within fire_px of the crosshair."""
        aligned = view is not None and view.px <= self.fire_px
        return self._presser.tick(now, ready, aligned)

    def reset(self) -> None:
        if self._active is not None:
            self._active.reset()
        self._active = None
