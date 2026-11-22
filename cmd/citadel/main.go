package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"

	"aadam-citadel/citadel"
)

// useVanguard selects the engine in vanguard/.
// false = run the citadel engine below, which is kept unchanged.
const useVanguard = true

func main() {
	if useVanguard {
		runVanguard()
		return
	}
	lines := make(chan []byte, 256)
	go func() {
		r := bufio.NewReaderSize(os.Stdin, 1<<20)
		for {
			line, err := r.ReadBytes('\n')
			if len(line) > 0 {
				lines <- line
			}
			if err != nil {
				close(lines)
				return
			}
		}
	}()
	out := bufio.NewWriterSize(os.Stdout, 1<<16)
	send := func(v any) {
		b, err := json.Marshal(v)
		if err != nil {
			return
		}
		out.Write(b)
		out.WriteByte('\n')
		out.Flush()
	}
	var brain *citadel.Brain
	var probe struct {
		Type string `json:"type"`
	}
	for {
		first, ok := <-lines
		if !ok {
			return
		}
		batch := [][]byte{first}
	drain:
		for {
			select {
			case l, ok := <-lines:
				if !ok {
					break drain
				}
				batch = append(batch, l)
			default:
				break drain
			}
		}
		var latest []byte
		for _, raw := range batch {
			probe.Type = ""
			if json.Unmarshal(raw, &probe) != nil {
				continue
			}
			switch probe.Type {
			case "hello":
				var h citadel.Hello
				if err := json.Unmarshal(raw, &h); err != nil {
					fmt.Fprintln(os.Stderr, "hello:", err)
				}
				dir := h.Map.Dir
				w, err := citadel.LoadWorld(dir)
				if err != nil && dir != "/arena" {
					w, err = citadel.LoadWorld("/arena")
				}
				if err != nil {
					fmt.Fprintln(os.Stderr, "map:", err, "- playing on a flat map")
					w = citadel.FlatWorld()
				}
				brain = citadel.NewBrain(&h, w)
				send(map[string]string{"type": "ready", "name": "AadamCitadel"})
			case "obs":
				latest = raw
			case "end":
				return
			}
		}
		if latest == nil {
			continue
		}
		var o citadel.Obs
		if err := json.Unmarshal(latest, &o); err != nil || brain == nil {
			var t struct {
				Tick int `json:"tick"`
			}
			json.Unmarshal(latest, &t)
			send(citadel.Action{Tick: t.Tick})
			continue
		}
		send(brain.Act(&o))
	}
}
