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
    EVADE             = "EVADE"
    RECOVER           = "RECOVER"

    def __init__(self) -> None:
        self.state: str = self.BOOTSTRAP_LOOT
        self._rooftop_target: Optional["Rooftop"] = None
        self._ticks_in_state: int = 0
        self._prev_state: str = self.BOOTSTRAP_LOOT
        # Opening calm: we deliberately do not engage for the first stretch.
        self._entered_combat: bool = False
        # True while the opening calm is in force this tick. motion.py and
        # combat.py read it: calm means "don't fight mechs, don't reveal
        # ourselves", not just a state label.
        self.calm: bool = False
        self._t_now: float = 0.0
        self._last_kill_t: float = -1e9
        self._attackers: dict = {}          # mech id -> last time it hit us
        self._recover_since: float = 0.0
        self._hold_until: float = 0.0       # minimum retreat time (no flapping)

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
        t = obs.get("t", 0.0)
        alive = int(obs.get("alive", 16) or 16)

        threat_count = tracker.threat_count()
        any_visible = tracker.any_visible()
        self._ticks_in_state += 1
        self._t_now = t
        self.calm = False

        # Did someone actually hit us this tick? That overrides the calm opening:
        # a bot that is being shot at cannot pretend it is alone. Also keep a
        # record of WHO hit us (including mechs we cannot see) and of kills.
        under_fire = False
        for ev in (obs.get("events") or []):
            typ = ev.get("type")
            if typ == "kill":
                self._last_kill_t = t
            elif typ == "damaged" and ev.get("weapon") != "zone":
                under_fire = True
                by = ev.get("by")
                if by is not None and 0 <= int(by) < 100:   # mechs only (NPC ids are 100+)
                    self._attackers[int(by)] = t
        if under_fire:
            self._entered_combat = True
        n_attackers = sum(1 for ts in self._attackers.values()
                          if t - ts < C.ATTACKER_WINDOW_S)

        # 0. Endgame circle check (outranks the calm opening: the circle is the
        #    clock, and by the last phase there is nowhere left to hide).
        if next_zone_r < C.ENDGAME_ZONE_RADIUS:
            self._entered_combat = True
            self._transition(self.ENDGAME_SURVIVAL)
            return self.state

        # 1. Opening calm — placement is the score, and the practice field is a
        #    15-bot squad that hunts the weakest. Don't volunteer as a target:
        #    loot and rotate until the clock says it's time, or until someone
        #    actually starts hurting us.
        if (not self._entered_combat
                and t < C.OPENING_CALM_S
                and hp >= me.get("max_hp", C.HP_DEFAULT) * 0.9):
            self.calm = True
            self._transition(self.BOOTSTRAP_LOOT)
            return self.state

        max_hp = me.get("max_hp", C.HP_DEFAULT) or C.HP_DEFAULT
        recent_threat = tracker.recent_mech_threat()
        pos = me["pos"]
        near_d = tracker.nearest_mech_dist(pos[0], pos[1])

        # 2. Health escape: disengage below DISENGAGE_HP_FRAC of max HP while an
        #    enemy mech is visible (scales with plating / cube HP bonuses)
        if hp < max_hp * C.DISENGAGE_HP_FRAC and any_visible:
            self._entered_combat = True
            self._transition(self.DISENGAGE_REGEN)
            return self.state

        # 2a. Panic: an enemy is inside PANIC_RANGE and we are not comfortable.
        #     Too close to kite from; hard-flee rather than trade point-blank.
        #     Not while we hold lightning (it wants us close: fleeing switches it
        #     off and hands the enemy free shots) and never from a duel we are
        #     winning (the enemy is the more hurt of the two).
        w_ = me.get("weapons") or [None, None]
        holds_lightning = len(w_) > 1 and w_[1] is not None and w_[1].get("kind") == "lightning"
        nearest = tracker.nearest_mech(pos[0], pos[1])
        winning = (nearest is not None and isinstance(nearest.hp, (int, float))
                   and nearest.hp / float(nearest.max_hp or 1400.0) < hp / max_hp)
        if (near_d is not None and near_d <= C.PANIC_RANGE
                and hp < max_hp * C.PANIC_HP_FRAC and not holds_lightning and not winning):
            self._entered_combat = True
            self._transition(self.EVADE)
            return self.state

        # 2b. Outnumbered: the squad shares vision, so a pack that knows where
        #     we are converges. Flee rather than trade at a disadvantage while
        #     there is still a field left to hide in.
        #     "Outnumbered" counts enemies that are hitting us even when we
        #     cannot see them: two shooters on us is a lost fight either way.
        #     Deliberately late (OUTNUMBERED_HP_FRAC, 60 %): the active bots in
        #     practice keep fighting until quite hurt, and kills are the score.
        if ((threat_count >= C.MULTI_ENEMY_THRESHOLD or n_attackers >= 2)
                and alive > C.LATE_GAME_ALIVE
                and hp < max_hp * C.OUTNUMBERED_HP_FRAC):
            self._entered_combat = True
            self._transition(self.EVADE)
            return self.state

        # 2c. Hit while hiding to heal (an unseen shooter found us): move.
        if self.state == self.RECOVER and under_fire:
            self._entered_combat = True
            self._transition(self.EVADE)
            return self.state

        # 3. Stay disengaged until HP has recovered or contact is broken, with a
        #    short minimum so a flickering sighting can't flap us in and out.
        #    When contact breaks we go to RECOVER (hide, heal) rather than back
        #    to looting, so the next hunter meets a healthy mech.
        if self.state in (self.DISENGAGE_REGEN, self.EVADE):
            if t < self._hold_until:
                return self.state
            if any_visible and hp >= max_hp * C.REGEN_RESUME_FRAC:
                self._transition(self.AMBUSH_HUNTER)
            elif hp >= max_hp * C.REGEN_RESUME_FRAC or not recent_threat:
                self._transition(self.RECOVER if hp < max_hp * C.RECOVER_DONE_FRAC
                                 else self.BOOTSTRAP_LOOT)
            return self.state

        # 4. Core combat transition. Engage only inside ENGAGE_RANGE (the laser
        #    buys LASER_ENGAGE_RANGE when that is what we hold), and only once
        #    the opening calm has passed or someone forced the fight by hitting
        #    us. Anything further away is ignored: walking at a distant mech
        #    just hands the squad a target.
        if any_visible or threat_count > 0:
            engage_r = C.ENGAGE_RANGE
            w = me.get("weapons") or [None, None]
            if len(w) > 1 and w[1] and w[1].get("kind") == "laser":
                engage_r = max(engage_r, C.LASER_ENGAGE_RANGE)
            in_range = near_d is None or near_d <= engage_r
            if in_range and (self._entered_combat or t >= C.OPENING_CALM_S):
                self._entered_combat = True
                self._transition(self.AMBUSH_HUNTER)
                return self.state

        # 5. Recover between waves. Regen is 45 HP/s but only after 5 s without
        #    dealing OR taking damage, and the squad sends the next hunter
        #    soon: after a kill, or whenever we are hurt with nobody in sight,
        #    hide still and deal no damage (crates and drones included) until
        #    healed. Capped so a regen that never starts can't pin us here.
        if self.state == self.RECOVER:
            if hp >= max_hp * C.RECOVER_DONE_FRAC or t - self._recover_since > C.RECOVER_MAX_S:
                self._transition(self.BOOTSTRAP_LOOT)
            return self.state
        just_killed = t - self._last_kill_t < C.POST_KILL_WINDOW_S
        if hp < max_hp * C.RECOVER_START_FRAC or (just_killed and hp < max_hp * C.POST_KILL_HP_FRAC):
            self._transition(self.RECOVER)
            return self.state

        # 6. Otherwise, rotate and loot toward safe zone
        self._transition(self.BOOTSTRAP_LOOT)
        return self.state

    def _transition(self, new_state: str) -> None:
        if new_state == self.state:
            return
        if new_state == self.RECOVER:
            self._recover_since = self._t_now
        if new_state in (self.DISENGAGE_REGEN, self.EVADE):
            self._hold_until = self._t_now + C.EVADE_MIN_S
        mf.log(f"Brain: {self.state} → {new_state} (after {self._ticks_in_state} ticks)")
        self._prev_state = self.state
        self.state = new_state
        self._ticks_in_state = 0

    @property
    def rooftop_target(self) -> Optional["Rooftop"]:
        return self._rooftop_target
