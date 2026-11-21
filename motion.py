"""
motion.py — Movement controller: dodging, strafing, range control, cover,
path-finding, looting, supply drops, and (optional) rooftop climbing.

Priority each tick (first that applies wins; dodges blend into all of them):
  1. Zone emergency      — outside / at the gas edge: path back in, boost.
  2. DISENGAGE_REGEN     — MRM cover-seeking away from threats.
  3. Combat (enemy mech) — hold the weapon's preferred range band while
                           strafing perpendicular on a random flip timer;
                           lightning circles toward the target's rear;
                           break LOS while the gun reloads (corner peek);
                           path-find instead of charging when far.
  4. Farming an NPC      — same strafing loop with a short band.
  5. Supply drop         — go for it when it lands about when we arrive.
  6. Loot                — remembered cubes, wanted weapons, crates.
  7. Rooftop climb       — only if C.ROOF_CLIMB_ENABLED (off by default).
  8. Rotate              — path to the next zone; stand still once well inside.

Dodging (blended on top, never overriding the zone emergency):
  - bullets (gun / NPC) whose path passes within our body + margin,
  - mortar shells predicted to land within splash radius + margin,
  - enemies charging a laser with their aim on us.

Every final move is checked against walls and bent to the nearest walkable
direction, so retreats no longer back into buildings.
"""

from __future__ import annotations

import math
import random
from typing import Dict, List, Optional, Tuple

import config as C
import risk as R
from world import Rooftop, WorldIndex

Vec2 = Tuple[float, float]


def _unit(x: float, y: float) -> Vec2:
    d = math.hypot(x, y)
    if d < 1e-9:
        return 0.0, 0.0
    return x / d, y / d


def _wrap(a: float) -> float:
    return (a + math.pi) % (2.0 * math.pi) - math.pi


class MotionController:
    def __init__(self, nav=None, seed: int = 0) -> None:
        self.nav = nav                       # nav.PathFollower or None
        self._rng = random.Random(seed)
        self._prev_dir: Optional[Vec2] = None
        self._tick_counter: int = 0
        self._strafe_sign: float = 1.0
        self._next_flip_t: float = 0.0
        # Remembered loot: key -> {"kind", "pos", "t", "item"}
        self._loot: Dict[Tuple[str, int], dict] = {}
        # Rooftop climb state
        self._climb_start_t: Optional[float] = None
        self._mrm_budget: Optional[float] = None
        self.debug_mode: str = ""

    # ==================================================================
    def decide(
        self,
        obs:             dict,
        state:           str,
        rooftop_goal:    Optional[Rooftop],
        enemy_positions: List[List[float]],
        combat_aim:      Optional[List[float]],
        is_on_roof:      bool,
        nav_goal:        Optional[Vec2] = None,
        world:           Optional[WorldIndex] = None,
        enemies:         Optional[List[dict]] = None,
        target:          Optional[dict] = None,
        nav_budget_s:    Optional[float] = None,
        mrm_budget_s:    Optional[float] = None,
    ) -> dict:
        me       = obs["self"]
        pos      = me["pos"]
        energy   = me.get("energy", 0.0)
        grounded = me.get("grounded", True)
        zone     = obs["zone"]
        t        = obs.get("t", 0.0)
        self._tick_counter += 1

        if enemies is None:
            enemies = [{"id": -1 - i, "pos": list(p), "vel": [0.0, 0.0, 0.0],
                        "aim": None, "weapons": [], "charging": False}
                       for i, p in enumerate(enemy_positions)]

        sx, sy, sz = pos[0], pos[1], pos[2]
        zone_cx, zone_cy = zone["center"][0], zone["center"][1]
        zone_r           = zone["radius"]
        next_cx, next_cy = zone["next_center"][0], zone["next_center"][1]
        next_r           = zone["next_radius"]

        self._mrm_budget = mrm_budget_s
        self._remember_loot(obs, sx, sy, t)
        dodge, urgent = self._dodge_vector(obs, enemies, world, t)

        # ------------------------------------------------------------------
        # RULE 1: EMERGENCY ZONE RETURN (never die in the storm)
        # ------------------------------------------------------------------
        dist_from_zone_center = math.hypot(sx - zone_cx, sy - zone_cy)
        if not me.get("in_zone", True) or dist_from_zone_center > (zone_r - 8.0):
            d = self._nav_dir(pos, (next_cx, next_cy), t, nav_budget_s)
            return self._finish(d, energy > 10.0 and grounded, False, world, pos,
                                dodge=None, mode="zone")

        # ------------------------------------------------------------------
        # RULE 2: DISENGAGE & DUCK BEHIND COVER
        # ------------------------------------------------------------------
        if state in ("DISENGAGE_REGEN", "EVADE"):
            d = R.compute_mrm(
                pos=pos, enemy_positions=[e["pos"] for e in enemies],
                zone_cx=zone_cx, zone_cy=zone_cy, zone_r=zone_r,
                next_zone_cx=next_cx, next_zone_cy=next_cy, next_zone_r=next_r,
                is_on_roof=is_on_roof, rooftop_goal=None, prev_dir=self._prev_dir,
                world=world, state=state, time_budget_s=self._mrm_budget,
            )
            boost = (energy > 20.0 and grounded) or (urgent and energy > C.DODGE_BOOST_ENERGY)
            mode = "evade" if state == "EVADE" else "disengage"
            return self._finish(d, boost, False, world, pos, dodge=dodge, mode=mode)

        # ------------------------------------------------------------------
        # RULE 3: COMBAT AGAINST AN ENEMY MECH
        # ------------------------------------------------------------------
        # Only fight what is in engagement range. Beyond it the brain is in a
        # non-combat state anyway; this is a belt-and-braces guard so the legs
        # never path toward a distant mech just because it is visible.
        foe = self._pick_foe(enemies, target, sx, sy)
        if foe is not None:
            foe_d = math.hypot(foe["pos"][0] - sx, foe["pos"][1] - sy)
            if foe_d <= C.ENGAGE_RANGE:
                return self._combat_move(obs, foe, enemies, world, t, dodge, urgent, nav_budget_s)
            foe = None   # out of range: fall through to loot / rotate

        # ------------------------------------------------------------------
        # RULE 4: FARMING AN NPC DRONE (it shoots back: strafe it)
        # ------------------------------------------------------------------
        if target is not None and target.get("kind") == "npc":
            return self._combat_move(obs, target, [], world, t, dodge, urgent, nav_budget_s,
                                     band=C.BAND_NPC)

        calm_budget = nav_budget_s if nav_budget_s is not None else C.NAV_BUDGET_CALM_S

        # ------------------------------------------------------------------
        # RULE 5: SUPPLY DROP (3 cubes + full heal + full energy)
        # ------------------------------------------------------------------
        drop_goal = self._drop_goal(obs, enemies, sx, sy, next_cx, next_cy, next_r)
        if drop_goal is not None:
            (gx, gy), inside = drop_goal
            if inside:
                return self._finish((0.0, 0.0), False, False, world, pos, dodge=dodge, mode="drop-hold")
            d = self._nav_dir(pos, (gx, gy), t, calm_budget)
            far = math.hypot(gx - sx, gy - sy) > 30.0
            return self._finish(d, far and energy > 40.0 and grounded, False, world, pos,
                                dodge=dodge, mode="drop")

        # ------------------------------------------------------------------
        # RULE 6: LOOT (cubes, wanted weapons, crates)
        # ------------------------------------------------------------------
        loot = self._loot_goal(obs, sx, sy, t, next_cx, next_cy, next_r, zone)
        if loot is not None:
            (gx, gy), stop = loot
            if stop:
                return self._finish((0.0, 0.0), False, False, world, pos, dodge=dodge, mode="crate")
            d = self._nav_dir(pos, (gx, gy), t, calm_budget)
            far = math.hypot(gx - sx, gy - sy) > 40.0
            return self._finish(d, far and energy > 70.0 and grounded, False, world, pos,
                                dodge=dodge, mode="loot")

        # ------------------------------------------------------------------
        # RULE 7: ROOFTOP CLIMB (experimental, off by default)
        # ------------------------------------------------------------------
        if C.ROOF_CLIMB_ENABLED:
            climb = self._climb(obs, rooftop_goal, is_on_roof, world, t, calm_budget)
            if climb is not None:
                return climb

        # ------------------------------------------------------------------
        # RULE 8: ROTATE — path into the next zone, then stand still (silent)
        # ------------------------------------------------------------------
        dist_next = math.hypot(sx - next_cx, sy - next_cy)
        if dist_next > next_r * 0.5:
            d = self._nav_dir(pos, (next_cx, next_cy), t, calm_budget)
            return self._finish(d, False, False, world, pos, dodge=dodge, mode="rotate")
        return self._finish((0.0, 0.0), False, False, world, pos, dodge=dodge, mode="hold")

    # ==================================================================
    # Combat movement
    # ==================================================================
    def _pick_foe(self, enemies: List[dict], target: Optional[dict],
                  sx: float, sy: float) -> Optional[dict]:
        if not enemies:
            return None
        if target is not None and target.get("kind") == "mech":
            for e in enemies:
                if e.get("id") == target.get("id"):
                    return e
        return min(enemies, key=lambda e: math.hypot(e["pos"][0] - sx, e["pos"][1] - sy))

    def _band(self, me: dict, foe: dict) -> Tuple[float, float]:
        weapons = me.get("weapons", [None, None])
        w1 = weapons[1] if len(weapons) > 1 else None
        kind = w1.get("kind") if w1 else None
        if kind == "lightning":
            lo, hi = C.BAND_LIGHTNING
        elif kind == "laser":
            lo, hi = C.BAND_LASER
        elif kind == "mortar":
            lo, hi = C.BAND_MORTAR
        else:
            lo, hi = C.BAND_GUN
        # Keep out of an enemy lightning's reach unless we're the one holding it
        if kind != "lightning" and "lightning" in (foe.get("weapons") or []):
            lo = max(lo, C.LIGHTNING_KEEPOUT_M)
            hi = max(hi, lo + 30.0)
        return lo, hi

    def _combat_move(self, obs: dict, foe: dict, enemies: List[dict], world, t: float,
                     dodge: Optional[Vec2], urgent: bool, nav_budget_s: Optional[float],
                     band: Optional[Tuple[float, float]] = None) -> dict:
        me = obs["self"]
        pos = me["pos"]
        sx, sy = pos[0], pos[1]
        energy = me.get("energy", 0.0)
        grounded = me.get("grounded", True)
        fx, fy = foe["pos"][0], foe["pos"][1]
        dist = math.hypot(fx - sx, fy - sy)
        lo, hi = band if band is not None else self._band(me, foe)
        ux, uy = _unit(fx - sx, fy - sy)          # toward foe

        weapons = me.get("weapons", [None, None])
        w0 = weapons[0] if weapons else None
        w1 = weapons[1] if len(weapons) > 1 else None
        holds_lightning = bool(w1 and w1.get("kind") == "lightning")

        # --- Far away: path-find toward it instead of charging blind ---
        if dist > hi + C.FAR_APPROACH_M:
            d = self._nav_dir(pos, (fx, fy), t, nav_budget_s)
            self._update_strafe(t, holds_lightning, foe, sx, sy)
            # small sideways weave so the approach isn't a straight line
            px, py = -uy * self._strafe_sign, ux * self._strafe_sign
            d = _unit(d[0] + 0.35 * px, d[1] + 0.35 * py)
            return self._finish(d, energy > 50.0 and grounded, False, world, pos,
                                dodge=dodge, mode="approach")

        # --- Corner peek: break LOS while the gun reloads ---
        reloading = bool(w0 and w0.get("kind") == "gun" and (w0.get("reload", 0.0) or 0.0) > 0.0)
        slot1_ready = bool(w1 and (w1.get("cooldown", 0.0) or 0.0) <= 0.1
                           and w1.get("kind") in ("laser", "mortar", "lightning"))
        if C.PEEK_ON_RELOAD and reloading and not slot1_ready and world is not None:
            zone = obs["zone"]
            d = R.compute_mrm(
                pos=pos, enemy_positions=[e["pos"] for e in (enemies or [foe])],
                zone_cx=zone["center"][0], zone_cy=zone["center"][1], zone_r=zone["radius"],
                next_zone_cx=zone["next_center"][0], next_zone_cy=zone["next_center"][1],
                next_zone_r=zone["next_radius"], prev_dir=self._prev_dir,
                world=world, state="DISENGAGE_REGEN",
                time_budget_s=self._mrm_budget,
            )
            boost = urgent and energy > C.DODGE_BOOST_ENERGY and grounded
            return self._finish(d, boost, False, world, pos, dodge=dodge, mode="peek-cover")

        # --- Radial: hold the band ---
        if dist > hi:
            radial = min(1.0, (dist - hi) / 15.0 + 0.3)
        elif dist < lo:
            radial = -min(1.0, (lo - dist) / 10.0 + 0.3)
        else:
            radial = 0.0

        # --- Tangential strafe ---
        self._update_strafe(t, holds_lightning, foe, sx, sy)
        a = math.atan2(sy - fy, sx - fx)                    # angle of us around foe
        tx, ty = -math.sin(a) * self._strafe_sign, math.cos(a) * self._strafe_sign

        mx = C.RADIAL_WEIGHT * radial * ux + C.STRAFE_WEIGHT * tx
        my = C.RADIAL_WEIGHT * radial * uy + C.STRAFE_WEIGHT * ty

        # --- Don't get sandwiched: push away from other close foes ---
        for e in enemies:
            if e is foe:
                continue
            ex, ey = e["pos"][0], e["pos"][1]
            de = math.hypot(ex - sx, ey - sy)
            if 1.0 < de < 60.0:
                k = C.OTHER_FOE_PUSH * (60.0 - de) / 60.0
                mx -= k * (ex - sx) / de
                my -= k * (ey - sy) / de

        # --- Stay inside the coming zone ---
        zone = obs["zone"]
        ncx, ncy, nr = zone["next_center"][0], zone["next_center"][1], zone["next_radius"]
        dn = math.hypot(sx - ncx, sy - ncy)
        if dn > nr * 0.9:
            k = min(1.0, (dn - nr * 0.9) / 20.0 + 0.3)
            mx += k * (ncx - sx) / max(dn, 1e-6)
            my += k * (ncy - sy) / max(dn, 1e-6)

        d = _unit(mx, my)
        charging = bool(w1 and w1.get("kind") == "laser" and (w1.get("charge", 0.0) or 0.0) > 0.0)
        boost = grounded and not charging and (
            energy > C.STRAFE_BOOST_ENERGY
            or (urgent and energy > C.DODGE_BOOST_ENERGY)
            or (radial < -0.5 and energy > 30.0))
        return self._finish(d, boost, False, world, pos, dodge=dodge, mode="strafe",
                            flip_on_block=True)

    def _update_strafe(self, t: float, holds_lightning: bool, foe: dict,
                       sx: float, sy: float) -> None:
        if holds_lightning and foe.get("aim"):
            # Circle toward the foe's back (rear hits deal ×1.5)
            fx, fy = foe["pos"][0], foe["pos"][1]
            rear = foe["aim"][0] + math.pi
            a = math.atan2(sy - fy, sx - fx)
            self._strafe_sign = 1.0 if _wrap(rear - a) > 0 else -1.0
            return
        if t >= self._next_flip_t:
            self._strafe_sign = -self._strafe_sign
            self._next_flip_t = t + self._rng.uniform(C.STRAFE_FLIP_MIN_S, C.STRAFE_FLIP_MAX_S)

    # ==================================================================
    # Dodging
    # ==================================================================
    def _dodge_vector(self, obs: dict, enemies: List[dict], world, t: float
                      ) -> Tuple[Optional[Vec2], bool]:
        me = obs["self"]
        sx, sy, sz = me["pos"]
        stats = me.get("stats", {}) or {}
        radius = stats.get("radius", C.MECH_RADIUS)
        height = stats.get("height", C.MECH_HEIGHT)
        cz = sz + height * 0.5
        vx_sum = vy_sum = 0.0
        urgent = False

        for p in obs.get("projectiles", []) or []:
            px, py, pz = p["pos"]
            vx, vy, vz = p["vel"]
            if p.get("kind") == "mortar":
                land = self._predict_landing(px, py, pz, vx, vy, vz, world, sz)
                if land is None:
                    continue
                lx, ly, t_land = land
                danger = C.MORTAR_SPLASH_RADIUS + C.SPLASH_DANGER_M
                dl = math.hypot(sx - lx, sy - ly)
                if dl < danger and t_land > 0.15:
                    if dl < 0.5:
                        ox, oy = -vy, vx
                        if math.hypot(ox, oy) < 0.1:      # falling straight down on us
                            ox, oy = self._strafe_sign, 0.0
                    else:
                        ox, oy = sx - lx, sy - ly
                    ox, oy = _unit(ox, oy)
                    w = 1.0 - dl / danger + 0.3
                    vx_sum += ox * w
                    vy_sum += oy * w
                    urgent = True
            else:
                vv = vx * vx + vy * vy + vz * vz
                if vv < 1.0:
                    continue
                rx, ry, rz = sx - px, sy - py, cz - pz
                tca = (rx * vx + ry * vy + rz * vz) / vv
                if tca <= 0.0 or tca > 1.5:
                    continue
                cx_, cy_, cz_ = px + vx * tca, py + vy * tca, pz + vz * tca
                if abs(cz_ - cz) > height * 0.5 + C.BULLET_DANGER_M:
                    continue
                miss = math.hypot(sx - cx_, sy - cy_)
                if miss < radius + C.BULLET_DANGER_M:
                    ox, oy = sx - cx_, sy - cy_
                    if math.hypot(ox, oy) < 0.3:
                        ox, oy = -vy * self._strafe_sign, vx * self._strafe_sign
                    ox, oy = _unit(ox, oy)
                    vx_sum += ox
                    vy_sum += oy
                    if tca > 0.12:
                        urgent = True

        # Laser telegraph: an enemy charging with its torso on us → sidestep
        for e in enemies:
            if not e.get("charging") or not e.get("aim"):
                continue
            ex, ey = e["pos"][0], e["pos"][1]
            de = math.hypot(sx - ex, sy - ey)
            if de > C.LASER_RANGE + 10.0:
                continue
            to_me = math.atan2(sy - ey, sx - ex)
            if math.degrees(abs(_wrap(e["aim"][0] - to_me))) <= C.LASER_DODGE_DEG:
                vx_sum += -math.sin(to_me) * self._strafe_sign * 1.5
                vy_sum += math.cos(to_me) * self._strafe_sign * 1.5
                urgent = True

        if vx_sum == 0.0 and vy_sum == 0.0:
            return None, False
        return _unit(vx_sum, vy_sum), urgent

    @staticmethod
    def _predict_landing(px, py, pz, vx, vy, vz, world, ref_z):
        g = C.MORTAR_GRAVITY
        dt = 0.1
        x, y, z = px, py, pz
        t = 0.0
        while t < 8.0:
            x += vx * dt
            y += vy * dt
            z += vz * dt - 0.5 * g * dt * dt
            vz -= g * dt
            t += dt
            ground = world.terrain_height(x, y) if world is not None else ref_z
            if vz < 0.0 and z <= ground:
                return x, y, t
        return None

    # ==================================================================
    # Supply drops & loot
    # ==================================================================
    def _drop_goal(self, obs, enemies, sx, sy, ncx, ncy, nr):
        me = obs["self"]
        my_id = me.get("id")
        speed = (me.get("stats", {}) or {}).get("walk_speed", C.WALK_SPEED)
        best = None
        for d in obs.get("drops", []) or []:
            dpos = d.get("pos", [0, 0, 0])
            dist = math.hypot(dpos[0] - sx, dpos[1] - sy)
            if dist > C.DROP_RANGE:
                continue
            if math.hypot(dpos[0] - ncx, dpos[1] - ncy) > nr:
                continue
            radius = d.get("radius", 8.0)
            inside = dist <= radius - 1.5
            eta = dist / max(1.0, speed)
            if d.get("lands_in", 99.0) > eta + 5.0:
                continue
            if any(math.hypot(e["pos"][0] - dpos[0], e["pos"][1] - dpos[1]) < C.DROP_ENEMY_CLEAR_M
                   for e in enemies):
                continue

            # Capture is solo: two mechs in the ring freeze the clock, and if
            # the ring empties or the sole holder changes, the clock restarts.
            # So an enemy holder is worth contesting only when we can reach the
            # ring before their clock finishes — stepping in freezes them.
            holder = d.get("holder")
            if holder is not None and holder != my_id and not inside:
                progress = float(d.get("progress", 0.0) or 0.0)
                capture_s = float(d.get("capture_s", 4.0) or 4.0)
                if progress + eta >= capture_s:
                    continue   # they win the race; don't feed them a target
            if best is None or dist < best[0]:
                best = (dist, (dpos[0], dpos[1]), inside)
        if best is None:
            return None
        return best[1], best[2]

    def _remember_loot(self, obs: dict, sx: float, sy: float, t: float) -> None:
        seen = set()
        for kind, items in (("cube", obs.get("cubes")), ("crate", obs.get("crates")),
                            ("pickup", obs.get("pickups"))):
            for it in items or []:
                key = (kind, it.get("id"))
                seen.add(key)
                self._loot[key] = {"kind": kind, "pos": list(it["pos"]), "t": t, "item": it}
        for key, v in list(self._loot.items()):
            if key in seen:
                continue
            near = math.hypot(v["pos"][0] - sx, v["pos"][1] - sy) < C.LOOT_FORGET_R
            if near or t - v["t"] > C.LOOT_MAX_AGE_S:
                del self._loot[key]

    def _loot_goal(self, obs, sx, sy, t, ncx, ncy, nr, zone):
        from combat import should_take_pickup   # local import: avoid a cycle at load time
        me = obs["self"]
        cubes = me.get("cubes", 0)
        rng = C.LOOT_RANGE_EARLY if t < C.LOOT_EARLY_S else C.LOOT_RANGE
        zone_soon = zone.get("shrink_start", 1e9) - t < 20.0
        best = None
        for (kind, _id), v in self._loot.items():
            lx, ly = v["pos"][0], v["pos"][1]
            dist = math.hypot(lx - sx, ly - sy)
            if dist > rng:
                continue
            if zone_soon and math.hypot(lx - ncx, ly - ncy) > nr * 0.9:
                continue
            if kind == "cube":
                if cubes >= C.STAT_CAP_CUBES:
                    continue
                score = dist * 0.7
            elif kind == "pickup":
                if not should_take_pickup(obs, v["item"]):
                    continue
                score = dist * 0.8
            else:  # crate
                if cubes >= C.STAT_CAP_CUBES:
                    continue
                score = dist * 1.0 + 10.0
            if best is None or score < best[0]:
                best = (score, kind, lx, ly, dist)
        if best is None:
            return None
        _, kind, lx, ly, dist = best
        stop = kind == "crate" and dist <= C.CRATE_STANDOFF_M
        return (lx, ly), stop

    # ==================================================================
    # Rooftop climb (experimental)
    # ==================================================================
    def _climb(self, obs, roof: Optional[Rooftop], is_on_roof: bool, world, t: float,
               budget: float) -> Optional[dict]:
        me = obs["self"]
        pos = me["pos"]
        sx, sy = pos[0], pos[1]
        energy = me.get("energy", 0.0)
        grounded = me.get("grounded", True)
        max_hp = me.get("max_hp", C.HP_DEFAULT) or C.HP_DEFAULT
        if roof is None or world is None:
            self._climb_start_t = None
            return None
        if is_on_roof:
            self._climb_start_t = None
            # hold near the middle of the roof (silent, hard to see from the street)
            if math.hypot(roof.centroid_x - sx, roof.centroid_y - sy) > 6.0 \
                    and world.point_in_rooftop(sx, sy, roof):
                d = _unit(roof.centroid_x - sx, roof.centroid_y - sy)
                return {"move": [d[0] * 0.5, d[1] * 0.5], "boost": False, "jump": False}
            return {"move": [0.0, 0.0], "boost": False, "jump": False}

        climbing = self._climb_start_t is not None
        if climbing and t - self._climb_start_t > C.CLIMB_TIMEOUT_S:
            self._climb_start_t = None
            return None
        if not climbing:
            if energy < C.CLIMB_MIN_ENERGY or me.get("hp", 0) < 0.7 * max_hp:
                return None
            rel = roof.elevation - world.terrain_height(roof.approach_x, roof.approach_y)
            if rel > C.CLIMB_MAX_DZ:
                return None
            if math.hypot(roof.approach_x - sx, roof.approach_y - sy) > 3.0:
                d = self._nav_dir(pos, (roof.approach_x, roof.approach_y), t, budget)
                return self._finish(d, False, False, world, pos, dodge=None, mode="to-roof")
            self._climb_start_t = t
        # At the wall: jump, then jet while pushing toward the roof's middle
        d = _unit(roof.centroid_x - sx, roof.centroid_y - sy)
        self.debug_mode = "climb"
        return {"move": [d[0], d[1]], "boost": not grounded, "jump": grounded}

    # ==================================================================
    # Helpers
    # ==================================================================
    def _nav_dir(self, pos, goal: Vec2, t: float, budget: Optional[float]) -> Vec2:
        if self.nav is not None:
            return self.nav.direction(pos, goal, t, budget)
        return R.direction_to(pos[0], pos[1], goal[0], goal[1])

    def _finish(self, d: Vec2, boost: bool, jump: bool, world, pos,
                dodge: Optional[Vec2] = None, mode: str = "",
                flip_on_block: bool = False) -> dict:
        """Blend in the dodge, steer around walls, remember direction."""
        dx, dy = d
        if dodge is not None:
            dx, dy = _unit(dx + C.DODGE_WEIGHT * dodge[0], dy + C.DODGE_WEIGHT * dodge[1])
        if world is not None and (dx or dy):
            if not self._dir_clear(world, pos, dx, dy):
                if flip_on_block:
                    self._strafe_sign = -self._strafe_sign
                dx, dy = self._nearest_clear(world, pos, dx, dy)
        if dx or dy:
            self._prev_dir = (dx, dy)
        self.debug_mode = mode
        return {"move": [dx, dy], "boost": bool(boost), "jump": bool(jump)}

    @staticmethod
    def _dir_clear(world, pos, dx, dy) -> bool:
        for dist in (3.0, 6.0):
            if not world.is_walkable(pos[0] + dx * dist, pos[1] + dy * dist, pos[2]):
                return False
        return True

    def _nearest_clear(self, world, pos, dx, dy) -> Vec2:
        """Walkable direction (of 16) closest to the desired one; else the desired one."""
        base = math.atan2(dy, dx)
        for k in range(1, 9):
            for s in (1, -1):
                a = base + s * k * (math.pi / 8.0)
                cx, cy = math.cos(a), math.sin(a)
                if self._dir_clear(world, pos, cx, cy):
                    return cx, cy
        return dx, dy
