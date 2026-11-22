package bastion

// seedSalt shifts the RNG seed. The practice scorer is deterministic, so a new salt is a new sample of the same strategy.
const seedSalt int64 = 0

// useExtras switches on our own additions that the friend's v19 does not have: the Ghost switch when the next
// zone radius is 70 m or less, the early laser avoidance, the adaptive gun lead and beam tracking.
// false = behave like v19.
const useExtras = false
