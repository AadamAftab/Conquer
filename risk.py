"""
risk.py — Vectorized Minimum Risk Movement (MRM).

Evaluates 16 candidate movement directions arranged in a radial ring
around the bot's current position and returns the safest direction
to move as a normalised [dx, dy] unit vector.

The risk model combines:
  - Enemy proximity penalty (inverse-square law)
  - Zone out-of-bounds hard barrier + gradient near edge
  - Rooftop surface reward (strong incentive to move onto roofs)
  - Direction reversal penalty (prevents jitter/oscillation)

All maths uses the standard library — no NumPy required, so this runs
safely inside the Docker sandbox with zero extra dependencies.
"""

from __future__ import annotations

import math
import time
from typing import List, Optional, Tuple

import config as C


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------
Vec2 = Tuple[float, float]


# ---------------------------------------------------------------------------
# MRM core
# ---------------------------------------------------------------------------

def compute_mrm(
    pos:          List[float],       # [x, y, z] self position
    enemy_positions: List[List[float]],  # [[x,y,z], ...] all threats
    zone_cx:      float,             # zone centre x
    zone_cy:      float,             # zone centre y
    zone_r:       float,             # zone radius (safe if dist <= zone_r)
    next_zone_cx: float,             # next zone centre x
    next_zone_cy: float,             # next zone centre (pulls us inward)
    next_zone_r:  float,
    is_on_roof:   bool = False,      # already on a roof
    rooftop_goal: Optional[Vec2] = None,  # (gx, gy) of nearest roof goal
    prev_dir:     Optional[Vec2] = None,  # previous movement direction
    world = None,
    state: str = "",
    time_budget_s: Optional[float] = None,
) -> Vec2:
    """
    Evaluate MRM and return the best (dx, dy) normalised movement direction.
    LOS scoring is dropped if it would run past time_budget_s.
    """
    if time_budget_s is None:
        time_budget_s = C.MRM_TIME_BUDGET_S
    sx, sy = pos[0], pos[1]
    n      = C.MRM_CANDIDATES     # 16 candidates
    r      = C.MRM_RADIUS         # 15 m ring radius

    best_cost = float("inf")
    best_dx   = 0.0
    best_dy   = 0.0

    # LOS scoring is the expensive part (16 candidates x enemies x ray samples).
    # Only the nearest few enemies within range matter for cover/crossfire.
    los_enemies = sorted(
        (ep for ep in enemy_positions
         if math.hypot(ep[0] - sx, ep[1] - sy) <= C.MRM_LOS_RANGE),
        key=lambda ep: (ep[0] - sx) ** 2 + (ep[1] - sy) ** 2,
    )[:C.MRM_LOS_MAX_ENEMIES]

    t_start = time.perf_counter()
    k = 0
    while k < n:
        angle = 2.0 * math.pi * k / n
        cx    = sx + r * math.cos(angle)
        cy    = sy + r * math.sin(angle)

        cost = _risk(
            sx, sy, cx, cy, pos[2],
            enemy_positions,
            zone_cx, zone_cy, zone_r,
            next_zone_cx, next_zone_cy, next_zone_r,
            is_on_roof, rooftop_goal, prev_dir,
            angle, world, state, los_enemies,
        )

        if cost < best_cost:
            best_cost = cost
            best_dx   = math.cos(angle)
            best_dy   = math.sin(angle)
        k += 1

        # Latency guard: if LOS scoring is eating the tick budget, re-score all
        # candidates without it (cheap) so every candidate is judged the same way.
        if los_enemies and k < n and (time.perf_counter() - t_start) > time_budget_s:
            los_enemies = []
            best_cost, best_dx, best_dy, k = float("inf"), 0.0, 0.0, 0

    if best_cost >= C.W_ZONE_OOB * 0.5:
        gx = next_zone_cx - sx
        gy = next_zone_cy - sy
        length = math.hypot(gx, gy) or 1.0
        return gx / length, gy / length

    return best_dx, best_dy


# ---------------------------------------------------------------------------
# Per-candidate risk function
# ---------------------------------------------------------------------------

def _risk(
    sx: float, sy: float, cx: float, cy: float, cz: float,
    enemy_positions: List[List[float]],
    zone_cx: float, zone_cy: float, zone_r: float,
    next_zone_cx: float, next_zone_cy: float, next_zone_r: float,
    is_on_roof: bool,
    rooftop_goal: Optional[Vec2],
    prev_dir: Optional[Vec2],
    angle: float,
    world = None,
    state: str = "",
    los_enemies: Optional[List[List[float]]] = None,
) -> float:
    """Return the total risk score for candidate point (cx, cy)."""
    if los_enemies is None:
        los_enemies = enemy_positions
    cost = 0.0

    # ---- 1. Wall Awareness: Sample candidate vector at 4m, 8m, 14m ----
    if world is not None:
        for d in (4.0, 8.0, 14.0):
            sample_x = sx + d * math.cos(angle)
            sample_y = sy + d * math.sin(angle)
            if not world.is_walkable(sample_x, sample_y, cz):
                cost += 50000.0 / d

    # ---- 2. Slicing the Pie (Cover & Line-of-Sight Isolation) ----
    if world is not None and los_enemies:
        # Count how many nearby threats have direct LOS to candidate
        enemies_with_los = 0
        for ep in los_enemies:
            if world.has_los_2d(cx, cy, cz + 4.5, ep[0], ep[1], ep[2] + 4.5,
                                step=C.MRM_LOS_STEP_M):
                enemies_with_los += 1

        if state == "DISENGAGE_REGEN":
            # Reward finding complete cover (0 enemies with LOS triggers regen!)
            if enemies_with_los == 0:
                cost -= 600.0
            else:
                cost += 400.0 * enemies_with_los
        elif state in ("AMBUSH_HUNTER", "ENDGAME_SURVIVAL"):
            # Strongly penalize getting caught in multi-bot crossfire!
            if enemies_with_los >= 2:
                cost += 350.0 * (enemies_with_los - 1)
            elif enemies_with_los == 1:
                cost -= 250.0  # Reward isolating 1v1 fights!

    # ---- 3. Enemy proximity penalty (inverse-square) ----
    for ep in enemy_positions:
        d2 = max(1.0, (cx - ep[0]) ** 2 + (cy - ep[1]) ** 2)
        cost += C.W_ENEMY_PROXIMITY / d2

    # ---- 4. Zone constraints (Strict barrier against storm) ----
    dist_from_zone_centre = math.hypot(cx - zone_cx, cy - zone_cy)
    if dist_from_zone_centre > zone_r:
        return C.W_ZONE_OOB

    margin = zone_r - dist_from_zone_centre
    if margin < 10.0:
        cost += C.W_ZONE_NEAR_BORDER / max(0.5, margin)

    dist_from_next = math.hypot(cx - next_zone_cx, cy - next_zone_cy)
    if dist_from_next > next_zone_r * 0.95:
        cost += 20.0 * (dist_from_next - next_zone_r * 0.95)

    # ---- 5. Rooftop incentive ----
    if not is_on_roof and rooftop_goal is not None:
        gx, gy = rooftop_goal
        dist_to_goal = math.hypot(cx - gx, cy - gy)
        cost += C.W_ROOF_BONUS * math.exp(-0.03 * dist_to_goal)

    # ---- 6. Reversal penalty ----
    if prev_dir is not None:
        prev_dx, prev_dy = prev_dir
        dot = prev_dx * math.cos(angle) + prev_dy * math.sin(angle)
        if dot < -0.5:
            cost += C.W_REVERSAL * (-dot)

    return cost


# ---------------------------------------------------------------------------
# Zone edge-riding helper
# ---------------------------------------------------------------------------

def zone_edge_target(
    zone_cx: float, zone_cy: float, zone_r: float,
    self_x: float, self_y: float,
) -> Tuple[float, float]:
    """
    Return a target point on the near edge of the zone (behind us
    relative to the zone centre), so the bot naturally back-rides the
    zone wall with the zone gas covering its rear arc.

    The target is zone_r * 0.90 from the zone centre, in the direction
    from the zone centre to our current position.
    """
    dx = self_x - zone_cx
    dy = self_y - zone_cy
    dist = math.hypot(dx, dy) or 1.0
    # Normalise and scale to 90% of zone radius
    tx = zone_cx + dx / dist * zone_r * 0.90
    ty = zone_cy + dy / dist * zone_r * 0.90
    return tx, ty


# ---------------------------------------------------------------------------
# Convenience: convert an (x, y) goal to a normalised move vector
# ---------------------------------------------------------------------------

def direction_to(sx: float, sy: float, gx: float, gy: float) -> Vec2:
    """Return a normalised (dx, dy) from (sx, sy) toward (gx, gy)."""
    dx = gx - sx
    dy = gy - sy
    dist = math.hypot(dx, dy)
    if dist < 0.01:
        return 0.0, 0.0
    return dx / dist, dy / dist
