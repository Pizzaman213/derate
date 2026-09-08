// requires: coordinator -- checks the fold that moved to the server
// Checks `GET /api/models` and the two functions that turn it into rows.
//
// The sibling of `rows.check.mjs`, and split from it deliberately. That file
// checks the browser's own fold; this one checks the fold that moved to the
// server, and the boundary between them. Both matter, and the boundary is
// where a new class of bug lives: a source silently dropping out of the
// server-side merge is invisible on a screen that still looks full.
//
//   cd ui && node src/tabs/models/registry.check.mjs
//   DERATE_CHECK_ORIGIN=http://localhost:18088 node src/tabs/models/registry.check.mjs
//
// Needs a coordinator that serves /api/models: :8088 by default, or whatever
// DERATE_CHECK_ORIGIN names. Every assertion is about a fact the gateway
// reports, not a shape this file invented.
import { build } from 'esbuild'
import { fileURLToPath } from 'node:url'

const ORIGIN = process.env.DERATE_CHECK_ORIGIN || 'http://localhost:8088'
const entry = fileURLToPath(new URL('./rows.ts', import.meta.url))

const out = await build({
  entryPoints: [entry],
  bundle: true,
  write: false,
  format: 'esm',
  platform: 'neutral',
  logLevel: 'silent',
})
const R = await import(
  'data:text/javascript;base64,' +
    Buffer.from(out.outputFiles[0].text).toString('base64')
)

const get = async (p) => {
  const r = await fetch(ORIGIN + p)
  if (!r.ok) throw new Error(`${p} -> ${r.status}`)
  return r.json()
}

let failed = 0
const check = (cond, msg) => {
  console.log((cond ? 'PASS  ' : 'FAIL  ') + msg)
  if (!cond) failed++
}

const reg = await get('/api/models')
const rows = R.registryRows(reg)

console.log('\n--- the payload ---')
check(Array.isArray(reg.models), 'answers a model list')
check(reg.schema_version >= 1, 'names the schema it was written against')
check(typeof reg.revision === 'number', 'carries a revision, so a reader can tell a change from a re-poll')
check(
  reg.sources && typeof reg.sources === 'object',
  'names its contributing feeds -- folding five fetches into one leaves no failed request for the browser to notice, so the sentence has to travel here',
)

console.log('\n--- the merge is the union of what it replaced ---')
// The check. A source dropping out of the server-side fold does not empty the
// screen, it quietly shortens it, and nothing else in the product would say so.
const want = new Set()
for (const d of await get('/api/deployments')) if (d.model_id) want.add(d.model_id)
for (const c of await get('/api/catalog')) want.add(c.model_id)
for (const p of await get('/api/providers')) {
  for (const m of p.models ?? []) want.add(m.upstream_id)
  for (const m of await get(`/api/providers/${p.provider_id}/models`)) want.add(m.upstream_id)
}
const have = new Set(reg.models.map((m) => m.model_id))
const missing = [...want].filter((id) => !have.has(id))
check(missing.length === 0, `every model its sources name is in the registry (${want.size} checked)${missing.length ? ': missing ' + missing.slice(0, 5).join(', ') : ''}`)

console.log('\n--- identity ---')
check(have.size === reg.models.length, 'no duplicate model_id')
check(
  reg.models.every((m) => m.where.length > 0),
  'every row carries provenance -- a row with none exists for no reason anybody can name, and the band it would draw in is undefined',
)
check(
  reg.models.every((m) => !m.where.includes('hub')),
  'no row claims the hub facet: /api/models/search resolves nothing, so its hits are a query\'s answer rather than a fact about this cluster, and the browser adds that facet',
)

console.log('\n--- the fit boundary ---')
// The whole reason the registry and /api/capacity are separate endpoints. A
// verdict here would give the screen two sources of it that can disagree.
const fitFields = [
  'verdict',
  'reason',
  'basis',
  'predicted_decode_tps',
  'headroom',
  'total_params',
  'dtype',
  'requantized',
  'warnings',
]
check(
  reg.models.every((m) => fitFields.every((f) => !(f in m))),
  'no fit answer reaches the wire -- a verdict is a function of (model, context, concurrency, nodes) and this endpoint is asked none of them',
)
check(
  rows.every((r) => r.verdict === null && r.total_params === null),
  'and so a registry row carries none before /api/capacity is joined on',
)

console.log('\n--- credentials ---')
// Asked over field NAMES, not by searching the text: a substring test would
// miss `api_key: '***'` -- a key slot on every provider row -- and would fire
// on a provider whose `last_error` correctly names the reference that failed
// to resolve. The reference NAME is deliberately not secret; the slot is.
const banned = new Set(['api_key', 'api_key_ref', 'base_url', 'backend_url', 'secret', 'token'])
const slots = []
const walk = (node, path) => {
  if (Array.isArray(node)) node.forEach((v, i) => walk(v, `${path}[${i}]`))
  else if (node && typeof node === 'object')
    for (const [k, v] of Object.entries(node)) {
      if (banned.has(k)) slots.push(`${path}.${k}`)
      walk(v, `${path}.${k}`)
    }
}
walk(reg, '$')
check(slots.length === 0, `no credential slot anywhere in the payload${slots.length ? ': ' + slots.slice(0, 3).join(', ') : ''}`)

console.log('\n--- served, and merely published ---')
check(
  reg.models.every((m) => {
    const served = new Set(m.providers.map((p) => p.provider_id))
    return m.offers.every((o) => !served.has(o.provider_id))
  }),
  'no offer from a provider that already serves the model -- the browser had to filter these apart because its two feeds polled 15s and 300s apart; one read now decides it',
)
check(
  reg.models.every((m) => (m.where.length === 1 && m.where[0] === 'offered' ? m.served_names.length === 0 : true)),
  'an un-served row claims no served name: it does not answer at /v1 yet',
)
// Cross-endpoint, the same species as allowlist.check.mjs.
for (const p of await get('/api/providers')) {
  const enabled = new Set((p.models ?? []).map((m) => m.upstream_id))
  const servedHere = new Set(
    reg.models.filter((m) => m.providers.some((x) => x.provider_id === p.provider_id)).map((m) => m.model_id),
  )
  check(
    [...enabled].every((id) => servedHere.has(id)),
    `every model ${p.provider_id} serves is served in the registry (${enabled.size})`,
  )
}

console.log('\n--- weights on disk ---')
const storage = await get('/api/storage')
const onDisk = new Set()
for (const n of storage.nodes ?? [])
  for (const r of (n.models ?? {}).repos ?? []) if (r.blob_count !== 0) onDisk.add(r.repo_id)
check(
  [...onDisk].every((id) => have.has(id)),
  `every downloaded repository has a row (${onDisk.size})`,
)
check(
  reg.models.every((m) => (m.cached_on.length > 0) === m.where.includes('ondisk')),
  'the ondisk facet and cached_on say the same thing',
)
check(
  reg.models.every((m) => m.bytes_on_disk === null || m.bytes_on_disk > 0),
  'bytes_on_disk is null when nothing is cached, never 0 -- a zero reads as an empty download rather than as no reading',
)
const cacheNodes = reg.sources?.cache?.nodes ?? []
check(
  cacheNodes.every((n) => n.available || n.reason !== null || n.observed_at === null),
  'a node whose cache could not be read says why rather than reporting nothing',
)

console.log('\n--- rows, and the banding that survives ---')
const decorated = R.decorate(rows, R.capacityIndex(null), R.cacheIndex(null), new Set())
for (const { id } of R.SORTS) {
  const groups = R.groupRows(decorated, id)
  check(
    groups.reduce((n, g) => n + g.rows.length, 0) === decorated.length,
    `banding loses no row under sort=${id}`,
  )
  check(groups.every((g) => R.BAND_TITLE[g.band]), `every band has a title under sort=${id}`)
}
check(
  decorated.every((r) => r.remoteOnly === (r.where.length === 1 && r.where[0] === 'provider')),
  'remoteOnly is derived from where, not sent',
)
check(
  decorated.every((r) => r.unservedOnly === (r.where.length === 1 && r.where[0] === 'offered')),
  'unservedOnly likewise',
)
check(
  decorated.filter((r) => r.unservedOnly).every((r) => R.band(r) === 'unserved'),
  'an un-served row bands as not served, which is the band that draws collapsed',
)

console.log('\n--- the hub fold, the one merge left in the browser ---')
const hits = await get('/api/models/search?q=llama')
const folded = R.withHubHits(rows, hits)
check(
  new Set(folded.map((r) => r.model_id)).size === folded.length,
  'folding hub hits introduces no duplicate',
)
check(folded.length >= rows.length, 'and loses no registry row')
check(
  R.withHubHits(folded, hits).every((r) => r.where.filter((w) => w === 'hub').length <= 1),
  'folding twice does not double-append the hub facet',
)
check(
  folded.every((r) => r.servedNames.every((n) => typeof n === 'string')),
  'a hub hit contributes no served name of its own',
)

console.log(failed ? `\n${failed} FAILED` : `\nall passed (${reg.models.length} models)`)
process.exit(failed ? 1 : 0)
