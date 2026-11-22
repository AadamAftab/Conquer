package bastion

import "math"

const (
	leadHistCap = 16
	fireLatency = 0.075
)

// PosSample records a target's position and velocity snapshot.
type PosSample struct {
	T  float64
	X  float64
	Y  float64
	VX float64
	VY float64
}

// LeadTracker dynamically models target motion persistence to counter jitter strafing.
type LeadTracker struct {
	Samples []PosSample
	Index   int
	Count   int
}

// NewLeadTracker creates an empty ring-buffer tracker for an opponent.
func NewLeadTracker() *LeadTracker {
	return &LeadTracker{
		Samples: make([]PosSample, leadHistCap),
	}
}

// Record ingests a new observation of target kinematics.
func (lt *LeadTracker) Record(t, x, y, vx, vy float64) {
	if lt.Count > 0 {
		prev := lt.Samples[(lt.Index+leadHistCap-1)%leadHistCap]
		if t <= prev.T || t-prev.T > 0.35 {
			lt.Count = 0
			lt.Index = 0
		}
	}
	lt.Samples[lt.Index] = PosSample{T: t, X: x, Y: y, VX: vx, VY: vy}
	lt.Index = (lt.Index + 1) % leadHistCap
	if lt.Count < leadHistCap {
		lt.Count++
	}
}

// Multiplier evaluates how persistent the opponent's trajectory has been.
// Steady linear walkers return ~1.0 (full ballistic lead).
// Oscillating/jittering strafers return ~0.0-0.2 (aim directly at their current hull).
func (lt *LeadTracker) Multiplier(t float64) float64 {
	if lt.Count < 6 {
		return 1.0 // insufficient history; assume standard lead
	}
	var first, last *PosSample
	sumSpeed := 0.0
	n := 0
	for i := 0; i < lt.Count; i++ {
		s := &lt.Samples[(lt.Index+leadHistCap-1-i)%leadHistCap]
		if s.T > t+0.02 {
			continue
		}
		if s.T < t-1.25 {
			break
		}
		if last == nil {
			last = s
		}
		first = s
		sumSpeed += math.Hypot(s.VX, s.VY)
		n++
	}
	if n < 5 || last == nil || last.T-first.T < 0.35 {
		return 1.0
	}
	netDist := math.Hypot(last.X-first.X, last.Y-first.Y)
	dt := last.T - first.T
	meanSpeed := netDist / dt
	avgSpeed := sumSpeed / float64(n)
	if avgSpeed < 0.8 {
		return 0.1 // target is nearly stationary
	}
	pers := meanSpeed / math.Max(avgSpeed, 0.5)
	// If persistence > 0.70: straight walker -> 1.0
	// If persistence < 0.30: rapid jitterer -> 0.0
	return math.Max(0.0, math.Min(1.0, (pers-0.30)/0.40))
}
