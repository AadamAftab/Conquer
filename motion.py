"""
motion.py — 3D movement controller: rooftop ascent, reverse-kiting,
            zone rotation, and emergency flee.

Exposes one class:
  MotionController
    .decide(obs, state, rooftop_goal, enemy_positions,
            combat_aim, is_on_roof) -> dict (partial action)

Returns partial action dict with keys:
    move  : [dx, dy] normalised
    boost : bool
    jump  : bool
"""

from __future__ import annotations

import math
from typing import List, Optional, Tuple

import config as C
import risk as R
from world import Rooftop


Vec2 = Tuple[float, float]
Vec3 = Tuple[float, float, float]


class MotionController:
    """
    Decides movement vector, boost, and jump each tick.

    Strategy depends on the current tactical state:
      BOOTSTRAP_LOOT   — walk to nearest pickup / crate
      ASCEND_ROOFTOP   — navigate to rooftop, trigger Jump + Jet
      AMBUSH_HUNTER    — MRM while keeping torso on threat (reverse-kite)
      DISENGAGE_REGEN  — flee into cover, break LOS
      ZONE_ROTATION    — MRM edge-riding toward next zone centre
      ENDGAME_SURVIVAL — MRM with mortar-range buffer from cluster
    """

    def __init__(self) -> None:
        self._prev_dir: Optional[Vec2] = None
        self._climb_ticks:  int   = 0   # consecutive ticks in climb sequence
        self._last_z:       float = 0.0 # z from last tick (stuck detection)
        self._stuck_ticks:  int   = 0

    # ------------------------------------------------------------------
    def decide(
        self,
        obs:                dict,
        state:              str,          # FSM state string
        rooftop_goal:       Optional[Rooftop],
        enemy_positions:    List[List[float]],
        combat_aim:         Optional[List[float]],  # [yaw, pitch] from combat.py
        is_on_roof:         bool,
        nav_goal:           Optional[Vec2] = None,  # explicit (x,y) waypoint
    ) -> dict:
        """
        Returns partial action dict: {move, boost, jump}.
        """
        me      = obs["self"]
        pos     = me["pos"]           # [x, y, z]
        vel     = me["vel"]
        energy  = me.get("energy", 0.0)
        grounded = me.get("grounded", True)
        zone    = obs["zone"]

        sx, sy, sz = pos[0], pos[1], pos[2]

        # Stuck detection: if z hasn't changed in 15 ticks, we're stuck
        if abs(sz - self._last_z) < 0.05 and not grounded:
            self._stuck_ticks += 1
        else:
            self._stuck_ticks = 0
        self._last_z = sz

        # Default values
        move   = [0.0, 0.0]
        boost  = False
        jump   = False

        # ---- Route by state ----
        if state == "BOOTSTRAP_LOOT":
            move, boost, jump = self._loot_move(obs, pos, vel, grounded, energy)

        elif state == "ASCEND_ROOFTOP":
            if rooftop_goal is not None:
                move, boost, jump = self._climb_move(
                    pos, vel, grounded, energy, rooftop_goal
                )
            else:
                # No rooftop available — do MRM toward zone
                move = self._mrm_move(pos, enemy_positions, zone,
                                      rooftop_goal, is_on_roof)
                boost = False

        elif state in ("AMBUSH_HUNTER", "ZONE_ROTATION", "ENDGAME_SURVIVAL"):
            if is_on_roof:
                move = self._roof_hold_move(obs, pos, enemy_positions, zone,
                                            rooftop_goal, combat_aim, is_on_roof)
            else:
                move = self._mrm_move(pos, enemy_positions, zone,
                                      rooftop_goal, is_on_roof)
            # Boost while outside zone (saves time getting in)
            if not me.get("in_zone", True):
                boost = energy > C.ENERGY_CLIMB_FLOOR
            else:
                boost = False   # conserve energy on roof

        elif state == "DISENGAGE_REGEN":
            move, boost, jump = self._flee_move(obs, pos, enemy_positions,
                                                 grounded, energy, rooftop_goal,
                                                 zone, is_on_roof)

        # ---- Reverse-kiting: when combat_aim is set, decouple move from aim ---
        # The move direction should RETREAT from the highest-threat enemy
        # while combat.py keeps the torso locked onto the target.
        # We only override move if we're actively in an engagement AND
        # the highest threat is very close.
        if (combat_aim is not None
                and state == "AMBUSH_HUNTER"
                and enemy_positions):
            closest = min(enemy_positions,
                          key=lambda e: math.hypot(e[0]-sx, e[1]-sy))
            dist_to_closest = math.hypot(closest[0]-sx, closest[1]-sy)
            if dist_to_closest < C.PANIC_RANGE * 1.5:
                # Reverse-kite: move directly away from the threat
                flee_dx = sx - closest[0]
                flee_dy = sy - closest[1]
                flee_len = math.hypot(flee_dx, flee_dy) or 1.0
                move = [flee_dx / flee_len, flee_dy / flee_len]
                boost = energy > C.ENERGY_CLIMB_FLOOR

        # ---- Apply explicit nav_goal override if provided ----
        if nav_goal is not None and state == "BOOTSTRAP_LOOT":
            dx, dy = R.direction_to(sx, sy, nav_goal[0], nav_goal[1])
            move   = [dx, dy]

        self._prev_dir = (move[0], move[1])

        return {
            "move":  move,
            "boost": boost,
            "jump":  jump,
        }

    # ------------------------------------------------------------------
    # Movement sub-routines
    # ------------------------------------------------------------------

    def _loot_move(self, obs: dict, pos: List[float], vel: List[float],
                   grounded: bool, energy: float
                   ) -> Tuple[List[float], bool, bool]:
        """
        Walk toward the nearest priority pickup or crate.
        Returns (move, boost, jump).
        """
        sx, sy, sz = pos

        # Priority 1: uncollected power cubes nearby
        for cube in obs.get("cubes", []):
            cx, cy = cube["pos"][0], cube["pos"][1]
            if math.hypot(cx-sx, cy-sy) < 20.0:
                dx, dy = R.direction_to(sx, sy, cx, cy)
                return [dx, dy], True, False

        # Priority 2: preferred weapon pickups (Laser > Mortar > anything)
        weapons_self = obs["self"].get("weapons", [None, None])
        need_weapon  = len(weapons_self) < 2 or weapons_self[1] is None
        if need_weapon:
            best_pickup = None
            best_dist   = float("inf")
            for pickup in obs.get("pickups", []):
                if pickup.get("kind") not in ("laser", "mortar"):
                    continue
                d = math.hypot(pickup["pos"][0]-sx, pickup["pos"][1]-sy)
                if d < best_dist:
                    best_dist   = d
                    best_pickup = pickup
            if best_pickup and best_dist < C.PICKUP_SWEEP_RADIUS:
                dx, dy = R.direction_to(sx, sy,
                                        best_pickup["pos"][0],
                                        best_pickup["pos"][1])
                return [dx, dy], best_dist > 10.0, False

        # Priority 3: nearest crate (for cubes)
        best_crate = None
        best_cdist  = float("inf")
        for crate in obs.get("crates", []):
            d = math.hypot(crate["pos"][0]-sx, crate["pos"][1]-sy)
            if d < C.CRATE_SHOOT_RANGE and d < best_cdist:
                best_cdist  = d
                best_crate  = crate
        if best_crate:
            dx, dy = R.direction_to(sx, sy,
                                    best_crate["pos"][0],
                                    best_crate["pos"][1])
            return [dx, dy], False, False

        # Default: walk toward zone centre
        zone = obs["zone"]
        dx, dy = R.direction_to(sx, sy,
                                 zone["next_center"][0],
                                 zone["next_center"][1])
        return [dx, dy], False, False

    # ------------------------------------------------------------------
    def _climb_move(self, pos: List[float], vel: List[float],
                    grounded: bool, energy: float,
                    rooftop: Rooftop) -> Tuple[List[float], bool, bool]:
        """
        Navigate to the wall below a rooftop and jet/jump to ascend.
        Returns (move, boost, jump).
        """
        sx, sy, sz = pos
        ax, ay     = rooftop.approach_x, rooftop.approach_y
        cx, cy     = rooftop.centroid_x, rooftop.centroid_y
        roof_z     = rooftop.elevation

        dist_to_approach = math.hypot(sx - ax, sy - ay)
        dist_to_centroid = math.hypot(sx - cx, sy - cy)
        dz               = roof_z - sz

        self._climb_ticks += 1

        # Phase 1: walk to the approach point (outside wall)
        if dist_to_approach > C.CLIMB_TRIGGER_DIST:
            dx, dy = R.direction_to(sx, sy, ax, ay)
            # Boost to reach the wall quickly (conserving energy is secondary)
            boost = energy > C.ENERGY_CLIMB_FLOOR * 1.5
            return [dx, dy], boost, False

        # Phase 2: we're at the wall — start jump + jet sequence
        # Move FORWARD (toward centroid), jump, and boost in air
        dx, dy = R.direction_to(sx, sy, cx, cy)
        move   = [dx * C.CLIMB_FORWARD_FRAC, dy * C.CLIMB_FORWARD_FRAC]

        can_jet = energy > C.ENERGY_CLIMB_FLOOR

        if grounded:
            # Trigger jump; boost will kick in next tick when in air
            return move, False, True
        else:
            # In the air: jet upward
            boost = can_jet and dz > 1.0   # still below roof level
            return move, boost, False

    # ------------------------------------------------------------------
    def _mrm_move(self, pos: List[float],
                  enemy_positions: List[List[float]],
                  zone: dict,
                  rooftop_goal: Optional[Rooftop],
                  is_on_roof: bool) -> List[float]:
        """
        Standard Minimum Risk Movement evaluation.
        """
        rf_goal = None
        if rooftop_goal is not None:
            rf_goal = (rooftop_goal.approach_x, rooftop_goal.approach_y)

        dx, dy = R.compute_mrm(
            pos           = pos,
            enemy_positions = enemy_positions,
            zone_cx       = zone["center"][0],
            zone_cy       = zone["center"][1],
            zone_r        = zone["radius"],
            next_zone_cx  = zone["next_center"][0],
            next_zone_cy  = zone["next_center"][1],
            next_zone_r   = zone["next_radius"],
            is_on_roof    = is_on_roof,
            rooftop_goal  = rf_goal,
            prev_dir      = self._prev_dir,
        )
        return [dx, dy]

    # ------------------------------------------------------------------
    def _roof_hold_move(self, obs: dict, pos: List[float],
                        enemy_positions: List[List[float]],
                        zone: dict,
                        rooftop_goal: Optional[Rooftop],
                        combat_aim: Optional[List[float]],
                        is_on_roof: bool) -> List[float]:
        """
        When on a rooftop, do minimal movement — peek/un-peek the edge.
        If enemies are visible, stay near the edge for LOS.
        If taking heavy fire (from combat.py context), step back from edge.
        """
        sx, sy, sz = pos

        if not enemy_positions:
            # No threats — hold position
            return [0.0, 0.0]

        # Edge peek: move toward centroid of nearest enemy,
        # but stay within the rooftop polygon.
        closest_enemy = min(enemy_positions,
                            key=lambda e: math.hypot(e[0]-sx, e[1]-sy))
        dx, dy = R.direction_to(sx, sy, closest_enemy[0], closest_enemy[1])

        # Very slow peek (5% of walk speed) — just adjusting angle
        return [dx * 0.1, dy * 0.1]

    # ------------------------------------------------------------------
    def _flee_move(self, obs: dict, pos: List[float],
                   enemy_positions: List[List[float]],
                   grounded: bool, energy: float,
                   rooftop_goal: Optional[Rooftop],
                   zone: dict, is_on_roof: bool,
                   ) -> Tuple[List[float], bool, bool]:
        """
        DISENGAGE_REGEN: move away from all threats, toward cover.
        """
        sx, sy, sz = pos

        if not enemy_positions:
            # No threats — head to rooftop to regen safely
            if rooftop_goal:
                dx, dy = R.direction_to(sx, sy,
                                        rooftop_goal.approach_x,
                                        rooftop_goal.approach_y)
                return [dx, dy], True, False
            # Stay put
            return [0.0, 0.0], False, False

        # Average threat position
        avg_x = sum(e[0] for e in enemy_positions) / len(enemy_positions)
        avg_y = sum(e[1] for e in enemy_positions) / len(enemy_positions)

        # Move directly away from the threat centroid
        flee_dx = sx - avg_x
        flee_dy = sy - avg_y
        flee_len = math.hypot(flee_dx, flee_dy) or 1.0
        move  = [flee_dx / flee_len, flee_dy / flee_len]
        boost = energy > C.ENERGY_CLIMB_FLOOR

        return move, boost, False
