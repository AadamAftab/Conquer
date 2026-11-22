# AadamCitadel

My match-bot, written in Go. It reads observations as JSON lines on stdin and
answers with actions on stdout.

- `citadel/` — the original brain: world model, planning and combat logic.
- `vanguard/` — the current engine (bot, protocol, world/nav). Enabled by default via `useVanguard` in `cmd/citadel/main.go`.
- `cmd/citadel/` — entry point.

## Build and run

```bash
docker build -t aadam-citadel .
```

Check it under the match limits:

```bash
python check/check.py --map-dir /arena -- docker run --rm -i \
  --network none --cpus 1 --memory 512m --read-only \
  --tmpfs /tmp:rw,size=64m --user 65534:65534 \
  -v "${ARENA_DIR}:/arena:ro" aadam-citadel
```

Author: Aadam
