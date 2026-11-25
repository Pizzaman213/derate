import type { GpuProcessDTO } from '../../api/types'
import { OTHER_USE_FLOOR } from '../models/board'
import { TERMINAL } from '../models/rows'

/** One slice of a machine's used memory. `managed` is true for a process
 *  serving a live derate deployment, false for anything else holding memory,
 *  and null for the remainder no process accounts for (the OS, page cache,
 *  anything not on the GPU list). `held` is what the process list reported;
 *  `bytes` is what the bar may draw of it (see `memoryParts`). */
export interface MemoryPart {
  key: string
  label: string
  bytes: number
  held: number
  managed: boolean | null
}

/** Parts past this many fold into one "N more processes", so a busy machine
 *  does not grow its block over the one below it. */
export const MAX_LISTED = 5

/** Splits a machine's sampled `used` bytes by the processes holding GPU
 *  memory, as `GET /api/nodes/{id}/processes` reports them. Every process
 *  appears, whether or not derate launched it: a process nothing here manages
 *  is exactly what an unexplained bar would otherwise hide. A live
 *  deployment's processes (vLLM's API server and engine core) fold into one
 *  part under its served name; one left under a stopped or failed record
 *  is an orphan and is named as one. The two readings are taken apart, so
 *  `bytes` is clamped to keep the bar's sum within `used` -- the bar draws a
 *  reading, not a claim -- while `held` keeps each process listed at its real
 *  size. The remainder obeys the node board's `OTHER_USE_FLOOR`. `procs` null
 *  means the list was not read; then `used` stays one unattributed part
 *  rather than claiming nothing is resident. */
export function memoryParts(used: number, procs: GpuProcessDTO[] | null): MemoryPart[] {
  const byKey = new Map<string, Omit<MemoryPart, 'bytes'>>()
  for (const p of procs ?? []) {
    if (!Number.isFinite(p.gpu_memory) || p.gpu_memory <= 0) continue
    const managed = p.deployment_id != null && p.deployment_state != null && !TERMINAL.has(p.deployment_state)
    const key = managed ? `dep:${p.deployment_id}` : `pid:${p.pid}`
    const label = managed
      ? p.served_name || p.deployment_id || 'a derate deployment'
      : p.deployment_id != null
        ? `${p.name} (pid ${p.pid}, left by ${p.deployment_state ?? 'ended'} ${p.served_name || p.deployment_id})`
        : `${p.name} (pid ${p.pid}, not managed here)`
    const part = byKey.get(key)
    if (part) part.held += p.gpu_memory
    else byKey.set(key, { key, label, held: p.gpu_memory, managed })
  }
  // Biggest first, managed or not: the fold must never hide the largest holder
  // behind "N more", and the stray is often exactly that.
  const sorted = [...byKey.values()].sort((a, b) => b.held - a.held || a.key.localeCompare(b.key))
  const listed = sorted.slice(0, MAX_LISTED)
  const tail = sorted.slice(MAX_LISTED)
  if (tail.length > 0) {
    const held = tail.reduce((a, p) => a + p.held, 0)
    listed.push({ key: 'more', label: `${tail.length} more processes`, held, managed: tail.every((p) => p.managed) })
  }
  const out: MemoryPart[] = []
  let left = Number.isFinite(used) ? Math.max(0, used) : 0
  for (const p of listed) {
    const bytes = Math.min(p.held, left)
    out.push({ ...p, bytes })
    left -= bytes
  }
  const label = procs == null ? 'used (processes not read)' : 'not held by a GPU process'
  // Unread, the whole reading is the remainder and is drawn whatever its size.
  if (left > (procs == null ? 0 : OTHER_USE_FLOOR)) out.push({ key: 'rest', label, bytes: left, held: left, managed: null })
  return out
}
