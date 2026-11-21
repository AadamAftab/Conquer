"""
steer.py — Context steering (Fray, Game AI Pro 2): two separate 16-slot maps,
INTEREST (where we want to go) and DANGER (where it is lethal), resolved
without ever summing them against each other.

Why not blend vectors: opposing vectors cancel. An enemy to the east and a wall
to the west sum to "stand still" — exactly where bullets find you. Here each
wish votes only for slots it likes, each hazard only for slots it threatens,
and the answer is the most interesting slot among the SAFE ones (min danger +
margin), smoothed with its safe neighbours.

Bullets and shells are not treated as repulsive blobs but forward-simulated:
for every slot we ask "if I accelerate that way (58.5 m/s², capped speed), where
am I when the bullet passes, and how close is it?" — the cheap form of wave
surfing, with the exact acceleration limit.
"""

from __future__ import annotations

import math
from typing import List, Optional, Tuple

import config as C

N = 16
DIRS = [(math.cos(2.0 * math.pi * k / N), math.sin(2.0 * math.pi * k / N)) for k in range(N)]


def new_map() -> List[float]:
    return [0.0] * N


def add_dir(m: List[float], dx: float, dy: float, weight: float, power: float = 1.0) -> None:
    """Vote for (or threaten) every slot within 90° of (dx, dy), strongest dead ahead."""
    n = math.hypot(dx, dy)
    if n < 1e-9 or weight == 0.0:
        return
    dx, dy = dx / n, dy / n
    for k in range(N):
        c = DIRS[k][0] * dx + DIRS[k][1] * dy
        if c > 0.0:
            m[k] += weight * (c ** power)


def displacement(v0x: float, v0y: float, ux: float, uy: float,
                 speed: float, accel: float, t: float) -> Tuple[float, float]:
    """Where we are after t s if we start at velocity v0 and accelerate toward speed*(ux,uy)."""
    tx, ty = ux * speed - v0x, uy * speed - v0y
    dv = math.hypot(tx, ty)
    if dv < 1e-9:
        return v0x * t, v0y * t
    tr = dv / accel                           # time to reach the target velocity
    ax, ay = tx / dv * accel, ty / dv * accel
    if t <= tr:
        return v0x * t + 0.5 * ax * t * t, v0y * t + 0.5 * ay * t * t
    return (v0x * tr + 0.5 * ax * tr * tr + ux * speed * (t - tr),
            v0y * tr + 0.5 * ay * tr * tr + uy * speed * (t - tr))


def bullet_danger(danger: List[float], pos, vel, center_z: float, half_h: float,
                  bullets, radius: float, speed: float, accel: float) -> int:
    """Add per-slot danger from straight-flying bullets. Returns how many were relevant."""
    cands = []
    for p in bullets or []:
        if p.get("kind") == "mortar":
            continue
        bx, by, bz = p["pos"]
        vx, vy, vz = p["vel"]
        vv = vx * vx + vy * vy + vz * vz
        if vv < 1.0:
            continue
        rx, ry, rz = pos[0] - bx, pos[1] - by, center_z - bz
        tca = (rx * vx + ry * vy + rz * vz) / vv      # when it passes our CURRENT position
        if tca <= 0.0 or tca > C.STEER_BULLET_HORIZON_S:
            continue
        if abs(bz + vz * tca - center_z) > half_h + C.BULLET_DANGER_M:
            continue                                   # passes over / under us
        cands.append((tca, bx, by, bz, vx, vy, vz))
    cands.sort()
    reach = radius + C.STEER_BULLET_MARGIN_M
    for tca, bx, by, bz, vx, vy, vz in cands[:8]:
        # For each slot find WHEN the bullet reaches where we would be (a fixed point: we move
        # slowly compared with a 252 m/s bullet, so three iterations converge), then measure the
        # miss distance at that moment. Running along the bullet's line therefore does not
        # "escape" it, while a sidestep that clears its path does. (Sampling in time instead
        # would skip right over a bullet that covers 12 m per sample.)
        vxy2 = vx * vx + vy * vy
        if vxy2 < 1.0:
            continue
        for k in range(N):
            ux, uy = DIRS[k]
            tt = tca
            for _ in range(3):
                ox, oy = displacement(vel[0], vel[1], ux, uy, speed, accel, tt)
                tt = ((pos[0] + ox - bx) * vx + (pos[1] + oy - by) * vy) / vxy2
                tt = 0.02 if tt < 0.02 else (C.STEER_BULLET_HORIZON_S + 0.3 if tt > C.STEER_BULLET_HORIZON_S + 0.3 else tt)
            if abs(bz + vz * tt - center_z) > half_h + C.BULLET_DANGER_M:
                continue                               # passes over / under us on that slot
            ox, oy = displacement(vel[0], vel[1], ux, uy, speed, accel, tt)
            miss = math.hypot(pos[0] + ox - (bx + vx * tt), pos[1] + oy - (by + vy * tt))
            if miss < reach:
                danger[k] += C.STEER_BULLET_WEIGHT * (1.0 - miss / reach)
    return len(cands[:8])


def shell_danger(danger: List[float], pos, vel, landings, speed: float, accel: float) -> None:
    """landings: [(x, y, time_to_land)] of mortar shells."""
    reach = C.MORTAR_SPLASH_RADIUS + C.SPLASH_DANGER_M
    for lx, ly, tl in landings:
        if tl <= 0.15 or tl > C.STEER_BULLET_HORIZON_S * 4:
            continue
        for k in range(N):
            ox, oy = displacement(vel[0], vel[1], DIRS[k][0], DIRS[k][1], speed, accel, tl)
            miss = math.hypot(pos[0] + ox - lx, pos[1] + oy - ly)
            if miss < reach:
                danger[k] += C.STEER_BULLET_WEIGHT * (1.0 - miss / reach)


def wall_danger(danger: List[float], world, pos) -> None:
    """Probe each slot at 3 / 6 / 10 m: a wall right ahead is far worse than one 10 m away."""
    if world is None:
        return
    x, y, z = pos[0], pos[1], pos[2]
    for k in range(N):
        ux, uy = DIRS[k]
        d = 0.0
        if not world.is_walkable(x + ux * 3.0, y + uy * 3.0, z):
            d += 1.6
        if not world.is_walkable(x + ux * 6.0, y + uy * 6.0, z):
            d += 0.8
        if not world.is_walkable(x + ux * 10.0, y + uy * 10.0, z):
            d += 0.4
        danger[k] += d


def resolve(interest: List[float], danger: List[float], margin: float) -> Optional[Tuple[float, float]]:
    """
    Mask every slot whose danger exceeds (lowest danger + margin); among the rest take
    the most interesting and blend it with its safe neighbours for a smooth heading.
    Returns None only if there is no interest at all and nothing to flee (caller idles).
    """
    lo = min(danger)
    safe = [danger[k] <= lo + margin for k in range(N)]
    best_k, best_i = -1, -1.0
    for k in range(N):
        if safe[k] and interest[k] > best_i:
            best_k, best_i = k, interest[k]
    if best_k < 0 or best_i <= 1e-9:
        # Everything we want lies in dangerous slots (or nothing is wanted): take the
        # SAFE slot closest to the most wanted direction, i.e. slide around the hazard
        # instead of walking into it.
        wanted = max(range(N), key=lambda j: interest[j])
        if interest[wanted] <= 1e-9:
            return None                        # no wish and no hazard to dodge: caller decides
        cands = [j for j in range(N) if safe[j]]
        k = min(cands, key=lambda j: (min((j - wanted) % N, (wanted - j) % N), danger[j]))
        return DIRS[k]
    vx = vy = 0.0
    for j in (best_k - 1, best_k, best_k + 1):
        jj = j % N
        if safe[jj]:
            w = interest[jj]
            vx += DIRS[jj][0] * w
            vy += DIRS[jj][1] * w
    n = math.hypot(vx, vy)
    return (vx / n, vy / n) if n > 1e-9 else DIRS[best_k]
