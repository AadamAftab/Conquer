"""
brain.py — Fast Tactical State Machine (FSM) for the Mech Royale champion bot.

Core states:
  AMBUSH_HUNTER    — Active combat whenever an enemy is visible or recently tracked.
                      Executes reverse-kiting, focus fire on the weakest bot, and
                      continuous damage output.
  BOOTSTRAP_LOOT   — Zone-rotation & resource gathering when no enemies are in sight.
                      Advances toward next_zone center while picking up power cubes,
                      weapons, and breaking crates.
  DISENGAGE_REGEN  — High-urgency evasion when critically low HP (< 350) and facing
                      multiple opponents. Seeks safety toward zone center.
  ENDGAME_SURVIVAL — Final circle chaos (< 70m radius). High-intensity survival.
"""

from __future__ import annotations

import math
from typing import Optional, TYPE_CHECKING

import config as C
import mechfight as mf

if TYPE_CHECKING:
    from tracker import EpistemicTracker
    from world import WorldIndex, Rooftop


class Brain:
    BOOTSTRAP_LOOT    = "BOOTSTRAP_LOOT"
    ASCEND_ROOFTOP    = "ASCEND_ROOFTOP"
    AMBUSH_HUNTER     = "AMBUSH_HUNTER"
    DISENGAGE_REGEN   = "DISENGAGE_REGEN"
    ZONE_ROTATION     = "ZONE_ROTATION"
    ENDGAME_SURVIVAL  = "ENDGAME_SURVIVAL"

    def __init__(self) -> None:
        self.state: str = self.BOOTSTRAP_LOOT
        self._rooftop_target: Optional["Rooftop"] = None
        self._ticks_in_state: int = 0
        self._prev_state: str = self.BOOTSTRAP_LOOT

    def decide_state(
        self,
        obs: dict,
        tracker: "EpistemicTracker",
        world_index: "WorldIndex",
        is_on_roof: bool,
    ) -> str:
        me = obs["self"]
        hp = me.get("hp", C.HP_DEFAULT)
        zone = obs["zone"]
        next_zone_r = zone["next_radius"]

        threat_count = tracker.threat_count()
        any_visible = tracker.any_visible()
        self._ticks_in_state += 1

        # 1. Endgame circle check
        if next_zone_r < C.ENDGAME_ZONE_RADIUS:
            self._transition(self.ENDGAME_SURVIVAL)
            return self.state

        max_hp = me.get("max_hp", C.HP_DEFAULT) or C.HP_DEFAULT
        recent_threat = tracker.recent_mech_threat()

        # 2. Health escape: disengage below DISENGAGE_HP_FRAC of max HP while an
        #    enemy mech is visible (scales with plating / cube HP bonuses)
        if hp < max_hp * C.DISENGAGE_HP_FRAC and any_visible:
            self._transition(self.DISENGAGE_REGEN)
            return self.state

        # 3. Stay disengaged until HP has recovered or no enemy mech has been
        #    seen recently (T0/T1), so we don't re-engage the moment LOS breaks
        if self.state == self.DISENGAGE_REGEN:
            if hp >= max_hp * C.REGEN_RESUME_FRAC or not recent_threat:
                self._transition(self.AMBUSH_HUNTER if any_visible else self.BOOTSTRAP_LOOT)
            return self.state

        # 4. Core combat transition: if ANY enemy is visible or believed nearby, ENGAGE!
        if any_visible or threat_count > 0:
            self._transition(self.AMBUSH_HUNTER)
            return self.state

        # 5. Otherwise, rotate and loot toward safe zone
        self._transition(self.BOOTSTRAP_LOOT)
        return self.state

    def _transition(self, new_state: str) -> None:
        if new_state == self.state:
            return
        mf.log(f"Brain: {self.state} → {new_state} (after {self._ticks_in_state} ticks)")
        self._prev_state = self.state
        self.state = new_state
        self._ticks_in_state = 0

    @property
    def rooftop_target(self) -> Optional["Rooftop"]:
        return self._rooftop_target
