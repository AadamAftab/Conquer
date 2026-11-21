package mech

// Our tweaks on top of the conquer brain (zero values keep its original behaviour).
const (
	// tweakHopT: seconds of opening hop (jump + boost with the heading turned 60 degrees left), as in our TypeScript bot.
	tweakHopT = 0.0
	// tweakLoseK: scale of the TTUN "losing" thresholds of the squad lose rule (1 = original).
	tweakLoseK = 1.0
	// tweakSquadT: time (s) after which the squad (practice) parameters take over from the cautious opening ones (20 = original).
	tweakSquadT = 20.0
	// tweakDropV: squad mode only, weight of uncontested supply drops in the calm loot choice (0 = never, as conquer; 1 = 3 cubes + missing HP).
	tweakDropV = 0.0
	// tweakFightDropR: squad mode only, radius (m) within which a landed, uncontested supply drop is captured mid-fight (0 = never).
	tweakFightDropR = 0.0
	// tweakNano: squad mode only, repair nanites right after plating in the upgrade order (false = original laser order).
	tweakNano = false
)
