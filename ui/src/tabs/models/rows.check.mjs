// Checks `rows.ts` against the live coordinator on :8088.
//
// The sibling of `tabs/cluster/layout.check.mjs`, and for the same reason:
// there is no test runner in `ui/`, typecheck is the only other gate, and the
// interesting part of this module is what it does to real payloads rather than
// whether it compiles. It esbuild-bundles the module and imports the bundle,
// because the repo's `.ts` files use extensionless specifiers that node's ESM
// resolver rejects.
//
//   cd ui && node src/tabs/models/rows.check.mjs
//
// Needs a coordinator on :8088. Every assertion below is about a fact the
// gateway reports, not a shape this file invented.
import { build } from 'esbuild'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const out = join(mkdtempSync(join(tmpdir(), 'rows-')), 'rows.mjs')
await build({ entryPoints: [new URL('rows.ts', import.meta.url).pathname], bundle: true, format: 'esm', outfile: out, logLevel: 'silent' })
const R = await import(out)
const get = async (p) => (await fetch(`http://localhost:8088${p}`)).json()

const [capacity, storage, catalog, hits, ladder] = await Promise.all([
  get('/api/capacity?context=8192&concurrency=1'),
  get('/api/storage'),
  get('/api/catalog'),
  get('/api/models/search?q=qwen3&limit=10'),
  get('/api/models/variants?model_id=Qwen/Qwen3-30B-A3B&context=8192&concurrency=1'),
])
const cap = R.capacityIndex(capacity), cache = R.cacheIndex(storage)
let fail = 0
const check = (ok, msg) => { console.log(`${ok ? 'PASS' : 'FAIL'}  ${msg}`); if (!ok) fail++ }

console.log(`basis=${cap.basis}\n`)

// --- catalog banding
const catRows = R.catalogRows(catalog, cap, cache)
const groups = R.groupRows(catRows, 'fit')
console.log('Recommended:')
for (const g of groups) console.log(`  [${g.title}] ${g.rows.length}: ${g.rows.map(r => r.label).join(', ')}`)
check(groups[0].title === 'Fits here', 'the first band shown is the one that runs')
const gated = catRows.find(r => r.model_id.startsWith('meta-llama'))
check(gated.verdict === null && !!gated.reason, 'an unresolvable catalog model has no verdict but does have a reason')
console.log(`  gated reason: ${gated.reason.slice(0, 80)}…`)

// --- ladder partition: the whole point
const ordered = [...ladder.variants].sort((a, b) => a.rank - b.rank || a.label.localeCompare(b.label))
const servable = ordered.filter(v => v.launchable)
const reference = ordered.filter(v => !v.launchable)
console.log(`\nLadder: ${ordered.length} variants -> ${servable.length} servable, ${reference.length} reference`)
console.log(`  gateway ranks of the servable rows: ${servable.map(v => v.rank).join(', ')}`)
check(servable.length > 0, 'at least one variant can actually be served')
check(Math.min(...servable.map(v => v.rank)) > 5, 'the gateway does rank dead rows above live ones (the defect this partitions around)')
const ranks = servable.map(v => v.rank)
check(ranks.every((r, i) => i === 0 || r > ranks[i - 1]), 'partitioning preserves the gateway rank order inside the group')
const rec = ladder.recommended
check(rec == null || servable.some(v => v.repo_id === rec.repo_id), 'the recommended row is in the servable group')
console.log(`  pick -> ${(rec ?? servable[0]).repo_id}`)

// --- completeness
const stub = 'Qwen/Qwen3-30B-A3B'
check(cache.complete(stub, 21740302912) === false, 'a 2 MB metadata stub is not reported as a finished download')
check(cache.complete('openai/gpt-oss-120b', 195_000_000_000) === true, 'a fully cached repo is')
check(cache.complete(stub, null) === null, 'with no expected size, completeness is unknown rather than guessed')

// --- hub
const hub = R.hubRows(hits, cache)
check(hub.every(r => r.verdict === null), 'no hub row carries a verdict')
check(new Set(hub.map(R.band)).size === 1 && R.band(hub[0]) === 'unchecked', 'every hub row bands as unchecked')

// --- filter
check(R.catalogRows(catalog, cap, cache).filter(r => R.matches(r, 'mxfp4')).length === 1, 'the filter reaches quantization text')

console.log(`\n${fail === 0 ? 'all checks passed' : fail + ' FAILED'}`)
process.exit(fail ? 1 : 0)
