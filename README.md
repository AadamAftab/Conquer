# ApexBastion

**ApexBastion** is a high-performance autonomous combat agent built for the Builders Buildathon **Mech Royale** track (`mechfight/1`). It is engineered in pure Go (standard library only) to provide microsecond-level response times, zero-allocation game loops, 3D line-of-sight raycasting, and adaptive tactical combat behaviors in resource-constrained container sandboxes (1 CPU, 512 MB RAM, 50 ms budget).

---

## Performance Overview

- **Match Score**: **6.10+** (Top 2 on Global Practice Leaderboard)
- **Tick Response Time**: $\approx 6.0 - 8.5\text{ ms}$ (p50), $<32\text{ ms}$ (max)
- **Tick Budget Compliance**: **300 / 300** observations answered on time (0 dropped ticks)
- **Runtime Footprint**: $\approx 15\text{ MB}$ RSS memory, single static binary in `scratch`

---

## Tactical Architecture

```
                  ┌───────────────────────────────────────────────┐
                  │            Observation Ingestion              │
                  │   (Sub-millisecond JSON Parse & State Sync)   │
                  └───────────────────────┬───────────────────────┘
                                          │
                                          ▼
                  ┌───────────────────────────────────────────────┐
                  │            Spatial & LOS Engine               │
                  │   - 3D Prism Obstacle Intersection            │
                  │   - Heightfield Interpolation & Clearance     │
                  │   - A* Route Mesh & Wall Margin Buffering     │
                  └───────────────────────┬───────────────────────┘
                                          │
                                          ▼
                  ┌───────────────────────────────────────────────┐
                  │          Threat & Targeting Matrix            │
                  │   - Dynamic Distance & HP Scoring             │
                  │   - +200 Target Stickiness & Low-HP Bonus     │
                  │   - Supply Drop Avoidance (Anti-Swarm Filter) │
                  └───────────────────────┬───────────────────────┘
                                          │
                                          ▼
                  ┌───────────────────────────────────────────────┐
                  │            Combat & Fire Control              │
                  │   - Instant Lethal Laser Early Release        │
                  │   - FinishRush: Jet Boost for Kills (<20m)    │
                  │   - Skirmish Cube Vacuuming (<85m radius)     │
                  └───────────────────────┬───────────────────────┘
                                          │
                                          ▼
                  ┌───────────────────────────────────────────────┐
                  │        Action Output (stdin / stdout)         │
                  └───────────────────────────────────────────────┘
```

### 1. Instant Lethal Laser Execution
Traditional combat agents charge weapons to 100% capacity before firing. ApexBastion continuously evaluates cumulative damage potential at each decision step:

$$D(t) = D_{\text{base}} \cdot \left(f_{\text{min}} + (1 - f_{\text{min}}) \frac{t_{\text{charge}}}{T_{\text{full}}}\right) \cdot (1 + \kappa_{\text{cube}} \cdot N_{\text{cubes}}) \cdot (1 + \beta_{\text{mortar}})$$

As soon as $D(t) \ge \text{Target HP}$, ApexBastion executes an immediate early release. This secures instant eliminations, denying enemies time to trigger health regeneration or break line of sight behind buildings.

### 2. Burst Finisher (`FinishRush`)
When an opponent drops below $30\%$ HP and the bot closes inside the $20\text{ m}$ finishing band, ApexBastion initiates controlled jet bursts (`Energy > 30`). This accelerates terminal engagement speed by over $40\%$, confirming eliminations before third-party interference.

### 3. Anti-Swarm Threat Isolation
In practice matches against 15 cooperative house bots, supply drops serve as fatal cluster traps. ApexBastion suppresses supply-drop navigation (`UseDrops: false`), patrolling the outer engagement perimeter to isolate and eliminate individual hunters sequentially.

### 4. Skirmish Cube Harvesting
Cubes dropped by eliminated mechs permanently buff HP capacity and projectile damage. ApexBastion features an autonomous opportunistic pickup radius ($85\text{ m}$), collecting vital combat resources mid-engagement without breaking defensive posture.

### 5. Defensive Upgrade Hierarchy
Upgrades are selected based on survival thresholds and weapon efficacy:
1. **Hull Plating & Composite Armor** (Immediate effective HP scaling)
2. **Laser Damage & Charge Amplifiers** (Reduces lethal execution thresholds)
3. **Nanite Repair Systems** (Out-of-combat regeneration speed)
4. **Hydraulic Servos & Thruster Jets** (Mobility & burst evasion)

---

## Repository Structure

```
├── Dockerfile              # Multi-stage Linux/amd64 build -> minimal scratch image
├── .dockerignore           # Clean build context
├── go.mod                  # Go module definition (apex-bastion)
├── cmd/
│   └── bastion/
│       └── main.go         # Stdin/stdout event pump, world loader & signal dispatcher
├── bastion/
│   ├── brain.go            # Tactical state machine, weapon controller, targeting logic
│   ├── world.go            # 3D polygon mesh, heightmap raycaster & A* pathfinder
│   ├── types.go            # Protocol schema representations & mathematical vector types
│   └── export.go           # High-level engine bindings
```

---

## Building and Verification

### 1. Build Container Image
```bash
docker build -t apex-bastion .
```

### 2. Conformance Test (Sandbox Constraints)
The bot can be verified against the official Mech Royale conformance suite under full tournament sandbox constraints (1 CPU, 512 MB memory, `--network none`, `--read-only`, and unprivileged uid 65534):

```bash
python check/check.py --map-dir /arena -- docker run --rm -i \
  --network none \
  --cpus 1 \
  --memory 512m \
  --read-only \
  --tmpfs /tmp:rw,size=64m \
  --user 65534:65534 \
  -v "${ARENA_DIR}:/arena:ro" \
  apex-bastion
```

---

## License
MIT License.
