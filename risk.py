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
) -> Vec2:
    """
    Evaluate MRM and return the best (dx, dy) normalised movement direction.

    Parameters
    ----------
    pos               : current [x, y, z]
    enemy_positions   : list of visible/believed enemy [x, y, z]
    zone_cx/cy/r      : current safe zone
    next_zone_cx/cy/r : next safe zone (pre-rotate toward it)
    is_on_roof        : if True, roof bonus is suppressed (already there)
    rooftop_goal      : if set, adds a pull toward the nearest rooftop
    prev_dir          : previous tick's move direction (for jitter penalty)

    Returns
    -------
    (dx, dy) normalised to length 1 — feed directly into action["move"].
    """
    sx, sy = pos[0], pos[1]
    n      = C.MRM_CANDIDATES     # 16 candidates
    r      = C.MRM_RADIUS         # 15 m ring radius

    best_cost = float("inf")
    best_dx   = 0.0
    best_dy   = 0.0

    for k in range(n):
        angle = 2.0 * math.pi * k / n
        cx    = sx + r * math.cos(angle)
        cy    = sy + r * math.sin(angle)

        cost = _risk(
            cx, cy, pos[2],
            enemy_positions,
            zone_cx, zone_cy, zone_r,
            next_zone_cx, next_zone_cy, next_zone_r,
            is_on_roof, rooftop_goal, prev_dir,
            angle,
        )

        if cost < best_cost:
            best_cost = cost
            best_dx   = math.cos(angle)
            best_dy   = math.sin(angle)

    # If all candidates are behind zone walls (all infinite), just head to
    # the next zone centre as a safe fallback.
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
    cx: float, cy: float, cz: float,
    enemy_positions: List[List[float]],
    zone_cx: float, zone_cy: float, zone_r: float,
    next_zone_cx: float, next_zone_cy: float, next_zone_r: float,
    is_on_roof: bool,
    rooftop_goal: Optional[Vec2],
    prev_dir: Optional[Vec2],
    angle: float,
) -> float:
    """Return the total risk score for candidate point (cx, cy)."""
    cost = 0.0

    # ---- Enemy proximity penalty: sum of w / dist² per threat ----
    for ep in enemy_positions:
        d2 = max(1.0, (cx - ep[0]) ** 2 + (cy - ep[1]) ** 2)
        cost += C.W_ENEMY_PROXIMITY / d2

    # ---- Zone constraints ----
    dist_from_zone_centre = math.hypot(cx - zone_cx, cy - zone_cy)

    if dist_from_zone_centre > zone_r:
        # Hard barrier: candidate is outside the safe zone
        return C.W_ZONE_OOB

    # Gradient near zone edge (penalty rises as we approach the wall)
    margin = zone_r - dist_from_zone_centre
    if margin < 10.0:
        cost += C.W_ZONE_NEAR_BORDER / max(0.5, margin)

    # Soft pull toward next zone centre so we pre-rotate before shrink
    dist_from_next = math.hypot(cx - next_zone_cx, cy - next_zone_cy)
    if dist_from_next > next_zone_r * 0.95:
        cost += 20.0 * (dist_from_next - next_zone_r * 0.95)

    # ---- Rooftop incentive ----
    if not is_on_roof and rooftop_goal is not None:
        gx, gy = rooftop_goal
        dist_to_goal = math.hypot(cx - gx, cy - gy)
        # Strong pull toward the rooftop approach waypoint
        cost += C.W_ROOF_BONUS * math.exp(-0.03 * dist_to_goal)

    # ---- Reversal penalty ----
    if prev_dir is not None:
        prev_dx, prev_dy = prev_dir
        # Dot product: 1 = same direction, -1 = opposite
        dot = prev_dx * math.cos(angle) + prev_dy * math.sin(angle)
        if dot < -0.5:   # more than ~120° turn
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
