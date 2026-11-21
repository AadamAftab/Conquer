"""
brain.py — Tactical State Machine (FSM) for the Mech Royale champion bot.

The Brain is the highest-level decision layer. Each tick it examines the
observation, the tracker's belief state, and the map to decide WHAT the bot
should be doing. The lower-level layers (motion.py, combat.py) decide HOW.

FSM States
----------
  BOOTSTRAP_LOOT     — Grab cubes + preferred weapon from the map before
                        any enemies can engage. Transitions when we have
                        enough cubes AND a slot-1 weapon, or when forced
                        out of the zone.

  ASCEND_ROOFTOP     — Navigate to the highest safe rooftop inside the
                        upcoming zone. Transitions when we confirm we're on
                        the rooftop.

  AMBUSH_HUNTER      — Fire from an elevated position. MRM keeps us at
                        zone-edge; combat.py handles fire-control. Transitions
                        on low HP, overwhelming numbers, or zone shrink.

  DISENGAGE_REGEN    — Retreat to cover and wait for HP regen. Motion.py
                        flees; combat still fires opportunistically.
                        Transitions when HP recovers above REGEN_RESUME_HP.

  ZONE_ROTATION      — Current rooftop is about to be outside the shrinking
                        zone. Walk to the next rooftop inside the new zone.
                        Transitions when new rooftop is reached.

  ENDGAME_SURVIVAL   — Zone is so small that rooftop advantage is gone.
                        Fight from ground; mortar enemies in the cluster.
                        Never leaves (game is nearly over).

Transition diagram (simplified):
  BOOTSTRAP_LOOT
      └→ ASCEND_ROOFTOP (got weapon + cubes, or zone forces move)
  ASCEND_ROOFTOP
      └→ AMBUSH_HUNTER (is_on_roof == True)
  AMBUSH_HUNTER
      ├→ DISENGAGE_REGEN (hp < DISENGAGE_HP or multi-enemy)
      └→ ZONE_ROTATION (rooftop leaving zone)
  DISENGAGE_REGEN
      └→ ASCEND_ROOFTOP (hp > REGEN_RESUME_HP)
  ZONE_ROTATION
      ├→ ASCEND_ROOFTOP (new rooftop found, navigate to it)
      └→ ENDGAME_SURVIVAL (zone too small)
  ENDGAME_SURVIVAL
      (terminal)
"""

from __future__ import annotations

import math
from typing import Optional, TYPE_CHECKING

import config as C
import mechfight as mf

if TYPE_CHECKING:
    from tracker import EpistemicTracker
    from world import WorldIndex, Rooftop


# ---------------------------------------------------------------------------
# Helper: check if a rooftop will remain inside the NEXT zone
# ---------------------------------------------------------------------------

def _rooftop_in_next_zone(roof: "Rooftop",
                           next_cx: float, next_cy: float,
                           next_r: float) -> bool:
    """True if the rooftop centroid will be inside the upcoming zone circle."""
    dist = math.hypot(roof.centroid_x - next_cx,
                      roof.centroid_y - next_cy)
    # Leave a margin so we start moving before we're cut off
    return dist <= (next_r - C.ZONE_EDGE_BUFFER)


# ---------------------------------------------------------------------------
# Brain FSM
# ---------------------------------------------------------------------------

class Brain:
    """
    Tactical state machine. Call decide_state() once per tick before
    invoking motion.py and combat.py.

    Attributes exposed for other modules
    ------------------------------------
    state           : str — current FSM state name
    _rooftop_target : Rooftop | None — the building we're heading to
    _ticks_in_state : int — how many ticks in the current state
    """

    # Valid state names
    BOOTSTRAP_LOOT    = "BOOTSTRAP_LOOT"
    ASCEND_ROOFTOP    = "ASCEND_ROOFTOP"
    AMBUSH_HUNTER     = "AMBUSH_HUNTER"
    DISENGAGE_REGEN   = "DISENGAGE_REGEN"
    ZONE_ROTATION     = "ZONE_ROTATION"
    ENDGAME_SURVIVAL  = "ENDGAME_SURVIVAL"

    def __init__(self) -> None:
        self.state: str                   = self.BOOTSTRAP_LOOT
        self._rooftop_target: Optional["Rooftop"] = None
        self._ticks_in_state: int         = 0
        self._prev_state: str             = self.BOOTSTRAP_LOOT

        # Track the last rooftop we confirmed standing on
        self._confirmed_roof: Optional["Rooftop"] = None

        # Suppress log spam: only log on state changes
        self._logged_state: str = ""

    # ------------------------------------------------------------------
    def decide_state(
        self,
        obs:           dict,
        tracker:       "EpistemicTracker",
        world_index:   "WorldIndex",
        is_on_roof:    bool,
    ) -> str:
        """
        Examine observation + tracker, run transition logic, return the
        FSM state string for this tick.

        Side-effects
        ------------
        Updates self.state, self._rooftop_target, self._ticks_in_state.
        """
        me       = obs["self"]
        t        = obs["t"]
        pos      = me["pos"]
        hp       = me.get("hp", C.HP_DEFAULT)
        weapons  = me.get("weapons", [None, None])
        zone     = obs["zone"]
        alive    = obs.get("alive", 16)

        zone_cx       = zone["center"][0]
        zone_cy       = zone["center"][1]
        zone_r        = zone["radius"]
        next_zone_cx  = zone["next_center"][0]
        next_zone_cy  = zone["next_center"][1]
        next_zone_r   = zone["next_radius"]

        cubes     = me.get("cubes", 0)
        energy    = me.get("energy", C.ENERGY_MAX)
        in_zone   = me.get("in_zone", True)

        # Weapon slot inventory
        slot1 = weapons[1] if len(weapons) > 1 else None
        has_slot1_weapon = (slot1 is not None)
        slot1_kind = slot1["kind"] if slot1 else None

        # Tracker snapshot
        threat_count  = tracker.threat_count()          # T0 threats this tick
        t0_threats    = tracker.visible_threats()        # sorted T0+T1 by HP
        any_visible   = tracker.any_visible()

        # Tick counter
        self._ticks_in_state += 1

        # Find best rooftop in NEXT zone (computed every tick, cheap)
        best_roof = world_index.nearest_rooftop(
            pos[0], pos[1],
            next_zone_cx, next_zone_cy, next_zone_r,
        )

        # ------------------------------------------------------------------
        # ENDGAME check — always overrides everything
        # ------------------------------------------------------------------
        if next_zone_r < C.ENDGAME_ZONE_RADIUS:
            self._transition(self.ENDGAME_SURVIVAL)
            return self.state

        # ------------------------------------------------------------------
        # State transitions
        # ------------------------------------------------------------------

        if self.state == self.BOOTSTRAP_LOOT:
            self._handle_bootstrap(cubes, has_slot1_weapon, slot1_kind,
                                   in_zone, zone_r, next_zone_r,
                                   t, obs)

        elif self.state == self.ASCEND_ROOFTOP:
            self._handle_ascend(is_on_roof, best_roof, pos,
                                next_zone_cx, next_zone_cy, next_zone_r,
                                hp, threat_count, t)

        elif self.state == self.AMBUSH_HUNTER:
            self._handle_ambush(hp, threat_count, in_zone, best_roof,
                                pos, next_zone_cx, next_zone_cy,
                                next_zone_r, next_zone_r, alive, t)

        elif self.state == self.DISENGAGE_REGEN:
            self._handle_regen(hp, any_visible, best_roof, t)

        elif self.state == self.ZONE_ROTATION:
            self._handle_rotation(is_on_roof, best_roof, pos,
                                  next_zone_cx, next_zone_cy, next_zone_r,
                                  hp, t)

        elif self.state == self.ENDGAME_SURVIVAL:
            pass   # terminal — no transitions out

        # ------------------------------------------------------------------
        # Store rooftop target for motion.py
        # ------------------------------------------------------------------
        self._rooftop_target = best_roof

        return self.state

    # ------------------------------------------------------------------
    # Per-state transition handlers
    # ------------------------------------------------------------------

    def _handle_bootstrap(
        self, cubes: int, has_slot1: bool, slot1_kind: Optional[str],
        in_zone: bool, zone_r: float, next_zone_r: float,
        t: float, obs: dict,
    ) -> None:
        """
        BOOTSTRAP_LOOT → ASCEND_ROOFTOP when:
          (a) We have at least CUBES_TO_ASCEND cubes AND a preferred slot-1
              weapon (laser or mortar), OR
          (b) The zone is shrinking fast and we must move now, OR
          (c) We've spent > 25 s looting (time-box).
        """
        has_good_weapon = has_slot1 and slot1_kind in ("laser", "mortar")

        # Time-box: don't loot forever
        if self._ticks_in_state > 250:   # ~25 s at 10 Hz
            self._transition(self.ASCEND_ROOFTOP)
            return

        # Zone forcing us: next zone is very small — head to position now
        if next_zone_r < 180.0 and self._ticks_in_state > 50:
            self._transition(self.ASCEND_ROOFTOP)
            return

        # Normal graduation: enough cubes + weapon
        if cubes >= C.CUBES_TO_ASCEND and has_good_weapon:
            self._transition(self.ASCEND_ROOFTOP)
            return

        # Also transition if we have a good weapon and have been looting ≥ 10s
        if has_good_weapon and self._ticks_in_state > 100:
            self._transition(self.ASCEND_ROOFTOP)
            return

    def _handle_ascend(
        self, is_on_roof: bool, best_roof: Optional["Rooftop"],
        pos: list, next_cx: float, next_cy: float, next_r: float,
        hp: float, threat_count: int, t: float,
    ) -> None:
        """
        ASCEND_ROOFTOP → AMBUSH_HUNTER when we confirm is_on_roof.
        → DISENGAGE_REGEN if severely damaged during the climb.
        """
        if hp < C.DISENGAGE_HP * 0.6:
            # Took heavy damage during approach — bail
            self._transition(self.DISENGAGE_REGEN)
            return

        if is_on_roof:
            self._transition(self.AMBUSH_HUNTER)
            return

        # Safety: don't get stuck ascending for more than 60 s
        if self._ticks_in_state > 600:
            # Either roof is unreachable or we're stuck — go to ENDGAME logic
            mf.log("Brain: ascend timeout — falling back to MRM")
            self._transition(self.AMBUSH_HUNTER)

    def _handle_ambush(
        self, hp: float, threat_count: int, in_zone: bool,
        best_roof: Optional["Rooftop"],
        pos: list, next_cx: float, next_cy: float, next_r: float,
        next_zone_r: float, alive: int, t: float,
    ) -> None:
        """
        AMBUSH_HUNTER transitions:
          → DISENGAGE_REGEN  : HP too low OR too many enemies
          → ZONE_ROTATION    : current rooftop won't be in next zone
        """
        # HP-based disengage
        if hp < C.DISENGAGE_HP:
            self._transition(self.DISENGAGE_REGEN)
            return

        # Numerical disadvantage disengage
        if threat_count >= C.MULTI_ENEMY_THRESHOLD:
            self._transition(self.DISENGAGE_REGEN)
            return

        # Zone rotation: check if current rooftop is about to be cut off
        if best_roof is not None:
            # We're on a rooftop — check if that rooftop is still in next zone
            if not _rooftop_in_next_zone(best_roof, next_cx, next_cy, next_r):
                self._transition(self.ZONE_ROTATION)
                return
        else:
            # No good roof found inside next zone — rotate to find one
            self._transition(self.ZONE_ROTATION)

    def _handle_regen(
        self, hp: float, any_visible: bool,
        best_roof: Optional["Rooftop"], t: float,
    ) -> None:
        """
        DISENGAGE_REGEN → ASCEND_ROOFTOP when HP recovers to safe level.
        """
        if hp >= C.REGEN_RESUME_HP:
            # Head back up
            self._transition(self.ASCEND_ROOFTOP)
            return

        # Safety time-box: don't regen forever if enemies are gone
        if self._ticks_in_state > 400 and not any_visible:
            self._transition(self.ASCEND_ROOFTOP)

    def _handle_rotation(
        self, is_on_roof: bool, best_roof: Optional["Rooftop"],
        pos: list, next_cx: float, next_cy: float, next_r: float,
        hp: float, t: float,
    ) -> None:
        """
        ZONE_ROTATION → ASCEND_ROOFTOP once we have a destination.
                      → ENDGAME_SURVIVAL if zone is too small for a roof.
        """
        # Already handled by endgame check above.

        # HP-based disengage during rotation (taking fire while running)
        if hp < C.DISENGAGE_HP * 0.7:
            self._transition(self.DISENGAGE_REGEN)
            return

        if best_roof is not None:
            # Transition back to ascend with the new target
            self._transition(self.ASCEND_ROOFTOP)
        else:
            # No roof available in next zone — switch to endgame mode
            self._transition(self.ENDGAME_SURVIVAL)

    # ------------------------------------------------------------------
    # Transition helper
    # ------------------------------------------------------------------

    def _transition(self, new_state: str) -> None:
        """Change state and reset the tick counter. Log only on change."""
        if new_state == self.state:
            return
        mf.log(f"Brain: {self.state} → {new_state} "
               f"(after {self._ticks_in_state} ticks)")
        self._prev_state    = self.state
        self.state          = new_state
        self._ticks_in_state = 0

    # ------------------------------------------------------------------
    # Read-only helpers used by bot.py
    # ------------------------------------------------------------------

    @property
    def rooftop_target(self) -> Optional["Rooftop"]:
        """The current rooftop navigation goal (may be None)."""
        return self._rooftop_target

    def is_aggressive(self) -> bool:
        """True when we should actively seek and fire on enemies."""
        return self.state in (self.AMBUSH_HUNTER, self.ENDGAME_SURVIVAL)

    def is_moving(self) -> bool:
        """True when we should be actively repositioning (not roof-holding)."""
        return self.state in (self.BOOTSTRAP_LOOT, self.ASCEND_ROOFTOP,
                              self.ZONE_ROTATION, self.DISENGAGE_REGEN)
