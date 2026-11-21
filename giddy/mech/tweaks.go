package mech

// Our tweaks on top of the giddy brain (the default values keep its original behaviour, commit 70c8b23).
const (
	// tweakHopT: seconds of opening hop (jump + boost with the heading turned 60 degrees left), as in our TypeScript bot.
	// It acts before the bot can tell practice from free-for-all, so it changes free-for-all too.
	tweakHopT = 0.0
	// tweakLoseK: scale of the TTUN "losing" thresholds of the squad lose rule (1 = original).
	tweakLoseK = 1.0
	// tweakSquadT: time (s) after which the squad (practice) parameters take over from the cautious opening ones (20 = original).
	tweakSquadT = 20.0
	// tweakDropV: squad mode only, weight of uncontested supply drops in the calm loot choice (0 = never, as giddy; 1 = 3 cubes + missing HP).
	tweakDropV = 0.0
	// tweakFightDropR: squad mode only, radius (m) within which a landed, uncontested supply drop is captured mid-fight (0 = never).
	tweakFightDropR = 0.0
	// tweakNano: squad mode only, repair nanites right after plating in the upgrade order (false = original laser order).
	tweakNano = false

	// Squad (practice) parameters, applied by squadParams only.
	// tweakFinish: range (m) we close to on a weak foe (Params.Finish, 14 = original).
	tweakFinish = 14.0
	// tweakFinishHP: foe HP fraction under which we close in (Params.FinishHP, 0 = original 0.38).
	tweakFinishHP = 0.0
	// tweakFinishRush: boost while closing in on a weak foe more than 2*Finish away with energy > 30 (Params.FinishRush, false = original).
	tweakFinishRush = false
	// tweakPursue: chase an engaged foe out of line of sight when its HP fraction is under this (Params.Pursue, 1.01 = original: always).
	tweakPursue = 1.01
	// tweakStick: target score bonus for keeping the current target (Params.Stick, 120 = original).
	tweakStick = 120.0
	// tweakHear: squad mode only, look toward enemy sounds no visible mech explains and side-step unseen laser charges (false = original).
	tweakHear = false
	// tweakNpcGoneHP: a drone that vanishes from the observation below this HP fraction is dead and forgotten (0 = original: chased and shot for up to 12 s).
	tweakNpcGoneHP = 0.0
	// tweakCubeLevel: in-fight cube detours skip cubes on roofs or decks above our level, which cannot be picked up (false = original).
	tweakCubeLevel = false
	// tweakNoAirBoost: no boost while airborne outside escape and shell dodging, since boost in the air is a jet climb (false = original).
	tweakNoAirBoost = false
	// tweakNpcTie: ties between drones that hit us are broken by distance then id instead of map order (false = original, not reproducible).
	tweakNpcTie = false
	// tweakRear: squad mode only, target score bonus for a foe showing us its back (rear armor takes rear_mult damage),
	// and the rear multiplier counted in the lethal laser release (0 = original).
	tweakRear = 0.0
	// tweakFFAUntil: a death we did not cause switches to free-for-all play only if first seen before this time (s).
	// In 77 recorded free-for-all matches the first such death always came before 37 s, for every mech, so those
	// matches play exactly as before; in practice a late zone or drone death no longer drops the squad settings.
	// (0 = original: any such death, at any time.)
	tweakFFAUntil = 0.0
)

type tweakSet struct {
	hopT, loseK, squadT float64
	finish, finishHP    float64
	finishRush          bool
	pursue, stick       float64
}

// originalTweaks is the 70c8b23 behaviour.
func originalTweaks() tweakSet {
	return tweakSet{hopT: 0, loseK: 1, squadT: 20, finish: 14, finishHP: 0, finishRush: false, pursue: 1.01, stick: 120}
}

// tw holds the tweaks in use (a variable so that tests can compare against originalTweaks).
var tw = tweakSet{
	hopT: tweakHopT, loseK: tweakLoseK, squadT: tweakSquadT,
	finish: tweakFinish, finishHP: tweakFinishHP, finishRush: tweakFinishRush,
	pursue: tweakPursue, stick: tweakStick,
}
