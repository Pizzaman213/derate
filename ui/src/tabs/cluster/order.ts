// The persisted machine arrangement.
//
// What is stored is a PERMUTATION of node ids, not coordinates. Free x/y
// positions stop meaning anything the moment the viewport resizes or the
// cluster crosses a density tier and the card size changes, and they let a
// card sit on top of a link. A permutation survives both, keeps the floor
// composed, and is still enough to arrange the machines to match the rack.
//
// Ids of departed nodes are KEPT rather than pruned, so a machine that leaves
// and rejoins returns to the slot someone put it in. layout.ts filters them
// out when it places; nothing here has to know who is currently present.

const VERSION = 1
/** Enough for any cluster this ships against, plus a long tail of machines
 *  that have come and gone. Beyond it the oldest entries are dropped rather
 *  than letting a churny cluster grow the entry without bound. */
const MAX_IDS = 64

const key = (clusterId: string) => `derate.cluster.order.${clusterId || 'unknown'}`

/** Never throws. Private mode, a disabled store, a hand-edited value and a
 *  value written by a future version all mean the same thing here -- there is
 *  no arrangement, so use the deterministic default. */
export function readOrder(clusterId: string): string[] | null {
  try {
    const raw = window.localStorage.getItem(key(clusterId))
    if (!raw) return null
    const parsed: unknown = JSON.parse(raw)
    if (typeof parsed !== 'object' || parsed === null) return null
    const { v, order } = parsed as { v?: unknown; order?: unknown }
    if (v !== VERSION || !Array.isArray(order)) return null
    const ids = order.filter((id): id is string => typeof id === 'string' && id.length > 0)
    return ids.length ? [...new Set(ids)].slice(0, MAX_IDS) : null
  } catch {
    return null
  }
}

export function writeOrder(clusterId: string, order: string[]): void {
  try {
    const ids = [...new Set(order)].slice(0, MAX_IDS)
    window.localStorage.setItem(key(clusterId), JSON.stringify({ v: VERSION, order: ids }))
  } catch {
    // A layout preference is not worth failing a render over.
  }
}

export function clearOrder(clusterId: string): void {
  try {
    window.localStorage.removeItem(key(clusterId))
  } catch {
    // as above
  }
}
