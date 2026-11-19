// Where the operator has dragged each block, per cluster, in localStorage.
//
// A preference, not state the server knows about: losing it costs a re-deal
// of the column layout and nothing else. So anything that does not read back
// as exactly what was written -- garbage, another version, an absurd number
// of entries, a coordinate that is not a finite number -- is dropped rather
// than half-trusted.

import type { XY } from './doc'

export const VERSION = 1
/** More blocks than any real cluster draws. A record past this was not
 *  written by this code and is dropped whole. */
export const MAX_ENTRIES = 256

const key = (clusterId: string): string => `derate.wiring.positions.${clusterId}`

/** Parse a stored record. Pure, so positions.check.mjs can drive it. */
export function parsePositions(raw: string | null): Record<string, XY> {
  if (!raw) return {}
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    return {}
  }
  if (typeof parsed !== 'object' || parsed === null) return {}
  const { v, positions } = parsed as { v?: unknown; positions?: unknown }
  if (v !== VERSION || typeof positions !== 'object' || positions === null || Array.isArray(positions)) return {}
  const entries = Object.entries(positions as Record<string, unknown>)
  if (entries.length > MAX_ENTRIES) return {}
  const out: Record<string, XY> = {}
  for (const [id, xy] of entries) {
    const { x, y } = (xy ?? {}) as { x?: unknown; y?: unknown }
    if (typeof x === 'number' && typeof y === 'number' && Number.isFinite(x) && Number.isFinite(y)) out[id] = { x, y }
  }
  return out
}

export function readPositions(clusterId: string): Record<string, XY> {
  try {
    return parsePositions(window.localStorage.getItem(key(clusterId)))
  } catch {
    return {}
  }
}

export function writePositions(clusterId: string, positions: Record<string, XY>): void {
  try {
    if (Object.keys(positions).length === 0) window.localStorage.removeItem(key(clusterId))
    else window.localStorage.setItem(key(clusterId), JSON.stringify({ v: VERSION, positions }))
  } catch {
    // A full or disabled store loses the arrangement, never the editor.
  }
}
