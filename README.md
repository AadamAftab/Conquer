# mech-royale-bot

Bot for the Builders Buildathon "Mech Royale" track (protocol `mechfight/1`), team "not even close".

## What runs

The submitted image (`Dockerfile`) runs the Go bot in `conquer/`: it is
[conquer-mech-royale](https://github.com/justnau/conquer-mech-royale) by conquer,
used under the MIT License (see `conquer/LICENSE`), taken at commit `70c8b23`. Our changes to it:
the bot name, and an RNG seed salt (`conquer/mech/salt.go`).

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
