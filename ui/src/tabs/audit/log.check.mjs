// requires: coordinator -- reads the live audit log
// The audit log as the coordinator serves it: newest first, and the action
// filter narrows the page rather than being ignored.
//
// `diff.ts` trusts both: the table renders entries in the order they arrive
// and pages with "Newer"/"Older". A server that returned oldest-first, or
// dropped the `action` parameter, would typecheck and render a plausible,
// wrong table.
//
//   cd ui && node src/tabs/audit/log.check.mjs
//   DERATE_CHECK_ORIGIN=http://localhost:18120 node src/tabs/audit/log.check.mjs

import '../../check/auth.mjs'
import { load, report } from '../../check/harness.mjs'

const ORIGIN = process.env.DERATE_CHECK_ORIGIN ?? 'http://localhost:8088'
const M = await load(import.meta.url, './diff.ts')
const { check, done } = report()

const get = async (path) => {
  const res = await fetch(`${ORIGIN}${path}`)
  if (!res.ok) throw new Error(`${path} answered ${res.status}: ${await res.text()}`)
  return res.json()
}

const newestFirst = (entries) =>
  entries.every((e, i) => i === 0 || entries[i - 1].ts > e.ts || (entries[i - 1].ts === e.ts && entries[i - 1].id > e.id))

const page = await get('/api/audit?page_size=5')
check(Array.isArray(page.entries) && typeof page.total === 'number', 'GET /api/audit answers {entries, total}')
check(page.entries.length <= 5, `page_size=5 returns at most 5 (${page.entries.length})`)
// The check admin signed in to run this, so there is at least one `login`.
check(page.entries.length > 0, `the log is not empty (total ${page.total})`)
check(newestFirst(page.entries), 'entries are newest first')
check(page.page === 1 && page.page_size === 5, 'the page echoes page and page_size')

for (const e of page.entries) {
  check(M.AUDIT_ACTIONS.includes(e.action), `action ${e.action} is in AUDIT_ACTIONS`)
  check(e.before === null || typeof e.before === 'object', `entry ${e.id}: before is an object or null`)
  check(e.after === null || typeof e.after === 'object', `entry ${e.id}: after is an object or null`)
}

const all = await get('/api/audit?page_size=1000')
check(newestFirst(all.entries), 'the full page is newest first too')
const counts = new Map()
for (const e of all.entries) counts.set(e.action, (counts.get(e.action) ?? 0) + 1)
const action = counts.has('login') ? 'login' : [...counts.keys()][0]
const filtered = await get(`/api/audit?page_size=1000&action=${action}`)
check(filtered.entries.length > 0, `action=${action} matches something`)
check(filtered.entries.every((e) => e.action === action), `action=${action} returns only ${action}`)
if (all.total <= all.entries.length) {
  check(filtered.total === counts.get(action), `action=${action} total ${filtered.total} == ${counts.get(action)} in the unfiltered log`)
}
check(
  counts.size < 2 || filtered.total < all.total,
  `the filter narrows (${filtered.total} of ${all.total})`,
)

// Page 2 continues page 1, no overlap.
if (page.total > 5) {
  const p2 = await get('/api/audit?page_size=5&page=2')
  const ids = new Set(page.entries.map((e) => e.id))
  check(p2.entries.every((e) => !ids.has(e.id)), 'page 2 does not repeat page 1')
  const last = page.entries.at(-1)
  check(p2.entries.every((e) => e.ts <= last.ts), 'page 2 is older than page 1')
}

done()
