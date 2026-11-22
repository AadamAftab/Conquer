# AGENTS.md

Go 1.26 match bot. Reads JSON-line observations on stdin, writes JSON-line actions on stdout. No third-party dependencies.

## Layout
- `cmd/citadel/main.go` — entry point; `useVanguard` picks the engine. `vanguard.go` runs the vanguard engine and has a `blind` fallback when the map can't load.
- `vanguard/bot` — current engine (movement, combat, cover/hide, roofs, strategy). `hidemap.gz` is embedded via `go:embed`; keep it next to `hidemap.go`.
- `vanguard/proto` — wire types (Obs, Action). `vanguard/world` — map loading, nav, exposure.
- `citadel/` — original engine (brain, world, types) with its own test.

## Commands
- Build: `go build ./cmd/citadel`
- Vet/test: `go vet ./... && go test ./...`
- Docker: `docker build -t aadam-citadel .` (see README for the match-limit check)

## Conventions
- Stdout is the protocol channel: never print debug output there; use stderr.
- Runs under 1 CPU / 512 MB, read-only filesystem, no network; keep per-tick work cheap.
- Run `gofmt` on files you edit.
