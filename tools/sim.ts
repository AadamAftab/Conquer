// Approximate kinematic sandbox: our Brain against scripted hunters on the real map.
// Not the real game. It is for finding stuck spots, zone deaths, firing bugs and timing, not for tuning.
//   node tools/sim.ts [matches=4] [seed=1] [kit=../mechfight-kit]
import { readFileSync } from "node:fs"
import { gunzipSync } from "node:zlib"
import { Brain } from "../src/brain.ts"
import { GameMap } from "../src/map.ts"
import { Navigator } from "../src/nav.ts"
import { draw, type Observation } from "../mechfight.ts"
import { angDiff, rng, type V2, type V3 } from "../src/geom.ts"

const N_MATCH = Number(process.argv[2] ?? 4)
const SEED = Number(process.argv[3] ?? 1)
const KIT = process.argv[4] ?? "../mechfight-kit"
const lines = gunzipSync(readFileSync(KIT + "/check/stream.jsonl.gz")).toString("utf8").trim().split("\n")
const HELLO = JSON.parse(lines[0])
HELLO.map.dir = KIT + "/arena"
const C = HELLO.config
const MAP = new GameMap(HELLO.map.dir)
const DT = 1 / C.match.tick_hz
const RQ = C.mech.radius + 0.9

type W = { kind: string; cooldown: number; ammo?: number; reload?: number; charge?: number; stats: Record<string, number> }
type Mech = {
  id: number; pos: V3; vel: V3; aim: V2; cmdAim: V2; hp: number; maxHp: number; energy: number; spendT: number
  grounded: boolean; weapons: (W | null)[]; alive: boolean; deathTick: number; cubes: number
  lastCombatT: number; seen: Map<number, number>; brain: Brain | null; nav: Navigator | null
  move: V2; boost: boolean; jump: boolean; fire: [boolean, boolean]; skill: number; hunter: boolean; events: any[]; sounds: any[]
}
type Shot = { by: number; pos: V3; vel: V3; ttl: number; dmg: number }

const mkWeapon = (kind: string): W => {
  const s = { ...C.weapons[kind] }
  return kind === "gun" ? { kind, cooldown: 0, ammo: s.mag, reload: 0, stats: s } : kind === "laser" ? { kind, cooldown: 0, charge: 0, stats: s } : { kind, cooldown: 0, stats: s }
}

function zoneAt(phases: { c: V2; r: number; t0: number; t1: number; dps: number }[], t: number) {
  // phases[i]: circle at end of shrink i; hold before shrink
  let prev = phases[0]
  for (let i = 1; i < phases.length; i++) {
    const p = phases[i]
    if (t < p.t0) return { c: prev.c, r: prev.r, dps: p.dps, next: p }
    if (t < p.t1) {
      const f = (t - p.t0) / (p.t1 - p.t0)
      return { c: [prev.c[0] + (p.c[0] - prev.c[0]) * f, prev.c[1] + (p.c[1] - prev.c[1]) * f] as V2, r: prev.r + (p.r - prev.r) * f, dps: p.dps, next: p, prev }
    }
    prev = p
  }
  return { c: prev.c, r: prev.r, dps: 260, next: prev }
}

function runMatch(seed: number) {
  const R = rng(seed)
  const t0wall = performance.now()
  // zone schedule
  const phases: { c: V2; r: number; t0: number; t1: number; dps: number }[] = [{ c: C.zone.start_center, r: C.zone.start_radius, t0: 0, t1: 0, dps: 0 }]
  let tt = 0
  for (const ph of C.zone.phases) {
    const prev = phases[phases.length - 1]
    const a = R() * Math.PI * 2, d = R() * (prev.r - ph.radius) * 0.9
    phases.push({ c: [prev.c[0] + Math.cos(a) * d, prev.c[1] + Math.sin(a) * d], r: ph.radius, t0: tt + ph.hold_s, t1: tt + ph.hold_s + ph.shrink_s, dps: ph.dps })
    tt += ph.hold_s + ph.shrink_s
  }
  // spawns on passable ground
  const comp = MAP.comp(RQ)
  const spawn = (): V3 => {
    for (;;) {
      const a = R() * Math.PI * 2, d = Math.sqrt(R()) * C.zone.start_radius * 0.8
      const x = C.zone.start_center[0] + Math.cos(a) * d, y = C.zone.start_center[1] + Math.sin(a) * d
      const n = MAP.nodeOf(x, y)
      if (n >= 0 && comp[n] === 0 && MAP.nClear[n] > RQ + 1) return [x, y, MAP.surfAt(x, y)]
    }
  }
  const mechs: Mech[] = []
  for (let i = 0; i < 16; i++) {
    const me = i === 0
    const m: Mech = {
      id: i, pos: spawn(), vel: [0, 0, 0], aim: [R() * 6.28 - 3.14, 0], cmdAim: [0, 0], hp: C.mech.hp, maxHp: C.mech.hp, energy: C.mech.energy, spendT: -9,
      grounded: true, weapons: [mkWeapon("gun"), null], alive: true, deathTick: 1e9, cubes: 0, lastCombatT: -99, seen: new Map(),
      brain: null, nav: me ? null : new Navigator(MAP), move: [0, 0], boost: false, jump: false, fire: [false, false],
      skill: i < 5 ? 0.25 : i < 9 ? 0.45 : i < 13 ? 0.65 : 0.85, hunter: false, events: [], sounds: [],
    }
    m.cmdAim = [...m.aim] as V2
    if (!me && R() < 0.5) m.weapons[1] = mkWeapon(R() < 0.5 ? "laser" : "lightning")
    mechs.push(m)
  }
  const hello = { ...HELLO, slot: 0, seed: seed * 7 + 1 }
  mechs[0].brain = new Brain(hello)
  const pickups: { id: number; kind: string; pos: V3 }[] = []
  for (let k = 0; k < C.loot.floor_count; k++) {
    const p = spawn()
    pickups.push({ id: 8000 + k, kind: ["laser", "lightning", "mortar"][Math.floor(R() * 3)], pos: p })
  }
  let shots: Shot[] = []
  const drops: { id: number; pos: V3; callT: number; landT: number; holder: number | null; progress: number; taken: boolean }[] = []
  let dropsTaken = 0
  const times: number[] = []
  let late = 0
  const stats = { shotsUs: 0, hitsUs: 0, dmgDealt: 0, dmgTaken: 0, zoneDmg: 0, kills: 0, stuckTicks: 0 }
  let tick = 0
  let lastPos: V3 = [...mechs[0].pos] as V3
  let lastMoveT = 0
  const alive = () => mechs.filter((m) => m.alive)
  const center = (m: Mech): V3 => [m.pos[0], m.pos[1], m.pos[2] + 4.5]
  const damage = (target: Mech, amount: number, by: Mech | null, weapon: string, from: V3 | null, t: number) => {
    if (!target.alive) return
    let rear = false
    if (from && weapon !== "zone") {
      const b = Math.atan2(from[1] - target.pos[1], from[0] - target.pos[0])
      if (Math.abs(angDiff(b, target.aim[0])) > ((C.mech.front_arc_deg / 2) * Math.PI) / 180) (amount *= C.mech.rear_mult), (rear = true)
    }
    target.hp -= amount
    target.lastCombatT = t
    if (by) by.lastCombatT = t
    const bearing = from ? Math.atan2(from[1] - target.pos[1], from[0] - target.pos[0]) : null
    target.events.push({ type: "damaged", amount, weapon, bearing, by: by ? by.id : null, ...(rear && { rear }) })
    if (by && by.id === 0) (stats.dmgDealt += amount), mechs[0].events.push({ type: "hit", target: target.id, amount, weapon })
    if (target.id === 0) {
      if (weapon === "zone") stats.zoneDmg += amount
      else stats.dmgTaken += amount
    }
    if (target.hp <= 0) {
      target.alive = false
      target.deathTick = tick
      if (by) {
        by.events.push({ type: "kill", target: target.id })
        if (by.id === 0) stats.kills++
      }
    }
  }
  const visible = (a: Mech, b: Mech) => {
    const d = Math.hypot(b.pos[0] - a.pos[0], b.pos[1] - a.pos[1])
    const eye: V3 = [a.pos[0], a.pos[1], a.pos[2] + 8]
    if (d > C.vision.range) return false
    if (d > C.vision.near_range) {
      const yaw = Math.atan2(b.pos[1] - a.pos[1], b.pos[0] - a.pos[0])
      if (Math.abs(angDiff(yaw, a.aim[0])) > ((C.vision.fov_deg / 2) * Math.PI) / 180) return false
    }
    return MAP.los(eye[0], eye[1], eye[2], b.pos[0], b.pos[1], b.pos[2] + 4.5) || MAP.los(eye[0], eye[1], eye[2], b.pos[0], b.pos[1], b.pos[2] + 9)
  }
  // ---- main loop ----
  while (tick < C.match.duration_s * C.match.tick_hz) {
    const t = tick * DT
    const live = alive()
    if (live.length <= 1 || !mechs[0].alive) break
    const Z = zoneAt(phases, t)
    // hunters: one early, more as their squad thins, all late
    const houseAlive = live.filter((m) => m.id !== 0)
    const nHunt = t > 300 ? 99 : 1 + (15 - houseAlive.length)
    houseAlive.sort((a, b) => a.skill - b.skill)
    houseAlive.forEach((m, k) => (m.hunter = k < nHunt))
    // decisions every 2 ticks
    if (tick % C.match.decide_every === 0) {
      for (const m of live) {
        // vision (squad shares vision)
        for (const o of live) if (o !== m && visible(m, o)) m.seen.set(o.id, t)
      }
      const squadSeesUs = houseAlive.some((h) => (h.seen.get(0) ?? -99) === t)
      for (const m of live) {
        if (m.brain) {
          const obs = buildObs(m, t, Z, live, pickups, shots, tick)
          obs.drops = drops.filter((d) => !d.taken).map((d) => ({ id: d.id, pos: d.pos, radius: C.drops.radius, lands_in: Math.max(0, d.landT - t), capture_s: C.drops.capture_s, progress: d.progress, holder: d.holder }))
          const t0 = performance.now()
          const a = m.brain.act(obs)
          const ms = performance.now() - t0
          times.push(ms)
          if (ms > 50) late++
          draw.clear()
          m.move = (a.move as V2) ?? [0, 0]
          m.boost = !!a.boost
          m.jump = !!a.jump
          if (a.aim) m.cmdAim = a.aim as V2
          m.fire = (a.fire as [boolean, boolean]) ?? [false, false]
          if (a.take !== undefined) {
            const p = pickups.find((p) => p.id === a.take)
            if (p && Math.hypot(p.pos[0] - m.pos[0], p.pos[1] - m.pos[1]) <= C.loot.pickup_radius) {
              let slot = m.weapons.findIndex((w) => !w)
              if (slot < 0 && a.drop !== undefined) slot = a.drop
              if (slot >= 0 && !m.weapons.some((w) => w?.kind === p.kind)) {
                const old = m.weapons[slot]
                const w = mkWeapon(p.kind)
                w.cooldown = Math.max(C.loot.equip_s, w.stats.cooldown ?? 0)
                m.weapons[slot] = w
                pickups.splice(pickups.indexOf(p), 1)
                m.events.push({ type: "took", id: p.id, kind: p.kind, slot })
                if (old) {
                  const id = 9000 + Math.floor(R() * 900)
                  pickups.push({ id, kind: old.kind, pos: [...m.pos] as V3 })
                  m.events.push({ type: "dropped", id, kind: old.kind, slot })
                }
              }
            }
          }
        } else houseAct(m, t, Z, mechs[0], squadSeesUs || (m.seen.get(0) ?? -99) > t - 5, R)
      }
    }
    // physics + weapons per tick
    for (const m of live) {
      // aim slew
      const dy = angDiff(m.cmdAim[0], m.aim[0])
      const my = C.mech.turn_rate * DT
      m.aim[0] = angDiff(m.aim[0] + Math.max(-my, Math.min(my, dy)), 0)
      const dp = Math.max(C.mech.min_pitch, Math.min(C.mech.max_pitch, m.cmdAim[1])) - m.aim[1]
      m.aim[1] += Math.max(-C.mech.pitch_rate * DT, Math.min(C.mech.pitch_rate * DT, dp))
      // movement
      const charging = m.weapons.some((w) => w?.kind === "laser" && (w.charge ?? 0) > 0)
      let ml = Math.hypot(m.move[0], m.move[1])
      const mv: V2 = ml > 1 ? [m.move[0] / ml, m.move[1] / ml] : [m.move[0], m.move[1]]
      ml = Math.min(1, ml)
      let speed = C.mech.walk_speed
      let spend = false
      if (m.boost && m.grounded && ml > 0.1 && m.energy > 0) (speed = C.mech.boost_speed), (m.energy -= C.mech.boost_drain * DT), (spend = true)
      if (charging) speed *= C.mech.charge_move_mult
      const acc = (m.grounded ? C.mech.ground_accel : C.mech.air_accel) * DT
      let dvx = mv[0] * speed - m.vel[0], dvy = mv[1] * speed - m.vel[1]
      const dl = Math.hypot(dvx, dvy)
      if (dl > acc) (dvx *= acc / dl), (dvy *= acc / dl)
      m.vel[0] += dvx
      m.vel[1] += dvy
      if (m.jump && m.grounded) (m.vel[2] = C.mech.jump_speed), (m.grounded = false)
      if (m.boost && !m.grounded && m.energy > 0) {
        m.vel[2] = Math.min(C.mech.max_rise_speed, m.vel[2] + C.mech.jet_accel * DT)
        m.energy -= C.mech.jet_drain * DT
        spend = true
      }
      if (spend) m.spendT = t
      else if (t - m.spendT > C.mech.energy_regen_delay) m.energy = Math.min(C.mech.energy, m.energy + C.mech.energy_regen * DT)
      m.energy = Math.max(0, m.energy)
      m.vel[2] -= C.mech.gravity * DT
      const blocked = (x: number, y: number, z: number) => {
        for (let k = 0; k < 8; k++) {
          const a = (k / 8) * Math.PI * 2
          const sx = x + Math.cos(a) * C.mech.radius * 0.9, sy = y + Math.sin(a) * C.mech.radius * 0.9
          if (MAP.surfAt(sx, sy) > z + C.mech.step_up) return true
          const c = MAP.cellOf(sx, sy)
          if (c < 0) return true
        }
        return false
      }
      let nx = m.pos[0] + m.vel[0] * DT, ny = m.pos[1] + m.vel[1] * DT
      if (blocked(nx, ny, m.pos[2])) {
        if (!blocked(nx, m.pos[1], m.pos[2])) (ny = m.pos[1]), (m.vel[1] = 0)
        else if (!blocked(m.pos[0], ny, m.pos[2])) (nx = m.pos[0]), (m.vel[0] = 0)
        else (nx = m.pos[0]), (ny = m.pos[1]), (m.vel[0] = 0), (m.vel[1] = 0)
      }
      let nz = m.pos[2] + m.vel[2] * DT
      const floor = MAP.surfAt(nx, ny)
      if (nz <= floor) (nz = floor), (m.vel[2] = 0), (m.grounded = true)
      else m.grounded = nz - floor < 0.05
      m.pos = [nx, ny, nz]
      // weapons
      for (let s = 0; s < 2; s++) {
        const w = m.weapons[s]
        if (!w) continue
        w.cooldown = Math.max(0, w.cooldown - DT)
        const trig = m.fire[s]
        const muz: V3 = [m.pos[0], m.pos[1], m.pos[2] + 7]
        const dir: V3 = [Math.cos(m.aim[1]) * Math.cos(m.aim[0]), Math.cos(m.aim[1]) * Math.sin(m.aim[0]), Math.sin(m.aim[1])]
        if (w.kind === "gun") {
          if ((w.reload ?? 0) > 0) {
            w.reload! -= DT
            if (w.reload! <= 0) (w.reload = 0), (w.ammo = w.stats.mag)
          } else if (trig && w.cooldown <= 0 && (w.ammo ?? 0) > 0) {
            const sp = (w.stats.spread_deg * Math.PI) / 180
            const ya = m.aim[0] + gauss(R) * sp, pa = m.aim[1] + gauss(R) * sp
            const d: V3 = [Math.cos(pa) * Math.cos(ya), Math.cos(pa) * Math.sin(ya), Math.sin(pa)]
            shots.push({ by: m.id, pos: [muz[0] + d[0] * 3, muz[1] + d[1] * 3, muz[2] + d[2] * 3], vel: [d[0] * w.stats.speed, d[1] * w.stats.speed, d[2] * w.stats.speed], ttl: w.stats.range / w.stats.speed, dmg: w.stats.damage })
            w.cooldown = w.stats.cooldown
            w.ammo!--
            if (m.id === 0) stats.shotsUs++
            soundAll(m, "gun", t)
            if (w.ammo === 0) w.reload = w.stats.reload
          }
        } else if (w.kind === "laser") {
          if (trig && w.cooldown <= 0) {
            w.charge = (w.charge ?? 0) + DT
            if (w.charge >= w.stats.charge + w.stats.hold_s) fireLaser(m, w, muz, dir, t)
          } else if ((w.charge ?? 0) > 0 && w.charge! >= w.stats.min_charge) fireLaser(m, w, muz, dir, t)
          else if ((w.charge ?? 0) > 0 && !trig) w.charge = (w.charge ?? 0) + DT
        } else if (w.kind === "lightning") {
          if (trig && w.cooldown <= 0 && m.energy >= w.stats.energy) {
            m.energy -= w.stats.energy
            m.spendT = t
            w.cooldown = w.stats.cooldown
            let best: Mech | null = null, bd = Infinity
            for (const o of live) {
              if (o === m || !o.alive) continue
              const d = Math.hypot(o.pos[0] - m.pos[0], o.pos[1] - m.pos[1])
              const yaw = Math.atan2(o.pos[1] - m.pos[1], o.pos[0] - m.pos[0])
              if (d < w.stats.range && Math.abs(angDiff(yaw, m.aim[0])) < ((w.stats.cone_deg / 2) * Math.PI) / 180 && d < bd && MAP.los(muz[0], muz[1], muz[2], o.pos[0], o.pos[1], o.pos[2] + 4.5)) (bd = d), (best = o)
            }
            if (best) damage(best, w.stats.damage, m, "lightning", m.pos, t)
            soundAll(m, "lightning", t)
          }
        }
      }
    }
    // projectiles
    const next: Shot[] = []
    for (const s of shots) {
      const np: V3 = [s.pos[0] + s.vel[0] * DT, s.pos[1] + s.vel[1] * DT, s.pos[2] + s.vel[2] * DT]
      let hit = false
      for (const o of mechs) {
        if (!o.alive || o.id === s.by) continue
        if (segCyl(s.pos, np, o.pos, C.mech.radius, C.mech.height)) {
          damage(o, s.dmg, mechs[s.by], "gun", s.pos, t)
          if (s.by === 0) stats.hitsUs++
          hit = true
          break
        }
      }
      if (hit || !MAP.los(s.pos[0], s.pos[1], s.pos[2], np[0], np[1], np[2])) continue
      s.pos = np
      s.ttl -= DT
      if (s.ttl > 0) next.push(s)
    }
    shots = next
    // supply drops
    for (const dt of C.drops?.times ?? []) {
      if (Math.abs(t - dt) < DT / 2) {
        for (let tries = 0; tries < 50; tries++) {
          const a = R() * Math.PI * 2, d = Math.sqrt(R()) * Z.next.r * (C.drops.spread ?? 0.7)
          const x = Z.next.c[0] + Math.cos(a) * d, y = Z.next.c[1] + Math.sin(a) * d
          const n = MAP.nodeOf(x, y)
          if (n >= 0 && MAP.nClear[n] > 6) {
            drops.push({ id: 20000 + drops.length, pos: [x, y, MAP.surfAt(x, y)], callT: t, landT: t + C.drops.fall_s, holder: null, progress: 0, taken: false })
            break
          }
        }
      }
    }
    for (const d of drops) {
      if (d.taken || t < d.landT) continue
      const inRing = alive().filter((m) => Math.hypot(m.pos[0] - d.pos[0], m.pos[1] - d.pos[1]) <= C.drops.radius)
      if (inRing.length === 1) {
        if (d.holder !== inRing[0].id) (d.holder = inRing[0].id), (d.progress = 0)
        d.progress += DT
        if (d.progress >= C.drops.capture_s) {
          const m = inRing[0]
          d.taken = true
          m.hp = m.maxHp
          m.energy = C.mech.energy
          m.events.push({ type: "drop", id: d.id, cubes: m.cubes })
          if (m.id === 0) dropsTaken++
        }
      } else if (inRing.length === 0) (d.holder = null), (d.progress = 0)
    }
    // zone + regen
    for (const m of alive()) {
      const out = Math.hypot(m.pos[0] - Z.c[0], m.pos[1] - Z.c[1]) > Z.r
      if (out) damage(m, Z.dps * DT, null, "zone", null, t)
      else if (t - m.lastCombatT > C.mech.regen_delay) m.hp = Math.min(m.maxHp, m.hp + C.mech.regen_per_s * DT)
    }
    // stuck stats for our mech
    const me = mechs[0]
    if (Math.hypot(me.pos[0] - lastPos[0], me.pos[1] - lastPos[1]) > 1) (lastPos = [...me.pos] as V3), (lastMoveT = t)
    else if (Math.hypot(me.move[0], me.move[1]) > 0.5 && t - lastMoveT > 2) stats.stuckTicks++
    tick++
  }
  // placement
  const me = mechs[0]
  const deadBefore = mechs.filter((m) => m.id !== 0 && !m.alive && m.deathTick < me.deathTick).length
  const place = me.alive ? 1 + mechs.filter((m) => m.id !== 0 && m.alive && m.hp > me.hp).length : 16 - deadBefore
  times.sort((a, b) => a - b)
  return {
    seed, place, score: 16 - place, t: (tick * DT).toFixed(0), alive: me.alive, ...stats,
    p99: times[Math.floor(times.length * 0.99)]?.toFixed(1), max: times[times.length - 1]?.toFixed(1), late, wall: ((performance.now() - t0wall) / 1000).toFixed(1),
    weapons: me.weapons.map((w) => w?.kind ?? "-").join("+"), slow: me.brain!.slowTicks, drops: dropsTaken,
  }

  function fireLaser(m: Mech, w: W, muz: V3, dir: V3, t: number) {
    const frac = Math.min(1, w.charge! / w.stats.charge)
    const dmg = w.stats.damage * (w.stats.min_frac + (1 - w.stats.min_frac) * frac)
    w.charge = 0
    w.cooldown = w.stats.cooldown
    soundAll(m, "laser", t)
    let best: Mech | null = null, bt = Infinity
    for (const o of alive()) {
      if (o === m) continue
      const tt = rayCyl(muz, dir, o.pos, C.mech.radius, C.mech.height)
      if (tt !== null && tt < bt && tt < w.stats.range) (bt = tt), (best = o)
    }
    if (best && MAP.los(muz[0], muz[1], muz[2], muz[0] + dir[0] * bt, muz[1] + dir[1] * bt, muz[2] + dir[2] * bt)) damage(best, dmg, m, "laser", m.pos, t)
  }
  function soundAll(src: Mech, kind: string, t: number) {
    const range = C.sound.range[kind] ?? 100
    for (const o of mechs) {
      if (o === src || !o.alive) continue
      const d = Math.hypot(o.pos[0] - src.pos[0], o.pos[1] - src.pos[1])
      if (d > range) continue
      o.sounds.push({ kind, bearing: Math.atan2(src.pos[1] - o.pos[1], src.pos[0] - o.pos[0]) + gauss(R) * 0.12, dist: d < 60 ? "near" : d < 160 ? "mid" : "far", age: 0 })
    }
  }
}

function houseAct(m: Mech, t: number, Z: ReturnType<typeof zoneAt>, us: Mech, know: boolean, R: () => number) {
  const goal: V2 = m.hunter && us.alive && know ? [us.pos[0], us.pos[1]] : [Z.next.c[0], Z.next.c[1]]
  const d = Math.hypot(us.pos[0] - m.pos[0], us.pos[1] - m.pos[1])
  const sees = (m.seen.get(0) ?? -99) === t
  m.fire = [false, false]
  m.boost = false
  m.jump = false
  if (m.hunter && sees && d < 160) {
    // fight: strafe and shoot with skill-scaled lead
    const s = Math.sin(t * (0.8 + m.skill)) > 0 ? 1 : -1
    const b = Math.atan2(us.pos[1] - m.pos[1], us.pos[0] - m.pos[0])
    const radial = d > 90 ? 0.6 : d < 30 ? -0.6 : 0
    m.move = [Math.cos(b) * radial - Math.sin(b) * s * 0.8, Math.sin(b) * radial + Math.cos(b) * s * 0.8]
    const tl = d / 252
    const px = us.pos[0] + us.vel[0] * tl * m.skill, py = us.pos[1] + us.vel[1] * tl * m.skill
    const err = (1 - m.skill) * 0.05
    m.cmdAim = [Math.atan2(py - m.pos[1], px - m.pos[0]) + gauss(R) * err, Math.atan2(us.pos[2] + 4.5 - m.pos[2] - 7, d)]
    const w1 = m.weapons[1]
    m.fire = [true, false]
    if (w1?.kind === "laser") m.fire[1] = (w1.charge ?? 0) < w1.stats.charge
    if (w1?.kind === "lightning") m.fire[1] = d < 40
  } else {
    const r = m.nav!.steer(m.pos[0], m.pos[1], goal[0], goal[1], t, 3000)
    m.move = r.arrived ? [0, 0] : r.dir
    m.jump = r.jump
    m.cmdAim = [Math.atan2(m.move[1], m.move[0]) || m.aim[0], 0]
  }
}

function buildObs(m: Mech, t: number, Z: ReturnType<typeof zoneAt>, live: Mech[], pickups: { id: number; kind: string; pos: V3 }[], shots: Shot[], tick: number): Observation {
  const mem = C.vision.memory_s
  const mechsObs = live
    .filter((o) => o !== m && t - (m.seen.get(o.id) ?? -99) <= mem)
    .map((o) => ({ id: o.id, kind: "mech" as const, pos: [...o.pos] as V3, vel: [...o.vel] as V3, aim: [...o.aim] as V2, hp: o.hp, max_hp: o.maxHp, cubes: o.cubes, weapons: o.weapons.filter(Boolean).map((w) => w!.kind) as any, charging: o.weapons.some((w) => w?.kind === "laser" && (w.charge ?? 0) > 0) }))
  const vis = (p: V3) => {
    const d = Math.hypot(p[0] - m.pos[0], p[1] - m.pos[1])
    if (d > C.vision.range) return false
    if (d > C.vision.near_range && Math.abs(angDiff(Math.atan2(p[1] - m.pos[1], p[0] - m.pos[0]), m.aim[0])) > ((C.vision.fov_deg / 2) * Math.PI) / 180) return false
    return MAP.los(m.pos[0], m.pos[1], m.pos[2] + 8, p[0], p[1], p[2] + 0.8)
  }
  const z = Z as any
  const obs: any = {
    type: "obs", tick, t, alive: live.length,
    self: {
      id: m.id, pos: [...m.pos], vel: [...m.vel], aim: [...m.aim], hp: m.hp, max_hp: m.maxHp, energy: m.energy, max_energy: C.mech.energy, grounded: m.grounded, cubes: m.cubes, upgrades: [],
      weapons: m.weapons.map((w) => (w ? { ...w, stats: { ...w.stats } } : null)), in_zone: Math.hypot(m.pos[0] - Z.c[0], m.pos[1] - Z.c[1]) <= Z.r, stats: { ...C.mech },
    },
    mechs: mechsObs,
    projectiles: shots.filter((s) => s.by !== m.id && vis(s.pos)).map((s, k) => ({ id: k, kind: "gun", pos: s.pos, vel: s.vel })),
    crates: [], cubes: [],
    pickups: pickups.filter((p) => vis(p.pos)),
    sounds: m.sounds.splice(0), beams: [], events: m.events.splice(0),
    zone: { center: z.prev ? z.prev.c : Z.c, radius: z.prev ? z.prev.r : Z.r, next_center: Z.next.c, next_radius: Z.next.r, shrink_start: Z.next.t0, shrink_end: Z.next.t1, dps: Z.dps },
    offer: null, drops: [],
  }
  return obs
}

function gauss(R: () => number) {
  return Math.sqrt(-2 * Math.log(R() + 1e-12)) * Math.cos(2 * Math.PI * R())
}
function segCyl(a: V3, b: V3, p: V3, r: number, h: number) {
  // sample the segment (bullets move ~12.6 m per tick)
  for (let k = 0; k <= 8; k++) {
    const f = k / 8
    const x = a[0] + (b[0] - a[0]) * f, y = a[1] + (b[1] - a[1]) * f, z = a[2] + (b[2] - a[2]) * f
    if (Math.hypot(x - p[0], y - p[1]) <= r + 0.3 && z >= p[2] && z <= p[2] + h) return true
  }
  return false
}
function rayCyl(o: V3, d: V3, p: V3, r: number, h: number): number | null {
  const ox = o[0] - p[0], oy = o[1] - p[1]
  const a = d[0] * d[0] + d[1] * d[1]
  if (a < 1e-9) return null
  const b = 2 * (ox * d[0] + oy * d[1]), c = ox * ox + oy * oy - r * r
  const disc = b * b - 4 * a * c
  if (disc < 0) return null
  const t = (-b - Math.sqrt(disc)) / (2 * a)
  if (t < 0) return null
  const z = o[2] + d[2] * t
  return z >= p[2] && z <= p[2] + h ? t : null
}

const results = []
for (let k = 0; k < N_MATCH; k++) {
  const r = runMatch(SEED + k)
  console.log(JSON.stringify(r))
  results.push(r)
}
console.log("mean score", (results.reduce((s, r) => s + r.score, 0) / results.length).toFixed(2))
