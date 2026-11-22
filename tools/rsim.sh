#!/usr/bin/env bash
# usage: tools/rsim.sh <n_matches> <seed0> [docker -e options...]  -> mean score of the Rust bot in the sim
n=$1; seed=$2; shift 2
S="$(cd "$(dirname "$0")/../.." && pwd)"
MSYS_NO_PATHCONV=1 docker run --rm -v "$(cygpath -w "$S/chk"):/w" -v "$(cygpath -w "$S/kitx/mechfight-kit"):/kit:ro" -w /w -e SIMRUST=1 -e MECH_PARAMS=/w/automata/bot.json "$@" sim-img node tools/sim_rust.ts $n $seed 0 /kit 2>&1 | grep "^mean score" | cut -c1-120
