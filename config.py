"""
config.py — All tunable constants and weights for the Mech Royale champion bot.

Every magic number lives here. Adjust these to tune behaviour without
touching any logic file. Values are read once at startup; nothing is
recomputed at runtime except where explicitly noted.

Sections
--------
1. Identity
2. Timing governors
3. Physics & mech geometry
4. Weapon parameters (mirrors hello.config — override at startup)
5. Vision & sound thresholds
6. Minimum Risk Movement (MRM) weights
7. Tactical state machine thresholds
8. Upgrade priority order
9. Rooftop navigation
10. Debug-draw colours
"""

from __future__ import annotations
from typing import List

# ---------------------------------------------------------------------------
# 1. IDENTITY
# ---------------------------------------------------------------------------
BOT_NAME: str = "mech-champion"

# ---------------------------------------------------------------------------
# 2. TIMING GOVERNORS (seconds)
# ---------------------------------------------------------------------------

# Soft deadline: abort iterative search, re-use cached path.
SOFT_DEADLINE_S: float = 0.028

# Hard deadline: immediately flush reflex action (last move + current aim).
HARD_DEADLINE_S: float = 0.038

# Tick duration (1 decision = 2 sim ticks at 20 Hz).
TICK_DT: float = 0.1  # seconds per decision

# ---------------------------------------------------------------------------
# 3. PHYSICS & MECH GEOMETRY
# ---------------------------------------------------------------------------
# These are the defaults from the kit; the bot overwrites them from hello.config
# at startup via update_from_config() below so balance patches auto-apply.

MECH_RADIUS: float       = 2.5    # m
MECH_HEIGHT: float       = 9.0    # m
MECH_EYE_HEIGHT: float   = 8.0    # m above feet
MECH_MUZZLE_HEIGHT: float = 7.0   # m above feet (gun slot 0)
MECH_MORTAR_HEIGHT: float = 9.5   # m above feet (mortar tube)

WALK_SPEED: float        = 11.7   # m/s
BOOST_SPEED: float       = 19.5   # m/s
GROUND_ACCEL: float      = 58.5   # m/s²
AIR_ACCEL: float         = 12.0   # m/s²
GRAVITY: float           = 22.0   # m/s²
JUMP_SPEED: float        = 11.0   # m/s (instant vertical impulse)
JET_ACCEL: float         = 34.0   # m/s² upward while boosting in air
MAX_RISE_SPEED: float    = 10.0   # m/s (jet vertical cap)
BOOST_DRAIN: float       = 17.0   # energy/s while ground-boosting
JET_DRAIN: float         = 15.0   # energy/s while air-jetting
ENERGY_MAX: float        = 100.0  # default; scaled by upgrades
ENERGY_REGEN: float      = 18.0   # /s after ENERGY_REGEN_DELAY
ENERGY_REGEN_DELAY: float = 0.6   # s without spending before regen starts

TURN_RATE: float         = 4.0    # rad/s (yaw slew)
PITCH_RATE: float        = 3.0    # rad/s
MIN_PITCH: float         = -0.7   # rad
MAX_PITCH: float         =  1.3   # rad
STEP_UP: float           = 1.2    # m ledge auto-step height
MAX_SLOPE_DEG: float     = 50.0   # degrees: steeper = wall

REGEN_DELAY: float       = 5.0    # s without dealing/taking damage
REGEN_PER_S: float       = 45.0   # HP/s out-of-combat inside zone
REAR_MULT: float         = 1.5    # damage multiplier from outside front arc
FRONT_ARC_DEG: float     = 180.0  # degrees, centred on aim yaw
CHARGE_MOVE_MULT: float  = 0.5    # movement speed penalty while laser charges
SELF_DAMAGE_MULT: float  = 0.5    # own mortar blast damage fraction

HP_DEFAULT: float        = 1400.0
HP_PER_CUBE: float       = 0.04   # +4% max HP per cube
DAMAGE_PER_CUBE: float   = 0.04   # +4% damage per cube
STAT_CAP_CUBES: int      = 10     # cube bonus capped at 10 cubes
SIZE_PER_CUBE: float     = 0.05   # +5% physical scale per cube

# ---------------------------------------------------------------------------
# 4. WEAPON PARAMETERS (defaults; overwritten from hello.config at startup)
# ---------------------------------------------------------------------------

GUN_DAMAGE: float        = 45.0
GUN_COOLDOWN: float      = 0.24   # s between shots
GUN_SPEED: float         = 252.0  # m/s projectile speed
GUN_RANGE: float         = 230.0  # m max range (then disappears)
GUN_SPREAD_DEG: float    = 0.6
GUN_MAG: int             = 12     # rounds before reload
GUN_RELOAD: float        = 2.4    # s to reload

MORTAR_DAMAGE: float     = 200.0
MORTAR_SPLASH: float     = 160.0
MORTAR_SPLASH_RADIUS: float = 7.0  # m
MORTAR_COOLDOWN: float   = 2.6    # s
MORTAR_SPEED: float      = 72.0   # m/s muzzle speed
MORTAR_GRAVITY: float    = 22.0   # m/s²  (same as world gravity)
MORTAR_KNOCK: float      = 16.0   # m/s   concussion impulse
MORTAR_KNOCK_UP: float   = 9.0    # m/s   upward component of concussion
MORTAR_DRAIN: float      = 45.0   # energy drained by concussion per hit

LASER_DAMAGE: float      = 315.0
LASER_CHARGE_TIME: float = 0.75   # s full charge
LASER_MIN_CHARGE: float  = 0.15   # s minimum to trigger a shot
LASER_MIN_FRAC: float    = 0.35   # damage fraction at minimum charge
LASER_HOLD_S: float      = 2.0    # s a full charge auto-fires if held
LASER_COOLDOWN: float    = 1.65   # s
LASER_RANGE: float       = 280.0  # m
LASER_COOKOFF: float     = 0.60   # extra damage fraction vs mortar carriers
LASER_CHARGE_TURN_MULT: float = 1.0  # turn-rate multiplier during charge

LIGHTNING_DAMAGE: float  = 60.0
LIGHTNING_COOLDOWN: float = 0.25  # s
LIGHTNING_RANGE: float   = 45.0   # m
LIGHTNING_CONE_DEG: float = 30.0  # half-cone in front of aim yaw
LIGHTNING_CHAIN: int     = 2      # extra targets
LIGHTNING_CHAIN_RANGE: float = 14.0  # m chain radius
LIGHTNING_CHAIN_FALLOFF: float = 0.7  # damage per chain hop
LIGHTNING_ENERGY: float  = 3.0    # energy cost per shot
LIGHTNING_SHORT_S: float = 2.0    # s a shorted laser cannot charge

# ---------------------------------------------------------------------------
# 5. VISION & SOUND THRESHOLDS
# ---------------------------------------------------------------------------

VISION_RANGE: float      = 200.0  # m within FOV cone
VISION_FOV_DEG: float    = 120.0  # horizontal (yaw) FOV
VISION_NEAR_RANGE: float = 45.0   # m all-direction proximity
VISION_MEMORY_S: float   = 5.0    # s the server keeps tracking after break

SOUND_NEAR_M: float      = 60.0   # m radius for "near" tier
SOUND_MID_M: float       = 160.0  # m radius for "mid" tier
SOUND_BEARING_NOISE_DEG: float = 7.0   # 1-σ Gaussian bearing error

# How we interpret tracked belief confidence for aiming decisions
# A shot is committed only if estimated position confidence >= FIRE_CONFIDENCE.
FIRE_CONFIDENCE_T0: float = 1.00  # T0 — directly visible: always shoot
FIRE_CONFIDENCE_T1: float = 0.90  # T1 — server memory (< 5 s): shoot
FIRE_CONFIDENCE_T2: float = 0.40  # T2 — stale particle cloud: suppress
FIRE_CONFIDENCE_T3: float = 0.15  # T3 — sound bearing only: suppress

# ---------------------------------------------------------------------------
# 6. MINIMUM RISK MOVEMENT (MRM) WEIGHTS
# ---------------------------------------------------------------------------

MRM_CANDIDATES: int      = 16     # candidate directions sampled per tick
MRM_RADIUS: float        = 15.0   # m radius of candidate ring
MRM_LOS_MAX_ENEMIES: int = 4      # LOS-score only the nearest N enemies (latency cap)
MRM_LOS_RANGE: float     = 150.0  # m — ignore enemies beyond this for LOS scoring
MRM_LOS_STEP_M: float    = 4.0    # coarser ray step for movement scoring (heuristic only)
MRM_TIME_BUDGET_S: float = 0.012  # drop LOS scoring if it runs past this

# Risk component weights — positive = penalty, negative = bonus
W_ENEMY_PROXIMITY: float = 100.0  # / dist² per enemy
W_ZONE_NEAR_BORDER: float = 50.0  # gradient within 10 m of zone edge
W_ZONE_OOB: float        = 1e6    # effectively infinite penalty outside zone
W_ROOF_BONUS: float      = -200.0 # negative = incentive to move onto a roof
W_REVERSAL: float        = 30.0   # penalty for 180° direction flip (jitter prevention)

# ---------------------------------------------------------------------------
# 6b. NAVIGATION (nav.py)
# ---------------------------------------------------------------------------
NAV_CELL_M: float      = 5.0     # walkability grid cell size
NAV_BUDGET_S: float    = 0.006   # max A* time per tick in combat
NAV_BUDGET_CALM_S: float = 0.012 # max A* time per tick when no enemy is in view
NAV_MAX_EXPAND: int    = 6000    # node cap for one-shot NavGrid.astar (tests/tools)
NAV_GOAL_MOVE_M: float = 10.0    # restart the search when the goal moves this far

# ---------------------------------------------------------------------------
# 6c. COMBAT MOVEMENT (motion.py)
# ---------------------------------------------------------------------------
# Preferred (min, max) distance to the target by the weapon we fight with
# Closer than before: the squad's bots are beaten at close range (lightning 22 m,
# gun ~40 m, laser ~55 m is what the stronger bots in practice hold).
BAND_GUN: tuple       = (30.0, 55.0)
BAND_LASER: tuple     = (110.0, 180.0)
BAND_MORTAR: tuple    = (70.0, 170.0)
BAND_LIGHTNING: tuple = (10.0, 28.0)
BAND_NPC: tuple       = (20.0, 45.0)
LIGHTNING_KEEPOUT_M: float = 55.0  # stay out of an enemy's lightning when we don't hold one
STRAFE_FLIP_MIN_S: float   = 0.70  # strafe direction flips on a random timer in [min, max]
STRAFE_FLIP_MAX_S: float   = 1.40
STRAFE_WEIGHT: float       = 1.0
RADIAL_WEIGHT: float       = 0.8
FAR_APPROACH_M: float      = 40.0  # beyond band max + this, path-find toward the target
STRAFE_BOOST_ENERGY: float = 60.0  # boost-strafe only above this energy
DODGE_BOOST_ENERGY: float  = 15.0  # boost on an urgent dodge above this energy
DODGE_WEIGHT: float        = 2.0
LASER_DODGE_DEG: float     = 10.0  # enemy charging a laser aimed within this of us → sidestep
BULLET_DANGER_M: float     = 1.5   # extra miss margin around our body for bullets
SPLASH_DANGER_M: float     = 3.0   # extra margin around mortar splash radius
PEEK_ON_RELOAD: bool       = True  # break LOS while the gun reloads

# ---- Optional architectures (clean baseline: OFF until isolated testing) ----
USE_CONTEXT_STEER: bool    = False  # steer.py: interest/danger maps instead of vector blending
USE_LEAD_LEARNER: bool     = False  # combat.py: learn the best lead multiplier from virtual bullets
USE_INFLUENCE_COVER: bool  = False  # influence.py: grid cover queries instead of the MRM ring
USE_UTILITY_BRAIN: bool    = False  # utility.py: continuous behaviour scores instead of the FSM
ISOLATE_MIN_ENEMIES: int   = 2      # influence "isolate" corner query when this many enemies are known
ISOLATE_REFRESH_S: float   = 0.6    # ...recomputed this often (line-of-sight checks are not free)
ISOLATE_WEIGHT: float      = 1.0    # ...and pulled toward with this weight
STEER_SAFE_MARGIN: float   = 0.45   # slots within this of the lowest danger count as safe
STEER_BULLET_HORIZON_S: float = 0.7 # only bullets reaching us within this are simulated
STEER_BULLET_MARGIN_M: float  = 2.5 # extra clearance around our body when judging a miss
STEER_BULLET_WEIGHT: float    = 2.0 # danger of a bullet that would hit us dead on
OTHER_FOE_PUSH: float      = 0.5   # push away from secondary enemies within 60 m

# ---------------------------------------------------------------------------
# 6d. LOOT & SUPPLY DROPS (motion.py)
# ---------------------------------------------------------------------------
LOOT_EARLY_S: float     = 90.0    # first this many seconds: loot far
LOOT_RANGE_EARLY: float = 150.0
LOOT_RANGE: float       = 70.0
CRATE_STANDOFF_M: float = 20.0    # stop this far from a crate and shoot it
LOOT_FORGET_R: float    = 30.0    # forget remembered loot we're near but no longer see
LOOT_MAX_AGE_S: float   = 90.0
DROP_RANGE: float       = 220.0   # go for supply drops within this distance
DROP_ENEMY_CLEAR_M: float = 40.0  # skip a drop with an enemy mech this close to it

# Fire control
GUN_EXTRA_TOL_DEG: float   = 3.5   # aim slack on top of target size for the gun
LASER_EXTRA_TOL_DEG: float = 1.5   # aim slack on top of target size for the laser
REGEN_QUIET_S: float       = 1.5   # in DISENGAGE, hold fire only after this long unhurt

# TARGET SELECTION
# Keep the current target while it stays legal: a 180 deg torso turn costs
# 0.785 s with no shots, so alternating between similar targets (and reversing
# the strafe that follows it) is pure loss. Switch only when a rival scores
# this much better.
TARGET_STICKY: bool        = True
TARGET_SWITCH_MARGIN: float = 35.0
# HP weight in the target score. Higher favours finishing wounded mechs;
# too high and a wounded mech across the map outranks the one shooting us.
TARGET_HP_WEIGHT: float    = 0.15

# Distance thresholds inside which we switch from MRM to direct flee
PANIC_RANGE: float       = 25.0   # m — enemy too close to MRM: hard flee

# ---------------------------------------------------------------------------
# 7. TACTICAL STATE MACHINE THRESHOLDS
# ---------------------------------------------------------------------------

# BOOTSTRAP_LOOT
PICKUP_SWEEP_RADIUS: float = 60.0  # m — sweep for weapons within this range at start
CRATE_SHOOT_RANGE: float   = 30.0  # m — shoot crates within this range for cubes
CUBES_TO_ASCEND: int       = 1     # collect at least this many cubes before ascending

# ASCEND_ROOFTOP
MIN_ROOF_HEIGHT: float     = 12.0  # m — only target roofs at least this tall
MAX_ROOF_HEIGHT: float     = 28.0  # m — above this is too hard to reach safely
ROOF_ARRIVAL_XY: float     = 5.0   # m horizontal dist to consider "on the roof"
ROOF_ARRIVAL_Z: float      = 2.0   # m vertical tolerance

# AMBUSH / COMBAT
ENGAGE_RANGE: float        = 220.0 # m — engage if enemy is within this range
LASER_ENGAGE_RANGE: float  = 270.0 # m — laser has extra range; prefer laser engages

# OPENING & PACK SURVIVAL
# Placement is the score (16 - place), and practice is one 15-bot squad that
# shares vision and hunts the weakest. Stay out of fights early: loot, take a
# drop, rotate. Engage only after this many seconds, when hurt enough to be
# forced into it, or once combat has already started.
OPENING_CALM_S: float      = 15.0  # 15s quiet scout phase: loot cubes & weapons without loud gunshots
# Above this many mechs alive we still accept an outnumbered fight as a reason
# to break off; below it the circle is small and hiding is no longer an option.
LATE_GAME_ALIVE: int       = 6

# RECOVER (hide and heal between waves)
RECOVER_START_FRAC: float  = 0.0   # 0.0 = fight on; only recover post-kill or when disengage explicitly triggered
RECOVER_DONE_FRAC: float   = 0.75  # heal to this before looting / roaming again
POST_KILL_WINDOW_S: float  = 4.0   # right after a kill...
POST_KILL_HP_FRAC: float   = 0.75  # ...recover if below this (kept small: idling costs kills)
RECOVER_MAX_S: float       = 25.0  # give up recovering after this long
RECOVER_LOOT_RANGE_M: float = 45.0 # while recovering, still fetch cubes / weapons / drops this close
RECOVER_MOVE_SCALE: float  = 0.40  # ~4.7 m/s: under sound.step_speed (5 m/s) → silent
RECOVER_ZONE_LEAD_S: float = 20.0  # while recovering, head into the next circle this long before it shrinks
EVADE_MIN_S: float         = 1.5   # minimum time in a retreat state (no flapping)
# Outnumbered: enemies that HIT us count even when unseen
ATTACKER_WINDOW_S: float   = 2.0
OUTNUMBERED_HP_FRAC: float = 0.45  # retreat from a crowd / 2+ attackers only once below this
PANIC_HP_FRAC: float       = 0.45  # an enemy inside PANIC_RANGE only forces a retreat below this
# Don't hand the squad a target
APPROACH_MIN_HP_FRAC: float = 0.50 # walk toward a far enemy only above this HP...
APPROACH_ALONE_M: float    = 40.0  # ...and only if no other known enemy is this close to it
APPROACH_BOOST_ENERGY: float = 80.0 # boost while closing in only with plenty of energy (keep dodge fuel)
FARM_MIN_HP_FRAC: float    = 0.55  # shoot crates / drones only above this (damage dealt stops regen)
CUBE_FIGHT_RANGE_M: float  = 30.0  # mid-fight, steer toward a cube this close (kills drop several)
CUBE_FIGHT_WEIGHT: float   = 1.4   # ...with this much weight next to the strafe/range vector
HURT_AIM_S: float          = 1.5   # face a hit's direction this long when nothing is visible
CALM_AVOID_M: float        = 140.0 # during the opening calm, take cover from a mech this close
DISENGAGE_HP_FRAC: float   = 0.35  # fraction of max HP — below this (with threats), disengage

# EXPLORING (nothing known, healthy): sweep unvisited cells inside the zone to
# find weapons, cubes, crates and the enemies we need to kill for points.
EXPLORE_STEP_M: float      = 70.0
EXPLORE_UNTIL_S: float     = 240.0
EXPLORE_HP_FRAC: float     = 0.60
EXPLORE_REPICK_S: float    = 25.0

# STUCK DETECTION (motion.py): ordered to travel but not moving → break away
STUCK_WINDOW_S: float      = 1.5   # watch this long...
STUCK_MIN_MOVE_M: float    = 2.5   # ...and call it stuck if we moved less than this
ESCAPE_S: float            = 1.0   # random sideways jump-and-boost for this long

# FLEEING an unseen shooter (hit, but nothing in sight to shoot back at)
FLEE_S: float              = 1.5   # keep running this long after the hit
FLEE_JUKE_RAD: float       = 0.8   # angle off the straight-away direction (harder to follow)
FLEE_BOOST_ENERGY: float   = 40.0

# AIMING
AIM_LATENCY_S: float       = 0.075 # decision + tick delay before a shot actually leaves
REGEN_RESUME_FRAC: float   = 0.65  # fraction of max HP — above this, re-engage
GUN_MIN_TOL_DEG: float     = 0.8   # minimum aim tolerance for firing the gun (deg)
MORTAR_AIM_TOL_DEG: float  = 2.0   # torso must be within this of the lob aim before firing
PICKUP_RADIUS: float       = 4.0   # loot.pickup_radius (overwritten from hello.config)
LOS_STEP_M: float          = 3.0   # line-of-sight sampling step (m)
DEAD_RECKON_MAX_S: float   = 1.0   # stop extrapolating a lost track after this long
MULTI_ENEMY_THRESHOLD: int = 3     # if >= this many enemies visible (and hurt), disengage
LOS_BREAK_DIST: float      = 8.0   # m — step back this far from roof edge to hide

# ZONE_ROTATION
ZONE_EDGE_BUFFER: float    = 15.0  # m — stay at least this far from zone wall
ZONE_BACK_BUFFER: float    = 5.0   # m — allow being this close to zone wall at back

# ENDGAME
ENDGAME_ZONE_RADIUS: float = 70.0  # m — activate endgame state below this zone radius
MORTAR_CLUSTER_RANGE: float = 80.0  # m — mortar into enemy clusters within this range
MORTAR_MIN_RANGE: float    = 15.0  # m — don't mortar yourself

# ---------------------------------------------------------------------------
# 8. UPGRADE PRIORITY ORDER
# ---------------------------------------------------------------------------
# Listed from highest to lowest priority. The bot picks the first item in this
# list that appears among the offered choices.
UPGRADE_PRIORITY: List[str] = [
    "plating",         # +15% max HP (×3) — raw health pool
    "armor",           # -8% dmg taken (×3) — survivability
    "laser_damage",    # +18% laser dmg (×3) — alpha damage
    "laser_charge",    # -25% charge time (×2) — faster shots
    "nanites",         # +60% regen, -1.5s delay (×2) — fast recovery
    "gun_damage",      # +15% gun dmg (×3) — sustained DPS
    "gun_rate",        # +15% fire rate (×3) — cyclic DPS
    "lightning_efficiency", # -35% energy / +10% dmg (×2)
    "capacitor",       # +30 max energy (×3)
    "gyros",           # +30% turn rate (×2)
    "lightning_chain", # +1 chain hop (×2)
    "reactor",         # +30% energy regen (×3)
    "servos",          # +8% move/boost speed (×3)
    "gun_mag",         # +8 mag / -20% reload (×2)
    "lightning_range", # +20% range/cone (×2)
    "jets",            # -25% boost/jet drain (×2)
    "sensors",         # +15m prox / +25m vision / half noise (×2)
    "gun_velocity",    # +25% speed / +15% range (×2)
    "laser_pierce",    # through 1 extra target (×1)
    "mortar_reload",
    "mortar_splash",
    "mortar_velocity",
]

# ---------------------------------------------------------------------------
# 9. ROOFTOP NAVIGATION
# ---------------------------------------------------------------------------

# How close we must be to a wall horizontally before we start jumping
CLIMB_TRIGGER_DIST: float = 4.0   # m horizontal
# Minimum roof height delta to trigger the climb sequence
CLIMB_MIN_DZ: float       = 4.0   # m — don't bother if roof is barely elevated
# When climbing, hold forward momentum at this fraction of walk speed
CLIMB_FORWARD_FRAC: float = 0.8

# Energy safety floor — don't jet if energy is below this (saves for escape)
ENERGY_CLIMB_FLOOR: float = 20.0

# Rooftop climbing is EXPERIMENTAL and OFF by default: the jump/jet physics
# could not be tested offline. Turn on only after checking it in replays.
ROOF_CLIMB_ENABLED: bool  = False
CLIMB_MIN_ENERGY: float   = 90.0   # need nearly full energy to start a climb
CLIMB_MAX_DZ: float       = 25.0   # only roofs at most this high above the ground
CLIMB_TIMEOUT_S: float    = 6.0    # give up a climb attempt after this long

# ---------------------------------------------------------------------------
# 10. DEBUG-DRAW COLOURS
# ---------------------------------------------------------------------------

COLOR_TARGET: str         = "#ff5040"  # target mech line
COLOR_ZONE_GOAL: str      = "#40c0ff"  # zone navigation point
COLOR_SOUND: str          = "#ffd060"  # sound bearing wedge
COLOR_MRM_BEST: str       = "#80ff80"  # MRM selected direction
COLOR_ROOF_GOAL: str      = "#c060ff"  # rooftop ascent goal
COLOR_REGEN: str          = "#00ff80"  # disengaging to regen


# ---------------------------------------------------------------------------
# RUNTIME OVERRIDE — called once from bot.py after hello is received
# ---------------------------------------------------------------------------

def update_from_config(cfg: dict) -> None:
    """
    Overwrite the physics & weapon defaults above with live values from
    hello["config"], so the bot auto-adapts to any server-side balance patches.
    Call this exactly once, at the top of Bot.__init__.
    """
    import sys as _sys
    _m = _sys.modules[__name__]

    # Mech physics
    mech = cfg.get("mech", {})
    for attr, key in [
        ("MECH_RADIUS",        "radius"),
        ("MECH_HEIGHT",        "height"),
        ("MECH_EYE_HEIGHT",    "eye_height"),
        ("MECH_MUZZLE_HEIGHT", "muzzle_height"),
        ("WALK_SPEED",         "walk_speed"),
        ("BOOST_SPEED",        "boost_speed"),
        ("GROUND_ACCEL",       "ground_accel"),
        ("AIR_ACCEL",          "air_accel"),
        ("GRAVITY",            "gravity"),
        ("JUMP_SPEED",         "jump_speed"),
        ("JET_ACCEL",          "jet_accel"),
        ("MAX_RISE_SPEED",     "max_rise_speed"),
        ("BOOST_DRAIN",        "boost_drain"),
        ("JET_DRAIN",          "jet_drain"),
        ("ENERGY_MAX",         "energy"),
        ("ENERGY_REGEN",       "energy_regen"),
        ("ENERGY_REGEN_DELAY", "energy_regen_delay"),
        ("TURN_RATE",          "turn_rate"),
        ("PITCH_RATE",         "pitch_rate"),
        ("MIN_PITCH",          "min_pitch"),
        ("MAX_PITCH",          "max_pitch"),
        ("REGEN_DELAY",        "regen_delay"),
        ("REGEN_PER_S",        "regen_per_s"),
        ("REAR_MULT",          "rear_mult"),
        ("FRONT_ARC_DEG",      "front_arc_deg"),
        ("HP_DEFAULT",         "hp"),
    ]:
        if key in mech:
            setattr(_m, attr, float(mech[key]))

    # Vision
    vision = cfg.get("vision", {})
    for attr, key in [
        ("VISION_RANGE",      "range"),
        ("VISION_FOV_DEG",    "fov_deg"),
        ("VISION_NEAR_RANGE", "near_range"),
        ("VISION_MEMORY_S",   "memory_s"),
    ]:
        if key in vision:
            setattr(_m, attr, float(vision[key]))

    # Sound
    sound = cfg.get("sound", {})
    for attr, key in [
        ("SOUND_NEAR_M",           "near"),
        ("SOUND_MID_M",            "mid"),
        ("SOUND_BEARING_NOISE_DEG","bearing_noise_deg"),
    ]:
        if key in sound:
            setattr(_m, attr, float(sound[key]))

    # Weapons
    weapons = cfg.get("weapons", {})
    gun  = weapons.get("gun",  {})
    mort = weapons.get("mortar", {})
    las  = weapons.get("laser",  {})
    ltng = weapons.get("lightning", {})

    for attr, key in [
        ("GUN_DAMAGE",    "damage"), ("GUN_COOLDOWN",  "cooldown"),
        ("GUN_SPEED",     "speed"),  ("GUN_RANGE",     "range"),
        ("GUN_SPREAD_DEG","spread_deg"), ("GUN_RELOAD","reload"),
    ]:
        if key in gun: setattr(_m, attr, float(gun[key]))
    if "mag" in gun: _m.GUN_MAG = int(gun["mag"])

    for attr, key in [
        ("MORTAR_DAMAGE",        "damage"),   ("MORTAR_SPLASH",   "splash"),
        ("MORTAR_SPLASH_RADIUS", "splash_radius"), ("MORTAR_COOLDOWN","cooldown"),
        ("MORTAR_SPEED",         "speed"),    ("MORTAR_GRAVITY",  "gravity"),
        ("MORTAR_KNOCK",         "knock"),    ("MORTAR_KNOCK_UP", "knock_up"),
        ("MORTAR_DRAIN",         "drain"),
    ]:
        if key in mort: setattr(_m, attr, float(mort[key]))

    for attr, key in [
        ("LASER_DAMAGE",          "damage"),    ("LASER_CHARGE_TIME","charge"),
        ("LASER_MIN_CHARGE",      "min_charge"),("LASER_MIN_FRAC",   "min_frac"),
        ("LASER_HOLD_S",          "hold_s"),    ("LASER_COOLDOWN",   "cooldown"),
        ("LASER_RANGE",           "range"),     ("LASER_COOKOFF",    "cookoff"),
    ]:
        if key in las: setattr(_m, attr, float(las[key]))

    for attr, key in [
        ("LIGHTNING_DAMAGE",     "damage"),     ("LIGHTNING_COOLDOWN", "cooldown"),
        ("LIGHTNING_RANGE",      "range"),      ("LIGHTNING_CONE_DEG", "cone_deg"),
        ("LIGHTNING_CHAIN_RANGE","chain_range"),("LIGHTNING_CHAIN_FALLOFF","chain_falloff"),
        ("LIGHTNING_ENERGY",     "energy"),     ("LIGHTNING_SHORT_S",  "short_s"),
    ]:
        if key in ltng: setattr(_m, attr, float(ltng[key]))
    if "chain" in ltng: _m.LIGHTNING_CHAIN = int(ltng["chain"])

    # Cubes / economy
    cubes = cfg.get("cubes", {})
    for attr, key in [
        ("HP_PER_CUBE",     "hp_per_cube"),
        ("DAMAGE_PER_CUBE", "damage_per_cube"),
        ("SIZE_PER_CUBE",   "size_per_cube"),
    ]:
        if key in cubes: setattr(_m, attr, float(cubes[key]))
    if "stat_cap" in cubes: _m.STAT_CAP_CUBES = int(cubes["stat_cap"])

    # Loot
    loot = cfg.get("loot", {})
    if "pickup_radius" in loot: _m.PICKUP_RADIUS = float(loot["pickup_radius"])

    # Derived: mortar tube sits half a metre above the mech's head
    _m.MECH_MORTAR_HEIGHT = _m.MECH_HEIGHT + 0.5
