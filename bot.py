"""
bot.py — Championship Mech Royale bot entry-point.

Architecture (in call order per tick)
--------------------------------------
  Bot.__init__(hello)
      ├─ config.update_from_config(hello["config"])  # live physics constants
      ├─ WorldIndex(arena_dir)                       # static map (precomputed)
      ├─ EpistemicTracker()                          # belief state
      ├─ Brain()                                     # FSM
      ├─ CombatController()                          # ballistics + fire FSM
      └─ MotionController()                          # movement planner

  Bot.act(obs) → action
      1. tracker.update(obs)         — update beliefs from observation
      2. Compute helper values       — is_on_roof, enemy list, rooftop goal
      3. brain.decide_state(...)     — FSM tick, returns current state string
      4. combat.decide(...)          — aim + fire + pickup decisions
      5. motion.decide(...)          — move + boost + jump decisions
      6. Merge partial actions       — combat overrides motion for aim
      7. Hard-deadline safety check  — flush last_move if over budget
      8. Return merged action dict

Design principles
-----------------
  - GC disabled after __init__ to eliminate latency spikes.
  - Hard deadline at 38 ms: return stale action rather than miss tick.
  - Soft deadline at 28 ms: skip iterative search, use cached result.
  - stdout carries ONLY mechfight JSON protocol lines.
  - All logging goes through mf.log() → stderr.
  - Zero third-party imports (pure stdlib + mechfight.py).
"""

from __future__ import annotations

import gc
import math
import time
from typing import List, Optional

import mechfight as mf
import config as C
from world import WorldIndex
from tracker import EpistemicTracker
from brain import Brain
from combat import CombatController
from motion import MotionController
from nav import NavGrid, PathFollower
from influence import InfluenceMap
from utility import UtilityBrain


# ---------------------------------------------------------------------------
# Bot class
# ---------------------------------------------------------------------------

class Bot:
    """
    Top-level bot object.  mechfight.run(Bot) calls __init__ with the hello
    message, then act() with each observation tick.
    """

    name = C.BOT_NAME   # displayed on the leaderboard

    # ------------------------------------------------------------------
    def __init__(self, hello: mf.Hello) -> None:
        """
        Called once before any observation arrives.

        Parameters
        ----------
        hello : mf.Hello
            The initial "hello" message containing:
              hello["config"]  — live physics + weapon parameters
              hello["map"]     — {"dir": "/arena/v2"} path to map files
              hello["slot"]    — our slot index (0-15)
        """
        # 1. Apply live physics overrides (must be first — other modules read C)
        C.update_from_config(hello.get("config", {}))

        # 2. Freeze GC — prevents GC pauses from blowing the 50 ms budget
        gc.disable()

        # 3. Load static map data (once; takes a few hundred ms — allowed
        #    during the hello phase which has a 20 s budget)
        arena_dir = hello.get("map", {}).get("dir", "/arena")
        self.world = WorldIndex(arena_dir, C.MIN_ROOF_HEIGHT, C.MAX_ROOF_HEIGHT)
        mf.log(f"Bot: WorldIndex loaded — {len(self.world.rooftops)} rooftops, "
               f"{len(self.world.prisms)} prisms")

        # 4. Walkability grid for path-finding (~1.5 s; inside the 20 s hello budget)
        t_nav = time.perf_counter()
        try:
            grid = NavGrid(self.world)
        except Exception as exc:          # never let a nav bug stop the bot from starting
            mf.log(f"Bot: NavGrid failed ({exc!r}) — straight-line movement only")
            grid = None
        mf.log(f"Bot: NavGrid {'ready' if grid is not None and grid.ok else 'unavailable'} "
               f"in {(time.perf_counter() - t_nav) * 1000:.0f} ms")

        # 5. Subsystem controllers
        influence = None
        if C.USE_INFLUENCE_COVER and grid is not None and grid.ok:
            try:
                influence = InfluenceMap(grid, self.world)       # static cover layer, built once
            except Exception as exc:
                mf.log(f"Bot: InfluenceMap failed ({exc!r}) — falling back to MRM cover")
        self.tracker = EpistemicTracker()
        self.brain   = UtilityBrain() if C.USE_UTILITY_BRAIN else Brain()
        self.combat  = CombatController()
        self.motion  = MotionController(nav=PathFollower(grid),
                                        seed=int(hello.get("seed", 0) or 0),
                                        influence=influence)

        # 5. Stale-action cache — returned on hard-deadline miss
        self._last_move: List[float] = [0.0, 0.0]
        self._last_aim:  List[float] = [0.0, 0.0]

        # 6. Debug: our slot index
        self._slot: int = hello.get("slot", 0)
        mf.log(f"Bot: hello complete — slot={self._slot}, "
               f"budget={C.HARD_DEADLINE_S*1000:.0f} ms hard / "
               f"{C.SOFT_DEADLINE_S*1000:.0f} ms soft")

    # ------------------------------------------------------------------
    def act(self, obs: mf.Observation) -> mf.Action:
        """
        Called by mechfight.run() every tick (10 Hz, 50 ms budget).

        Steps
        -----
        1.  Tracker update (parse obs → beliefs)
        2.  Derive local state helpers
        3.  Brain FSM tick → current tactical state
        4.  Combat layer → aim / fire / reload / pickups
        5.  Motion layer → move / boost / jump
        6.  Merge actions (combat overrides motion's aim)
        7.  Cache last move/aim for deadline safety
        8.  Return action dict
        """
        t_start = time.perf_counter()

        # ================================================================
        # Step 1 — Update epistemic tracker from raw observation
        # ================================================================
        self.tracker.update(obs, world=self.world)

        # ================================================================
        # Step 2 — Extract commonly used fields
        # ================================================================
        me       = obs["self"]
        t        = obs["t"]
        pos      = me["pos"]           # [x, y, z] feet
        vel      = me["vel"]           # [vx, vy, vz]
        hp       = me.get("hp", C.HP_DEFAULT)
        energy   = me.get("energy", C.ENERGY_MAX)
        grounded = me.get("grounded", True)
        in_zone  = me.get("in_zone", True)
        zone     = obs["zone"]

        # Derive is_on_roof:
        # We're on a roof if our feet are significantly above terrain AND
        # grounded (landed).  Terrain height uses bilinear interpolation.
        ground_z = self.world.terrain_height(pos[0], pos[1])
        is_on_roof = grounded and (pos[2] - ground_z) > C.ROOF_ARRIVAL_Z * 2.0

        # Build enemy position list from tracker (T0 + T1 mechs only; NPC drones
        # are farm targets, not threats to kite from or charge at)
        enemy_positions: List[List[float]] = [
            b.pos
            for b in self.tracker.beliefs.values()
            if b.tier <= 1 and b.kind == "mech"
        ]

        # Rooftop navigation goal (inside next zone)
        rooftop_goal = self.world.nearest_rooftop(
            pos[0], pos[1],
            zone["next_center"][0],
            zone["next_center"][1],
            zone["next_radius"],
        )

        # ================================================================
        # Step 3 — Brain FSM: decide tactical state
        # ================================================================
        state = self.brain.decide_state(obs, self.tracker, self.world, is_on_roof)

        # ================================================================
        # Step 4 — Combat layer (aim, fire, pickups, upgrades)
        # (runs always; has its own deadline guard internally via returned
        #  cached aim if tracker_beliefs is empty)
        # ================================================================
        # Package tracker beliefs as a dict keyed by id for combat.py
        tracker_beliefs = {
            b.id: {
                "id":    b.id,
                "kind":  b.kind,
                "tier":  b.tier,
                "pos":   b.pos,
                "vel":   b.vel,
                "hp":    b.hp,
                "confidence": b.confidence,
            }
            for b in self.tracker.beliefs.values()
        }

        t_mid = time.perf_counter()
        combat_action: dict = {}

        if (t_mid - t_start) < C.SOFT_DEADLINE_S:
            combat_action = self.combat.decide(obs, tracker_beliefs, world=self.world,
                                               state=state, calm=self.brain.calm)
        else:
            # Soft deadline exceeded — skip combat computation this tick
            mf.log(f"Bot: soft deadline exceeded at step 4 "
                   f"({(t_mid-t_start)*1000:.1f} ms) — reusing last aim")
            combat_action = {
                "aim":  list(self._last_aim),
                "fire": [False, False],
            }

        # ================================================================
        # Step 5 — Hard deadline check before motion
        # ================================================================
        t_before_motion = time.perf_counter()
        if (t_before_motion - t_start) >= C.HARD_DEADLINE_S:
            # Can't afford motion — flush stale action NOW
            return _make_safe_action(self._last_move, self._last_aim,
                                     combat_action)

        # ================================================================
        # Step 6 — Motion layer (move, boost, jump)
        # ================================================================
        combat_aim = combat_action.get("aim")  # may be None if no target

        # Enemy mechs with what motion needs for dodging/positioning
        enemies = [
            {"id": b.id, "pos": b.pos, "vel": b.vel, "aim": b.aim,
             "weapons": b.weapons, "charging": b.charging, "tier": b.tier}
            for b in self.tracker.beliefs.values()
            if b.tier <= 1 and b.kind == "mech"
        ]
        # Path planning gets whatever is left of the soft deadline (capped)
        remaining = C.SOFT_DEADLINE_S - (time.perf_counter() - t_start)
        cap = C.NAV_BUDGET_S if enemies else C.NAV_BUDGET_CALM_S
        nav_budget = max(0.0, min(cap, remaining - 0.004))
        mrm_budget = max(0.002, min(C.MRM_TIME_BUDGET_S, remaining - 0.006))

        motion_action = self.motion.decide(
            obs          = obs,
            state        = state,
            rooftop_goal = rooftop_goal,
            enemy_positions = enemy_positions,
            combat_aim   = combat_aim,
            is_on_roof   = is_on_roof,
            world        = self.world,
            enemies      = enemies,
            target       = self.combat.target,
            nav_budget_s = nav_budget,
            mrm_budget_s = mrm_budget,
            calm         = self.brain.calm,
        )
        mf.draw.label(f"{state[:10]} {self.motion.debug_mode}")

        # ================================================================
        # Step 7 — Merge partial actions
        # Priority: motion sets move/boost/jump; combat overrides aim/fire
        # ================================================================
        action: dict = {}
        action.update(motion_action)   # move, boost, jump
        action.update(combat_action)   # aim, fire, reload, take, drop, pick
        # (combat dict's aim, fire always present and take priority)

        # ================================================================
        # Step 8 — Cache, deadline check, return
        # ================================================================
        self._last_move = list(action.get("move", self._last_move))
        self._last_aim  = list(action.get("aim",  self._last_aim))

        # Final hard-deadline safety (should rarely trigger at this point)
        t_end = time.perf_counter()
        elapsed_ms = (t_end - t_start) * 1000.0
        if elapsed_ms > 30.0:   # getting close to the 50 ms wall: say where it went
            mf.log(f"Bot: slow tick {obs.get('tick')} {elapsed_ms:.1f} ms — "
                   f"pre-combat {(t_mid - t_start) * 1000:.1f}, "
                   f"combat {(t_before_motion - t_mid) * 1000:.1f}, "
                   f"motion {(t_end - t_before_motion) * 1000:.1f} ({self.motion.debug_mode})")

        return action


# ---------------------------------------------------------------------------
# Helper: build a minimal safe action to return under deadline pressure
# ---------------------------------------------------------------------------

def _make_safe_action(
    last_move: List[float],
    last_aim:  List[float],
    combat_action: dict,
) -> mf.Action:
    """
    Returns the safest possible action when the hard deadline was hit:
    keep last move direction, keep current aim, do not fire.
    If combat_action already computed an aim, use that.
    """
    aim = combat_action.get("aim", last_aim)
    return {
        "move":  last_move,
        "aim":   aim,
        "fire":  [False, False],
        "boost": False,
        "jump":  False,
    }


# ---------------------------------------------------------------------------
# Entry-point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mf.run(Bot)
