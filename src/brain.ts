import { draw, log, type BotAction, type DropObs, type Hello, type Observation, type SelfObs, type WeaponObs } from "../mechfight.ts"
import { GameMap } from "./map.ts"
import { Navigator } from "./nav.ts"
import { World, type Track } from "./world.ts"
import { aimAt, centerOffset, clampPitch, leadAim, missDist, predict, shellLanding, type Aim } from "./combat.ts"
import { planPeek, type PeekPlan } from "./cover.ts"
import { angDiff, rng, type V2, type V3 } from "./geom.ts"

const UPG_FFA = ["plating", "armor", "nanites", "laser_damage", "laser_charge", "lightning_efficiency", "gun_damage", "sensors", "gyros", "gun_rate", "servos", "jets", "lightning_range", "reactor", "capacitor", "gun_mag", "gun_velocity", "lightning_chain", "laser_pierce"]
const UPG_PRACTICE = ["plating", "laser_damage", "nanites", "armor", "laser_charge", "lightning_efficiency", "gun_damage", "gun_rate", "gyros", "sensors", "lightning_range", "servos", "gun_mag", "gun_velocity", "jets", "reactor", "capacitor", "lightning_chain", "laser_pierce"]
const LOUD = new Set(["gun", "laser", "laser_charge", "lightning", "mortar", "explosion", "step", "boost", "jet", "reload"])

type Goal = { pos: V2; kind: "pickup" | "cube" | "farm" | "explore"; id: number }
type LaserMode = "off" | "charge" | "shoot"

export class Brain {
  name = "not-even-close"
  hello: Hello
  m: GameMap
  w: World
  nav: Navigator
  rnd: () => number
  perCube: number
  g: number
  // movement memory
  jinkSign = 1
  jinkT = 0
  holdGoal: V2 | null = null
  holdKey = ""
  scanYaw = 0
  exploreGoal: V2 | null = null
  exploreT = -99
  // combat memory
  targetId = -1
  regen = false
  lastKillT = -99
  plan: PeekPlan | null = null
  peekPhase: "hide" | "out" = "hide"
  peekSince = 0
  peekHp = 0
  peekShots = 0
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
    const scale = (st.radius ?? 2.5) / (this.hello.config.mech.radius ?? 2.5)
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
    const dTo = (p: ArrayLike<number>) => Math.hypot(p[0] - x, p[1] - y)
    const losTo = (tr: Track) => m.los(muz[0], muz[1], muz[2], tr.pos[0], tr.pos[1], tr.pos[2] + centerOffset(tr, this.perCube))
    let nearestD = Infinity
    for (const e of enemies) nearestD = Math.min(nearestD, dTo(e.pos))
    const hitRecently = t - w.lastHurtT < 4
    const attackers = new Set(w.hurt.filter((h) => t - h.t < 5 && h.by !== null && h.weapon !== "npc").map((h) => h.by))

    // ---------- choose the main threat ----------
    let T: Track | null = null
    let ts = -Infinity
    const npcAttackers = w.liveNpcs().filter((n) => t - n.hitUsT < 4 && dTo(n.pos) < 60)
    for (const e of [...enemies, ...npcAttackers]) {
      const d = dTo(e.pos)
      if (d > 300) continue
      let sc = -d * 0.015
      if (e.kind === "npc") sc += 3 + (d < 25 ? 3 : 0)
      else if (t - e.hitUsT < 6) sc += 5
      if (e.id === this.targetId) sc += 2
      if (e.charging && this.aimedAtUs(e, x, y)) sc += 3
      sc += (1 - e.hp / Math.max(1, e.maxHp)) * 1.5
      if (sc > ts) (ts = sc), (T = e)
    }
    const engage = T !== null && this.shouldEngage(T, x, y, t)
    this.targetId = engage && T ? T.id : -1

    // ---------- regen / retreat state ----------
    const threatsNear = enemies.filter((e) => dTo(e.pos) < 160 && t - e.seenT < 1).length
    const lowHp = attackers.size >= 2 || threatsNear >= 2 ? 0.65 : 0.5
    const justKilled = obs.events.some((ev) => ev.type === "kill") || t - this.lastKillT < 3
    if (obs.events.some((ev) => ev.type === "kill")) this.lastKillT = t
    // a nearly dead target is worth finishing before breaking off
    const finishing = T !== null && engage && T.hp / Math.max(1, T.maxHp) < 0.2 && T.hp < me.hp * 0.5
    if (!this.regen && !finishing && ((hpFrac < lowHp && (hitRecently || nearestD < 120)) || (justKilled && hpFrac < 0.75))) this.regen = true
    if (this.regen && (hpFrac > 0.9 || (t - w.lastHurtT > 10 && hpFrac > 0.75 && nearestD > 140))) this.regen = false

    // ---------- movement / mode ----------
    let mv: V2 = [0, 0]
    let boost = false
    let jump = false
    let speedCap = 1
    let laserMode: LaserMode = "off"
    let shootT: Track | null = null
    let gunOk = false
    let lightOk = false
    const go = (gx: number, gy: number, budget = 5000) => {
      const r = this.nav.steer(x, y, gx, gy, t, budget)
      if (r.jump) jump = true
      if (r.boost) boost = true
      return r
    }
    const zoneNow = w.zoneMargin(x, y, t)
    const zoneSoon = w.zoneMargin(x, y, t + 10)
    const zGoal = this.zoneGoal(obs, x, y, rq)
    const rotate = this.needRotate(obs, x, y, t)
    const dodge = this.mortarDodge(obs, x, y)
    const comp = m.comp(rq)
    const sNode = m.snap(x, y, rq, -1, 4)
    const myComp = sNode >= 0 ? comp[sNode] : -1
    const zoneOk = (px: number, py: number) => w.zoneMargin(px, py, t + 8) > 4
    let drop: DropObs | null = null

    if (dodge) {
      this.state = "dodge"
      mv = dodge
      boost = me.energy > 15
      if (engage && T) (shootT = T), (gunOk = true), (lightOk = true)
    } else if (zoneSoon < 4 || zoneNow < 8) {
      this.state = "zone"
      const r = go(zGoal[0], zGoal[1])
      mv = r.dir
      if (zoneNow < 2 || zoneSoon < -15) boost = boost || me.energy > 10
      if (engage && T) (shootT = T), (gunOk = true), (lightOk = true), (laserMode = "shoot")
    } else if (engage && T) {
      const d = dTo(T.pos)
      const brawlD = iLight >= 0 && me.energy > 25 ? 38 : 26
      shootT = T
      if (this.regen) {
        // break line of sight from every threat and heal
        this.state = "regen"
        this.replan(x, y, rq, comp, myComp, enemies, T, scale, t, zoneOk, 0.5)
        const hide = this.plan?.hide
        if (hide && !(this.plan!.hiddenNow && Math.hypot(hide[0] - x, hide[1] - y) < 1.5)) {
          const r = go(hide[0], hide[1])
          mv = r.dir
          if (!r.arrived && hitRecently) boost = me.energy > 20
        } else if (!hide) mv = this.jink(x, y, T, rq, t, 0.9, -0.8)
        // shoot back only if it is in our face
        gunOk = d < 60 && hitRecently
        lightOk = d < 40
        laserMode = iLaser >= 0 && this.plan?.hiddenNow ? "charge" : "off"
      } else if (d < brawlD) {
        this.state = "brawl"
        mv = this.brawlMove(x, y, T, iLight >= 0 && me.energy > 25, rq, t)
        gunOk = true
        lightOk = true
        laserMode = "shoot"
      } else {
        this.replan(x, y, rq, comp, myComp, enemies, T, scale, t, zoneOk, 0.35)
        const P = this.plan!
        if (!P.hide || !P.peek) {
          // nowhere to hide nearby: strafe at range
          this.state = "open"
          mv = this.fightMove(x, y, T, iLight >= 0 && me.energy > 30, iLaser >= 0, rq, t, hpFrac)
          gunOk = true
          lightOk = true
          laserMode = "shoot"
        } else {
          const r = this.peekStep(obs, x, y, T, P, iGun, iLaser, me.hp, t)
          this.state = "peek-" + this.peekPhase
          const dest = r.dest
          if (Math.hypot(dest[0] - x, dest[1] - y) > 1.2) {
            if (m.walkLine(x, y, dest[0], dest[1], rq)) {
              const dl = Math.hypot(dest[0] - x, dest[1] - y)
              mv = [(dest[0] - x) / dl, (dest[1] - y) / dl]
            } else mv = go(dest[0], dest[1]).dir
          }
          gunOk = this.peekPhase === "out"
          lightOk = true
          laserMode = r.laser
        }
      }
    } else if (this.regen && !this.dropGoal(obs, x, y, t, enemies, hpFrac)) {
      // healing with nobody engaged: stay out of sight of whoever is around, keep still
      this.state = "heal"
      let near: Track | null = null
      for (const e of enemies) if (!near || dTo(e.pos) < dTo(near.pos)) near = e
      if (near && dTo(near.pos) < 220) {
        this.replan(x, y, rq, comp, myComp, enemies, near, scale, t, zoneOk, 0.6)
        const hide = this.plan?.hide
        if (hide && !this.plan!.hiddenNow) mv = go(hide[0], hide[1]).dir
      }
      if (iLaser >= 0) laserMode = "charge"
    } else if ((drop = this.dropGoal(obs, x, y, t, enemies, hpFrac))) {
      this.state = "drop"
      const dd = Math.hypot(drop.pos[0] - x, drop.pos[1] - y)
      if (dd > Math.max(1.5, drop.radius - 4)) {
        const r = go(drop.pos[0], drop.pos[1])
        mv = r.dir
        if (dd > 60 && me.energy > 50 && drop.lands_in < dd / 11) boost = true
      }
    } else if (rotate) {
      this.state = "rotate"
      const r = go(zGoal[0], zGoal[1])
      mv = r.dir
      if (Math.hypot(zGoal[0] - x, zGoal[1] - y) > 100 && me.energy > 60 && nearestD > 120) boost = true
    } else {
      const goal = this.lootGoal(obs, x, y, slots, nearestD, t, hpFrac)
      if (goal) {
        this.state = goal.kind
        const tr = goal.kind === "farm" ? this.farmTrack(goal.id) : null
        if (tr) {
          shootT = tr
          gunOk = true
          lightOk = true
          laserMode = tr.id < 1000 ? "shoot" : "off"
          const d = dTo(tr.pos)
          const want = iLight >= 0 && me.energy > 30 ? 30 : 42
          if (d > want || !losTo(tr)) mv = go(goal.pos[0], goal.pos[1]).dir
          else mv = this.jink(x, y, tr, rq, t, 0.5)
        } else mv = go(goal.pos[0], goal.pos[1]).dir
        if (goal.kind === "explore" && me.energy > 80 && Math.hypot(goal.pos[0] - x, goal.pos[1] - y) > 60) boost = true
      } else {
        this.state = "hold"
        const r = go(zGoal[0], zGoal[1])
        mv = r.arrived ? [0, 0] : r.dir
        if (Math.hypot(zGoal[0] - x, zGoal[1] - y) > 120 && me.energy > 70 && nearestD > 150) boost = true
        if (enemies.length || w.heard.some((h) => h.dist === "near")) speedCap = 0.4
      }
      // visible but not engaged enemies: don't wander into them
      if (T && !engage && dTo(T.pos) < 120) speedCap = Math.min(speedCap, 0.4)
    }
    if (this.state !== "peek-hide" && this.state !== "peek-out") this.peekPhase = "hide"
    const ml = Math.hypot(mv[0], mv[1])
    if (ml > speedCap) mv = [(mv[0] / ml) * speedCap, (mv[1] / ml) * speedCap]

    // ---------- fire control ----------
    const fire: [boolean, boolean] = [false, false]
    let aim: Aim | null = null
    if (shootT) {
      const r = this.fireControl(me, muz, shootT, iGun, iLaser, iLight, laserMode, gunOk, lightOk, engage || shootT.kind === "npc", t)
      aim = r.aim
      fire[0] = r.fire[0]
      fire[1] = r.fire[1]
      if (r.reload) act.reload = true
    } else {
      if (iGun >= 0) {
        const G = slots[iGun]!
        if ((G.ammo ?? 0) < (G.stats.mag ?? 12) * 0.6 && !(G.reload ?? 0) && nearestD > 60) act.reload = true
      }
      if (iLaser >= 0) {
        const L = slots[iLaser]!
        const c = L.charge ?? 0
        if (c > 0) fire[iLaser] = c < (L.stats.charge ?? 0.75) + (L.stats.hold_s ?? 2) - 0.3
      }
    }

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
    const safe = nearestD > 80 && !hitRecently
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
    draw.label(`${this.state} ${w.mode[0]} hp${Math.round(hpFrac * 100)} e${Math.round(me.energy)} ${shootT ? "→" + shootT.id : ""} ${ms.toFixed(0)}ms`)
    if (shootT) draw.line(muz, [shootT.pos[0], shootT.pos[1], shootT.pos[2] + 4], "#ff4040")
    if (this.plan && t - this.plan.t < 1) {
      if (this.plan.hide) draw.point([this.plan.hide[0], this.plan.hide[1], z + 1], "#40ff60", 5)
      if (this.plan.peek) draw.point([this.plan.peek[0], this.plan.peek[1], z + 1], "#ffb040", 5)
    }
    return act
  }

  /** Should we fight this mech (vs. keep a low profile)? */
  private shouldEngage(e: Track, x: number, y: number, t: number): boolean {
    const d = Math.hypot(e.pos[0] - x, e.pos[1] - y)
    if (e.kind === "npc") return t - e.hitUsT < 4 && d < 60
    // it is fighting us
    if (t - e.hitUsT < 8) return true
    if (t - e.hitByUsT < 6) return true
    if (d < 35) return true
    if (this.aimedAtUs(e, x, y, 0.2) && d < 160 && t - e.seenT < 0.5) return true
    if (e.charging && this.aimedAtUs(e, x, y, 0.15) && d < 290) return true
    // easy prey (a kill is a placement point)
    if (e.hp / Math.max(1, e.maxHp) < 0.35 && d < 130) return true
    if (this.w.mode === "ffa" && Math.abs(angDiff(e.aim[0], Math.atan2(y - e.pos[1], x - e.pos[0]))) > 2.2 && d < 90) return true
    return false
  }

  private aimedAtUs(e: Track, x: number, y: number, tol = 0.12) {
    return Math.abs(angDiff(e.aim[0], Math.atan2(y - e.pos[1], x - e.pos[0]))) < tol
  }

  private replan(x: number, y: number, rq: number, comp: Int32Array, myComp: number, enemies: Track[], T: Track, scale: number, t: number, zoneOk: (px: number, py: number) => boolean, every: number) {
    const P = this.plan
    if (P && P.tid === T.id && t - P.t < every) return
    // the threats that matter: target + nearest others
    const threats = enemies
      .filter((e) => this.w.t - e.seenT < 0.5 && Math.hypot(e.pos[0] - x, e.pos[1] - y) < 230)
      .sort((a, b) => Math.hypot(a.pos[0] - x, a.pos[1] - y) - Math.hypot(b.pos[0] - x, b.pos[1] - y))
      .slice(0, 4)
    if (!threats.includes(T)) threats.unshift(T)
    this.plan = planPeek(this.m, x, y, rq, comp, myComp, threats, T, scale, this.perCube, t, zoneOk)
  }

  /** One step of the peek cycle: hide (charge / reload / heal), step out, shoot, hide again. */
  private peekStep(obs: Observation, x: number, y: number, T: Track, P: PeekPlan, iGun: number, iLaser: number, hp: number, t: number): { dest: V2; laser: LaserMode } {
    const me = obs.self
    const hide = P.hide!, peek = P.peek!
    const L = iLaser >= 0 ? me.weapons[iLaser]! : null
    const G = iGun >= 0 ? me.weapons[iGun]! : null
    const charge = L?.charge ?? 0
    const full = L ? (L.stats.charge ?? 0.75) : 0
    if (this.peekPhase === "hide") {
      const atHide = Math.hypot(hide[0] - x, hide[1] - y) < 1.8 || P.hiddenNow
      let ready = false
      if (L && L.cooldown <= 0.05 && charge >= full - 0.05) ready = true
      if (!L && G && (G.ammo ?? 0) >= 4 && !(G.reload ?? 0) && G.cooldown <= 0) ready = true
      if (L && G && L.cooldown > 0.3 && (G.ammo ?? 0) >= 4 && !(G.reload ?? 0)) ready = true
      // do not sit forever: a hunter walking around our cover will find us anyway
      if (atHide && ready && t - this.peekSince > 0.4) {
        this.peekPhase = "out"
        this.peekSince = t
        this.peekHp = hp
        this.peekShots = 0
      }
      return { dest: hide, laser: L && L.cooldown <= 0.05 ? "charge" : "off" }
    }
    // out: shoot, then go back
    const out = t - this.peekSince
    const hurt = this.peekHp - hp
    const laserDone = L ? charge === 0 && L.cooldown > 0.2 && out > 0.15 : true
    const gunDone = G ? (G.ammo ?? 0) === 0 || (G.reload ?? 0) > 0 : true
    const done = (L ? laserDone && (out > 0.9 || gunDone) : gunDone || out > 1.6) || out > 2.6 || hurt > 220
    if (done) {
      this.peekPhase = "hide"
      this.peekSince = t
      return { dest: hide, laser: "off" }
    }
    return { dest: P.shotNow ? [x, y] : peek, laser: "shoot" }
  }

  /** Aim and triggers against `tr`. */
  private fireControl(me: SelfObs, muz: V3, tr: Track, iGun: number, iLaser: number, iLight: number, laserMode: LaserMode, gunOk: boolean, lightOk: boolean, allowed: boolean, t: number): { aim: Aim; fire: [boolean, boolean]; reload: boolean } {
    const m = this.m
    const slots = me.weapons
    const turn = me.stats.turn_rate ?? 4
    const off = centerOffset(tr, this.perCube)
    const pNow = predict(m, tr, 0.075, this.g)
    const direct = aimAt(muz, [pNow[0], pNow[1], pNow[2] + off])
    const d = direct.dist
    const radius = tr.kind === "npc" ? 1.5 : 2.5 * (1 + this.perCube * tr.cubes)
    const los = m.los(muz[0], muz[1], muz[2], direct.pt[0], direct.pt[1], direct.pt[2])
    const fire: [boolean, boolean] = [false, false]
    let aim = direct
    let reload = false
    let laserBusy = false
    if (iLaser >= 0) {
      const L = slots[iLaser]!
      const charge = L.charge ?? 0
      const full = L.stats.charge ?? 0.75
      const range = L.stats.range ?? 280
      const left = full + (L.stats.hold_s ?? 2) - charge
      if (laserMode === "charge") {
        if (charge > 0 || L.cooldown <= 0.05) fire[iLaser] = left > 0.3
        laserBusy = charge > 0
      } else if (laserMode === "shoot" && allowed) {
        if (charge > 0) {
          laserBusy = true
          const ok = d <= range && los && missDist(me, direct, 0.05) < radius * 0.7
          fire[iLaser] = !((charge >= full - 0.02 && ok) || left < 0.2)
        } else if (L.cooldown <= 0.05 && d <= range * 0.97 && los && !this.shortRisk(tr, d)) {
          fire[iLaser] = true
          laserBusy = true
        }
      } else if (charge > 0) fire[iLaser] = left > 0.3
    }
    let lightningOn = false
    if (iLight >= 0 && lightOk && allowed) {
      const L = slots[iLight]!
      const range = L.stats.range ?? 45
      const half = (((L.stats.cone_deg ?? 30) * Math.PI) / 180) * 0.5
      const reserve = tr.kind === "npc" || tr.hp / tr.maxHp < 0.25 ? 6 : 18
      if (d < range * 0.9 && me.energy > reserve) {
        lightningOn = true
        const ey = Math.abs(angDiff(direct.yaw, me.aim[0])) - turn * 0.05
        if (L.cooldown <= 0 && ey < half * 0.6 && los) fire[iLight] = true
      }
    }
    if (iGun >= 0) {
      const G = slots[iGun]!
      const lead = leadAim(m, muz, tr, G.stats.speed ?? 252, 0.075, off, this.g)
      if (!laserBusy && !lightningOn) aim = lead
      const ammo = G.ammo ?? 1
      const okRange = lead.dist <= (G.stats.range ?? 230) * 0.98
      const laserOk = !laserBusy || d < 40
      if (gunOk && allowed && G.cooldown <= 0 && ammo > 0 && !(G.reload ?? 0) && okRange && laserOk && los && missDist(me, lead, 0.05) < radius * 0.75) fire[iGun] = true
      if (ammo === 0 && !(G.reload ?? 0)) reload = true
      if (!gunOk && ammo < (G.stats.mag ?? 12) * 0.5 && !(G.reload ?? 0)) reload = true
    }
    return { aim, fire, reload }
  }

  /** Is this target holding lightning close enough to short our laser? */
  private shortRisk(tr: Track, d: number) {
    return tr.weapons.includes("lightning") && d < 50
  }

  /** Track for a farm goal id (NPC from tracks, crate from memory). */
  private farmTrack(id: number): Track | null {
    const tr = this.w.tracks.get(id)
    if (tr) return tr.seenT === this.w.t ? tr : null
    const c = this.w.crates.get(id)
    if (!c) return null
    return { id: c.id, kind: "npc", pos: c.pos, vel: [0, 0, 0], aim: [0, 0], hp: c.hp, maxHp: 300, cubes: 0, weapons: [], charging: false, seenT: this.w.t, firstT: 0, hitUsT: -99, hitByUsT: -99 }
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

  /** Start moving to the next circle early enough, before its shrink starts. */
  private needRotate(obs: Observation, x: number, y: number, t: number): boolean {
    const z = obs.zone
    const R = z.next_radius
    const d = Math.hypot(x - z.next_center[0], y - z.next_center[1])
    const inner = R < 45 ? R * 0.5 : R * 0.75
    if (d <= inner) return false
    const travel = (d - inner) / 10 + 12
    return t > z.shrink_start - travel - 10
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

  /** A supply drop worth going for: reachable, not contested, inside the zone. */
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

  /** Where to go when nothing threatens us: weapons, cubes, farm targets, exploring. */
  private lootGoal(obs: Observation, x: number, y: number, slots: SelfObs["weapons"], nearestD: number, t: number, hpFrac: number): Goal | null {
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
    const armed = slots.some((s) => s?.kind === "laser") && slots.some((s) => s?.kind === "lightning")
    for (const p of w.pickups.values()) {
      if (!this.wants(p.kind, slots, nearestD > 80).ok) continue
      const c = m.cellOf(p.pos[0], p.pos[1])
      if (c < 0 || m.wall[c] || Math.abs(p.pos[2] - m.groundAt(p.pos[0], p.pos[1])) > 2.5) continue
      consider([p.pos[0], p.pos[1]], "pickup", p.id, 0.5, nearestD > 100 ? 260 : 100)
    }
    for (const c of obs.cubes) {
      if (Math.abs(c.pos[2] - m.surfAt(c.pos[0], c.pos[1])) > 3) continue
      consider([c.pos[0], c.pos[1]], "cube", c.id, 0.45, nearestD > 60 ? 120 : 40)
    }
    // farm drones and crates for cubes (and crate weapons) while it is calm
    if (nearestD > 140 && hpFrac > 0.5 && t < 380 && w.recentDamage(5) === 0) {
      for (const n of w.liveNpcs()) consider([n.pos[0], n.pos[1]], "farm", n.id, 1.1, 130)
      for (const c of w.crates.values())
        if (Math.abs(c.pos[2] - m.groundAt(c.pos[0], c.pos[1])) < 3) consider([c.pos[0], c.pos[1]], "farm", c.id, armed ? 1.3 : 0.9, 110)
    }
    if (best) return best
    // nothing known: explore while we still miss a weapon
    if (!armed && t < 240 && nearestD > 100) {
      if (!this.exploreGoal || t - this.exploreT > 14 || Math.hypot(this.exploreGoal[0] - x, this.exploreGoal[1] - y) < 6) {
        this.exploreGoal = this.pickExplore(obs, x, y)
        this.exploreT = t
      }
      if (this.exploreGoal) return { pos: this.exploreGoal, kind: "explore", id: -1 }
    }
    return null
  }

  private pickExplore(obs: Observation, x: number, y: number): V2 | null {
    const m = this.m
    const rq = this.nav.rq
    const comp = m.comp(rq)
    const s = m.snap(x, y, rq, -1, 4)
    if (s < 0) return null
    const z = obs.zone
    for (let k = 0; k < 30; k++) {
      const a = this.rnd() * Math.PI * 2, r = 50 + this.rnd() * 90
      const px = x + Math.cos(a) * r, py = y + Math.sin(a) * r
      if (Math.hypot(px - z.next_center[0], py - z.next_center[1]) > z.next_radius * 0.85) continue
      const n = m.nodeOf(px, py)
      if (!m.passable(n, rq) || comp[n] !== comp[s]) continue
      return [px, py]
    }
    return null
  }

  /** Close-range fight: circle-strafe at lightning range, or keep 25-40 m with the gun. */
  private brawlMove(x: number, y: number, tr: Track, lightning: boolean, rq: number, t: number): V2 {
    const d = Math.hypot(tr.pos[0] - x, tr.pos[1] - y)
    const [lo, hi] = lightning ? [12, 30] : [22, 40]
    const radial = d > hi ? 0.7 : d < lo ? -0.7 : 0
    return this.jink(x, y, tr, rq, t, 0.95, radial)
  }

  /** Open-ground fight: strafe with random direction changes, keeping a preferred range. */
  private fightMove(x: number, y: number, tr: Track, lightning: boolean, laser: boolean, rq: number, t: number, hpFrac: number): V2 {
    const d = Math.hypot(tr.pos[0] - x, tr.pos[1] - y)
    let lo = 30, hi = 70
    if (lightning && (hpFrac > 0.5 || tr.hp / tr.maxHp < 0.35)) (lo = 10), (hi = 30)
    else if (laser) (lo = 60), (hi = 170)
    if (d > hi + 20) return this.nav.steer(x, y, tr.pos[0], tr.pos[1], t, 4000).dir
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
      if (this.m.walkLine(x, y, x + dx * 5, y + dy * 5, this.nav.rq) && this.w.zoneMargin(x + dx * 8, y + dy * 8, t + 3) > 5) return [dx, dy]
      this.jinkSign = -this.jinkSign
    }
    if (radial !== 0 && this.m.walkLine(x, y, x + rx * radial * 5, y + ry * radial * 5, this.nav.rq)) return [rx * Math.sign(radial), ry * Math.sign(radial)]
    return [0, 0]
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
      return [my + Math.sin(t * 1.3) * 1.1, 0]
    }
    this.scanYaw += 0.3
    return [this.scanYaw, 0]
  }
}

const round2 = (v: number) => Math.round(v * 100) / 100
const round3 = (v: number) => Math.round(v * 1000) / 1000
