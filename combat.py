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
    center_h: float = 4.5,
    latency: float = None,
) -> Optional[Tuple[float, float]]:
    """
    Compute the (yaw, pitch) needed to intercept a moving target with an
    Arm Gun projectile (flat trajectory, constant speed, no gravity drop).

    center_h: height of the point to hit above the target's feet (4.5 m for a
              mech, scaled up by its cubes; less for drones and crates).
    latency:  seconds between deciding and the shot leaving the barrel (the
              action applies on the next sim tick and the torso is still
              slewing); the target keeps moving meanwhile, so lead for it.

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

    if latency is None:
        latency = C.AIM_LATENCY_S
    Vt = list(target_vel)                  # target velocity

    # Aim at the centre of mass, where the target will be when the shot leaves
    aim_at = [target_pos[0] + Vt[0] * latency,
              target_pos[1] + Vt[1] * latency,
              target_pos[2] + center_h + Vt[2] * latency]

    D  = _sub(aim_at, muzzle)              # relative position

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
    tube_height: Optional[float] = None,
) -> Optional[Tuple[Tuple[float, float], float]]:
    """
    Compute the mortar tube pitch (radians) to hit tgt_pos from tube_pos.

    Physics: parabolic, no air drag, constant gravity.
        tan(θ) = (v0² ± sqrt(v0⁴ - g(gR² + 2Δz·v0²))) / (g·R)

    Returns (pitch_low, pitch_high) where low-arc arrives sooner and
    high-arc clears buildings. Returns None if target is out of range.

    The caller picks pitch_high when lobbing over obstacles (default),
    pitch_low when the target is in the open.

    Pitches are NOT clamped here: the caller must check them against
    [MIN_PITCH, MAX_PITCH], because a clamped pitch lands somewhere else.
    """
    v0 = C.MORTAR_SPEED
    g  = C.MORTAR_GRAVITY
    if tube_height is None:
        tube_height = C.MECH_MORTAR_HEIGHT

    # Mortar tube is at the top of the mech
    tube = [tube_pos[0], tube_pos[1], tube_pos[2] + tube_height]

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

    return (pitch_low, pitch_high), yaw


def mortar_lead_aim(
    me_pos: List[float],
    tgt_pos: List[float],
    tgt_vel: List[float],
    tube_height: Optional[float] = None,
    prefer_high: bool = True,
    iters: int = 3,
) -> Optional[Tuple[float, float, float]]:
    """
    Aim a mortar shell at where a moving target will be when the shell lands.

    Iterates: solve the arc to the predicted point, compute the flight time
    T = R / (v0·cos θ), move the predicted point to tgt + v·T, repeat.
    Uses the high arc when it fits within the pitch limits, else the low arc.

    Returns (yaw, pitch, flight_time) or None if no legal arc reaches it.
    """
    if tube_height is None:
        tube_height = C.MECH_MORTAR_HEIGHT
    v0 = C.MORTAR_SPEED
    vx, vy = (tgt_vel[0], tgt_vel[1]) if tgt_vel else (0.0, 0.0)

    aim_pt = list(tgt_pos)
    sol = None
    for _ in range(max(1, iters)):
        res = mortar_pitch(me_pos, aim_pt, tube_height=tube_height)
        if res is None:
            return None
        (p_low, p_high), yaw = res
        legal = [p for p in ((p_high, p_low) if prefer_high else (p_low, p_high))
                 if C.MIN_PITCH <= p <= C.MAX_PITCH]
        if not legal:
            return None
        pitch = legal[0]
        R = math.hypot(aim_pt[0] - me_pos[0], aim_pt[1] - me_pos[1])
        T = R / max(1e-3, v0 * math.cos(pitch))
        sol = (yaw, pitch, T)
        aim_pt = [tgt_pos[0] + vx * T, tgt_pos[1] + vy * T, tgt_pos[2]]
    return sol


def gun_fire_tolerance_deg(dist: float, target_radius: float) -> float:
    """
    Largest aim error (deg) at which a gun shot still hits: the angle the
    target's body subtends (80 % of its radius), but never below GUN_MIN_TOL_DEG.
    """
    ang = math.degrees(math.atan2(0.8 * target_radius, max(1.0, dist)))
    return max(C.GUN_MIN_TOL_DEG, ang)


def _wrap(a: float) -> float:
    return (a + math.pi) % (2.0 * math.pi) - math.pi


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
             aim_error_deg: float,
             release_tol_deg: float = 1.5,
             full_charge: Optional[float] = None) -> Tuple[bool, bool]:
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

            # Release at (near) full charge once the beam would land on the target body.
            # release_tol_deg is the target's angular size plus slack, so it is
            # reachable while both mechs move (a fixed 1.5° rarely was).
            if full_charge is None:
                full_charge = C.LASER_CHARGE_TIME
            if charge >= full_charge * 0.93 and aim_error_deg <= release_tol_deg:
                self.state = LaserState.COOLDOWN   # will confirm when obs updates
                return False, False   # release = fire

            # Keep charging
            return True, False

        return False, False


# ---------------------------------------------------------------------------
# 3. Target selection
# ---------------------------------------------------------------------------

def select_target(obs: dict, tracker_beliefs: dict, world=None, cubes: int = 0,
                  attackers: Optional[Dict[int, float]] = None,
                  lock_id: Optional[int] = None) -> Optional[dict]:
    """
    Choose highest-priority target:
      1. T0 enemy mech with direct LOS, ranked by a score mixing distance, HP,
         who is shooting us right now, and laser cook-off on mortar carriers
      2. T0 enemy mech without direct LOS (same score)
      3. T1 enemy mech from tracker beliefs
      4. If no enemy mechs & cubes < 10:
         - Nearest NPC drone within 70m with LOS (drops 2 cubes)
         - Nearest crate within 60m with LOS (drops 1 cube + 50% weapon)

    `lock_id` is the target we chose last tick. If it is still a legal, visible
    mech we keep it unless another mech beats it by TARGET_SWITCH_MARGIN. A
    torso turn is 0.785 s with no shots, so flapping between two similar
    targets (and the strafe direction that follows the target) is pure loss.
    """
    me = obs["self"]
    my_x, my_y, my_z = me["pos"]
    muzzle_h = me.get("stats", {}).get("muzzle_height", C.MECH_MUZZLE_HEIGHT)

    # --- 1. Directly visible enemy mechs ---
    t0_mechs = [m for m in obs.get("mechs", []) if m["kind"] == "mech"]

    if t0_mechs:
        t_now = obs.get("t", 0.0)
        w = me.get("weapons", [None, None])
        my_laser = len(w) > 1 and w[1] is not None and w[1].get("kind") == "laser"

        def score(m):
            tx, ty = m["pos"][0], m["pos"][1]
            # hp is omitted entirely when vision.show_enemy_hp is false, and may
            # be present-but-None: treat both as "unknown", never as a number.
            hp = m.get("hp")
            if not isinstance(hp, (int, float)):
                hp = 1400.0
            s = _dist2d(my_x, my_y, tx, ty) + C.TARGET_HP_WEIGHT * hp
            if attackers and t_now - attackers.get(m["id"], -1e9) < 2.0:
                s -= 60.0          # whoever is hurting us right now
            if my_laser and "mortar" in (m.get("weapons") or []):
                s -= 30.0          # cook-off: +60 % laser damage
            return s

        ranked = sorted(t0_mechs, key=score)

        # --- Stickiness: keep the locked target unless clearly beaten ---
        if lock_id is not None and C.TARGET_STICKY:
            current = next((m for m in ranked if m["id"] == lock_id), None)
            if current is not None and score(current) <= score(ranked[0]) + C.TARGET_SWITCH_MARGIN:
                return current

        # Best score with a clear shot wins. LOS rays are the expensive part,
        # so only the 4 best-scored candidates get one.
        if world is None:
            return ranked[0]
        for m in ranked[:4]:
            tx, ty, tz = m["pos"]
            if world.has_los_2d(my_x, my_y, my_z + muzzle_h, tx, ty, tz + 4.5):
                return m
        return ranked[0]

    # --- 2. T1 tracker beliefs ---
    t1 = [v for v in tracker_beliefs.values() if v.get("tier") == 1 and v.get("kind") == "mech"]
    if t1:
        # Same unknown-HP hazard as score(): don't let None compare against None.
        return min(t1, key=lambda b: b.get("hp") if isinstance(b.get("hp"), (int, float)) else 9999)

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

# Mortar is deliberately absent (never picked up): it is beaten by both other
# specials, useless up close, a laser's cook-off target, and the strong bots in
# practice skip it. An empty slot 1 costs nothing; the next lightning/laser fills it.
WEAPON_PRIORITY = {"lightning": 3, "laser": 2}


def should_take_pickup(obs: dict, pickup: dict) -> bool:
    """
    Decide whether to take a weapon pickup.
    Never drop the Arm Gun (slot 0).
    Take any weapon if slot 1 is empty, or upgrade (lightning > laser > mortar).

    Lightning leads: 240 DPS at close range, shorts a laser, and the strongest
    bots in practice build around it. Mortar is only a placeholder (it is
    beaten by both and useless up close).
    """
    kind = pickup.get("kind", "")
    if kind not in WEAPON_PRIORITY:
        return False

    weapons = obs["self"].get("weapons", [None, None])
    # Never hold two of a kind (the server refuses it)
    if any(w and w.get("kind") == kind for w in weapons):
        return False
    slot1 = weapons[1] if len(weapons) > 1 else None
    if slot1 is None:
        return True

    return WEAPON_PRIORITY[kind] > WEAPON_PRIORITY.get(slot1.get("kind"), 0)


# ---------------------------------------------------------------------------
# 6. CombatController — main per-tick fire-control logic
# ---------------------------------------------------------------------------

class LeadLearner:
    """
    Guess-factor style statistical targeting (the Robocode champions' trick).

    Linear lead assumes the target keeps its current velocity for the whole flight;
    against a bot that strafe-flips it overshoots on every reversal. So we ask the match
    itself: every time we fire, remember a VIRTUAL bullet wave (where we shot from, where
    the target was and how it moved). When the wave reaches the target's distance, check
    which lead multipliers (0 = aim at it, 1 = full linear lead, ...) WOULD have hit given
    where the target actually is, and tally that per distance bucket. The multiplier with
    the best tally becomes the lead scale for the next shots. Until a bucket has enough
    samples, or while 1.0 is as good as anything, we keep plain linear lead.
    """
    MULTS = (0.0, 0.4, 0.7, 1.0, 1.3)
    BUCKETS = (50.0, 100.0, 160.0, 1e9)
    MIN_SAMPLES = 8
    DECAY = 0.97

    def __init__(self) -> None:
        self.waves: List[dict] = []
        self.score = [[0.0] * len(self.MULTS) for _ in self.BUCKETS]
        self.count = [0] * len(self.BUCKETS)

    def _bucket(self, d: float) -> int:
        for i, b in enumerate(self.BUCKETS):
            if d <= b:
                return i
        return len(self.BUCKETS) - 1

    def best(self, dist: float) -> float:
        b = self._bucket(dist)
        if self.count[b] < self.MIN_SAMPLES:
            return 1.0
        s = list(self.score[b])
        s[self.MULTS.index(1.0)] += 0.5            # prior: linear lead until something clearly beats it
        return self.MULTS[max(range(len(s)), key=lambda i: s[i])]

    def on_shot(self, t: float, org, tid: int, tpos, tvel, speed: float) -> None:
        self.waves.append({"t": t, "ox": org[0], "oy": org[1], "id": tid,
                           "px": tpos[0], "py": tpos[1], "vx": tvel[0], "vy": tvel[1],
                           "speed": max(speed, 1.0)})
        if len(self.waves) > 24:
            self.waves.pop(0)

    def update(self, t: float, targets: Dict[int, Tuple[float, float, float, float]]) -> None:
        keep = []
        for w in self.waves:
            age = t - w["t"]
            tg = targets.get(w["id"])
            if age > 2.5 or (tg is None and age > 0.4):
                continue
            if tg is None:
                keep.append(w)
                continue
            dist_now = math.hypot(tg[0] - w["ox"], tg[1] - w["oy"])
            if w["speed"] * age < dist_now - 1.0:       # the wave has not reached it yet
                keep.append(w)
                continue
            actual = math.atan2(tg[1] - w["oy"], tg[0] - w["ox"])
            body = math.atan2(C.MECH_RADIUS, max(dist_now, 1.0))
            flight = math.hypot(w["px"] - w["ox"], w["py"] - w["oy"]) / w["speed"]
            b = self._bucket(dist_now)
            for i, m in enumerate(self.MULTS):
                ang = math.atan2(w["py"] + w["vy"] * flight * m - w["oy"],
                                 w["px"] + w["vx"] * flight * m - w["ox"])
                self.score[b][i] = self.score[b][i] * self.DECAY + (
                    1.0 if abs(_wrap(ang - actual)) < body else 0.0)
            self.count[b] += 1


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
        # EMA weight for new observations. 1.0 = raw server velocity. The server's
        # velocity is exact, so smoothing only adds lag behind strafe reversals: in a
        # duel simulation 0.5 hit 75 % of shots, 1.0 hit 85 % (+22 % damage per second).
        self._vel_alpha    = 1.0
        self._smoothed_vel: Dict[int, List[float]] = {}
        self._last_pos:     Dict[int, List[float]] = {}
        # Who shot us and when (match time), and when we last took weapon damage
        self._attackers:    Dict[int, float] = {}
        self._last_hurt_t:  float = -1e9
        self._hurt_bearing: Optional[float] = None   # yaw toward our latest attacker
        self._npc_hurt_t:   float = -1e9             # when a drone last hit us
        # Current target (read by motion.py for positioning)
        self.target: Optional[dict] = None
        self._learner = LeadLearner()
        self._last_wave_t: float = -1e9

    # ------------------------------------------------------------------
    @staticmethod
    def _residual_err_deg(aim: List[float], yaw: float, pitch: float,
                          dt: float, turn_rate: float, pitch_rate: float) -> float:
        """
        Aim error (deg) left after the torso slews for `dt` seconds toward
        (yaw, pitch). A held trigger fires on the first sim tick the weapon is
        ready, by which time the torso has already turned some of the way.
        """
        dyaw = abs(_wrap(aim[0] - yaw))
        dpitch = abs(aim[1] - pitch)
        ry = max(0.0, dyaw - turn_rate * dt)
        rp = max(0.0, dpitch - pitch_rate * dt)
        return math.degrees(math.hypot(ry * math.cos(pitch), rp))

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
    def decide(self, obs: dict, tracker_beliefs: dict, world=None, state: str = "",
               calm: bool = False) -> dict:
        """
        Main entry point called by brain.py each tick.
        Returns a partial action dict (tick will be set by the wrapper).

        calm: the opening calm is in force (brain.calm): track mechs but do not
        fire at them — gunfire is audible at 320 m and starts the squad's hunt.
        """
        me       = obs["self"]
        t        = obs["t"]
        pos      = me["pos"]           # [x, y, z]
        vel      = me["vel"]
        cubes    = me.get("cubes", 0)
        weapons  = me.get("weapons", [None, None])
        aim      = me.get("aim", [0.0, 0.0])  # actual slewed torso aim
        stats    = me.get("stats", {}) or {}
        muzzle_h = stats.get("muzzle_height", C.MECH_MUZZLE_HEIGHT)
        tube_h   = stats.get("height", C.MECH_HEIGHT) + 0.5   # mortar tube: height + 0.5
        pickup_radius = C.PICKUP_RADIUS

        action: dict = {}

        # ---- Upgrade offer ----
        offer = obs.get("offer")
        if offer:
            action["pick"] = pick_upgrade(offer)

        # ---- Handle short events ----
        for event in obs.get("events", []):
            if event.get("type") == "effect" and event.get("effect") == "short":
                self._laser.notify_shorted(t)
            elif event.get("type") == "damaged" and event.get("weapon") != "zone":
                self._last_hurt_t = t
                if event.get("bearing") is not None:     # world-frame yaw toward the shooter
                    self._hurt_bearing = float(event["bearing"])
                if event.get("weapon") == "npc":
                    self._npc_hurt_t = t
                if event.get("by") is not None:
                    self._attackers[int(event["by"])] = t
        turn_rate  = stats.get("turn_rate", C.TURN_RATE)
        pitch_rate = stats.get("pitch_rate", C.PITCH_RATE)

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
        # Pass the previous mech target so the choice sticks from tick to tick
        # (prevents target flapping and the strafe reversal that follows it).
        lock_id = self.target.get("id") if (self.target and self.target.get("kind") == "mech") else None
        target = select_target(obs, tracker_beliefs, world=world, cubes=cubes,
                               attackers=self._attackers, lock_id=lock_id)

        # ---- Retreating: farming is counter-productive ----
        # Any damage we deal restarts the server's regen_delay timer, so
        # shooting a crate or drone while disengaging stops us healing. Skip
        # resource targets entirely in the retreat/evade states (and never
        # substitute a crate/drone for a mech that is actually visible).
        # The same holds whenever we are hurt: below FARM_MIN_HP_FRAC no crate or
        # drone gets shot (regen needs 5 s without DEALING damage), unless a
        # drone is itself shooting us — then shooting back is self-defence.
        if target is not None and target.get("kind") in ("crate", "npc"):
            max_hp = me.get("max_hp", C.HP_DEFAULT) or C.HP_DEFAULT
            hurt = me.get("hp", max_hp) < max_hp * C.FARM_MIN_HP_FRAC
            drone_on_us = target.get("kind") == "npc" and t - self._npc_hurt_t < 3.0
            if state in ("DISENGAGE_REGEN", "EVADE", "RECOVER") or (hurt and not drone_on_us):
                target = None
        self.target = target
        if C.USE_LEAD_LEARNER:
            self._learner.update(t, {m["id"]: (m["pos"][0], m["pos"][1], m["vel"][0], m["vel"][1])
                                     for m in obs.get("mechs", []) if m.get("kind") == "mech"})

        # ---- Active Torso Scanning when no target ----
        if target is None:
            # Something just hit us and we can't see it: face it. A hit from
            # behind costs x1.5, and turning also brings the shooter into our
            # 120° view so the normal targeting can take over. (`bearing` is
            # exact, unlike a sound's ±7°.) We don't fire blind: range unknown.
            hurt_recent = (self._hurt_bearing is not None
                           and t - self._last_hurt_t < C.HURT_AIM_S)
            # Check for recent sounds to snap aim towards
            recent_sounds = [s for s in obs.get("sounds", []) if s.get("kind") in ("step", "boost", "gun", "laser_charge", "lightning", "mortar")]
            if hurt_recent:
                scan_yaw = _wrap(self._hurt_bearing)
                action["aim"] = [scan_yaw, 0.0]
                self._scan_yaw = scan_yaw
            elif recent_sounds:
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

            # Reload during quiet moments so a fight starts with a full magazine
            w0 = weapons[0] if weapons else None
            if w0 and w0.get("kind") == "gun":
                mag = (w0.get("stats") or {}).get("mag", C.GUN_MAG)
                if w0.get("reload", 0.0) == 0.0 and w0.get("ammo", mag) < mag * 0.5:
                    action["reload"] = True
            return action

        # ---- Smooth target velocity ----
        tgt_id  = target.get("id", -1)
        tgt_pos = target.get("pos", [0.0, 0.0, 0.0])
        tgt_vel = self._smooth_velocity(tgt_id, tgt_pos,
                                        target.get("vel", [0.0, 0.0, 0.0]))

        dist_to_target = _dist2d(pos[0], pos[1], tgt_pos[0], tgt_pos[1])

        # Target center of mass (mech: +4.5m, npc: +1.8m, crate: +1.0m) and body radius
        # A mech grows 5 % per cube it carries (no cap): centre of mass and
        # radius scale with it, so a cube-heavy enemy is bigger and taller.
        size = 1.0 + C.SIZE_PER_CUBE * float(target.get("cubes") or 0) \
            if target.get("kind") == "mech" else 1.0
        tgt_h, tgt_r = 4.5 * size, C.MECH_RADIUS * size
        if target.get("kind") == "npc":
            tgt_h, tgt_r = 1.8, 1.8
        elif target.get("kind") == "crate":
            tgt_h, tgt_r = 1.0, 1.4
        dist_3d = math.hypot(dist_to_target,
                             (tgt_pos[2] + tgt_h) - (pos[2] + muzzle_h))

        # ---- Check Line of Sight ----
        has_los = True
        if world is not None:
            has_los = world.has_los_2d(pos[0], pos[1], pos[2] + muzzle_h,
                                       tgt_pos[0], tgt_pos[1], tgt_pos[2] + tgt_h)

        # ---- Primary aim: gun quadratic lead ----
        lead_vel = tgt_vel
        if C.USE_LEAD_LEARNER and target.get("kind") == "mech":
            mult = self._learner.best(dist_to_target)        # learned lead scale (1.0 = linear lead)
            lead_vel = [v * mult for v in tgt_vel]
        gun_aim = gun_lead_aim(pos, tgt_pos, lead_vel, muzzle_h, center_h=tgt_h)
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

        # ---- Opening calm: keep the torso on the mech but hold fire ----
        if calm and target.get("kind") == "mech":
            action["fire"] = [False, False]
            return action

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

            mag = (w0.get("stats") or {}).get("mag", C.GUN_MAG)
            if w0_reload == 0.0 and (w0_ammo == 0 or (not has_los and w0_ammo < mag * 0.5)):
                action["reload"] = True   # empty, or a lull behind cover

            # HOLD the trigger whenever the shot would land: the server then fires
            # at the full cyclic rate the instant the cooldown ends (releasing until
            # cooldown == 0 cost ~20 % of our DPS). "Would land" = the aim error
            # left after the torso slews until the shot goes off is within the
            # target's angular size plus a little slack.
            gun_range = (w0.get("stats") or {}).get("range", C.GUN_RANGE)
            if dist_to_target <= gun_range and has_los and w0_ammo > 0:
                dt = min(0.1, max(0.0, w0_cooldown))
                err_deg = self._residual_err_deg(aim, aim_yaw, aim_pitch, dt, turn_rate, pitch_rate)
                if err_deg <= gun_fire_tolerance_deg(dist_3d, tgt_r) + C.GUN_EXTRA_TOL_DEG:
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
            las_err   = self._residual_err_deg(aim, las_yaw, las_pitch, 0.0, turn_rate, pitch_rate)
            las_tol   = gun_fire_tolerance_deg(dist_3d, tgt_r) + C.LASER_EXTRA_TOL_DEG
            las_range = (w1.get("stats") or {}).get("range", C.LASER_RANGE)
            full_chg  = (w1.get("stats") or {}).get("charge", C.LASER_CHARGE_TIME)

            # Weapon triangle: lightning shorts a charging laser — don't start a
            # charge inside an enemy lightning's reach.
            short_risk = ("lightning" in (target.get("weapons") or [])
                          and dist_to_target <= C.LIGHTNING_RANGE + 10.0
                          and (w1.get("charge", 0.0) or 0.0) <= 0.0)

            # Only charge and fire laser if we have direct line of sight!
            hold, cancel = self._laser.step(
                t, w1,
                target_visible and has_los and dist_to_target <= las_range and not short_risk,
                las_err, release_tol_deg=las_tol, full_charge=full_chg)
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

            if cluster_aim_pos is not None:
                chosen_mortar_tgt, chosen_vel = cluster_aim_pos, [0.0, 0.0, 0.0]
            else:
                chosen_mortar_tgt, chosen_vel = tgt_pos, tgt_vel
            chosen_mortar_dist = _dist2d(pos[0], pos[1], chosen_mortar_tgt[0], chosen_mortar_tgt[1])

            if chosen_mortar_dist > C.MORTAR_MIN_RANGE:
                # Lead the shell: aim where the target will be when it lands.
                result = mortar_lead_aim(tube_pos, chosen_mortar_tgt, chosen_vel,
                                         tube_height=tube_h, prefer_high=True)
                if result is not None:
                    mort_yaw, mort_pitch, _flight_t = result

                    # Only hijack torso aim when mortar is ready AND (cluster, target
                    # behind cover, or far away > 45 m). Otherwise keep the gun lead aim.
                    if mort_cd <= 0.05 and (cluster_aim_pos is not None or not has_los or dist_to_target > 45.0):
                        action["aim"] = [mort_yaw, mort_pitch]
                        # Shells leave along the ACTUAL torso aim: wait until it has
                        # slewed onto the lob before pulling the trigger.
                        if mort_cd <= 0.01 and self._residual_err_deg(
                                aim, mort_yaw, mort_pitch, 0.05, turn_rate, pitch_rate) <= C.MORTAR_AIM_TOL_DEG:
                            fire1 = True

        elif w1_kind == "lightning" and target.get("kind") != "crate":
            # The arc only finds a target inside cone_deg of the ACTUAL aim yaw;
            # outside it, a shot just burns energy into the nearest wall.
            bearing = math.atan2(tgt_pos[1] - pos[1], tgt_pos[0] - pos[0])
            in_cone = math.degrees(abs(_wrap(bearing - aim[0]))) <= C.LIGHTNING_CONE_DEG * 0.9
            lt_range = (w1.get("stats") or {}).get("range", C.LIGHTNING_RANGE)
            lt_energy = (w1.get("stats") or {}).get("energy", C.LIGHTNING_ENERGY)
            if dist_to_target <= lt_range * 0.9 and has_los and in_cone:
                if me.get("energy", 0.0) >= lt_energy * 2:
                    # Hold the trigger: it fires the instant the cooldown ends
                    fire1 = True

            # Laser wants half the move speed while charging, lightning wants us
            # close: point the torso straight at the target (instant weapon, no lead)
            if fire1:
                dz = (tgt_pos[2] + tgt_h) - (pos[2] + muzzle_h)
                action["aim"] = [bearing, max(C.MIN_PITCH, min(C.MAX_PITCH,
                                 math.atan2(dz, max(0.1, dist_to_target))))]

        # ---- Retreating: shoot back unless we've actually broken contact ----
        # Holding fire only helps regen once nobody is hitting us; while a
        # hunter is still landing shots, regen can't start anyway, so return fire.
        if state == "DISENGAGE_REGEN" and dist_to_target > 25.0 \
                and t - self._last_hurt_t > C.REGEN_QUIET_S:
            fire0 = False
            fire1 = False

        if (C.USE_LEAD_LEARNER and fire0 and target.get("kind") == "mech"
                and t - self._last_wave_t >= C.GUN_COOLDOWN * 0.9):
            self._learner.on_shot(t, pos, target.get("id", -1), tgt_pos,
                                  target.get("vel", [0.0, 0.0, 0.0]), C.GUN_SPEED)
            self._last_wave_t = t
        action["fire"] = [fire0, fire1]
        return action
