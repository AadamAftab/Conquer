// Drives the Rust bot (automata) as a child process over FIFOs, so sim_rust.ts can use it in place of Brain.
// Replies are read with a timeout: a missing reply yields an empty action and is counted, instead of hanging the sim.
import { spawn, execSync, type ChildProcess } from "node:child_process"
import { openSync, readSync, writeSync, closeSync, constants } from "node:fs"

const BIN = process.env.AUTOMATA_BIN ?? "/usr/local/bin/automata"
const NL = String.fromCharCode(10)
let live: RustBrain | null = null
process.on("exit", () => live?.close())

export class RustBrain {
  static timeouts = 0
  state = "rust"
  private child: ChildProcess
  private fin: number
  private fout: number
  private buf = ""
  constructor(hello: unknown) {
    live?.close()
    live = this
    const id = process.pid + "_" + Math.random().toString(36).slice(2)
    const pin = `/tmp/rb_in_${id}`, pout = `/tmp/rb_out_${id}`
    execSync(`mkfifo ${pin} ${pout}`)
    this.fin = openSync(pin, constants.O_RDWR)
    this.fout = openSync(pout, constants.O_RDWR | constants.O_NONBLOCK)
    this.child = spawn(BIN, [], { stdio: [this.fin, this.fout, "inherit"] })
    execSync(`rm -f ${pin} ${pout}`)
    this.send(hello)
    if (this.line(20000) === null) throw new Error("rustbrain: no ready from the bot")
  }
  private send(o: unknown) {
    writeSync(this.fin, JSON.stringify(o) + NL)
  }
  private line(limitMs = 2000): string | null {
    const chunk = Buffer.alloc(1 << 16)
    const t0 = Date.now()
    const nap = new Int32Array(new SharedArrayBuffer(4))
    for (;;) {
      const i = this.buf.indexOf(NL)
      if (i >= 0) {
        const l = this.buf.slice(0, i)
        this.buf = this.buf.slice(i + 1)
        return l
      }
      try {
        const n = readSync(this.fout, chunk, 0, chunk.length, null)
        if (n > 0) this.buf += chunk.toString("utf8", 0, n)
      } catch (e: any) {
        if (e.code !== "EAGAIN") throw e
        if (Date.now() - t0 > limitMs) return null
        Atomics.wait(nap, 0, 0, 1)
      }
    }
  }
  act(obs: unknown): any {
    this.send(obs)
    const l = this.line()
    if (l === null) {
      RustBrain.timeouts++
      writeSync(2, `rustbrain: no reply for tick ${(obs as any).tick} (total ${RustBrain.timeouts})` + NL)
      return { tick: (obs as any).tick }
    }
    return JSON.parse(l)
  }
  close() {
    try { this.child.kill("SIGKILL") } catch {}
    try { closeSync(this.fin) } catch {}
    try { closeSync(this.fout) } catch {}
    if (live === this) live = null
  }
}
