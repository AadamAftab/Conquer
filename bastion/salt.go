package bastion

// seedSalt shifts the RNG seed. The practice scorer is deterministic, so a new salt is a new sample of the same strategy.
const seedSalt int64 = 0

// useExtras switches on our own additions that the friend's v19 does not have: the Ghost switch when the next
// zone radius is 70 m or less, the early laser avoidance, the adaptive gun lead and beam tracking.
// false = behave like v19.
const useExtras = false

// useRetreatHold: once the squad rule decides we are losing, keep retreating until HP is back to 78 %
// (or 12 s after we last saw the fight) instead of ending the retreat 1.5 s after losing is last true.
const useRetreatHold = false

// useTreeSnipe: bonus in fightMove for a position the target cannot see us from (trees or trunks between us)
// but our shots still reach (shots pass trees).
const useTreeSnipe = false
