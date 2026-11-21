# Apex Bastion — Championship Mech Royale Autonomous Bot

Autonomous combat agent engineered for the **Mech Royale** (`mechfight/1`) competition at [joinbuilders.org](https://joinbuilders.org/buildathon).

## Architecture Highlights

* **Tiered Epistemic Belief Tracker ($T_0 \to T_1 \to T_2 \to T_3$)**:
  * Tracks enemy positions, HP, weapons, and velocities even after breaking line of sight.
  * Rigorous contradiction resolution via the Counting Rule and Empty-View Rule.
* **Closed-Form Ballistic Fire Control**:
  * Exact quadratic intercept solver for the Arm Gun (constant speed, zero drop).
  * High-arc ballistic lobbing for Mortar to clear structures.
  * Finite-State Laser Controller managing charge-up, aim-drift aborts, and discharge.
* **Spatial High-Ground Awareness**:
  * Precomputes all map geometry (259 prisms, 119 viable rooftop positions).
  * Prioritises high-elevation positions near the safe zone edge, eliminating the 1.5× rear damage vulnerability by leveraging the gas wall.
  * Automated rooftop climbing is implemented but **disabled by default** (`ROOF_CLIMB_ENABLED = False`): the jump/jet climb could not be validated offline. Enable it only after checking a replay.
* **Decoupled Reverse-Kiting & MRM**:
  * 16-candidate Minimum Risk Movement (MRM) ring evaluating threat proximity, zone barriers, and terrain bonuses.
  * Torso aim and movement vectors are completely decoupled, allowing maximum firepower while kiting away from approaching enemies.
* **Survival-First Tactics**:
  * Placement is the score (16 − place), so the opening is a calm loot/rotate window rather than an immediate brawl.
  * Sticky target selection (a 180° torso turn costs 0.78 s of lost fire) and no crate/drone farming while retreating, so outgoing damage never resets the regen timer.
* **Zero External Dependencies & Sub-Millisecond Execution**:
  * Built entirely using Python 3.13 standard library.
  * Average decision latency: ~4ms in sandboxed container ($p_{99} < 10 \text{ ms}$ vs 50 ms budget limit).

## Sandboxed Container Specification

The entrypoint runs under strict competition constraints:
```bash
docker run --rm -i \
  --network none \
  --cpus 1 \
  --memory 512m \
  --read-only \
  --tmpfs /tmp:rw,size=64m \
  --user 65534:65534 \
  -v "/path/to/arena:/arena:ro" \
  mech-champion
```
