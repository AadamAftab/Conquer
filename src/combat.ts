import type { SelfObs } from "../mechfight.ts"
import type { GameMap } from "./map.ts"
import type { Track } from "./world.ts"
import { angDiff, clamp, type V3 } from "./geom.ts"

export type Aim = { yaw: number; pitch: number; dist: number; pt: V3 }

/** Scale factor of a mech with `cubes` power cubes. */
export const sizeOf = (cubes: number, perCube: number) => 1 + perCube * cubes

/** Center height offset of a target above its feet. */
export function centerOffset(tr: Track, perCube: number) {
  return tr.kind === "npc" ? 1.75 : 4.5 * sizeOf(tr.cubes, perCube)
}

/** Predicted feet position of a track after `dt` seconds. */
export function predict(m: GameMap, tr: Track, dt: number, g: number): V3 {
  const [px, py, pz] = tr.pos
  const [vx, vy, vz] = tr.vel
  const x = px + vx * dt, y = py + vy * dt
  let z = pz + vz * dt
  if (Math.abs(vz) > 0.4) {
    z = pz + vz * dt - 0.5 * g * dt * dt
    const floor = m.surfAt(x, y)
    if (z < floor) z = floor
  }
  return [x, y, z]
}

/** Aim at a point. */
export function aimAt(o: V3, p: V3): Aim {
  const dx = p[0] - o[0], dy = p[1] - o[1], dz = p[2] - o[2]
  const h = Math.hypot(dx, dy)
  return { yaw: Math.atan2(dy, dx), pitch: Math.atan2(dz, h), dist: Math.hypot(h, dz), pt: p }
}

/** Lead a straight-flying projectile of `speed` from `o` onto track `tr`. */
export function leadAim(m: GameMap, o: V3, tr: Track, speed: number, latency: number, zOff: number, g: number): Aim {
  let tt = Math.hypot(tr.pos[0] - o[0], tr.pos[1] - o[1], tr.pos[2] + zOff - o[2]) / speed
  let p: V3 = tr.pos
  for (let k = 0; k < 3; k++) {
    const f = predict(m, tr, tt + latency, g)
    p = [f[0], f[1], f[2] + zOff]
    tt = Math.hypot(p[0] - o[0], p[1] - o[1], p[2] - o[2]) / speed
  }
  return aimAt(o, p)
}

/** Residual miss distance (m) of our actual aim against a desired aim, after `dt` s of slewing. */
export function missDist(me: SelfObs, a: Aim, dt: number): number {
  const tr = me.stats.turn_rate ?? 4, pr = me.stats.pitch_rate ?? 3
  const ey = Math.max(0, Math.abs(angDiff(a.yaw, me.aim[0])) - tr * dt)
  const ep = Math.max(0, Math.abs(a.pitch - me.aim[1]) - pr * dt)
  return Math.hypot(ey, ep) * a.dist
}

export function clampPitch(me: SelfObs, p: number) {
  return clamp(p, me.stats.min_pitch ?? -0.7, me.stats.max_pitch ?? 1.3)
}

/** Landing point and time of a mortar shell (or null if it won't land within 8 s). */
export function shellLanding(m: GameMap, pos: V3, vel: V3, g: number): { x: number; y: number; z: number; t: number } | null {
  let [x, y, z] = pos
  let [vx, vy, vz] = vel
  const dt = 0.05
  for (let t = 0; t < 8; t += dt) {
    x += vx * dt
    y += vy * dt
    z += vz * dt - 0.5 * g * dt * dt
    vz -= g * dt
    if (vz < 0 && z <= m.surfAt(x, y)) return { x, y, z, t }
  }
  return null
}
