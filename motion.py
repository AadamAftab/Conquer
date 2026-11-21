"""
motion.py — High-Performance 3D Movement Controller: Reverse-Kiting & Zone Control.

Priorities:
  1. Safe Zone Survival: Never step into the deadly gas wall. If outside or near the edge,
     pull hard toward zone["next_center"] with boost.
  2. Reverse-Kiting: When an enemy closes in (< 25 m), boost backward to maintain distance
     and prevent rear-armor exposure while the torso delivers 100% firepower.
  3. Tactical Strafing: When in mid-range (25–45 m), strafe unpredictably via MRM to evade
     enemy bullets and mortar splash.
  4. Dynamic Looting: In the absence of combat, rapidly sweep up nearby power cubes and weapons
     along the path to the zone center.
"""

from __future__ import annotations

import math
from typing import List, Optional, Tuple

import config as C
import risk as R
from world import Rooftop

Vec2 = Tuple[float, float]


class MotionController:
    def __init__(self) -> None:
        self._prev_dir: Optional[Vec2] = None
        self._tick_counter: int = 0

    def decide(
        self,
        obs:             dict,
        state:           str,
        rooftop_goal:    Optional[Rooftop],
        enemy_positions: List[List[float]],
        combat_aim:      Optional[List[float]],
        is_on_roof:      bool,
        nav_goal:        Optional[Vec2] = None,
    ) -> dict:
        me       = obs["self"]
        pos      = me["pos"]
        vel      = me["vel"]
        energy   = me.get("energy", 0.0)
        grounded = me.get("grounded", True)
        zone     = obs["zone"]
        self._tick_counter += 1

        sx, sy, sz = pos[0], pos[1], pos[2]
        zone_cx, zone_cy = zone["center"][0], zone["center"][1]
        zone_r           = zone["radius"]
        next_cx, next_cy = zone["next_center"][0], zone["next_center"][1]
        next_r           = zone["next_radius"]

        dist_from_zone_center = math.hypot(sx - zone_cx, sy - zone_cy)
        is_in_gas             = not me.get("in_zone", True) or dist_from_zone_center > (zone_r - 8.0)

        # ------------------------------------------------------------------
        # RULE 1: EMERGENCY ZONE RETURN (Never die in the storm)
        # ------------------------------------------------------------------
        if is_in_gas:
            dx, dy = R.direction_to(sx, sy, next_cx, next_cy)
            boost  = energy > 10.0 and grounded
            jump   = (self._tick_counter % 30 == 0) and grounded
            self._prev_dir = (dx, dy)
            return {"move": [dx, dy], "boost": boost, "jump": jump}

        # ------------------------------------------------------------------
        # RULE 2: ACTIVE COMBAT KITING (Reverse-Kite & Strafe)
        # ------------------------------------------------------------------
        if enemy_positions:
            closest_enemy = min(enemy_positions, key=lambda e: math.hypot(e[0] - sx, e[1] - sy))
            dist_to_threat = math.hypot(closest_enemy[0] - sx, closest_enemy[1] - sy)

            # Close-range threat (< 25 m): Reverse-kite immediately!
            if dist_to_threat < 25.0:
                flee_dx = sx - closest_enemy[0]
                flee_dy = sy - closest_enemy[1]
                flee_len = math.hypot(flee_dx, flee_dy) or 1.0
                flee_x, flee_y = flee_dx / flee_len, flee_dy / flee_len

                # Check if retreating pushes us into the storm; if so, blend toward next_center
                new_x = sx + flee_x * 10.0
                new_y = sy + flee_y * 10.0
                if math.hypot(new_x - next_cx, new_y - next_cy) > next_r * 0.90:
                    toward_cx, toward_cy = R.direction_to(sx, sy, next_cx, next_cy)
                    move_x = flee_x * 0.5 + toward_cx * 0.5
                    move_y = flee_y * 0.5 + toward_cy * 0.5
                    m_len = math.hypot(move_x, move_y) or 1.0
                    flee_x, flee_y = move_x / m_len, move_y / m_len

                boost = energy > 20.0 and grounded
                self._prev_dir = (flee_x, flee_y)
                return {"move": [flee_x, flee_y], "boost": boost, "jump": False}

            # Mid-range threat (25–45 m): MRM strafe to dodge incoming projectiles
            elif dist_to_threat <= 45.0:
                dx, dy = R.compute_mrm(
                    pos             = pos,
                    enemy_positions = enemy_positions,
                    zone_cx         = zone_cx,
                    zone_cy         = zone_cy,
                    zone_r          = zone_r,
                    next_zone_cx    = next_cx,
                    next_zone_cy    = next_cy,
                    next_zone_r     = next_r,
                    is_on_roof      = is_on_roof,
                    rooftop_goal    = None,
                    prev_dir        = self._prev_dir,
                )
                self._prev_dir = (dx, dy)
                return {"move": [dx, dy], "boost": False, "jump": False}

            # Distant threat (> 45 m): Close the distance toward threat/center
            else:
                dx, dy = R.direction_to(sx, sy, closest_enemy[0], closest_enemy[1])
                self._prev_dir = (dx, dy)
                return {"move": [dx, dy], "boost": energy > 40.0, "jump": False}

        # ------------------------------------------------------------------
        # RULE 3: ROTATE & LOOT (No enemies in sight)
        # ------------------------------------------------------------------
        # Priority A: nearby power cubes (< 25m)
        for cube in obs.get("cubes", []):
            cx, cy = cube["pos"][0], cube["pos"][1]
            if math.hypot(cx - sx, cy - sy) < 25.0:
                dx, dy = R.direction_to(sx, sy, cx, cy)
                self._prev_dir = (dx, dy)
                return {"move": [dx, dy], "boost": True, "jump": False}

        # Priority B: weapon pickup if slot 1 is empty or upgradeable (< 30m)
        weapons = me.get("weapons", [None, None])
        if len(weapons) < 2 or weapons[1] is None or weapons[1].get("kind") == "lightning":
            for pickup in obs.get("pickups", []):
                px, py = pickup["pos"][0], pickup["pos"][1]
                if math.hypot(px - sx, py - sy) < 30.0:
                    dx, dy = R.direction_to(sx, sy, px, py)
                    self._prev_dir = (dx, dy)
                    return {"move": [dx, dy], "boost": True, "jump": False}

        # Priority C: advance steadily toward next zone center
        dx, dy = R.direction_to(sx, sy, next_cx, next_cy)
        jump   = (self._tick_counter % 40 == 0) and grounded
        self._prev_dir = (dx, dy)
        return {"move": [dx, dy], "boost": False, "jump": jump}
