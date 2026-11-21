"""
tracker.py — Tiered Epistemic Belief Tracker for enemy mechs.

Each detected mech or NPC is assigned a confidence tier:
  T0 — directly visible in obs["mechs"]  (exact position, velocity)
  T1 — server memory window < memory_s   (server keeps reporting real pos)
  T2 — stale (> memory_s since last seen) — position is uncertain
  T3 — heard a sound with a bearing, but never seen

The tracker is updated each tick from the observation and exposes:
  beliefs : Dict[int, BeliefEntry]   keyed by mech id

For T2 we maintain a simple centroid + radius rather than a full particle
cloud, since we have no physics engine to propagate particles.

Contradiction rules (applied every tick):
  Empty-View Rule : if our FOV/proximity contains the belief position
                    with no corresponding mech in obs["mechs"], delete it.
  Counting Rule   : active tracks <= obs["alive"] - 1.
  Speed Limit     : any hypothesis requiring > 25 m/s displacement per dt
                    is demoted to T2 (position uncertain).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import config as C
import mechfight as mf


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class BeliefEntry:
    """Belief about one tracked mech."""
    id:        int
    kind:      str           # "mech" or "npc"
    tier:      int           # 0,1,2,3
    pos:       List[float]   # best-estimate [x,y,z]
    vel:       List[float]   # best-estimate velocity [vx,vy,vz]
    hp:        Optional[float] = None
    max_hp:    Optional[float] = None
    weapons:   List[str] = field(default_factory=list)
    charging:  bool = False
    aim:       Optional[List[float]] = None   # torso [yaw, pitch] (laser telegraph, rear arc)
    last_seen_t: float = -999.0   # match time (s) of last T0 observation
    last_heard_t: float = -999.0  # match time (s) of last T3 sound
    # For T2: search radius around pos estimate
    uncertainty_radius: float = 10.0
    # Confidence score [0..1] — used by combat.py to gate shots
    confidence: float = 0.0
    bearing_from_sound: Optional[float] = None   # T3 bearing (radians)
    # Last muzzle world position seen on a beam/arc by this actor (exact-ish),
    # and when. Beams are the most accurate off-sight bearing we get: the path
    # starts at the shooter's muzzle (protocol §6, `beams`).
    beam_pos: Optional[List[float]] = None
    last_beam_t: float = -999.0


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------

class EpistemicTracker:
    """
    Maintains a belief dict for all tracked actors and updates it
    every decision tick from the latest observation.
    """

    # Maximum age (s) before a T1 belief is demoted to T2
    _MEMORY_S = C.VISION_MEMORY_S     # 5.0 s

    # Maximum age (s) before a T2 belief is discarded entirely
    _STALE_DROP_S = 20.0

    # Speed limit for hypothesis plausibility (m/s)
    _SPEED_LIMIT = 25.0

    def __init__(self) -> None:
        self.beliefs: Dict[int, BeliefEntry] = {}
        self._next_sound_id = 10000   # synthetic id for T3-only tracks

    # ------------------------------------------------------------------
    def update(self, obs: dict, world=None) -> None:
        """
        Main update call. Must be invoked at the start of each act() tick.
        Updates all tiers, applies contradiction rules, prunes stale entries.

        `world` (a WorldIndex) is optional but strongly recommended: without it
        the Empty-View rule cannot tell "I can see there is nobody" from "a
        building is in the way", and it demotes fresh tracks it should keep.
        """
        t     = obs["t"]
        me    = obs["self"]
        my_x, my_y, my_z = me["pos"]
        my_aim_yaw        = me["aim"][0]
        alive_count       = obs.get("alive", 1)   # includes us

        # --- Step 1: Update T0 from directly visible mechs/NPCs ---
        seen_ids = set()
        for actor in obs.get("mechs", []):
            aid = actor["id"]
            seen_ids.add(aid)
            entry = self.beliefs.get(aid)
            if entry is None:
                entry = BeliefEntry(
                    id=aid, kind=actor["kind"],
                    tier=0, pos=list(actor["pos"]),
                    vel=list(actor["vel"]),
                )
                self.beliefs[aid] = entry

            entry.tier          = 0
            entry.pos           = list(actor["pos"])
            entry.vel           = list(actor["vel"])
            entry.last_seen_t   = t
            entry.confidence    = C.FIRE_CONFIDENCE_T0
            entry.uncertainty_radius = 0.0
            if "hp"      in actor: entry.hp      = actor["hp"]
            if "max_hp"  in actor: entry.max_hp  = actor["max_hp"]
            if "weapons" in actor: entry.weapons = list(actor.get("weapons", []))
            entry.charging = actor.get("charging", False)
            if "aim" in actor:
                entry.aim = list(actor["aim"])

        # --- Step 2: Age non-visible entries ---
        for aid, entry in list(self.beliefs.items()):
            if aid in seen_ids:
                continue

            age = t - entry.last_seen_t

            if age <= 0.0:
                # Just demoted — still T0 from above
                pass
            elif age <= self._MEMORY_S:
                # T1: server memory window — keep last known pos/vel
                entry.tier       = 1
                entry.charging   = False   # can't see the telegraph any more
                entry.confidence = C.FIRE_CONFIDENCE_T1
                # Dead-reckon with constant velocity, but only briefly: a mech that
                # broke LOS has usually turned or stopped, so long extrapolation
                # just drives the track into walls.
                if age <= C.DEAD_RECKON_MAX_S:
                    entry.pos = [
                        entry.pos[0] + entry.vel[0] * C.TICK_DT,
                        entry.pos[1] + entry.vel[1] * C.TICK_DT,
                        entry.pos[2],
                    ]
                entry.uncertainty_radius = age * 2.0   # grows with time
            elif age <= self._STALE_DROP_S:
                # T2: stale particle cloud
                entry.tier        = 2
                entry.confidence  = C.FIRE_CONFIDENCE_T2
                entry.uncertainty_radius = min(50.0, age * C.WALK_SPEED)
            else:
                # Too old — drop entirely
                del self.beliefs[aid]
                continue

        # --- Step 3: Ingest sound observations (T3) ---
        self._process_sounds(obs, t, my_x, my_y, my_z, seen_ids)

        # --- Step 3b: Ingest beam/arc sightings ---
        self._process_beams(obs, t)

        # --- Step 4: Apply contradiction rules ---
        self._apply_contradictions(obs, t, my_x, my_y, my_z,
                                   my_aim_yaw, alive_count, world)

        # --- Step 5: Kill event processing ---
        for event in obs.get("events", []):
            if event.get("type") == "kill":
                dead_id = event.get("target")
                if dead_id in self.beliefs:
                    del self.beliefs[dead_id]

    # ------------------------------------------------------------------
    def _process_sounds(self, obs: dict, t: float,
                        my_x: float, my_y: float, my_z: float,
                        seen_ids: set) -> None:
        """
        Convert sound observations into T3 belief wedges.
        Only add new T3 entries for sounds we don't already have
        a better track for.
        """
        for sound in obs.get("sounds", []):
            kind    = sound.get("kind", "")
            bearing = sound.get("bearing", 0.0)
            dist_s  = sound.get("dist", "far")

            # Footstep / boost sounds reliably indicate a living mech
            if kind not in ("step", "boost", "gun", "mortar",
                            "laser", "lightning", "laser_charge"):
                continue

            dist_estimate = {
                "near": C.SOUND_NEAR_M * 0.5,
                "mid":  (C.SOUND_NEAR_M + C.SOUND_MID_M) * 0.5,
                "far":  C.SOUND_MID_M * 1.5,
            }.get(dist_s, 100.0)

            est_x = my_x + dist_estimate * math.cos(bearing)
            est_y = my_y + dist_estimate * math.sin(bearing)

            # Find if we already have a close track
            close_track = None
            for aid, entry in self.beliefs.items():
                if math.hypot(entry.pos[0] - est_x,
                              entry.pos[1] - est_y) < 40.0:
                    close_track = entry
                    break

            if close_track is not None:
                # Update the existing track's last-heard time and rough pos
                if close_track.tier >= 2:
                    close_track.last_heard_t = t
                    close_track.bearing_from_sound = bearing
            # else: we could add a T3-only entry, but that risks ghost tracks.
            # We deliberately stay conservative and only shoot at T0/T1.

    # ------------------------------------------------------------------
    def _process_beams(self, obs: dict, t: float) -> None:
        """
        Record exact muzzle positions from beams/arcs (protocol §6).

        `path[0]` starts at the shooter's muzzle, so this is a far better
        bearing than a ±7° sound. We only annotate an existing track (never
        invent one) — a beam with no known shooter id is useless for tracking.
        """
        my_id = obs["self"].get("id")
        for beam in obs.get("beams", []) or []:
            by = beam.get("by")
            if by is None:
                continue
            by = int(by)
            if by == my_id or not (0 <= by < 100):      # ours, or not a mech
                continue
            path = beam.get("path") or []
            if not path:
                continue
            try:
                origin = [float(path[0][0]), float(path[0][1]), float(path[0][2])]
            except (TypeError, ValueError, IndexError):
                continue
            age = float(beam.get("age", 0.0) or 0.0)
            if age > 0.5:
                continue                      # too old to locate anyone
            beam_t = t - age

            entry = self.beliefs.get(by)
            if entry is not None and entry.tier == 0:
                # Seen directly this tick: nothing to learn about WHERE.
                entry.beam_pos = origin
                entry.last_beam_t = beam_t
                continue

            # Not in view (or never seen): the muzzle is an exact position, far
            # better than a ±7° sound. Track the shooter at T1 so the bot turns
            # toward it, shoots back and takes cover instead of being a target
            # that cannot see what is killing it.
            feet = [origin[0], origin[1], origin[2] - C.MECH_MUZZLE_HEIGHT]
            if entry is None:
                entry = BeliefEntry(id=by, kind="mech", tier=1, pos=feet, vel=[0.0, 0.0, 0.0])
                self.beliefs[by] = entry
            entry.tier = 1
            entry.pos = feet
            entry.vel = [0.0, 0.0, 0.0]
            entry.confidence = C.FIRE_CONFIDENCE_T1
            entry.uncertainty_radius = 3.0
            entry.last_seen_t = beam_t
            entry.beam_pos = origin
            entry.last_beam_t = beam_t

    # ------------------------------------------------------------------
    def _in_fov(self, my_x: float, my_y: float, my_aim_yaw: float,
                tx: float, ty: float) -> bool:
        """True if (tx,ty) is inside our forward FOV cone."""
        if math.hypot(tx - my_x, ty - my_y) > C.VISION_RANGE:
            return False
        to_tgt_yaw = math.atan2(ty - my_y, tx - my_x)
        angle_diff  = abs(to_tgt_yaw - my_aim_yaw)
        angle_diff  = min(angle_diff, 2 * math.pi - angle_diff)
        return math.degrees(angle_diff) <= C.VISION_FOV_DEG / 2.0

    def _in_proximity(self, my_x: float, my_y: float,
                      tx: float, ty: float) -> bool:
        return math.hypot(tx - my_x, ty - my_y) <= C.VISION_NEAR_RANGE

    # ------------------------------------------------------------------
    def _apply_contradictions(self, obs: dict, t: float,
                              my_x: float, my_y: float, my_z: float,
                              my_aim_yaw: float, alive_count: int,
                              world=None) -> None:
        """
        Apply the two contradiction rules to remove impossible beliefs.
        """
        # --- Counting Rule (mechs only: `alive` does not count NPC drones) ---
        max_enemies = max(0, alive_count - 1)
        mech_items = [kv for kv in self.beliefs.items() if kv[1].kind == "mech"]
        if len(mech_items) > max_enemies:
            # Keep the best tracks: lowest tier first, then most recently seen
            mech_items.sort(key=lambda kv: (kv[1].tier, -kv[1].last_seen_t))
            for aid, _ in mech_items[max_enemies:]:
                del self.beliefs[aid]

        # --- Empty-View Rule ---
        # If we have direct LOS to a belief position with no actor there,
        # demote it from T1 → T2 (don't delete, just increase uncertainty).
        seen_positions = {
            (m["pos"][0], m["pos"][1])
            for m in obs.get("mechs", [])
        }
        for aid, entry in list(self.beliefs.items()):
            if entry.tier not in (1,):
                continue   # only apply to T1 (T0 is confirmed, T2 is already uncertain)
            if t - entry.last_beam_t < 0.3:
                continue   # just located by a beam: hidden from us, not gone
            bx, by = entry.pos[0], entry.pos[1]
            in_view = (self._in_fov(my_x, my_y, my_aim_yaw, bx, by) or
                       self._in_proximity(my_x, my_y, bx, by))
            if not in_view:
                continue
            # Being inside the FOV wedge is not the same as having a clear view:
            # buildings and tree crowns block it. Only claim "I see nothing
            # there" when the sight line is actually open. Without a world we
            # keep the old conservative behaviour (demote).
            if world is not None and hasattr(world, "has_sight"):
                if not world.has_sight(my_x, my_y, my_z + C.MECH_EYE_HEIGHT,
                                       bx, by, entry.pos[2] + C.MECH_EYE_HEIGHT):
                    continue   # blocked → we cannot contradict the belief
            if not any(
                math.hypot(px - bx, py - by) < 6.0
                for px, py in seen_positions
            ):
                entry.tier = 2
                entry.confidence = C.FIRE_CONFIDENCE_T2
                entry.uncertainty_radius = max(entry.uncertainty_radius, 15.0)

    # ------------------------------------------------------------------
    def visible_threats(self) -> List[BeliefEntry]:
        """Return T0 + T1 beliefs (high-confidence targets), sorted by HP asc."""
        candidates = [b for b in self.beliefs.values() if b.tier <= 1]
        return sorted(candidates,
                      key=lambda b: (b.hp if isinstance(b.hp, (int, float)) else 9999, b.tier))

    def any_visible(self) -> bool:
        """True if any enemy MECH is currently visible (NPC drones excluded)."""
        return any(b.tier == 0 and b.kind == "mech" for b in self.beliefs.values())

    def threat_count(self) -> int:
        """Number of enemy MECHS visible right now (NPC drones excluded)."""
        return sum(1 for b in self.beliefs.values() if b.tier == 0 and b.kind == "mech")

    def recent_mech_threat(self) -> bool:
        """True if any enemy mech is visible or still in recent memory (T0/T1)."""
        return any(b.tier <= 1 and b.kind == "mech" for b in self.beliefs.values())

    def nearest_mech(self, x: float, y: float, max_tier: int = 1) -> Optional[BeliefEntry]:
        """The closest tracked enemy MECH at tier <= max_tier (None if there is none)."""
        best, bd = None, 0.0
        for b in self.beliefs.values():
            if b.kind != "mech" or b.tier > max_tier:
                continue
            d = math.hypot(b.pos[0] - x, b.pos[1] - y)
            if best is None or d < bd:
                best, bd = b, d
        return best

    def nearest_mech_dist(self, x: float, y: float, max_tier: int = 1) -> Optional[float]:
        """
        Horizontal distance to the closest enemy MECH at tier <= max_tier,
        or None if none is tracked. Used for the engagement-range gates.
        """
        best = None
        for b in self.beliefs.values():
            if b.kind != "mech" or b.tier > max_tier:
                continue
            d = math.hypot(b.pos[0] - x, b.pos[1] - y)
            if best is None or d < best:
                best = d
        return best
