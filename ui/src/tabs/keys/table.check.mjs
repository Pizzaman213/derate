// requires: coordinator
// The rows the keys table builds from `GET /api/keys` equal the server's rows
// one for one: same ids in the same order, same statuses, the same tightest
// limit, and near/at phases that agree with the server's own flags.
//
//   cd ui && DERATE_CHECK_ORIGIN=http://localhost:18120 node src/tabs/keys/table.check.mjs
// First, for its side effect: logs in and authenticates every fetch below.
import { ORIGIN } from '../../check/auth.mjs'
import { load, report } from '../../check/harness.mjs'

const R = await load(import.meta.url, './rows.ts')
const { check, note, done } = report()

const get = async (path) => {
  const res = await fetch(`${ORIGIN}${path}`)
  if (!res.ok) throw new Error(`${path}: ${res.status} ${await res.text()}`)
  return res.json()
}

const keys = await get('/api/keys')
const now = Date.now() / 1000
const rows = keys.map((k) => R.tableRow(k, now))

check(Array.isArray(keys) && keys.length > 0, `the coordinator lists keys (${keys.length})`)
check(rows.length === keys.length, 'one table row per server row')
check(rows.every((r, i) => r.keyId === keys[i].key_id), 'ids match, in the server order')
check(rows.every((r, i) => r.status === keys[i].status), 'statuses match')
check(rows.every((r, i) => r.admin === keys[i].is_admin_key), 'admin marks match')

for (const [i, k] of keys.entries()) {
  const states = k.limit_states ?? []
  const maxF = states.length ? Math.max(...states.map((s) => s.fraction)) : null
  const r = rows[i]
  check(r.tightestFraction === maxF, `${k.key_id}: tightest fraction ${r.tightestFraction} = server max ${maxF}`)
  for (const s of states) {
    const phase = R.limitPhase(s.fraction)
    check(
      (phase === 'near') === (s.near && !s.at) && (phase === 'at') === s.at,
      `${k.key_id} ${s.limit_type}: phase ${phase} agrees with server near=${s.near} at=${s.at}`,
    )
  }
  if (k.usage_30d == null) check(r.usage.requests === '—', `${k.key_id}: no archive -> em dash`)
  else check(r.usage.requests === R.count(k.usage_30d.requests), `${k.key_id}: 30-day requests ${r.usage.requests}`)
}

// A filtered list is the same rule applied to fewer rows.
const active = await get('/api/keys?status=active')
check(active.every((k) => k.status === 'active'), `status=active returns only active keys (${active.length})`)
note(`${keys.length} keys`)

done()
