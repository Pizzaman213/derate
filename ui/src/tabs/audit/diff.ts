// The audit log's before -> after, reduced to what changed.
//
// Pure, so `audit.check.mjs` can load it without a DOM. The server writes the
// whole object on each side (`authz.key_dict`, `authz.org_dict`, a
// `LocalPrice`); a row that showed both dicts in full would bury the one field
// somebody edited under twenty that did not move.
//
// Absent and null are different facts and stay different here: an org whose
// `allowed_models` went from `["a"]` to `null` now inherits everything
// `/v1/models` exposes, which is not the same as the field having been
// removed. Absent renders as an em dash; null renders as `null`.

import type { AuditAction, AuditEntry, AuditQuery } from '../../api/types'

export type ChangeKind = 'added' | 'removed' | 'changed'

export interface Change {
  /** Dotted path into nested dicts: `limits.rpm`. */
  path: string
  kind: ChangeKind
  /** `undefined` = absent on that side, which is not the same as null. */
  before: unknown
  after: unknown
}

export const ABSENT = '\u2014'

function isDict(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

/** Deep equality over JSON values: what the server could have written. */
function same(a: unknown, b: unknown): boolean {
  if (a === b) return true
  if (Array.isArray(a) && Array.isArray(b)) {
    return a.length === b.length && a.every((v, i) => same(v, b[i]))
  }
  if (isDict(a) && isDict(b)) {
    const ka = Object.keys(a)
    const kb = Object.keys(b)
    return ka.length === kb.length && ka.every((k) => Object.prototype.hasOwnProperty.call(b, k) && same(a[k], b[k]))
  }
  return false
}

function walk(path: string, b: unknown, a: unknown, out: Change[]): void {
  // Recurse only where both sides are dicts. An array is one value: a model
  // allowlist that gained a member is one change, not an index-by-index list.
  if (isDict(b) && isDict(a)) {
    const keys = new Set([...Object.keys(b), ...Object.keys(a)])
    for (const k of keys) {
      const p = path ? `${path}.${k}` : k
      const inB = Object.prototype.hasOwnProperty.call(b, k)
      const inA = Object.prototype.hasOwnProperty.call(a, k)
      if (inB && inA) walk(p, b[k], a[k], out)
      else if (inA) flatten(p, a[k], 'added', out)
      else flatten(p, b[k], 'removed', out)
    }
    return
  }
  if (!same(b, a)) out.push({ path, kind: 'changed', before: b, after: a })
}

/** One side only: every leaf under it, so a created org lists `limits.rpm`
 *  rather than a `limits` blob. An empty dict is still a leaf -- `{}` was
 *  written, and saying nothing would hide that the field exists. */
function flatten(path: string, v: unknown, kind: 'added' | 'removed', out: Change[]): void {
  if (isDict(v) && Object.keys(v).length > 0) {
    for (const [k, inner] of Object.entries(v)) flatten(path ? `${path}.${k}` : k, inner, kind, out)
    return
  }
  out.push(
    kind === 'added'
      ? { path, kind, before: undefined, after: v }
      : { path, kind, before: v, after: undefined },
  )
}

/** The fields that differ between `before` and `after`, sorted by path.
 *
 *  A null side is "the object did not exist" (a create has no before, a delete
 *  no after), so every field on the other side is added or removed. Both null,
 *  or two equal objects (a login, a reset), is no changes at all -- the row
 *  still renders, its action says what happened. */
export function diffEntry(
  before: Record<string, unknown> | null | undefined,
  after: Record<string, unknown> | null | undefined,
): Change[] {
  const out: Change[] = []
  walk('', before ?? {}, after ?? {}, out)
  return out.sort((x, y) => (x.path < y.path ? -1 : x.path > y.path ? 1 : 0))
}

/** One value for a mono cell. Strings are quoted so `"null"` and `null`, or
 *  `"5"` and `5`, cannot be mistaken for each other. */
export function formatValue(v: unknown): string {
  if (v === undefined) return ABSENT
  if (typeof v === 'string') return JSON.stringify(v)
  if (typeof v === 'number' || typeof v === 'boolean' || v === null) return String(v)
  return JSON.stringify(v)
}

// ── Filters ─────────────────────────────────────────────────────────────────

export type AuditRange = '24h' | '7d' | '30d' | 'all'
export const AUDIT_RANGES: readonly AuditRange[] = ['24h', '7d', '30d', 'all']

const RANGE_S: Record<Exclude<AuditRange, 'all'>, number> = {
  '24h': 86400,
  '7d': 7 * 86400,
  '30d': 30 * 86400,
}

/** Every `AuditAction`, in the contract's order, for the filter select. The
 *  `Record` makes a new member in `types.ts` a type error here until it is
 *  listed, rather than an action nobody can filter by. */
const ACTION_ORDER: Record<AuditAction, number> = {
  login: 0,
  login_failed: 1,
  logout: 2,
  password_changed: 3,
  admin_added: 4,
  admin_removed: 5,
  setup_admin_created: 6,
  org_created: 7,
  org_updated: 8,
  org_deleted: 9,
  key_created: 10,
  key_updated: 11,
  key_disabled: 12,
  key_enabled: 13,
  key_rotated: 14,
  key_deleted: 15,
  limit_changed: 16,
  limit_raised: 17,
  cycle_reset: 18,
  pricing_changed: 19,
  api_write: 20,
  shell_opened: 21,
  shell_closed: 22,
  provider_created: 23,
  provider_updated: 24,
  provider_deleted: 25,
  settings_changed: 26,
  enrollment_minted: 27,
  enrollment_revoked: 28,
  deployment_launched: 29,
  deployment_stopped: 30,
  node_removed: 31,
  node_admitted: 32,
  node_process_killed: 33,
  routing_changed: 34,
  storage_deleted: 35,
  sessions_revoked: 36,
  node_joined: 37,
  provider_signed_in: 38,
  profile_changed: 39,
  download_changed: 40,
  benchmark_changed: 41,
  login_locked: 42,
  key_refused: 43,
  session_refused: 44,
  setup_refused: 45,
  join_rejected: 46,
  cross_origin_refused: 47,
  session_ip_changed: 48,
  audit_chain_broken: 49,
  agent_refused: 50,
  cluster_token_rotated: 51,
  node_runtime_model_changed: 52,
}
export const AUDIT_ACTIONS: readonly AuditAction[] = (Object.keys(ACTION_ORDER) as AuditAction[]).sort(
  (a, b) => ACTION_ORDER[a] - ACTION_ORDER[b],
)

export const AUDIT_PAGE_SIZE = 50

export interface AuditFilter {
  actor: string
  action: AuditAction | ''
  target: string
  range: AuditRange
  page: number
}

/** The query `backend.audit` sends. Empty text filters are omitted rather
 *  than sent blank; `start` is epoch seconds, measured from `now`. */
export function auditQuery(f: AuditFilter, now: number): AuditQuery {
  const q: AuditQuery = { page: Math.max(1, f.page), page_size: AUDIT_PAGE_SIZE }
  const actor = f.actor.trim()
  const target = f.target.trim()
  if (actor) q.actor = actor
  if (f.action) q.action = f.action
  if (target) q.target = target
  if (f.range !== 'all') q.start = Math.floor(now - RANGE_S[f.range])
  return q
}

/** Number of pages for `total` rows; at least one, so "page 1 of 1" holds
 *  for an empty result. */
export function pageCount(total: number, pageSize: number): number {
  return Math.max(1, Math.ceil(total / Math.max(1, pageSize)))
}

/** Where a target id links: a key has a screen of its own; orgs, admins,
 *  pricing rows and sessions do not. */
export function targetDest(kind: string, id: string): { dest: 'usage'; sub: 'keys'; key: string } | null {
  if (!id) return null
  if (kind === 'key') return { dest: 'usage', sub: 'keys', key: id }
  return null
}

/** One change as one line of text: `limits.rpm 60 → 120`. The log row shows
 *  the first one (clipped) and a count of the rest; its tooltip shows them
 *  all, one per line. */
export function changeText(c: Change): string {
  return `${c.path} ${formatValue(c.before)} \u2192 ${formatValue(c.after)}`
}

// ── Actor drill-down ────────────────────────────────────────────────────────
//
// The server matches `actor` exactly (`auth/service.py::audit_page`), so it is
// passed verbatim: trimming would query a different account. No `start`, so
// every query here spans all time -- the point is re-checking everything an
// account ever did.

/** `page_size` cap in `gateway/keys_api.py::audit_log`. */
export const ACTOR_KEYS_MAX = 1000
export const ACTOR_IPS_MAX = 1000

/** One page of everything `actor` did, newest first. */
export function actorEventsQuery(actor: string, page: number): AuditQuery {
  return { actor, page: Math.max(1, page), page_size: AUDIT_PAGE_SIZE }
}

/** Every key `actor` created, as the audit log recorded it. */
export function actorKeysQuery(actor: string): AuditQuery {
  return { actor, action: 'key_created', page: 1, page_size: ACTOR_KEYS_MAX }
}

/** The newest `ACTOR_IPS_MAX` events of `actor`, to summarise by address. */
export function actorIpsQuery(actor: string): AuditQuery {
  return { actor, page: 1, page_size: ACTOR_IPS_MAX }
}

/** One address an actor acted from. `first`/`last` are epoch seconds. */
export interface IpSeen {
  ip: string | null
  count: number
  first: number
  last: number
}

/** Events grouped by IP (null -- not recorded -- is its own group), most
 *  recently seen first; ties by address, null last. */
export function ipSummary(entries: readonly AuditEntry[]): IpSeen[] {
  const by = new Map<string | null, IpSeen>()
  for (const e of entries) {
    const ip = e.ip ?? null
    const s = by.get(ip)
    if (s) {
      s.count += 1
      if (e.ts < s.first) s.first = e.ts
      if (e.ts > s.last) s.last = e.ts
    } else {
      by.set(ip, { ip, count: 1, first: e.ts, last: e.ts })
    }
  }
  return [...by.values()].sort((a, b) => {
    if (a.last !== b.last) return b.last - a.last
    if (a.ip === b.ip) return 0
    if (a.ip === null) return 1
    if (b.ip === null) return -1
    return a.ip < b.ip ? -1 : 1
  })
}
