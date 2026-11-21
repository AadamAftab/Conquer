"""
combat.py — Weapons, ballistic solvers, and the fire-control loop.

Exposes:
  gun_lead_aim(me_pos, me_vel, target_pos, target_vel, muzzle_height, cfg)
      → (yaw, pitch) | None

  mortar_pitch(tube_x, tube_y, tube_z, tgt_x, tgt_y, tgt_z, cfg)
      → (pitch_low, pitch_high) | None

  CombatController.decide(obs, tracker_state) → dict (partial action fields)
      Handles:
        - target selection (threat priority)
        - Arm Gun quadratic lead
        - Laser charge-hold-release FSM
        - Mortar indirect fire
        - Reload timing
        - Reverse-kite aim decoupling
        - Weapon pickup/drop decisions
"""

from __future__ import annotations

import math
import time
from enum import Enum, auto
from typing import Dict, List, Optional, Tuple

import config as C
import mechfight as mf


# ---------------------------------------------------------------------------
# 1. Ballistic maths helpers
# ---------------------------------------------------------------------------

def _dot(a: List[float], b: List[float]) -> float:
    return sum(x * y for x, y in zip(a, b))

def _sub(a: List[float], b: List[float]) -> List[float]:
    return [x - y for x, y in zip(a, b)]

def _len2(v: List[float]) -> float:
    return sum(x * x for x in v)

def _dist2d(ax: float, ay: float, bx: float, by: float) -> float:
    return math.hypot(ax - bx, ay - by)


def gun_lead_aim(
    me_pos: List[float],
    target_pos: List[float],
    target_vel: List[float],
    muzzle_height: float,
    gun_speed: float = None,
) -> Optional[Tuple[float, float]]:
    """
    Compute the (yaw, pitch) needed to intercept a moving target with an
    Arm Gun projectile (flat trajectory, constant speed, no gravity drop).

    Uses the closed-form quadratic solution:
        (|Vt|² - v0²) t² + 2(D·Vt) t + |D|² = 0
    where D = target_pos - muzzle_pos, Vt = target velocity.

    Returns (yaw, pitch) in radians, or None if the target is out of range
    or moving faster than the bullet (physically impossible intercept).
    """
    if gun_speed is None:
        gun_speed = C.GUN_SPEED

    # Muzzle position (gun fires from muzzle_height, not feet)
    muzzle = [me_pos[0], me_pos[1], me_pos[2] + muzzle_height]

    # Aim at centre of mass (feet + 4.5 m)
    aim_at = [target_pos[0], target_pos[1], target_pos[2] + 4.5]

    D  = _sub(aim_at, muzzle)              # relative position
    Vt = list(target_vel)                  # target velocity

    A = _len2(Vt) - gun_speed * gun_speed
    B = 2.0 * _dot(D, Vt)
    Csq = _len2(D)

    if abs(A) < 1e-6:
        # Bullet is much faster than target — just shoot directly
        t_star = Csq / (gun_speed * gun_speed)   # approx
        t_star = max(0.0, math.sqrt(t_star / max(1e-6, _len2(Vt) + 1e-6)))
    else:
        disc = B * B - 4.0 * A * Csq
        if disc < 0.0:
            return None   # no real solution — target evading faster than bullet
        sqrt_disc = math.sqrt(disc)
        t1 = (-B - sqrt_disc) / (2.0 * A)
        t2 = (-B + sqrt_disc) / (2.0 * A)
        # Pick smallest positive root
        candidates = [t for t in (t1, t2) if t > 1e-4]
        if not candidates:
            return None
        t_star = min(candidates)

    # Lead point
    lead = [aim_at[0] + Vt[0] * t_star,
            aim_at[1] + Vt[1] * t_star,
            aim_at[2] + Vt[2] * t_star]

    dx = lead[0] - muzzle[0]
    dy = lead[1] - muzzle[1]
    dz = lead[2] - muzzle[2]

    yaw   = math.atan2(dy, dx)
    dist_xy = math.hypot(dx, dy)
    pitch = math.atan2(dz, dist_xy)

    # Clamp pitch
    pitch = max(C.MIN_PITCH, min(C.MAX_PITCH, pitch))
    return yaw, pitch


def mortar_pitch(
    tube_pos: List[float],
    tgt_pos: List[float],
    high_arc: bool = True,
) -> Optional[Tuple[float, float]]:
    """
    Compute the mortar tube pitch (radians) to hit tgt_pos from tube_pos.

    Physics: parabolic, no air drag, constant gravity.
        tan(θ) = (v0² ± sqrt(v0⁴ - g(gR² + 2Δz·v0²))) / (g·R)

    Returns (pitch_low, pitch_high) where low-arc arrives sooner and
    high-arc clears buildings. Returns None if target is out of range.

    The caller picks pitch_high when lobbing over obstacles (default),
    pitch_low when the target is in the open.
    """
    v0 = C.MORTAR_SPEED
    g  = C.MORTAR_GRAVITY

    # Mortar tube is at the top of the mech
    tube = [tube_pos[0], tube_pos[1], tube_pos[2] + C.MECH_MORTAR_HEIGHT]

    dx  = tgt_pos[0] - tube[0]
    dy  = tgt_pos[1] - tube[1]
    dz  = tgt_pos[2] - tube[2]
    R   = math.hypot(dx, dy)

    if R < 0.5:
        return None   # target directly under the tube

    discriminant = v0 ** 4 - g * (g * R * R + 2.0 * dz * v0 * v0)
    if discriminant < 0.0:
        return None   # out of mortar range

    sqrt_d = math.sqrt(discriminant)
    yaw    = math.atan2(dy, dx)

    tan_low  = (v0 * v0 - sqrt_d) / (g * R)
    tan_high = (v0 * v0 + sqrt_d) / (g * R)

    pitch_low  = math.atan(tan_low)
    pitch_high = math.atan(tan_high)

    pitch_low  = max(C.MIN_PITCH, min(C.MAX_PITCH, pitch_low))
    pitch_high = max(C.MIN_PITCH, min(C.MAX_PITCH, pitch_high))

    return (pitch_low, pitch_high), yaw


# ---------------------------------------------------------------------------
# 2. Laser Finite State Machine
# ---------------------------------------------------------------------------

class LaserState(Enum):
    READY     = auto()   # cooldown expired, can start charge
    CHARGING  = auto()   # holding trigger, charge building
    COOLDOWN  = auto()   # just fired, waiting for cooldown
    SHORTED   = auto()   # lightning shorted this laser


class LaserController:
    """
    Tracks the state of one Laser weapon in slot 1 and decides each tick
    whether to hold fire=True (charge) or release (fire=False).

    The server tells us `self.weapons[1].charge` (seconds charged so far),
    `self.weapons[1].cooldown` (seconds until ready), and events contain
    `effect == 'short'` when shorted by lightning.
    """

    def __init__(self) -> None:
        self.state   = LaserState.READY
        self._short_until: float = 0.0  # match time (s) when short expires

    def notify_shorted(self, t: float) -> None:
        """Call when a 'short' effect event is received."""
        self.state = LaserState.SHORTED
        self._short_until = t + C.LIGHTNING_SHORT_S

    def step(self,
             t: float,
             weapon_obs: Optional[dict],
             target_visible: bool,
             aim_error_deg: float) -> Tuple[bool, bool]:
        """
        Decide fire[1] for this tick.

        Parameters
        ----------
        t               : match time in seconds
        weapon_obs      : obs["self"]["weapons"][1] dict or None
        target_visible  : True if we have a T0/T1 target in LOS
        aim_error_deg   : angular error between current aim and target (degrees)

        Returns
        -------
        (hold_trigger, should_cancel)
        hold_trigger    : set fire[1] = True this tick
        should_cancel   : True if we must release (lets charge go to zero while idle)
        """
        if weapon_obs is None:
            return False, False   # no laser equipped

        # --- Handle short status ---
        if self.state == LaserState.SHORTED:
            if t >= self._short_until:
                self.state = LaserState.READY
            else:
                return False, True

        # --- Read weapon state from observation ---
        charge   = weapon_obs.get("charge",   0.0)
        cooldown = weapon_obs.get("cooldown",  0.0)

        # --- Cooldown: just fired ---
        if cooldown > 0.01:
            self.state = LaserState.COOLDOWN
            return False, False

        # Transition out of cooldown
        if self.state == LaserState.COOLDOWN:
            self.state = LaserState.READY

        # --- Not charging; decide whether to start ---
        if self.state == LaserState.READY:
            if target_visible and aim_error_deg <= 15.0:
                # Start charging — hold the trigger
                self.state = LaserState.CHARGING
                return True, False
            return False, False

        # --- Currently charging ---
        if self.state == LaserState.CHARGING:
            # Abort if no target or aim is way off
            if not target_visible or aim_error_deg > 25.0:
                self.state = LaserState.READY
                return False, True  # release trigger

            # Release at full charge when aim is tight
            if charge >= C.LASER_CHARGE_TIME * 0.93 and aim_error_deg <= 1.5:
                self.state = LaserState.COOLDOWN   # will confirm when obs updates
                return False, False   # release = fire

            # Keep charging
            return True, False

        return False, False


# ---------------------------------------------------------------------------
# 3. Target selection
# ---------------------------------------------------------------------------

def select_target(obs: dict, tracker_beliefs: dict, world=None, cubes: int = 0) -> Optional[dict]:
    """
    Choose highest-priority target:
      1. T0 enemy mech with direct LOS (lowest HP first)
      2. T0 enemy mech without direct LOS (lowest HP first)
      3. T1 enemy mech from tracker beliefs
      4. If no enemy mechs & cubes < 10:
         - Nearest NPC drone within 70m with LOS (drops 2 cubes)
         - Nearest crate within 60m with LOS (drops 1 cube + 50% weapon)
    """
    me = obs["self"]
    my_x, my_y, my_z = me["pos"]
    muzzle_h = me.get("stats", {}).get("muzzle_height", C.MECH_MUZZLE_HEIGHT)

    # --- 1. Directly visible enemy mechs ---
    t0_mechs = [m for m in obs.get("mechs", []) if m["kind"] == "mech"]

    if t0_mechs:
        def target_rank(m):
            tx, ty, tz = m["pos"]
            has_los = True
            if world is not None:
                has_los = world.has_los_2d(my_x, my_y, my_z + muzzle_h, tx, ty, tz + 4.5)
            # LOS first (0 = yes, 1 = no), then lowest HP, then distance
            return (0 if has_los else 1, m.get("hp", 9999), _dist2d(my_x, my_y, tx, ty))

        return min(t0_mechs, key=target_rank)

    # --- 2. T1 tracker beliefs ---
    t1 = [v for v in tracker_beliefs.values() if v.get("tier") == 1 and v.get("kind") == "mech"]
    if t1:
        return min(t1, key=lambda b: b.get("hp", 9999))

    # --- 3. Resource farming (only when under stat cap: cubes < 10) ---
    if cubes < 10:
        # Nearest visible NPC drone within 70 m
        npcs = [m for m in obs.get("mechs", []) if m["kind"] == "npc"]
        valid_npcs = []
        for n in npcs:
            nx, ny, nz = n["pos"]
            d = _dist2d(my_x, my_y, nx, ny)
            if d <= 70.0:
                if world is None or world.has_los_2d(my_x, my_y, my_z + muzzle_h, nx, ny, nz + 1.8):
                    valid_npcs.append((d, n))
        if valid_npcs:
            valid_npcs.sort(key=lambda x: x[0])
            return valid_npcs[0][1]

        # Nearest visible crate within 60 m
        crates = obs.get("crates", [])
        valid_crates = []
        for c in crates:
            cx, cy, cz = c["pos"]
            d = _dist2d(my_x, my_y, cx, cy)
            if d <= 60.0:
                if world is None or world.has_los_2d(my_x, my_y, my_z + muzzle_h, cx, cy, cz + 1.0):
                    valid_crates.append((d, {
                        "id": -c["id"],
                        "kind": "crate",
                        "pos": [cx, cy, cz],
                        "vel": [0.0, 0.0, 0.0],
                        "hp": c.get("hp", 300.0),
                    }))
        if valid_crates:
            valid_crates.sort(key=lambda x: x[0])
            return valid_crates[0][1]

    return None


# ---------------------------------------------------------------------------
# 4. Upgrade selection
# ---------------------------------------------------------------------------

def pick_upgrade(offer: dict) -> int:
    """
    Given an obs["offer"] dict, return the index (0, 1, or 2) of the best
    upgrade according to config.UPGRADE_PRIORITY.
    Falls back to index 0 if none match the priority list.
    """
    choices = offer.get("choices", [])
    for preferred_id in C.UPGRADE_PRIORITY:
        for i, choice in enumerate(choices):
            if choice.get("id") == preferred_id:
                return i
    return 0   # default to first choice


# ---------------------------------------------------------------------------
# 5. Weapon pickup decision
# ---------------------------------------------------------------------------

def should_take_pickup(obs: dict, pickup: dict) -> bool:
    """
    Decide whether to take a weapon pickup.
    Never drop the Arm Gun (slot 0).
    Take any weapon if slot 1 is empty, or upgrade (laser > lightning > mortar).
    """
    kind = pickup.get("kind", "")
    if kind not in ("laser", "lightning", "mortar"):
        return False

    weapons = obs["self"].get("weapons", [None, None])
    slot1 = weapons[1] if len(weapons) > 1 else None
    if slot1 is None:
        return True

    slot1_kind = slot1.get("kind")
    # Hierarchy: laser > lightning > mortar
    if slot1_kind == "mortar" and kind in ("lightning", "laser"):
        return True
    if slot1_kind == "lightning" and kind == "laser":
        return True

    return False


# ---------------------------------------------------------------------------
# 6. CombatController — main per-tick fire-control logic
# ---------------------------------------------------------------------------

class CombatController:
    """
    Wraps all weapon logic. Called each tick with the latest observation
    and tracker beliefs.

    Returns a partial action dict containing:
        aim       : [yaw, pitch]
        fire      : [fire_slot0, fire_slot1]
        reload    : bool
        take      : int | (absent)
        drop      : int | (absent)
        pick      : int | (absent)   (upgrade selection)
    """

    def __init__(self) -> None:
        self._laser = LaserController()
        self._last_aim: List[float] = [0.0, 0.0]
        self._scan_yaw: float = 0.0
        # Smoothed target velocity (exponential moving average)
        self._vel_alpha    = 0.5    # EMA weight for new observations
        self._smoothed_vel: Dict[int, List[float]] = {}
        self._last_pos:     Dict[int, List[float]] = {}

    # ------------------------------------------------------------------
    def _smooth_velocity(self, actor_id: int, pos: List[float],
                         obs_vel: List[float]) -> List[float]:
        """
        Blend the server-reported velocity with an EMA to reduce jitter
        from discrete-physics updates (the server runs at 20 Hz but we
        only see 10 Hz snapshots).
        """
        if actor_id not in self._smoothed_vel:
            self._smoothed_vel[actor_id] = list(obs_vel)
        else:
            alpha = self._vel_alpha
            prev  = self._smoothed_vel[actor_id]
            self._smoothed_vel[actor_id] = [
                alpha * v + (1.0 - alpha) * p
                for v, p in zip(obs_vel, prev)
            ]
        self._last_pos[actor_id] = list(pos)
        return self._smoothed_vel[actor_id]

    # ------------------------------------------------------------------
    def _aim_error_deg(self, actual_aim: List[float],
                       target_yaw: float, target_pitch: float) -> float:
        """Angular distance between actual torso aim and desired aim (degrees)."""
        dyaw   = abs(actual_aim[0] - target_yaw)
        dyaw   = min(dyaw, 2 * math.pi - dyaw)   # wrap
        dpitch = abs(actual_aim[1] - target_pitch)
        return math.degrees(math.hypot(dyaw, dpitch))

    # ------------------------------------------------------------------
    def decide(self, obs: dict, tracker_beliefs: dict, world=None, state: str = "") -> dict:
        """
        Main entry point called by brain.py each tick.
        Returns a partial action dict (tick will be set by the wrapper).
        """
        me       = obs["self"]
        t        = obs["t"]
        pos      = me["pos"]           # [x, y, z]
        vel      = me["vel"]
        cubes    = me.get("cubes", 0)
        weapons  = me.get("weapons", [None, None])
        aim      = me.get("aim", [0.0, 0.0])  # actual slewed torso aim
        muzzle_h = me.get("stats", {}).get("muzzle_height", C.MECH_MUZZLE_HEIGHT)
        pickup_radius = obs.get("config", {}).get("loot", {}).get("pickup_radius", 4.0)

        action: dict = {}

        # ---- Upgrade offer ----
        offer = obs.get("offer")
        if offer:
            action["pick"] = pick_upgrade(offer)

        # ---- Handle short events ----
        for event in obs.get("events", []):
            if event.get("type") == "effect" and event.get("effect") == "short":
                self._laser.notify_shorted(t)

        # ---- Weapon pickup (with 3D distance check) ----
        for pickup in obs.get("pickups", []):
            px, py, pz = pickup["pos"]
            dist_xy = _dist2d(pos[0], pos[1], px, py)
            dist_z  = abs(pos[2] - pz)
            if dist_xy <= pickup_radius and dist_z <= 4.0 and should_take_pickup(obs, pickup):
                slot1 = weapons[1] if len(weapons) > 1 else None
                if slot1 is not None:
                    action["drop"] = 1
                action["take"] = pickup["id"]
                break

        # ---- Select target (including NPC/crate farming) ----
        target = select_target(obs, tracker_beliefs, world=world, cubes=cubes)

        # ---- Active Torso Scanning when no target ----
        if target is None:
            # Check for recent sounds to snap aim towards
            recent_sounds = [s for s in obs.get("sounds", []) if s.get("kind") in ("step", "boost", "gun", "laser_charge", "lightning", "mortar")]
            if recent_sounds:
                best_sound = min(recent_sounds, key=lambda s: s.get("age", 999.0))
                scan_yaw = best_sound.get("bearing", self._scan_yaw)
                # Wrap to [-pi, pi]
                scan_yaw = (scan_yaw + math.pi) % (2.0 * math.pi) - math.pi
                action["aim"] = [scan_yaw, 0.0]
                self._scan_yaw = scan_yaw
            else:
                # Continuously sweep yaw at ~1.5 rad/s (+0.15 rad / 0.1s tick) wrapped to [-pi, pi]
                yaw = (self._scan_yaw + 0.15)
                self._scan_yaw = (yaw + math.pi) % (2.0 * math.pi) - math.pi
                action["aim"] = [self._scan_yaw, 0.0]

            self._last_aim = list(action["aim"])
            action["fire"] = [False, False]

            # Auto-reload if empty
            w0 = weapons[0] if weapons else None
            if w0 and w0.get("kind") == "gun":
                if w0.get("ammo", 1) == 0 and w0.get("reload", 0.0) == 0.0:
                    action["reload"] = True
            return action

        # ---- Smooth target velocity ----
        tgt_id  = target.get("id", -1)
        tgt_pos = target.get("pos", [0.0, 0.0, 0.0])
        tgt_vel = self._smooth_velocity(tgt_id, tgt_pos,
                                        target.get("vel", [0.0, 0.0, 0.0]))

        dist_to_target = _dist2d(pos[0], pos[1], tgt_pos[0], tgt_pos[1])

        # Target center of mass (mech: +4.5m, npc: +1.8m, crate: +1.0m)
        tgt_h = 4.5
        if target.get("kind") == "npc":
            tgt_h = 1.8
        elif target.get("kind") == "crate":
            tgt_h = 1.0

        # ---- Check Line of Sight ----
        has_los = True
        if world is not None:
            has_los = world.has_los_2d(pos[0], pos[1], pos[2] + muzzle_h,
                                       tgt_pos[0], tgt_pos[1], tgt_pos[2] + tgt_h)

        # ---- Primary aim: gun quadratic lead ----
        gun_aim = gun_lead_aim(pos, tgt_pos, tgt_vel, muzzle_h)
        if gun_aim is None:
            dx = tgt_pos[0] - pos[0]
            dy = tgt_pos[1] - pos[1]
            dz = (tgt_pos[2] + tgt_h) - (pos[2] + muzzle_h)
            gun_aim = (math.atan2(dy, dx),
                       math.atan2(dz, math.hypot(dx, dy)))

        aim_yaw, aim_pitch = gun_aim
        aim_pitch = max(C.MIN_PITCH, min(C.MAX_PITCH, aim_pitch))
        action["aim"] = [aim_yaw, aim_pitch]
        self._last_aim = [aim_yaw, aim_pitch]

        # ---- Slot 1 weapon analysis ----
        w0 = weapons[0] if len(weapons) > 0 else None
        w1 = weapons[1] if len(weapons) > 1 else None
        w1_kind = w1["kind"] if w1 else None

        # ---- Slot 0 (Arm Gun) fire decision ----
        fire0 = False
        if w0 and w0.get("kind") == "gun":
            w0_ammo     = w0.get("ammo", 1)
            w0_cooldown = w0.get("cooldown", 0.0)
            w0_reload   = w0.get("reload", 0.0)

            if w0_ammo == 0 and w0_reload == 0.0:
                action["reload"] = True

            # Fire whenever cooldown is up, in range, within 25° cone, AND WE HAVE LOS!
            if w0_cooldown <= 0.01 and dist_to_target <= C.GUN_RANGE and has_los:
                err_deg = self._aim_error_deg(aim, aim_yaw, aim_pitch)
                if err_deg <= 25.0:
                    fire0 = True

        # ---- Slot 1 weapon fire decision ----
        fire1 = False

        if w1_kind == "laser":
            target_visible = target.get("id") in [
                m["id"] for m in obs.get("mechs", [])
            ]
            err_deg = self._aim_error_deg(aim, aim_yaw, aim_pitch)
            dx = tgt_pos[0] - pos[0]
            dy = tgt_pos[1] - pos[1]
            dz = (tgt_pos[2] + tgt_h) - (pos[2] + muzzle_h)
            las_yaw   = math.atan2(dy, dx)
            las_pitch = max(C.MIN_PITCH, min(C.MAX_PITCH, math.atan2(dz, math.hypot(dx, dy))))
            las_err   = self._aim_error_deg(aim, las_yaw, las_pitch)

            # Only charge and fire laser if we have direct line of sight!
            hold, cancel = self._laser.step(t, w1, target_visible and has_los, las_err)
            fire1 = hold and has_los
            if w1 and w1.get("charge", 0.0) > 0.05:
                action["aim"] = [las_yaw, las_pitch]

        elif w1_kind == "mortar" and target.get("kind") != "crate":
            mort_cd = w1.get("cooldown", 0.0)
            tube_pos = [pos[0], pos[1], pos[2]]

            # Cluster Targeting: When Mortar ready, search for >= 2 tracked enemies within 12 m of each other
            cluster_aim_pos = None
            if mort_cd <= 0.05 and tracker_beliefs:
                tracked_mechs = [
                    b["pos"] for b in tracker_beliefs.values()
                    if b.get("tier", 9) <= 1 and b.get("kind") == "mech"
                ]
                n_mechs = len(tracked_mechs)
                for i in range(n_mechs):
                    for j in range(i + 1, n_mechs):
                        p1 = tracked_mechs[i]
                        p2 = tracked_mechs[j]
                        if _dist2d(p1[0], p1[1], p2[0], p2[1]) <= 12.0:
                            mid_x = (p1[0] + p2[0]) * 0.5
                            mid_y = (p1[1] + p2[1]) * 0.5
                            mid_z = (p1[2] + p2[2]) * 0.5
                            if _dist2d(pos[0], pos[1], mid_x, mid_y) > C.MORTAR_MIN_RANGE:
                                cluster_aim_pos = [mid_x, mid_y, mid_z]
                                break
                    if cluster_aim_pos is not None:
                        break

            chosen_mortar_tgt = cluster_aim_pos if cluster_aim_pos is not None else tgt_pos
            chosen_mortar_dist = _dist2d(pos[0], pos[1], chosen_mortar_tgt[0], chosen_mortar_tgt[1])

            if chosen_mortar_dist > C.MORTAR_MIN_RANGE:
                result = mortar_pitch(tube_pos, chosen_mortar_tgt, high_arc=True)
                if result is not None:
                    (pitch_low, pitch_high), mort_yaw = result
                    mort_pitch = max(C.MIN_PITCH, min(C.MAX_PITCH, pitch_high))

                    # Fix Mortar Aim Hijack: Only hijack torso aim when mortar is ready to fire
                    # AND (cluster detected OR target is behind cover OR far away > 45 m)! Otherwise keep Gun lead aim.
                    if mort_cd <= 0.05 and (cluster_aim_pos is not None or not has_los or dist_to_target > 45.0):
                        action["aim"] = [mort_yaw, mort_pitch]
                        if mort_cd <= 0.01:
                            fire1 = True

        elif w1_kind == "lightning" and target.get("kind") != "crate":
            if dist_to_target <= C.LIGHTNING_RANGE * 0.9 and has_los:
                me_energy = me.get("energy", 0.0)
                if me_energy >= C.LIGHTNING_ENERGY * 2:
                    lt_cd = w1.get("cooldown", 0.0)
                    if lt_cd <= 0.01:
                        fire1 = True

        # ---- Fix DISENGAGE_REGEN Self-Sabotage ----
        # In DISENGAGE_REGEN, hold fire unless enemy is dangerously close (< 25 m)
        # to allow the 5.0 s out-of-combat HP regeneration to trigger!
        if state == "DISENGAGE_REGEN" and dist_to_target > 25.0:
            fire0 = False
            fire1 = False

        action["fire"] = [fire0, fire1]
        return action
