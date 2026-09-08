import { OFFSET_LIMIT, type Point } from './layout'

// The persisted machine arrangement, per cluster. Two things, one record,
// because they are one preference and `Reset layout` has to clear both at once.
//
//   order    a PERMUTATION of node ids: which slot each machine is dealt on
//            the default grid. Ids of departed nodes are KEPT rather than
//            pruned, so a machine that leaves and rejoins returns to the slot
//            someone put it in. layout.ts filters them out when it places;
//            nothing here has to know who is currently present.
//
//   offsets  how far each machine has been DRAGGED from that slot, in the
//            authored units layout.ts works in.
//
// The offsets are displacements and not absolute coordinates, which is what
// makes free placement storable at all. This file used to refuse to store one:
// "free x/y positions stop meaning anything the moment the viewport resizes or
// the cluster crosses a density tier and the card size changes". That is true
// of an absolute position and false of a displacement -- an offset travels
// with the slot through a resize and through a tier change, so a floor
// arranged to match the rack still matches it on a narrower window.
//
// What it does not survive is the machine set changing shape underneath it: a
// node joining re-deals the slots, and a plate hand-placed relative to the old
// one lands somewhere nobody chose. That is the honest cost of the feature,
// and `Reset layout` is the way out of it.

/** Bumping this DISCARDS every stored arrangement, so `offsets` was added
 *  without touching it: a record written before offsets existed is a valid
 *  record with none, which is exactly what it meant. */
const VERSION = 1
/** Enough for any cluster this ships against, plus a long tail of machines
 *  that have come and gone. Beyond it the oldest entries are dropped rather
 *  than letting a churny cluster grow the entry without bound. */
const MAX_IDS = 64

export interface Arrangement {
  /** null = nobody has reordered; use the deterministic default. */
  order: string[] | null
  /** Keyed by node id. Absent for a machine nobody has moved. */
  offsets: Record<string, Point>
}

/** The arrangement of a floor nobody has touched. A module constant, so a
 *  caller with nothing stored hands React the same identity every render
 *  rather than a fresh object that re-runs every memo keyed on it. */
export const NO_ARRANGEMENT: Arrangement = { order: null, offsets: {} }

export function isArranged(a: Arrangement): boolean {
  return a.order != null || Object.keys(a.offsets).length > 0
}

const key = (clusterId: string) => `derate.cluster.order.${clusterId || 'unknown'}`

/** Never throws. Private mode, a disabled store, a hand-edited value and a
 *  value written by a future version all mean the same thing here -- there is
 *  no arrangement, so use the deterministic default. */
export function readArrangement(clusterId: string): Arrangement {
  try {
    const raw = window.localStorage.getItem(key(clusterId))
    if (!raw) return NO_ARRANGEMENT
    const parsed: unknown = JSON.parse(raw)
    if (typeof parsed !== 'object' || parsed === null) return NO_ARRANGEMENT
    const { v, order, offsets } = parsed as { v?: unknown; order?: unknown; offsets?: unknown }
    if (v !== VERSION) return NO_ARRANGEMENT
    return { order: readOrderField(order), offsets: readOffsetsField(offsets) }
  } catch {
    return NO_ARRANGEMENT
  }
}

export function writeArrangement(clusterId: string, next: Arrangement): void {
  try {
    window.localStorage.setItem(
      key(clusterId),
      JSON.stringify({
        v: VERSION,
        order: next.order ? [...new Set(next.order)].slice(0, MAX_IDS) : [],
        offsets: cleanOffsets(next.offsets),
      }),
    )
  } catch {
    // A layout preference is not worth failing a render over.
  }
}

export function clearArrangement(clusterId: string): void {
  try {
    window.localStorage.removeItem(key(clusterId))
  } catch {
    // as above
  }
}

function readOrderField(order: unknown): string[] | null {
  if (!Array.isArray(order)) return null
  const ids = order.filter((id): id is string => typeof id === 'string' && id.length > 0)
  return ids.length ? [...new Set(ids)].slice(0, MAX_IDS) : null
}

/** Same forgiveness as the order field: anything that is not a pair of finite
 *  numbers is a machine nobody moved, not a floor that fails to draw. */
function readOffsetsField(offsets: unknown): Record<string, Point> {
  if (typeof offsets !== 'object' || offsets === null || Array.isArray(offsets)) return {}
  return cleanOffsets(offsets as Record<string, unknown>)
}

/** Drops the machines that are back home and bounds the rest, on the way in
 *  AND on the way out. A stored `{x: 0, y: 0}` would make `isArranged` claim
 *  an arrangement -- and light the Reset button -- for a floor that has none. */
function cleanOffsets(offsets: Record<string, unknown>): Record<string, Point> {
  const out: Record<string, Point> = {}
  let kept = 0
  for (const [id, value] of Object.entries(offsets)) {
    if (kept >= MAX_IDS) break
    if (typeof value !== 'object' || value === null) continue
    const { x, y } = value as { x?: unknown; y?: unknown }
    if (typeof x !== 'number' || typeof y !== 'number') continue
    if (!Number.isFinite(x) || !Number.isFinite(y)) continue
    const p = { x: bound(x), y: bound(y) }
    if (p.x === 0 && p.y === 0) continue
    out[id] = p
    kept++
  }
  return out
}

const bound = (v: number) =>
  Math.round(Math.max(-OFFSET_LIMIT, Math.min(OFFSET_LIMIT, v)) * 10) / 10
