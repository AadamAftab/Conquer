package bastion

import (
	"math"
	"testing"
)

// A beam fired by a mech we cannot see must create/refresh its track at the beam's muzzle without claiming
// the mech is visible, and a beam from a mech that is visible this tick must leave its real track alone.
func TestBeamStampsHiddenShooter(t *testing.T) {
	b := NewBrain(&Hello{}, FlatWorld())
	o := &Obs{T: 30, Alive: 10}
	o.Self.Pos = Vec3{0, 0, 0}
	o.Self.HP, o.Self.MaxHP = 1400, 1400
	o.Self.Stats.Radius, o.Self.Stats.MuzzleHeight = 1, 4.5
	o.Beams = []Beam{{By: 5, Kind: "laser", Path: []Vec3{{100, 0, 4.5}, {0, 0, 4.5}}, Age: 0.1}}

	b.now = o.T
	b.observe(o)
	tr := b.tracks[5]
	if tr == nil {
		t.Fatal("beam shooter was not tracked")
	}
	if tr.Seen == o.T {
		t.Fatal("hidden shooter must not be marked visible")
	}
	if math.Abs(tr.Pos[0]-100) > 1e-9 || tr.Pos[2] != 0 {
		t.Fatalf("wrong shooter position %v", tr.Pos)
	}
	if tr.BeamAt != o.T || tr.Dist != 100 {
		t.Fatalf("beam bookkeeping wrong: %+v", tr)
	}

	o2 := &Obs{T: 31, Alive: 10, Mechs: []MechSeen{{ID: 5, Kind: "mech", Pos: Vec3{50, 10, 0}, HP: 900, MaxHP: 1400}}}
	o2.Self = o.Self
	o2.Beams = o.Beams
	b.now = o2.T
	b.observe(o2)
	if got := b.tracks[5].Pos; got[0] != 50 {
		t.Fatalf("visible mech overwritten by beam: %v", got)
	}
}
