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

def select_target(obs: dict, tracker_beliefs: dict) -> Optional[dict]:
    """
    Choose the highest-priority target from currently visible mechs and
    tracker beliefs.

    Priority rules (in order):
      1. T0 mech with lowest HP (kill it fast to prevent escalation)
      2. T1 mech with lowest HP (server still tracking — shoot it)
      3. T0 mech closest to us (threat to our position)

    Returns the mech obs dict (from obs["mechs"]) or a synthesised belief
    dict with the same keys, or None if nothing is worth targeting.
    """
    me = obs["self"]
    my_x, my_y, my_z = me["pos"]

    # --- Collect T0 mechs (directly visible) ---
    t0_mechs = [m for m in obs.get("mechs", []) if m["kind"] == "mech"]

    if not t0_mechs:
        # Try T1 tracker beliefs
        t1 = [v for v in tracker_beliefs.values() if v.get("tier") == 1]
        if not t1:
            return None
        return min(t1, key=lambda b: b.get("hp", 9999))

    # Prefer lowest HP target — finish off weakened enemies
    target = min(t0_mechs, key=lambda m: (
        m.get("hp", 9999),
        _dist2d(my_x, my_y, m["pos"][0], m["pos"][1])
    ))
    return target


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
    Take any weapon if slot 1 is empty, or upgrade (laser > mortar > lightning).
    """
    kind = pickup.get("kind", "")
    if kind not in ("laser", "mortar", "lightning"):
        return False

    weapons = obs["self"].get("weapons", [None, None])
    slot1 = weapons[1] if len(weapons) > 1 else None
    if slot1 is None:
        return True

    slot1_kind = slot1.get("kind")
    if slot1_kind == "lightning" and kind in ("laser", "mortar"):
        return True
    if slot1_kind == "mortar" and kind == "laser":
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
    def decide(self, obs: dict, tracker_beliefs: dict) -> dict:
        """
        Main entry point called by brain.py each tick.
        Returns a partial action dict (tick will be set by the wrapper).
        """
        me      = obs["self"]
        t       = obs["t"]
        pos     = me["pos"]           # [x, y, z]
        vel     = me["vel"]
        weapons = me.get("weapons", [None, None])
        aim     = me.get("aim", [0.0, 0.0])  # actual slewed torso aim
        muzzle_h = me.get("stats", {}).get("muzzle_height", C.MECH_MUZZLE_HEIGHT)
        mortar_h = me.get("stats", {}).get("height", C.MECH_HEIGHT) + 0.5

        action: dict = {}

        # ---- Upgrade offer ----
        offer = obs.get("offer")
        if offer:
            action["pick"] = pick_upgrade(offer)

        # ---- Handle short events ----
        for event in obs.get("events", []):
            if event.get("type") == "effect" and event.get("effect") == "short":
                self._laser.notify_shorted(t)

        # ---- Weapon pickup ----
        for pickup in obs.get("pickups", []):
            px, py = pickup["pos"][0], pickup["pos"][1]
            dist   = _dist2d(pos[0], pos[1], px, py)
            if dist <= 4.0 and should_take_pickup(obs, pickup):
                slot1 = weapons[1] if len(weapons) > 1 else None
                # Drop old slot-1 weapon first if needed
                if slot1 is not None:
                    action["drop"] = 1
                action["take"] = pickup["id"]
                break   # only pick up one per tick

        # ---- Select target ----
        target = select_target(obs, tracker_beliefs)

        if target is None:
            # No target — maintain last aim, don't fire
            action["aim"]  = list(self._last_aim)
            action["fire"] = [False, False]
            # Reload if gun is empty and not reloading
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

        # ---- Primary aim: gun quadratic lead ----
        gun_aim = gun_lead_aim(pos, tgt_pos, tgt_vel, muzzle_h)
        if gun_aim is None:
            # Fallback: direct aim
            dx = tgt_pos[0] - pos[0]
            dy = tgt_pos[1] - pos[1]
            dz = (tgt_pos[2] + 4.5) - (pos[2] + muzzle_h)
            gun_aim = (math.atan2(dy, dx),
                       math.atan2(dz, math.hypot(dx, dy)))

        aim_yaw, aim_pitch = gun_aim
        aim_pitch = max(C.MIN_PITCH, min(C.MAX_PITCH, aim_pitch))
        action["aim"] = [aim_yaw, aim_pitch]
        self._last_aim = [aim_yaw, aim_pitch]

        # ---- Determine which slot-1 weapon we have ----
        w0 = weapons[0] if len(weapons) > 0 else None
        w1 = weapons[1] if len(weapons) > 1 else None
        w1_kind = w1["kind"] if w1 else None

        # ---- Slot 0 (Arm Gun) fire decision ----
        fire0 = False
        if w0 and w0.get("kind") == "gun":
            w0_ammo     = w0.get("ammo", 1)
            w0_cooldown = w0.get("cooldown", 0.0)
            w0_reload   = w0.get("reload", 0.0)

            # Only auto-reload if empty
            if w0_ammo == 0 and w0_reload == 0.0:
                action["reload"] = True

            # Fire whenever target is within range and within front cone!
            if w0_cooldown <= 0.01 and dist_to_target <= C.GUN_RANGE:
                err_deg = self._aim_error_deg(aim, aim_yaw, aim_pitch)
                if err_deg <= 25.0:
                    fire0 = True

        # ---- Slot 1 weapon fire decision ----
        fire1 = False

        if w1_kind == "laser":
            target_visible = target.get("id") in [
                m["id"] for m in obs.get("mechs", []) if m["kind"] == "mech"
            ]
            err_deg = self._aim_error_deg(aim, aim_yaw, aim_pitch)
            # Override aim to direct-aim for laser (hitscan, no lead needed)
            dx = tgt_pos[0] - pos[0]
            dy = tgt_pos[1] - pos[1]
            dz = (tgt_pos[2] + 4.5) - (pos[2] + muzzle_h)
            las_yaw   = math.atan2(dy, dx)
            las_pitch = math.atan2(dz, math.hypot(dx, dy))
            las_pitch = max(C.MIN_PITCH, min(C.MAX_PITCH, las_pitch))
            las_err   = self._aim_error_deg(aim, las_yaw, las_pitch)

            hold, cancel = self._laser.step(t, w1, target_visible, las_err)
            fire1 = hold
            # Use laser aim (direct, not lead)
            if w1 and w1.get("charge", 0.0) > 0.05:
                # Already charging — keep the laser aim direction
                action["aim"] = [las_yaw, las_pitch]

        elif w1_kind == "mortar" and dist_to_target > C.MORTAR_MIN_RANGE:
            # Mortar: compute arc, prefer high-arc to lob over buildings
            tube_pos = [pos[0], pos[1], pos[2]]
            result   = mortar_pitch(tube_pos, tgt_pos, high_arc=True)
            if result is not None:
                (pitch_low, pitch_high), mort_yaw = result
                mort_pitch = pitch_high   # always try to clear buildings
                mort_pitch = max(C.MIN_PITCH, min(C.MAX_PITCH, mort_pitch))
                action["aim"] = [mort_yaw, mort_pitch]
                mort_cd = w1.get("cooldown", 0.0)
                if mort_cd <= 0.01:
                    fire1 = True

        elif w1_kind == "lightning":
            # Lightning: short range (<45 m), only fire if very close
            if dist_to_target <= C.LIGHTNING_RANGE * 0.9:
                me_energy = me.get("energy", 0.0)
                if me_energy >= C.LIGHTNING_ENERGY * 2:
                    lt_cd = w1.get("cooldown", 0.0)
                    if lt_cd <= 0.01:
                        fire1 = True

        action["fire"] = [fire0, fire1]
        return action
