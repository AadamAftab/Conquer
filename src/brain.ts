import { draw, log, type BotAction, type DropObs, type Hello, type Observation, type SelfObs, type WeaponObs } from "../mechfight.ts"
import { GameMap } from "./map.ts"
import { Navigator } from "./nav.ts"
import { World, type Track } from "./world.ts"
import { aimAt, centerOffset, clampPitch, leadAim, missDist, predict, shellLanding, type Aim } from "./combat.ts"
import { angDiff, rng, type V2, type V3 } from "./geom.ts"

const UPG_FFA = ["plating", "armor", "nanites", "laser_damage", "laser_charge", "lightning_efficiency", "gun_damage", "sensors", "gyros", "gun_rate", "servos", "jets", "lightning_range", "reactor", "capacitor", "gun_mag", "gun_velocity", "lightning_chain", "laser_pierce"]
const UPG_PRACTICE = ["laser_damage", "plating", "nanites", "laser_charge", "armor", "lightning_efficiency", "gun_damage", "gun_rate", "gyros", "sensors", "lightning_range", "servos", "gun_mag", "gun_velocity", "jets", "reactor", "capacitor", "lightning_chain", "laser_pierce"]
const LOUD = new Set(["gun", "laser", "laser_charge", "lightning", "mortar", "explosion", "step", "boost", "jet", "reload"])

type Goal = { pos: V2; kind: "pickup" | "cube" | "farm"; id: number }

export class Brain {
  name = "not-even-close"
  hello: Hello
  m: GameMap
  w: World
  nav: Navigator
  rnd: () => number
  perCube: number
  g: number
  jinkSign = 1
  jinkT = 0
  retreating = false
  cover: V2 | null = null
  coverT = -99
  holdGoal: V2 | null = null
  holdKey = ""
  scanYaw = 0
  targetId = -1
  state = "init"
  slowTicks = 0

  constructor(hello: Hello) {
    const t0 = performance.now()
    this.hello = hello
    const cfg = hello.config
    this.m = new GameMap(hello.map.dir, cfg.mech.step_up ?? 1.2)
    this.w = new World(this.m, hello)
    this.nav = new Navigator(this.m)
    this.rnd = rng((hello.seed ?? 1) ^ 0x5bd1e995)
    this.perCube = Number(cfg.cubes.size_per_cube ?? 0.05)
    this.g = cfg.mech.gravity ?? 22
    this.warmup()
    log(`brain ready in ${(performance.now() - t0).toFixed(0)} ms`)
  }

  /** Exercise the hot paths once so the JIT has compiled them before the first fight. */
  private warmup() {
    const m = this.m
    const rq = (this.hello.config.mech.radius ?? 2.5) + 0.9
    const comp = m.comp(rq)
    const r = rng(7)
    let n = 0
    for (let k = 0; k < 3000; k++) {
      const a = r() * 900 - 450, b = r() * 600 - 300
      if (m.los(a, b, 5, a + r() * 200 - 100, b + r() * 200 - 100, 4)) n++
    }
    for (let k = 0; k < 12; k++) {
      const s = m.snap(r() * 600 - 300, r() * 400 - 200, rq, -1, 20)
      if (s < 0) continue
      const t = m.snap(m.nodeX(s) + r() * 300 - 150, m.nodeY(s) + r() * 300 - 150, rq, comp[s], 30)
      if (t < 0) continue
      this.nav.pf.begin(s, t, rq)
      this.nav.pf.run(60000)
      n += this.nav.pf.result.length
    }
    this.nav.pf.state = "idle"
    for (let k = 0; k < 200; k++) n += m.walkLine(0, 0, r() * 50, r() * 50, rq) ? 1 : 0
    if (n < 0) log(n)
  }

  pickUpgrade(obs: Observation): number {
    const list = this.w.mode === "practice" ? UPG_PRACTICE : UPG_FFA
    let best = 0, bestR = Infinity
    obs.offer!.choices.forEach((c, i) => {
      let r = list.indexOf(c.id)
      if (r < 0) r = 50
      if (r < bestR) (bestR = r), (best = i)
    })
    return best
  }

  act(obs: Observation): BotAction {
    const T0 = performance.now()
    const w = this.w, m = this.m
    w.update(obs)
    const me = obs.self, t = obs.t
    const [x, y, z] = me.pos
    const st = me.stats
    const rq = (st.radius ?? 2.5) + 0.9
    this.nav.rq = rq
    const act: BotAction = {}
    if (obs.offer) act.pick = this.pickUpgrade(obs)

    const muz: V3 = [x, y, z + (st.muzzle_height ?? 7)]
    const eyeZ = z + (st.eye_height ?? 8)
    const hpFrac = me.hp / me.max_hp
    const slots = me.weapons
    const iGun = slots.findIndex((s) => s?.kind === "gun")
    const iLaser = slots.findIndex((s) => s?.kind === "laser")
    const iLight = slots.findIndex((s) => s?.kind === "lightning")
    const enemies = w.liveMechs()
    const turn = st.turn_rate ?? 4

    // ---------- targets ----------
    const losTo = (tr: Track) => {
      const off = centerOffset(tr, this.perCube)
      return m.los(muz[0], muz[1], muz[2], tr.pos[0], tr.pos[1], tr.pos[2] + off)
    }
    const dTo = (p: ArrayLike<number>) => Math.hypot(p[0] - x, p[1] - y)
    let target: Track | null = null
    let best = -Infinity
    let nearestEnemy: Track | null = null
    let nearestD = Infinity
    for (const e of enemies) {
      const d = dTo(e.pos)
      if (d < nearestD) (nearestD = d), (nearestEnemy = e)
      if (d > 285 || !losTo(e)) continue
      let sc = -d * 0.02 + (1 - e.hp / Math.max(1, e.maxHp)) * 2
      if (t - e.hitUsT < 4) sc += 3
      if (e.id === this.targetId) sc += 1.5
      if (sc > best) (best = sc), (target = e)
    }
    const threatened = enemies.some((e) => t - e.hitUsT < 5) || w.recentDamage(3) > 0
    const backTurned = (e: Track) => Math.abs(angDiff(e.aim[0], Math.atan2(y - e.pos[1], x - e.pos[0]))) > 1.9
    let engage = false
    if (target) {
      const d = dTo(target.pos)
      engage = w.mode === "practice" || threatened || d < 70 || target.hp / target.maxHp < 0.5 || backTurned(target) || t - target.hitByUsT < 6
      this.targetId = engage ? target.id : -1
    }
    // farm NPCs / crates when nobody is around
    let farm: Track | null = null
    const quiet = nearestD > 150 && !threatened
    if (!engage && quiet && hpFrac > 0.45 && t < 400) {
      let fd = Infinity
      for (const n of w.liveNpcs()) {
        const d = dTo(n.pos)
        if (d < 70 && d < fd && losTo(n)) (fd = d), (farm = n)
      }
      for (const c of w.crates.values()) {
        const d = dTo(c.pos)
        if (d < 55 && d < fd && Math.abs(c.pos[2] - m.groundAt(c.pos[0], c.pos[1])) < 3) {
          const fake: Track = { id: c.id, kind: "npc", pos: c.pos, vel: [0, 0, 0], aim: [0, 0], hp: c.hp, maxHp: 300, cubes: 0, weapons: [], charging: false, seenT: t, firstT: t, hitUsT: -99, hitByUsT: -99 }
          if (losTo(fake)) (fd = d), (farm = fake)
        }
      }
    }
    const shoot = engage ? target : farm

    // ---------- retreat state ----------
    if (!this.retreating && hpFrac < 0.4 && (threatened || nearestD < 80)) this.retreating = true
    if (this.retreating && (hpFrac > 0.8 || (t - w.lastHurtT > 14 && hpFrac > 0.6))) this.retreating = false

    // ---------- fire control ----------
    const fire: [boolean, boolean] = [false, false]
    let aim: Aim | null = null
    if (shoot) {
      const off = centerOffset(shoot, this.perCube)
      const pNow = predict(m, shoot, 0.075, this.g)
      const direct = aimAt(muz, [pNow[0], pNow[1], pNow[2] + off])
      const d = direct.dist
      const radius = shoot.kind === "npc" ? 1.5 : 2.5 * (1 + this.perCube * shoot.cubes)
      aim = direct
      let laserBusy = false
      const isCrate = shoot.id >= 1000 && shoot.id < 5000
      if (iLaser >= 0 && !isCrate) {
        const L = slots[iLaser]!
        const charge = L.charge ?? 0
        const full = L.stats.charge ?? 0.75
        const range = L.stats.range ?? 280
        if (charge > 0) {
          laserBusy = true
          const left = full + (L.stats.hold_s ?? 2) - charge
          const ok = d <= range && missDist(me, direct, 0.05) < radius * 0.7
          fire[iLaser] = !((charge >= full - 0.02 && ok) || left < 0.2)
        } else if (L.cooldown <= 0.05 && d <= range * 0.97 && !this.retreating && !this.shortRisk(enemies, x, y)) {
          fire[iLaser] = true
          laserBusy = true
        }
      }
      let lightningOn = false
      if (iLight >= 0) {
        const L = slots[iLight]!
        const range = L.stats.range ?? 45
        const half = (((L.stats.cone_deg ?? 30) * Math.PI) / 180) * 0.5
        const reserve = shoot.kind === "npc" || shoot.hp / shoot.maxHp < 0.25 ? 6 : 22
        if (d < range * 0.9 && me.energy > reserve) {
          lightningOn = true
          const ey = Math.abs(angDiff(direct.yaw, me.aim[0])) - turn * 0.05
          if (L.cooldown <= 0 && ey < half * 0.6) fire[iLight] = true
        }
      }
      if (iGun >= 0) {
        const G = slots[iGun]!
        const lead = leadAim(m, muz, shoot, G.stats.speed ?? 252, 0.075, off, this.g)
        if (!laserBusy && !lightningOn) aim = lead
        const ammo = G.ammo ?? 1
        const okRange = lead.dist <= (G.stats.range ?? 230) * 0.98
        const quietOk = shoot.kind === "npc" || engage
        const laserOk = !laserBusy || d < 40
        if (G.cooldown <= 0 && ammo > 0 && !(G.reload ?? 0) && okRange && quietOk && laserOk && missDist(me, lead, 0.05) < radius * 0.75) fire[iGun] = true
        if (ammo === 0 && !(G.reload ?? 0)) act.reload = true
      }
    } else if (iGun >= 0) {
      const G = slots[iGun]!
      if ((G.ammo ?? 0) < (G.stats.mag ?? 12) * 0.6 && !(G.reload ?? 0) && nearestD > 60) act.reload = true
    }
    // a charging laser with nothing to shoot: keep holding until close to auto-fire, then dump it
    if (iLaser >= 0 && !shoot) {
      const L = slots[iLaser]!
      const charge = L.charge ?? 0
      if (charge > 0) fire[iLaser] = charge < (L.stats.charge ?? 0.75) + (L.stats.hold_s ?? 2) - 0.25
    }

    // ---------- movement ----------
    let mv: V2 = [0, 0]
    let boost = false
    let jump = false
    let speedCap = 1
    const zoneNow = w.zoneMargin(x, y, t)
    const zoneSoon = w.zoneMargin(x, y, t + 10)
    const zGoal = this.zoneGoal(obs, x, y, rq)
    const dodge = this.mortarDodge(obs, x, y)
    let drop: DropObs | null = null
    const go = (gx: number, gy: number, budget = 5000) => {
      const r = this.nav.steer(x, y, gx, gy, t, budget)
      if (r.jump) jump = true
      if (r.boost) boost = true
      return r
    }
    if (dodge) {
      this.state = "dodge"
      mv = dodge
      boost = me.energy > 15
    } else if (zoneSoon < 4 || zoneNow < 8) {
      this.state = "zone"
      const r = go(zGoal[0], zGoal[1])
      mv = r.dir
      if (zoneNow < 2 || zoneSoon < -15) boost = boost || me.energy > 10
    } else if ((drop = this.dropGoal(obs, x, y, t, enemies, hpFrac))) {
      this.state = "drop"
      const dd = Math.hypot(drop.pos[0] - x, drop.pos[1] - y)
      if (dd > Math.max(1.5, drop.radius - 4)) {
        const r = go(drop.pos[0], drop.pos[1])
        mv = r.dir
        if (dd > 60 && me.energy > 50 && drop.lands_in < dd / 11) boost = true
      }
    } else if (this.retreating) {
      this.state = "retreat"
      const threat = this.threatPos(x, y, z)
      if (!this.cover || t - this.coverT > 0.8) {
        this.cover = threat ? this.findCover(x, y, threat, rq, t) : null
        this.coverT = t
      }
      if (this.cover) {
        const r = go(this.cover[0], this.cover[1])
        mv = r.dir
        if (!r.arrived && (threatened || nearestD < 60)) boost = me.energy > 20
      } else mv = this.jink(x, y, target ?? nearestEnemy, rq, t, 1)
    } else if (shoot && engage) {
      this.state = "fight"
      mv = this.fightMove(x, y, shoot, iLight >= 0 && me.energy > 30, iLaser >= 0, rq, t, hpFrac)
    } else {
      const goal = this.lootGoal(obs, x, y, slots, nearestD, t)
      if (goal) {
        this.state = goal.kind
        if (goal.kind === "farm" && farm && dTo(farm.pos) < 45) mv = this.jink(x, y, farm, rq, t, 0.6)
        else {
          const r = go(goal.pos[0], goal.pos[1])
          mv = r.dir
        }
      } else {
        this.state = "hold"
        const r = go(zGoal[0], zGoal[1])
        mv = r.arrived ? [0, 0] : r.dir
        const far = Math.hypot(zGoal[0] - x, zGoal[1] - y)
        if (far > 120 && me.energy > 70 && nearestD > 150) boost = true
        if (enemies.length || w.heard.some((h) => h.dist === "near")) speedCap = 0.4
      }
    }
    const ml = Math.hypot(mv[0], mv[1])
    if (ml > speedCap) mv = [(mv[0] / ml) * speedCap, (mv[1] / ml) * speedCap]

    // ---------- aim / look ----------
    let yaw: number, pitch = 0
    if (aim) {
      yaw = aim.yaw
      pitch = aim.pitch
    } else {
      const look = this.lookDir(x, y, z, eyeZ, enemies, t, mv)
      yaw = look[0]
      pitch = look[1]
    }
    pitch = clampPitch(me, pitch)

    // ---------- loot pickup ----------
    const safe = nearestD > 80 && !threatened
    const reach = this.hello.config.loot.pickup_radius ?? 4
    for (const p of w.pickups.values()) {
      if (Math.hypot(p.pos[0] - x, p.pos[1] - y) > reach - 0.3 || Math.abs(p.pos[2] - z) > 3.8) continue
      const want = this.wants(p.kind, slots, safe)
      if (want.ok) {
        act.take = p.id
        if (want.drop !== undefined) act.drop = want.drop
        break
      }
    }

    act.move = [round2(mv[0]), round2(mv[1])]
    act.boost = boost
    act.jump = jump
    act.aim = [round3(yaw), round3(pitch)]
    act.fire = fire

    // ---------- debug ----------
    const ms = performance.now() - T0
    if (ms > 25) this.slowTicks++
    draw.label(`${this.state} ${w.mode[0]} hp${Math.round(hpFrac * 100)} e${Math.round(me.energy)} ${shoot ? "→" + shoot.id : ""} ${ms.toFixed(0)}ms`)
    if (shoot) draw.line(muz, [shoot.pos[0], shoot.pos[1], shoot.pos[2] + 4], "#ff4040")
    if (this.nav.path.length && obs.tick % 4 === 0) {
      const p = this.nav.path
      for (let k = Math.max(1, this.nav.idx); k < Math.min(p.length, this.nav.idx + 12); k++)
        draw.line([m.nodeX(p[k - 1]), m.nodeY(p[k - 1]), z + 1], [m.nodeX(p[k]), m.nodeY(p[k]), z + 1], "#40c0ff")
    }
    return act
  }

  /** A supply drop worth going for: reachable before others settle on it, not contested, inside the zone. */
  private dropGoal(obs: Observation, x: number, y: number, t: number, enemies: Track[], hpFrac: number): DropObs | null {
    let best: DropObs | null = null
    let bd = Infinity
    const meId = obs.self.id
    for (const d of obs.drops) {
      const dist = Math.hypot(d.pos[0] - x, d.pos[1] - y)
      const maxD = hpFrac < 0.6 ? 320 : 240
      if (dist > maxD) continue
      const travel = dist / 11
      if (d.lands_in > travel + 8) continue
      if (d.holder !== null && d.holder !== meId) continue
      if (enemies.some((e) => Math.hypot(e.pos[0] - d.pos[0], e.pos[1] - d.pos[1]) < 45 && this.w.t - e.seenT < 1)) continue
      if (this.w.zoneMargin(d.pos[0], d.pos[1], t + travel + 6) < 3) continue
      if (dist < bd) (bd = dist), (best = d)
    }
    return best
  }

  /** Is an enemy with lightning close enough to short our laser? */
  private shortRisk(enemies: Track[], x: number, y: number) {
    return enemies.some((e) => e.weapons.includes("lightning") && Math.hypot(e.pos[0] - x, e.pos[1] - y) < 55)
  }

  /** Should we take a weapon of this kind? (and which slot to drop) */
  wants(kind: string, slots: SelfObs["weapons"], safe: boolean): { ok: boolean; drop?: number } {
    if (kind !== "laser" && kind !== "lightning") return { ok: false }
    if (slots.some((s) => s?.kind === kind)) return { ok: false }
    const empty = slots.findIndex((s) => !s)
    if (empty >= 0) return { ok: true }
    const mortar = slots.findIndex((s) => s?.kind === "mortar")
    if (mortar >= 0 && !(slots[mortar] as WeaponObs).charge) return { ok: true, drop: mortar }
    const gun = slots.findIndex((s) => s?.kind === "gun")
    if (gun >= 0 && safe) return { ok: true, drop: gun }
    return { ok: false }
  }

  /** A point inside the next circle to hold; recomputed when the zone plan changes. */
  private zoneGoal(obs: Observation, x: number, y: number, rq: number): V2 {
    const z = obs.zone
    const key = `${z.next_center[0]},${z.next_center[1]},${z.next_radius}`
    if (this.holdKey === key && this.holdGoal) return this.holdGoal
    const [cx, cy] = z.next_center
    const R = z.next_radius
    const want = R < 45 ? R * 0.3 : R * 0.55
    const d = Math.hypot(x - cx, y - cy)
    let gx = x, gy = y
    if (d > want) {
      gx = cx + ((x - cx) / d) * want
      gy = cy + ((y - cy) / d) * want
    }
    const m = this.m
    const comp = m.comp(rq)
    const s = m.snap(x, y, rq, -1, 6)
    const myComp = s >= 0 ? comp[s] : -1
    const n = m.snap(gx, gy, rq, myComp, 60)
    if (n >= 0) (gx = m.nodeX(n)), (gy = m.nodeY(n))
    // prefer a spot hugging a wall nearby (fewer angles to be shot from)
    if (R >= 45) {
      let bs = Infinity
      let bx = gx, by = gy
      for (let k = 0; k < 24; k++) {
        const a = (k / 24) * Math.PI * 2
        for (const r of [0, 8, 16, 26, 38]) {
          if (r === 0 && k > 0) continue
          const px = gx + Math.cos(a) * r, py = gy + Math.sin(a) * r
          const q = m.nodeOf(px, py)
          if (!m.passable(q, rq) || (myComp >= 0 && comp[q] !== myComp)) continue
          if (Math.hypot(px - cx, py - cy) > R * 0.7) continue
          const cl = m.nClear[q]
          const sc = r * 0.15 + (cl > rq + 2.5 ? 6 : 0) + Math.abs(cl - (rq + 1)) * 0.8
          if (sc < bs) (bs = sc), (bx = m.nodeX(q)), (by = m.nodeY(q))
        }
      }
      gx = bx
      gy = by
    }
    this.holdGoal = [gx, gy]
    this.holdKey = key
    return this.holdGoal
  }

  /** Where to go for loot: weapons we want, cubes, farm targets. */
  private lootGoal(obs: Observation, x: number, y: number, slots: SelfObs["weapons"], nearestD: number, t: number): Goal | null {
    const w = this.w, m = this.m
    let best: Goal | null = null
    let bs = Infinity
    const consider = (pos: V2, kind: Goal["kind"], id: number, weight: number, maxD: number) => {
      const d = Math.hypot(pos[0] - x, pos[1] - y)
      if (d > maxD) return
      if (w.zoneMargin(pos[0], pos[1], t + d / 10 + 8) < 6) return
      const sc = d * weight
      if (sc < bs) (bs = sc), (best = { pos, kind, id })
    }
    for (const p of w.pickups.values()) {
      if (!this.wants(p.kind, slots, nearestD > 80).ok) continue
      const c = m.cellOf(p.pos[0], p.pos[1])
      if (c < 0 || m.wall[c] || Math.abs(p.pos[2] - m.groundAt(p.pos[0], p.pos[1])) > 2.5) continue
      consider([p.pos[0], p.pos[1]], "pickup", p.id, 0.6, nearestD > 100 ? 220 : 90)
    }
    for (const c of obs.cubes) {
      if (Math.abs(c.pos[2] - m.groundAt(c.pos[0], c.pos[1])) > 2.5) continue
      consider([c.pos[0], c.pos[1]], "cube", c.id, 0.5, nearestD > 60 ? 90 : 35)
    }
    if (!best && nearestD > 150 && t < 400) {
      for (const n of w.liveNpcs()) consider([n.pos[0], n.pos[1]], "farm", n.id, 1, 120)
      for (const c of w.crates.values())
        if (Math.abs(c.pos[2] - m.groundAt(c.pos[0], c.pos[1])) < 3) consider([c.pos[0], c.pos[1]], "farm", c.id, 1.2, 100)
    }
    return best
  }

  /** Strafe with random direction changes, keeping a preferred range to the target. */
  private fightMove(x: number, y: number, tr: Track, lightning: boolean, laser: boolean, rq: number, t: number, hpFrac: number): V2 {
    const d = Math.hypot(tr.pos[0] - x, tr.pos[1] - y)
    let lo = 30, hi = 70
    if (lightning && (hpFrac > 0.5 || tr.hp / tr.maxHp < 0.35)) (lo = 10), (hi = 30)
    else if (laser) (lo = 60), (hi = 170)
    if (tr.kind === "npc") (lo = 20), (hi = 45)
    if (d > hi + 20) {
      const r = this.nav.steer(x, y, tr.pos[0], tr.pos[1], t, 4000)
      return r.dir
    }
    const radial = d > hi ? 0.8 : d < lo ? -0.8 : 0
    return this.jink(x, y, tr, rq, t, 0.9, radial)
  }

  private jink(x: number, y: number, tr: Track | null, rq: number, t: number, amp: number, radial = 0): V2 {
    if (t > this.jinkT) {
      this.jinkSign = this.rnd() < 0.5 ? -1 : 1
      this.jinkT = t + 0.3 + this.rnd() * 0.5
    }
    const b = tr ? Math.atan2(tr.pos[1] - y, tr.pos[0] - x) : this.scanYaw
    const rx = Math.cos(b), ry = Math.sin(b)
    for (let attempt = 0; attempt < 2; attempt++) {
      const s = this.jinkSign
      let dx = rx * radial + -ry * s * amp, dy = ry * radial + rx * s * amp
      const l = Math.hypot(dx, dy) || 1
      dx /= l
      dy /= l
      if (this.m.walkLine(x, y, x + dx * 5, y + dy * 5, rq)) {
        // keep inside the zone
        if (this.w.zoneMargin(x + dx * 8, y + dy * 8, t + 3) > 5) return [dx, dy]
      }
      this.jinkSign = -this.jinkSign
    }
    if (radial !== 0 && this.m.walkLine(x, y, x + rx * radial * 5, y + ry * radial * 5, rq)) return [rx * Math.sign(radial), ry * Math.sign(radial)]
    return [0, 0]
  }

  /** Best guess of where the main threat is. */
  private threatPos(x: number, y: number, z: number): V3 | null {
    const w = this.w
    let best: Track | null = null
    for (const e of w.tracks.values()) {
      if (e.kind !== "mech" || w.t - e.seenT > 6) continue
      if (!best || e.hitUsT > best.hitUsT || (e.hitUsT === best.hitUsT && Math.hypot(e.pos[0] - x, e.pos[1] - y) < Math.hypot(best.pos[0] - x, best.pos[1] - y))) best = e
    }
    if (best) return best.pos
    const h = w.hurt.filter((h) => h.bearing !== null).pop()
    if (h && h.bearing !== null) return [x + Math.cos(h.bearing) * 70, y + Math.sin(h.bearing) * 70, z]
    return null
  }

  /** A nearby reachable point hidden from `threat`, inside the zone. */
  private findCover(x: number, y: number, threat: V3, rq: number, t: number): V2 | null {
    const m = this.m, w = this.w
    const comp = m.comp(rq)
    const s = m.snap(x, y, rq, -1, 4)
    const myComp = s >= 0 ? comp[s] : -1
    let best: V2 | null = null
    let bs = -Infinity
    const tz = threat[2] + 8
    for (let k = 0; k < 16; k++) {
      const a = (k / 16) * Math.PI * 2
      for (const r of [10, 20, 32, 45]) {
        const px = x + Math.cos(a) * r, py = y + Math.sin(a) * r
        const n = m.nodeOf(px, py)
        if (!m.passable(n, rq) || (myComp >= 0 && comp[n] !== myComp)) continue
        if (w.zoneMargin(px, py, t + 8) < 5) continue
        const hidden = !m.los(threat[0], threat[1], tz, px, py, m.surfAt(px, py) + 5)
        let sc = (hidden ? 60 : 0) + 0.25 * Math.hypot(px - threat[0], py - threat[1]) - 0.6 * r
        if (!m.walkLine(x, y, px, py, rq)) sc -= 12
        if (sc > bs) (bs = sc), (best = [px, py])
      }
    }
    return best
  }

  /** Move away from a mortar shell about to land near us. */
  private mortarDodge(obs: Observation, x: number, y: number): V2 | null {
    const mo = this.hello.config.weapons.mortar
    const g = mo.gravity ?? 22
    const r = (mo.splash_radius ?? 7) * 1.4 + 3
    for (const p of obs.projectiles) {
      if (p.kind !== "mortar") continue
      const L = shellLanding(this.m, p.pos, p.vel, g)
      if (!L || L.t > 3) continue
      const d = Math.hypot(L.x - x, L.y - y)
      if (d < r) {
        const dx = x - L.x, dy = y - L.y
        const l = Math.hypot(dx, dy) || 1
        return [dx / l, dy / l]
      }
    }
    return null
  }

  /** Where to look when we are not shooting. */
  private lookDir(x: number, y: number, z: number, eyeZ: number, enemies: Track[], t: number, mv: V2): [number, number] {
    const w = this.w
    const lastHurt = w.hurt.length ? w.hurt[w.hurt.length - 1] : null
    if (lastHurt && t - lastHurt.t < 2.5 && lastHurt.bearing !== null) return [lastHurt.bearing, 0]
    let near: Track | null = null
    let nd = Infinity
    for (const e of enemies) {
      const d = Math.hypot(e.pos[0] - x, e.pos[1] - y)
      if (d < nd) (nd = d), (near = e)
    }
    if (near) {
      const a = aimAt([x, y, eyeZ], [near.pos[0], near.pos[1], near.pos[2] + 4.5])
      return [a.yaw, a.pitch]
    }
    const loud = w.heard.filter((h) => LOUD.has(h.kind) && t - h.t < 2.5)
    if (loud.length) return [loud[loud.length - 1].bearing, 0]
    const ml = Math.hypot(mv[0], mv[1])
    if (ml > 0.3) {
      const my = Math.atan2(mv[1], mv[0])
      this.scanYaw = my
      return [my + Math.sin(t * 1.3) * 1.0, 0]
    }
    this.scanYaw += 0.25
    return [this.scanYaw, 0]
  }
}

const round2 = (v: number) => Math.round(v * 100) / 100
const round3 = (v: number) => Math.round(v * 1000) / 1000
