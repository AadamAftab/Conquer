"""
utility.py — Utility-AI brain (Dave Mark; the BotPrize FPS winners).

Instead of hard thresholds ("hp < 0.35 AND an enemy is visible"), every high-level
behaviour gets a continuous score in [0, 1] from smooth response curves, and the best
score wins, with a momentum bonus for the behaviour we are already in so it never flaps
from tick to tick:

  FIGHT    = 0.30 + 0.55·advantage − pressure·(1−advantage) − crowd penalty + weapon match
  EVADE    = health pressure when losing + crowd pressure when hurt + panic (too close, hurt)
  RECOVER  = 1.6·(1−hp)² (+ a bonus right after a kill), only with nobody in sight
  LOOT     = a steady 0.45 (explore / loot / rotate)

where  advantage = logistic(5·(my_hp% − enemy_hp%))  and  pressure = (1−hp%)².
So at 25 % HP against a 10 % enemy it keeps fighting, against a 60 % enemy it backs off,
and three enemies in sight push it out of the fight earlier — with no special cases.

It reports the SAME state names as the FSM (AMBUSH_HUNTER, EVADE, DISENGAGE_REGEN, RECOVER,
BOOTSTRAP_LOOT, ENDGAME_SURVIVAL), so motion.py and combat.py need no changes.
"""

from __future__ import annotations

import math

import config as C
from brain import Brain


def _logistic(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, x))))


def _clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else 1.0 if v > 1.0 else v


class UtilityBrain(Brain):
    MOMENTUM = 0.10
    LOOT_BASE = 0.45

    def __init__(self) -> None:
        super().__init__()
        self.scores: dict = {}           # last scores, for debugging / tests

    # ------------------------------------------------------------------
    def decide_state(self, obs: dict, tracker, world_index, is_on_roof: bool) -> str:
        me = obs["self"]
        hp = me.get("hp", C.HP_DEFAULT)
        max_hp = me.get("max_hp", C.HP_DEFAULT) or C.HP_DEFAULT
        t = obs.get("t", 0.0)
        alive = int(obs.get("alive", 16) or 16)
        pos = me["pos"]
        self._ticks_in_state += 1
        self._t_now = t
        self.calm = False

        # ---- bookkeeping shared with the FSM: kills, who hit us, are we under fire ----
        under_fire = False
        for ev in (obs.get("events") or []):
            typ = ev.get("type")
            if typ == "kill":
                self._last_kill_t = t
            elif typ == "damaged" and ev.get("weapon") != "zone":
                under_fire = True
                by = ev.get("by")
                if by is not None and 0 <= int(by) < 100:
                    self._attackers[int(by)] = t
        if under_fire:
            self._entered_combat = True
        n_attackers = sum(1 for ts in self._attackers.values() if t - ts < C.ATTACKER_WINDOW_S)

        # ---- hard rules: the circle is a clock, and the optional opening calm ----
        if obs["zone"]["next_radius"] < C.ENDGAME_ZONE_RADIUS:
            self._entered_combat = True
            self._transition(self.ENDGAME_SURVIVAL)
            return self.state
        if not self._entered_combat and t < C.OPENING_CALM_S and hp >= max_hp * 0.9:
            self.calm = True
            self._transition(self.BOOTSTRAP_LOOT)
            return self.state

        # ---- features ----
        hp_f = _clamp01(hp / max_hp)
        pressure = (1.0 - hp_f) ** 2
        n_vis = tracker.threat_count()
        visible = tracker.any_visible()
        near_d = tracker.nearest_mech_dist(pos[0], pos[1])
        nearest = tracker.nearest_mech(pos[0], pos[1])
        if nearest is not None and isinstance(nearest.hp, (int, float)):
            enemy_f = _clamp01(nearest.hp / float(nearest.max_hp or 1400.0))
        else:
            enemy_f = 0.7                                   # unknown: assume it is fairly healthy
        winning = nearest is not None and enemy_f < hp_f
        adv = _logistic(5.0 * (hp_f - enemy_f))
        w = me.get("weapons") or [None, None]
        holds_lightning = len(w) > 1 and w[1] is not None and w[1].get("kind") == "lightning"
        crowd = _clamp01((max(n_vis, n_attackers) - 1) / 2.0)   # 1 enemy 0, 2 → 0.5, 3+ → 1
        late_game = alive <= C.LATE_GAME_ALIVE                 # no room left to run to

        engage_r = C.ENGAGE_RANGE
        if len(w) > 1 and w[1] and w[1].get("kind") == "laser":
            engage_r = max(engage_r, C.LASER_ENGAGE_RANGE)
        in_range = near_d is not None and near_d <= engage_r

        # ---- scores ----
        s = {}
        if visible and in_range:
            match = 1.0 if (holds_lightning and near_d <= 40.0) else 0.4     # lightning wants us close
            s["FIGHT"] = _clamp01(0.40 + 0.55 * adv - 0.60 * pressure * (1.0 - adv)
                                  - 0.55 * crowd * (1.0 - adv) + 0.25 * match)
        else:
            s["FIGHT"] = 0.0

        panic = 0.0
        if (near_d is not None and near_d <= C.PANIC_RANGE and hp_f < C.PANIC_HP_FRAC
                and not holds_lightning and not winning):
            panic = 0.30
        engaged = visible or n_attackers > 0
        s["EVADE"] = 0.0 if (late_game or not engaged) else _clamp01(
            1.8 * pressure * (1.0 - adv) + crowd * (0.20 + pressure) * (1.0 - 0.5 * adv) + panic)
        # one visible enemy, and we are the healthier of the two: never run from it
        if winning and n_vis <= 1:
            s["EVADE"] *= 0.4

        post_kill = 0.35 if (t - self._last_kill_t < C.POST_KILL_WINDOW_S and hp_f < C.POST_KILL_HP_FRAC) else 0.0
        s["RECOVER"] = 0.0 if (visible or n_attackers > 0) else _clamp01(1.6 * pressure + post_kill)
        if self.state == self.RECOVER and t - self._recover_since > C.RECOVER_MAX_S:
            s["RECOVER"] = 0.0
        # looting is worth little in a firefight: value × (1 − enemy threat)
        threat = 1.0 if (visible and in_range) else (0.5 if n_attackers > 0 else 0.0)
        s["LOOT"] = self.LOOT_BASE * (1.0 - 0.7 * threat)

        # ---- hysteresis: a minimum retreat time, and momentum for the current behaviour ----
        cur = self._behaviour_of(self.state)
        if self.state in (self.DISENGAGE_REGEN, self.EVADE) and t < self._hold_until:
            return self.state
        if self.state == self.RECOVER and under_fire:
            s["EVADE"] = max(s["EVADE"], 0.9)               # found while healing: move
        if cur in s:
            s[cur] += self.MOMENTUM
        self.scores = s

        pick = max(s, key=lambda k: s[k])
        if pick == "FIGHT":
            self._entered_combat = True
            self._transition(self.AMBUSH_HUNTER)
        elif pick == "EVADE":
            self._entered_combat = True
            # crowd / panic driven retreats keep fighting back (EVADE); pure low HP goes quiet
            self._transition(self.EVADE if (crowd >= 0.5 or panic > 0.0) else self.DISENGAGE_REGEN)
        elif pick == "RECOVER":
            self._transition(self.RECOVER)
        else:
            self._transition(self.BOOTSTRAP_LOOT)
        return self.state

    # ------------------------------------------------------------------
    def _behaviour_of(self, state: str) -> str:
        if state == self.AMBUSH_HUNTER:
            return "FIGHT"
        if state in (self.EVADE, self.DISENGAGE_REGEN):
            return "EVADE"
        if state == self.RECOVER:
            return "RECOVER"
        return "LOOT"
