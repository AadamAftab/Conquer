package mech

// Our tweaks on top of the conquer brain (zero values keep its original behaviour).
const (
	// tweakHopT: seconds of opening hop (jump + boost with the heading turned 60 degrees left), as in our TypeScript bot.
	tweakHopT = 0.0
	// tweakLoseK: scale of the TTUN "losing" thresholds of the squad lose rule (1 = original).
	tweakLoseK = 1.0
	// tweakSquadT: time (s) after which the squad (practice) parameters take over from the cautious opening ones (20 = original).
	tweakSquadT = 20.0
)
