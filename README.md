# mech-royale-bot

Bot for the Builders Buildathon "Mech Royale" track (protocol `mechfight/1`), team "Soloing".

## What runs

The submitted image (`Dockerfile`) runs `automata/`, our Rust bot (MIT License, see `automata/LICENSE`).
Its tunable parameters are in `automata/bot.json` (read through `MECH_PARAMS`).

```sh
docker build -t my-bot .
```

## Our own bot

The TypeScript bot (`bot.ts`, `src/`, run with bun, image `Dockerfile.ts`) is our earlier work:
an economy core (TTUN-style methods), a Lance-style core, free-for-all modes, a map model with
line of sight, tree-crown vision and A* navigation, and a local simulator in `tools/`.

```sh
docker build -f Dockerfile.ts -t my-ts-bot .
```
