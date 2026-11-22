// Where every downloaded model is, per node, as one decision per cell.
//
// A plain module, not part of the component, because `check.mjs` bundles with
// esbuild's `platform: 'neutral'` and cannot import React: `presence.check.mjs`
// is what holds the precedence below, and the precedence is the whole point --
// a half-finished derate copy is ALSO listed in that node's cache, and reading
// it as "present" is the one wrong answer this table could give.

import type {
  NodeStorage,
  NodeTransfer,
  ReplicationState,
} from '../../api/types'

/** What one model is on one node.
 *
 *  - `present`  the node's cache holds it (size = what its blobs hold)
 *  - `stale`    the node holds it at an older revision: its snapshots lack the
 *               commit the coordinator's `refs/main` names, or derate's record
 *               of the copy names another commit
 *  - `partial`  a copy derate placed and has not finished (`complete: false`)
 *  - `copying`  a copy to this node is running now
 *  - `proposed` derate proposed a copy here; it waits for a person's OK
 *  - `wontfit`  derate skipped it here because it could never be served there
 *  - `absent`   the node's cache was read and it is not there
 *  - `unknown`  the node's cache could not be read, so absence is not known */
export type PresenceState =
  | 'present'
  | 'stale'
  | 'partial'
  | 'copying'
  | 'proposed'
  | 'wontfit'
  | 'absent'
  | 'unknown'

export interface PresenceCell {
  state: PresenceState
  /** Bytes on that node's disk, when its cache lists the folder. */
  bytes: number | null
  /** `completed / total` of a running copy, when the copy knows its total. */
  fraction: number | null
  /** The server's own sentence behind the state, shown verbatim: the skip
   *  reason, the cache's unavailability reason, a proposal's last error. */
  note: string | null
}

export interface PresenceRow {
  folder: string
  repo_id: string
  /** Largest copy anywhere; rows sort on it. */
  bytes: number
  cells: Record<string, PresenceCell>
}

export interface PresenceTable {
  nodes: string[]
  rows: PresenceRow[]
}

/** The phrase a replication skip uses when the model cannot run on that node
 *  at all (transfer/replication.py). Any other skip -- no room right now, a
 *  node too old -- is not a property of the pair and stays out of the table. */
export const WONT_FIT_PHRASE = 'could never be served'

/** The note on a `stale` cell. */
export const STALE_NOTE = 'older revision'

const RUNNING: Record<NodeTransfer['status'], boolean> = {
  running: true,
  verifying: true,
  // Being stopped: what already landed shows as partial or present instead.
  cancelling: false,
  done: false,
  error: false,
}

const pair = (folder: string, node: string) => `${node}\u0000${folder}`

/** One row per model any node's cache lists, one column per node.
 *
 *  `coordinatorId` goes first (copies are sent from its cache); the rest are
 *  sorted. Rows are largest first, the same order the cache tables use. */
export function presenceTable(
  storageNodes: readonly NodeStorage[],
  replication: ReplicationState | null,
  transfers: readonly NodeTransfer[],
  coordinatorId?: string | null,
): PresenceTable {
  const nodes = storageNodes
    .map((n) => n.node_id)
    .sort((a, b) =>
      a === coordinatorId ? -1 : b === coordinatorId ? 1 : a < b ? -1 : a > b ? 1 : 0,
    )

  const repoIds = new Map<string, string>()
  const held = new Map<string, number>()
  const snapshots = new Map<string, readonly string[]>()
  const current = new Map<string, string>() // folder -> coordinator's refs/main
  const unreadable = new Map<string, string>()
  for (const n of storageNodes) {
    const cache = n.models
    if (!cache || !cache.available) {
      unreadable.set(n.node_id, cache?.reason ?? n.reason ?? 'its model cache was not read')
      continue
    }
    for (const r of cache.repos) {
      repoIds.set(r.folder, r.repo_id)
      held.set(pair(r.folder, n.node_id), r.bytes)
      snapshots.set(pair(r.folder, n.node_id), r.revisions)
      if (n.node_id === coordinatorId && r.commit) current.set(r.folder, r.commit)
    }
  }

  const partial = new Set<string>()
  const recorded = new Map<string, string>()
  for (const r of replication?.replicas ?? []) {
    if (!r.complete) partial.add(pair(r.folder, r.node_id))
    if (r.revision) recorded.set(pair(r.folder, r.node_id), r.revision)
  }

  const copying = new Map<string, number | null>()
  for (const t of transfers) {
    if (!RUNNING[t.status]) continue
    copying.set(pair(t.folder, t.node_id), t.total ? Math.min(1, t.completed / t.total) : null)
  }
  const proposed = new Map<string, string | null>()
  for (const p of replication?.proposals ?? []) {
    const k = pair(p.folder, p.node_id)
    if (p.status === 'copying') {
      if (!copying.has(k)) copying.set(k, null)
    } else {
      proposed.set(k, p.last_error)
    }
  }

  const wontFit = new Map<string, string>()
  for (const s of replication?.skipped ?? []) {
    if (s.sentence.includes(WONT_FIT_PHRASE)) wontFit.set(pair(s.folder, s.node_id), s.sentence)
  }

  const rows: PresenceRow[] = [...repoIds].map(([folder, repo_id]) => {
    let largest = 0
    const cells: Record<string, PresenceCell> = {}
    for (const node of nodes) {
      const k = pair(folder, node)
      const bytes = held.get(k) ?? null
      if (bytes != null && bytes > largest) largest = bytes
      cells[node] = cell(k, node, folder, bytes)
    }
    return { folder, repo_id, bytes: largest, cells }
  })
  rows.sort((a, b) => b.bytes - a.bytes || (a.repo_id < b.repo_id ? -1 : 1))
  return { nodes, rows }

  /** Unknown on either side is never stale: no commit on the coordinator,
   *  or nothing that names the node's, proves nothing. */
  function older(k: string, node: string, folder: string): boolean {
    const want = current.get(folder)
    if (want == null || node === coordinatorId) return false
    if (!(snapshots.get(k) ?? [want]).includes(want)) return true
    const rec = recorded.get(k)
    return rec != null && rec !== want
  }

  function cell(k: string, node: string, folder: string, bytes: number | null): PresenceCell {
    // A running copy outranks everything: its folder is already in the
    // target's cache listing, partially, and is in the replica record as
    // incomplete -- both true, neither what is happening.
    if (copying.has(k)) return { state: 'copying', bytes, fraction: copying.get(k) ?? null, note: null }
    if (partial.has(k)) return { state: 'partial', bytes, fraction: null, note: null }
    if (bytes != null && older(k, node, folder)) return { state: 'stale', bytes, fraction: null, note: STALE_NOTE }
    if (bytes != null) return { state: 'present', bytes, fraction: null, note: null }
    if (proposed.has(k)) return { state: 'proposed', bytes: null, fraction: null, note: proposed.get(k) ?? null }
    if (wontFit.has(k)) return { state: 'wontfit', bytes: null, fraction: null, note: wontFit.get(k) ?? null }
    const why = unreadable.get(node)
    if (why != null) return { state: 'unknown', bytes: null, fraction: null, note: why }
    return { state: 'absent', bytes: null, fraction: null, note: null }
  }
}

/** Why a Copy to `target` would fail before anybody clicks it, or null when
 *  the target's agent said it can take one. A gateway too old to report the
 *  receiver (no `copy_receiver`) blocks nothing: the click is then the only
 *  way to find out, which is what it was before. */
export function copyBlocked(storageNodes: readonly NodeStorage[], target: string): string | null {
  const row = storageNodes.find((n) => n.node_id === target)
  const receiver = row?.copy_receiver
  if (!receiver || receiver.available) return null
  return receiver.reason ?? `${target} cannot take a copy`
}
