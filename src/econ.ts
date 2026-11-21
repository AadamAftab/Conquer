import { draw, type BotAction, type Hello, type Observation, type SelfObs } from "../mechfight.ts"
import type { GameMap } from "./map.ts"
import type { Navigator } from "./nav.ts"
import type { Track, World } from "./world.ts"
import { aimAt, clampPitch, shellLanding } from "./combat.ts"
import { angDiff, type V2, type V3 } from "./geom.ts"

/** Survival-first upgrade order of the economy core. */
const UPG = ["plating", "armor", "nanites", "lightning_efficiency", "laser_damage", "laser_charge", "gun_damage", "gun_rate", "lightning_range", "lightning_chain", "gyros", "servos", "gun_mag", "capacitor", "reactor", "jets", "gun_velocity", "sensors", "mortar_reload", "mortar_velocity", "mortar_splash", "laser_pierce"]
const PREF: Record<string, number> = { laser: 3.2, lightning: 2.5, mortar: 1, gun: 0 }
const LOUD = new Set(["gun", "laser", "laser_charge", "lightning", "mortar", "boost", "step", "jet"])

type Host = { m: GameMap; w: World; nav: Navigator; hello: Hello; rnd: () => number; perCube: number; g: number }

/**
 * Economy core: fight whatever is targeted at a fixed range band with fast random strafes, keep the
 * gun and add the best special weapon, detour to cubes and weapons even mid-fight, retreat only
 * when clearly losing, heal by creeping into cover, farm drones and crates, go early to the zone.
 */
export class Econ {
  h: Host
  state = "init"
  targetId = -1
  targetT = -99
  strafeDir = 1
  strafeT = 0
  hideUntil = -1
  stuckT = 0
  lastPos: V2 = [0, 0]
  stuckRef: V2 = [0, 0]
  mechHitT = -99
  mechHitBearing = 0
  threatBearing = 0
  threatT = -99
  phase = 0
  spin = 0

  constructor(h: Host) {
    this.h = h
  }

  pickUpgrade(obs: Observation): number {
    let best = 0, bestR = Infinity
    obs.offer!.choices.forEach((c, i) => {
      let r = UPG.indexOf(c.id)
      if (r < 0) r = 50
      if (r < bestR) (bestR = r), (best = i)
    })
    return best
  }

  /** Is a weapon of this kind wanted, and which slot does it replace? The gun is never dropped. */
  wants(kind: string, slots: SelfObs["weapons"]): { ok: boolean; drop?: number } {
    if (slots.some((s) => s?.kind === kind)) return { ok: false }
    if (slots.some((s) => !s)) return { ok: true }
    let worst = -1
    for (let i = 0; i < slots.length; i++) {
      const s = slots[i]
      if (!s || s.kind === "gun" || (s.kind === "laser" && (s.charge ?? 0) > 0)) continue
      if (worst < 0 || PREF[s.kind] < PREF[slots[worst]!.kind]) worst = i
    }
    if (worst >= 0 && (PREF[kind] ?? 0) > PREF[slots[worst]!.kind] + 0.5) return { ok: true, drop: worst }
    return { ok: false }
  }

  act(obs: Observation, T0: number): BotAction {
    const { m, w, nav, hello } = this.h
    const cfg = hello.config
    const me = obs.self, t = obs.t
    const [x, y, z] = me.pos
    const st = me.stats
    const scale = (st.radius ?? 2.5) / (cfg.mech.radius ?? 2.5)
    nav.rq = (st.radius ?? 2.5) + 0.9
    nav.costFn = null
    const walk = st.walk_speed ?? 11.7
    const act: BotAction = {}
    if (obs.offer) act.pick = this.pickUpgrade(obs)
    const slots = me.weapons
    const iGun = slots.findIndex((s) => s?.kind === "gun")
    const iLaser = slots.findIndex((s) => s?.kind === "laser")
    const iLight = slots.findIndex((s) => s?.kind === "lightning")
    const iMortar = slots.findIndex((s) => s?.kind === "mortar")
    const onlyGun = slots.some((s) => !s)
    const hpFrac = me.hp / me.max_hp
    const muzZ = z + (st.muzzle_height ?? 7)
    const dTo = (p: ArrayLike<number>) => Math.hypot(p[0] - x, p[1] - y)

    // ---------- memory of threats / bearings ----------
    for (const ev of obs.events) {
      if (ev.type !== "damaged" || ev.weapon === "zone") continue
      if (ev.bearing !== null) (this.threatBearing = ev.bearing), (this.threatT = t)
      if (ev.by !== null && ev.by < 16 && ev.bearing !== null) {
        const tr = w.tracks.get(ev.by)
        if (!tr || tr.seenT !== t) (this.mechHitT = t), (this.mechHitBearing = ev.bearing)
      }
    }
    for (const s of obs.sounds) if (s.dist !== "far" && LOUD.has(s.kind) && t - this.threatT > 1.5) (this.threatBearing = s.bearing), (this.threatT = t - s.age)

    // ---------- take a pickup in reach ----------
    const reach = cfg.loot.pickup_radius ?? 4
    for (const p of w.pickups.values()) {
      if (Math.hypot(p.pos[0] - x, p.pos[1] - y) > reach - 0.3 || Math.abs(p.pos[2] - z) >= 3.5) continue
      const want = this.wants(p.kind, slots)
      if (want.ok) {
        act.take = p.id
        if (want.drop !== undefined) act.drop = want.drop
        break
      }
    }

    // ---------- target ----------
    let T: Track | null = null
    let best = Infinity
    for (const e of w.tracks.values()) {
      if (e.kind !== "mech") continue
      const age = t - e.seenT
      if (age > 4) continue
      const sc = dTo(e.pos) + (e.hp / 1400) * 90 + age * 30 - (t - e.hitUsT < 3 ? 50 : 0) - (e.id === this.targetId && t - this.targetT < 2 ? 40 : 0)
      if (sc < best) (best = sc), (T = e)
    }
    if (T) (this.targetId = T.id), (this.targetT = t)
    const threats = [...w.tracks.values()].filter((e) => e.kind === "mech" && t - e.seenT < 3 && dTo(e.pos) < 160).length

    // ---------- zone ----------
    const zo = obs.zone
    const cur = w.zoneAt(t)
    const nr = zo.next_radius, nc = zo.next_center
    const dNext = Math.hypot(x - nc[0], y - nc[1])
    const dCur = Math.hypot(x - cur.c[0], y - cur.c[1])
    const shrinking = t >= zo.shrink_start && t < zo.shrink_end
    const toShrink = zo.shrink_start - t
    const needNext = dNext > 0.8 * nr && (shrinking || toShrink < (dNext - 0.6 * nr) / 11.7 + 12)
    const outside = !me.in_zone
    let zoneGoal: V2 | null = null
    if (outside || dCur > 0.9 * cur.r || needNext) {
      const k = dNext > 1e-3 ? (0.45 * nr) / dNext : 0
      zoneGoal = [nc[0] + (x - nc[0]) * k, nc[1] + (y - nc[1]) * k]
    }

    // ---------- loot memory views ----------
    const groundOk = (p: V3) => Math.abs(p[2] - m.groundAt(p[0], p[1])) <= 2
    let nearCube: { p: V3; d: number } | null = null
    for (const c of w.cubes.values()) {
      if (t - c.seenT > 40 || !groundOk(c.pos)) continue
      const d = dTo(c.pos)
      if (!nearCube || d < nearCube.d) nearCube = { p: c.pos, d }
    }
    let nearPick: { p: V3; d: number; eff: number } | null = null
    for (const p of w.pickups.values()) {
      if (t - p.seenT > 60 || !groundOk(p.pos) || !this.wants(p.kind, slots).ok) continue
      const d = dTo(p.pos)
      const eff = d - (PREF[p.kind] ?? 0) * 5
      if (!nearPick || eff < nearPick.eff) nearPick = { p: p.pos, d, eff }
    }

    // ---------- decide movement ----------
    let goal: V2 | null = null
    let stand = false
    let creep = false
    let boost = false
    let mode = "roam"
    const fire: [boolean, boolean] = [false, false]
    let aimYP: [number, number] | null = null
    let firedGun = false
    if (T) {
      mode = "fight"
      const d = dTo(T.pos)
      const age = t - T.seenT
      const seen = age < 0.11
      const tScale = 1 + this.h.perCube * T.cubes
      const tc: V3 = [T.pos[0] + T.vel[0] * age, T.pos[1] + T.vel[1] * age, T.pos[2] + 4.5 * tScale]
      const hasLos = m.los(x, y, muzZ, tc[0], tc[1], tc[2])
      const tFrac = T.hp / Math.max(1, T.maxHp)
      const finishing = tFrac < 0.2 && hpFrac > 0.2 && threats <= 2
      const losing = !finishing && ((hpFrac < 0.35 && tFrac > hpFrac + 0.1) || (threats >= 2 && hpFrac < 0.55) || (threats >= 3 && hpFrac < 0.8))
      if (losing) this.hideUntil = t + 6
      // aim and fire
      const r = this.fireAt(me, T, tc, d, age, seen, hasLos, iGun, iLaser, iLight, iMortar)
      aimYP = r.aim
      fire[0] = r.fire[0]
      fire[1] = r.fire[1]
      firedGun = r.firedGun
      // move
      if (losing) {
        mode = "retreat"
        const cv = this.coverFrom(x, y, [T.pos[0], T.pos[1], T.pos[2] + 8], 50)
        goal = cv ?? [x + ((x - T.pos[0]) / Math.max(1, d)) * 20, y + ((y - T.pos[1]) / Math.max(1, d)) * 20]
        if (me.energy > 15) boost = true
      } else {
        const lightning = iLight >= 0
        const prefer = lightning ? 28 : iLaser >= 0 ? 110 : 70
        let radial = 0
        if (d > prefer + 15) {
          radial = 1
          if (lightning && d < 90 && me.energy > 70) boost = true
        } else if (d < prefer - 15) radial = -0.7
        if (!hasLos && !seen) radial = 1
        if (t > this.strafeT) {
          this.strafeDir = this.h.rnd() < 0.5 ? -1 : 1
          this.strafeT = t + 0.35 + this.h.rnd() * 0.7
        }
        const ux = (T.pos[0] - x) / Math.max(1, d), uy = (T.pos[1] - y) / Math.max(1, d)
        const px = -uy * this.strafeDir, py = ux * this.strafeDir
        if (radial > 0 && !hasLos) {
          // approach around cover along the nav route, still strafing a little
          const sr = nav.steer(x, y, T.pos[0], T.pos[1], t, this.budget(T0))
          goal = [x + sr.dir[0] * 10 + px * 3, y + sr.dir[1] * 10 + py * 3]
        } else goal = [x + (ux * radial + px * 0.9) * 10, y + (uy * radial + py * 0.9) * 10]
        // dodge a charging laser pointed at us
        if (T.charging && Math.abs(angDiff(T.aim[0], Math.atan2(y - T.pos[1], x - T.pos[0]))) < 0.25 && me.energy > (lightning ? 45 : 10)) boost = true
      }
      // detours to cubes and weapons, even mid-fight
      if (nearCube && nearCube.d < (losing ? 12 : hpFrac > 0.5 ? 70 : 40)) (goal = [nearCube.p[0], nearCube.p[1]]), (mode = "fight-cube")
      else if (!losing && nearPick && nearPick.eff < (onlyGun ? 60 : 35)) (goal = [nearPick.p[0], nearPick.p[1]]), (mode = "fight-loot")
      if (zoneGoal && outside) (goal = null), (mode = "fight-zone")
    }
    const heal = !goal && !T && !zoneGoal && !(nearCube && nearCube.d <= 40) && (hpFrac < 0.55 || (t < this.hideUntil && hpFrac < 0.9))
    if (heal) {
      mode = "heal"
      let last: Track | null = null
      for (const e of w.tracks.values()) if (e.kind === "mech" && (!last || e.seenT > last.seenT)) last = e
      if (last && m.los(last.pos[0], last.pos[1], last.pos[2] + 8, x, y, z + 6)) {
        const cv = this.coverFrom(x, y, [last.pos[0], last.pos[1], last.pos[2] + 8], 36)
        if (cv) (goal = cv), (creep = true)
        else stand = true
      } else stand = true
    } else if (!goal) {
      if (zoneGoal) {
        if (mode === "roam") mode = "zone"
        goal = zoneGoal
        if (outside && me.energy > 20) boost = true
      } else if (!T) {
        const lg = this.lootGoal(obs, x, y, hpFrac, slots, onlyGun, walk, nearCube, t)
        if (lg) {
          mode = "loot"
          goal = lg.p
          stand = lg.stand
        } else {
          const k = dNext > 1e-3 ? Math.min(0.6 * nr, Math.max(20, dNext - 30)) / dNext : 0
          goal = [nc[0] + (x - nc[0]) * k, nc[1] + (y - nc[1]) * k]
        }
        // shoot drones and crates while looting or roaming
        if (hpFrac > 0.4 && t - this.mechHitT > 1.5) {
          const f = this.farmShot(me, x, y, z, muzZ, iGun, iLight)
          if (f) {
            aimYP = f.aim
            if (iGun >= 0) fire[iGun] = f.fireGun
            if (iLight >= 0) fire[iLight] = f.fireLight
            firedGun = f.fireGun
            if (f.walkTo) (goal = f.walkTo), (stand = false)
          }
        }
      }
    }

    // ---------- execute movement ----------
    let mv: V2 = [0, 0]
    let jump = false
    if (goal && !stand) {
      const r = nav.steer(x, y, goal[0], goal[1], t, this.budget(T0))
      mv = r.arrived ? [0, 0] : r.dir
      if (r.jump) jump = true
    }
    if (creep) mv = [mv[0] * 0.4, mv[1] * 0.4]
    const dg = this.dodge(obs, x, y, z, scale, me.vel)
    if (dg[0] || dg[1]) {
      mv = [0.3 * mv[0] + 0.95 * dg[0], 0.3 * mv[1] + 0.95 * dg[1]]
      const l = Math.hypot(mv[0], mv[1])
      if (l > 1) mv = [mv[0] / l, mv[1] / l]
      if (me.energy > (iLight >= 0 && T ? 55 : 25)) boost = true
    }
    // stuck handling
    const moved = Math.hypot(x - this.lastPos[0], y - this.lastPos[1])
    this.lastPos = [x, y]
    if (Math.hypot(x - this.stuckRef[0], y - this.stuckRef[1]) > 0.6) (this.stuckT = 0), (this.stuckRef = [x, y])
    if (Math.hypot(mv[0], mv[1]) > 0.1 && moved < 0.25 && me.grounded) this.stuckT += 0.1
    if (this.stuckT > 4) this.stuckT = 0
    if (this.stuckT > 0.6 && me.energy > 10) (jump = true), (boost = true)
    if (this.stuckT > 2.5) {
      const l = Math.hypot(mv[0], mv[1]) || 1
      mv = [(-mv[1] / l) * this.strafeDir, (mv[0] / l) * this.strafeDir]
    }
    if (!me.grounded && this.stuckT > 0 && me.energy > 5) boost = true

    // ---------- idle aim, laser hold, reload ----------
    if (!aimYP) aimYP = this.idleAim(x, y, t, mv)
    if (iLaser >= 0) {
      const L = slots[iLaser]!
      const c = L.charge ?? 0
      if (c > 0 && c < (L.stats.charge ?? 0.75) - 0.05) fire[iLaser] = true
    }
    if (iGun >= 0 && mode !== "fight") {
      const G = slots[iGun]!
      if ((G.ammo ?? 0) < (G.stats.mag ?? 12) * 0.5 && !(G.reload ?? 0) && !firedGun) act.reload = true
    }

    act.move = [Math.round(mv[0] * 100) / 100, Math.round(mv[1] * 100) / 100]
    act.boost = boost
    act.jump = jump
    act.aim = [Math.round(aimYP[0] * 1000) / 1000, Math.round(clampPitch(me, aimYP[1]) * 1000) / 1000]
    act.fire = fire
    this.state = mode
    draw.label(`E:${mode} hp${Math.round(hpFrac * 100)}${T ? " →" + T.id : ""}`)
    return act
  }

  private budget(T0: number) {
    const spent = performance.now() - T0
    return spent > 20 ? 300 : spent > 10 ? 1500 : 5000
  }

  /** Aim and triggers against the target. */
  private fireAt(me: SelfObs, T: Track, tc: V3, d: number, age: number, seen: boolean, hasLos: boolean, iGun: number, iLaser: number, iLight: number, iMortar: number): { aim: [number, number]; fire: [boolean, boolean]; firedGun: boolean } {
    const slots = me.weapons
    const [x, y, z] = me.pos
    const muz: V3 = [x, y, z + (me.stats.muzzle_height ?? 7)]
    const fire: [boolean, boolean] = [false, false]
    const tScale = 1 + this.h.perCube * T.cubes
    const tol = Math.atan2(2.2 * tScale, Math.max(d, 1))
    const direct = aimAt(muz, tc)
    // gun lead: 3 passes, vertical lead limited to 0.3 s
    const speed = iGun >= 0 ? (slots[iGun]!.stats.speed ?? 252) : 252
    let lead = direct
    {
      let p = tc
      for (let k = 0; k < 3; k++) {
        const tt = Math.hypot(p[0] - muz[0], p[1] - muz[1], p[2] - muz[2]) / speed
        p = [T.pos[0] + T.vel[0] * (tt + age), T.pos[1] + T.vel[1] * (tt + age), tc[2] + T.vel[2] * Math.min(tt, 0.3)]
      }
      lead = aimAt(muz, p)
    }
    const lightNear = iLight >= 0 && d < (slots[iLight]!.stats.range ?? 45) * 1.1
    const L = iLaser >= 0 ? slots[iLaser]! : null
    const laserFocus = L !== null && ((L.charge ?? 0) > 0 || L.cooldown < 0.2)
    const emph = lightNear || laserFocus ? direct : lead
    const yawErr = (a: { yaw: number }) => Math.abs(angDiff(a.yaw, me.aim[0]))
    const pitchErr = Math.abs(emph.pitch - me.aim[1])
    let firedGun = false
    if (iGun >= 0 && seen && hasLos) {
      const G = slots[iGun]!
      if (d < (G.stats.range ?? 230) && yawErr(lead) < 1.2 * tol && pitchErr < 2 * tol + 0.03 && !(G.reload ?? 0)) (fire[iGun] = true), (firedGun = true)
    }
    if (iLight >= 0 && seen && hasLos) {
      const Lt = slots[iLight]!
      const cone = ((Lt.stats.cone_deg ?? 30) * 0.45 * Math.PI) / 180
      if (d < (Lt.stats.range ?? 45) - 1 && yawErr(direct) < cone && me.energy > (Lt.stats.energy ?? 3) + 2) fire[iLight] = true
    }
    if (L) {
      const charge = L.charge ?? 0
      const full = L.stats.charge ?? 0.75
      if (charge > 0) {
        const aligned = seen && hasLos && yawErr(direct) < 0.9 * tol && Math.abs(direct.pitch - me.aim[1]) < 1.5 * tol + 0.02
        fire[iLaser] = !(charge >= full - 0.05 && aligned)
      } else if (L.cooldown <= 0.05 && seen && d < (L.stats.range ?? 280) && (hasLos || d < 60)) fire[iLaser] = true
    }
    let aim: [number, number] = [emph.yaw, emph.pitch]
    // mortar: lob when nothing else fires this tick
    if (iMortar >= 0 && !fire[0] && !fire[1] && !(L && (L.charge ?? 0) > 0)) {
      const Mo = slots[iMortar]!
      if (Mo.cooldown <= 0 && d > 18 && d < 220) {
        const v = Mo.stats.speed ?? 72, g = Mo.stats.gravity ?? 22
        const tof = d / (0.7 * v)
        const px = T.pos[0] + T.vel[0] * tof, py = T.pos[1] + T.vel[1] * tof
        const dh = Math.hypot(px - x, py - y)
        const h = T.pos[2] - (z + 9.5 * (1 + this.h.perCube * me.cubes))
        const disc = v ** 4 - g * (g * dh * dh + 2 * h * v * v)
        if (disc >= 0) {
          const lo = Math.atan((v * v - Math.sqrt(disc)) / (g * dh)), hi = Math.atan((v * v + Math.sqrt(disc)) / (g * dh))
          let pitch = hasLos ? lo : hi
          if (pitch > (me.stats.max_pitch ?? 1.3)) pitch = lo
          const yaw = Math.atan2(py - y, px - x)
          aim = [yaw, pitch]
          if (Math.abs(angDiff(yaw, me.aim[0])) < 0.03 && Math.abs(pitch - me.aim[1]) < 0.03) fire[iMortar] = true
        }
      }
    }
    return { aim, fire, firedGun }
  }

  /** Rings of 6 m up to maxR, 16 angles: walkable spots the threat eye cannot see (at ground + 6). */
  private coverFrom(x: number, y: number, eye: V3, maxR: number): V2 | null {
    const m = this.h.m, rq = this.h.nav.rq
    const d0 = Math.hypot(x - eye[0], y - eye[1])
    let best: V2 | null = null
    let bs = Infinity
    for (let r = 6; r <= maxR; r += 6)
      for (let k = 0; k < 16; k++) {
        const a = (k / 16) * Math.PI * 2
        const px = x + Math.cos(a) * r, py = y + Math.sin(a) * r
        if (!m.passable(m.nodeOf(px, py), rq)) continue
        if (m.los(eye[0], eye[1], eye[2], px, py, m.groundAt(px, py) + 6)) continue
        const sc = r - 0.3 * (Math.hypot(px - eye[0], py - eye[1]) - d0)
        if (sc < bs) (bs = sc), (best = [px, py])
      }
    return best
  }

  /** Out-of-combat goal: weapons, cubes, supply drops, drones, crates (lowest score wins). */
  private lootGoal(obs: Observation, x: number, y: number, hpFrac: number, slots: SelfObs["weapons"], onlyGun: boolean, walk: number, nearCube: { p: V3; d: number } | null, t: number): { p: V2; stand: boolean } | null {
    const w = this.h.w, m = this.h.m
    let best: { p: V2; stand: boolean } | null = null
    let bs = Infinity
    const consider = (p: V2, sc: number, stand: boolean) => {
      if (sc < bs) (bs = sc), (best = { p, stand })
    }
    for (const p of w.pickups.values()) {
      if (t - p.seenT > 60 || Math.abs(p.pos[2] - m.groundAt(p.pos[0], p.pos[1])) > 2 || !this.wants(p.kind, slots).ok) continue
      const d = Math.hypot(p.pos[0] - x, p.pos[1] - y)
      consider([p.pos[0], p.pos[1]], d - (PREF[p.kind] ?? 0) * 20 - (onlyGun ? 50 : 0), false)
    }
    if (nearCube) consider([nearCube.p[0], nearCube.p[1]], nearCube.d - 30, false)
    const zo = obs.zone
    for (const d of obs.drops) {
      if (d.holder !== null && d.holder !== obs.self.id) continue
      const dd = Math.hypot(d.pos[0] - x, d.pos[1] - y)
      if (dd > 220 || d.lands_in > dd / walk + 3) continue
      if (Math.hypot(d.pos[0] - zo.next_center[0], d.pos[1] - zo.next_center[1]) > zo.next_radius && zo.shrink_start - t < 15) continue
      consider(dd < d.radius - 3 ? [x, y] : [d.pos[0], d.pos[1]], dd - 80, dd < d.radius - 3)
    }
    if (hpFrac > 0.5)
      for (const n of w.liveNpcs()) {
        const d = Math.hypot(n.pos[0] - x, n.pos[1] - y)
        if (d < 170) consider([n.pos[0], n.pos[1]], d - 20, d <= 38)
      }
    if (hpFrac > 0.45)
      for (const c of w.crates.values()) {
        if (Math.abs(c.pos[2] - m.groundAt(c.pos[0], c.pos[1])) > 2) continue
        const d = Math.hypot(c.pos[0] - x, c.pos[1] - y)
        if (d < 150) consider([c.pos[0], c.pos[1]], d + 5, d <= 30)
      }
    return best
  }

  /** Shoot the nearest drone (110 m) or crate (120 m) in sight with the gun (lightning when close). */
  private farmShot(me: SelfObs, x: number, y: number, z: number, muzZ: number, iGun: number, iLight: number): { aim: [number, number]; fireGun: boolean; fireLight: boolean; walkTo: V2 | null } | null {
    const w = this.h.w, m = this.h.m
    let tgt: { p: V3; v: V3; npc: boolean } | null = null
    let bd = Infinity
    for (const n of w.liveNpcs()) {
      const d = Math.hypot(n.pos[0] - x, n.pos[1] - y)
      if (d < 110 && d < bd && m.los(x, y, muzZ, n.pos[0], n.pos[1], n.pos[2] + 1.75)) (bd = d), (tgt = { p: [n.pos[0], n.pos[1], n.pos[2] + 1.75], v: n.vel, npc: true })
    }
    if (!tgt)
      for (const c of w.crates.values()) {
        const d = Math.hypot(c.pos[0] - x, c.pos[1] - y)
        if (d < 120 && d < bd && m.los(x, y, muzZ, c.pos[0], c.pos[1], c.pos[2] + 1.2)) (bd = d), (tgt = { p: [c.pos[0], c.pos[1], c.pos[2] + 1.2], v: [0, 0, 0], npc: false })
      }
    if (!tgt) return null
    const tt = bd / 252
    const a = aimAt([x, y, muzZ], [tgt.p[0] + tgt.v[0] * tt, tgt.p[1] + tgt.v[1] * tt, tgt.p[2]])
    const tol = Math.atan2(1.2, Math.max(bd, 1))
    const ok = Math.abs(angDiff(a.yaw, me.aim[0])) < tol && Math.abs(a.pitch - me.aim[1]) < 2 * tol + 0.02
    const G = iGun >= 0 ? me.weapons[iGun]! : null
    const fireGun = !!G && ok && !(G.reload ?? 0)
    const fireLight = iLight >= 0 && ok && bd < 40
    return { aim: [a.yaw, a.pitch], fireGun, fireLight, walkTo: !tgt.npc && bd < 25 ? [tgt.p[0], tgt.p[1]] : null }
  }

  /** Mortar landings near our predicted position and bullets about to hit. */
  private dodge(obs: Observation, x: number, y: number, z: number, scale: number, vel: V3): V2 {
    let dx = 0, dy = 0
    const g = this.h.hello.config.weapons.mortar.gravity ?? 22
    const cz = z + 4.5 * scale
    for (const p of obs.projectiles) {
      if (p.kind === "mortar") {
        const L = shellLanding(this.h.m, p.pos, p.vel, g)
        if (!L || L.t > 3) continue
        const ex = x + vel[0] * L.t, ey = y + vel[1] * L.t
        const d = Math.hypot(L.x - ex, L.y - ey)
        if (d < 11) {
          const l = Math.max(0.5, Math.hypot(x - L.x, y - L.y))
          dx += (x - L.x) / l
          dy += (y - L.y) / l
        }
        continue
      }
      const rx = x - p.pos[0], ry = y - p.pos[1], rz = cz - p.pos[2]
      const vv = p.vel[0] ** 2 + p.vel[1] ** 2 + p.vel[2] ** 2
      if (vv < 1) continue
      const tc = (rx * p.vel[0] + ry * p.vel[1] + rz * p.vel[2]) / vv
      if (tc < 0 || tc > 0.9) continue
      const ox = p.pos[0] + p.vel[0] * tc - x, oy = p.pos[1] + p.vel[1] * tc - y, oz = p.pos[2] + p.vel[2] * tc - cz
      if (Math.hypot(ox, oy, 0.5 * oz) < 3.6 * scale) {
        // perpendicular to the bullet path, away from its closest point
        const hv = Math.hypot(p.vel[0], p.vel[1]) || 1
        let px = -p.vel[1] / hv, py = p.vel[0] / hv
        if (px * ox + py * oy > 0) (px = -px), (py = -py)
        dx += px
        dy += py
      }
    }
    return [dx, dy]
  }

  private idleAim(x: number, y: number, t: number, mv: V2): [number, number] {
    if (t < 3) {
      this.spin += 1.6
      return [this.spin, 0]
    }
    if (t - this.mechHitT < 2) return [this.mechHitBearing, 0]
    if (t - this.threatT < 3) return [this.threatBearing, 0]
    if (Math.hypot(mv[0], mv[1]) > 0.1) {
      this.phase += 0.1
      return [Math.atan2(mv[1], mv[0]) + 0.9 * Math.sin(1.3 * this.phase), 0]
    }
    this.spin += 0.6
    return [this.spin, 0]
  }
}
