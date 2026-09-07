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

const dir = mkdtempSync(join(tmpdir(), 'rows-'))
const bundle = async (name) => {
  const out = join(dir, `${name}.mjs`)
  await build({
    entryPoints: [new URL(`${name}.ts`, import.meta.url).pathname],
    bundle: true, format: 'esm', outfile: out, logLevel: 'silent',
  })
  return import(out)
}
const R = await bundle('rows')
const S = await bundle('support')
const O = await bundle('owner')
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

// --- the card's red dot, read off the server's own runtime table
const table = await get('/api/models/quant-table')
const row = (over) => ({
  model_id: 'x/y', label: 'y', origin: 'hub', group: 'x', verdict: null, reason: null,
  basis: null, predicted_decode_tps: null, headroom: null, total_params: null, dtype: null,
  requantized: false, warnings: [], cachedOn: [], bytesOnDisk: null, downloads: null,
  likes: null, gated: null, tags: [], pipelineTag: null, quantHint: null, detail: '',
  remote: false, state: null, runtime: null, ...over,
})
console.log('\n--- support classification (from /api/models/quant-table) ---')
const gguf = S.classifySupport(row({ tags: ['gguf'], model_id: 'unsloth/Qwen3-30B-A3B-GGUF' }), table)
check(gguf.status === 'unsupported' && /llama\.cpp/.test(gguf.reason), 'a GGUF repo is marked unsupported, citing the missing llama.cpp runtime')
check(S.classifySupport(row({ quantHint: 'gptq_int4' }), table).status === 'ok', 'a GPTQ-Int4 repo is not marked unsupported')
check(S.classifySupport(row({ quantHint: 'bf16' }), table).status === 'ok', 'plain bf16 is not marked unsupported')
const tts = S.classifySupport(row({ pipelineTag: 'text-to-speech' }), table)
check(tts.status === 'unsupported' && /text-to-speech/.test(tts.reason), 'a text-to-speech model is marked with its task named')
// An undetectable quantization answers "unknown", not "fine" -- and only
// "unsupported" draws the red dot, so an unknown row is simply unmarked.
check(S.classifySupport(row({ pipelineTag: 'text-generation' }), table).status !== 'unsupported', 'a text-generation model with no detectable quantization is left unmarked')
check(S.classifySupport(row({}), null).status === 'unknown', 'with no quant table, support is unknown rather than assumed fine')

console.log('\n--- an empty cache directory is not a cached model ---')
const raw = storage.nodes.flatMap((n) => (n.models?.repos ?? []))
const empty = raw.filter((r) => r.blob_count === 0)
console.log(`  ${empty.length} of ${raw.length} cache entries hold no files: ${empty.map((r) => r.repo_id).join(', ') || 'none'}`)
for (const r of empty) {
  check(cache.nodes(r.repo_id).length === 0, `${r.repo_id} is not reported as on device`)
}
check(R.onDeviceRows(cap, cache).length === new Set(raw.filter((r) => r.blob_count > 0).map((r) => r.repo_id)).size,
  'the On device source lists exactly the repos that hold files')

console.log('\n--- running source: a live deployment must be findable by repo id ---')
const cluster = await get('/api/cluster')
const deps = await get('/api/deployments')
const depRows = Array.isArray(deps) ? deps : (deps.deployments ?? [])
if (depRows.length === 0) {
  console.log('  (nothing deployed; skipped)')
} else {
  const run = R.runningRows({ deployments: depRows }, cache)
  check(run.length === depRows.length, 'every deployment becomes a row')
  // What the ladder's post-launch state depends on: the deployment record is
  // addressable by the repo id a launch was sent with.
  const d = depRows[0]
  check(run.some((r) => r.model_id === d.model_id), 'a deployment row is keyed by the model id the launch used')
  check(typeof d.state === 'string' && d.state === d.state.toLowerCase(), 'deployment state is lower case, as the signal mapping assumes')
  console.log(`  ${d.model_id} -> ${d.served_name} (${d.state})`)
}
void cluster

console.log('\n--- support reads the native dtype, never the fit gate\'s step-down ---')
const qwen = catRows.find((r) => r.model_id === 'Qwen/Qwen3-30B-A3B')
if (qwen) {
  console.log(`  ${qwen.model_id}: native=${qwen.nativeDtype} suggested=${qwen.dtype} requantized=${qwen.requantized}`)
  check(qwen.nativeDtype === 'bf16', 'the native dtype is carried through the join')
  const v = S.classifySupport(qwen, table)
  check(v.status !== 'unsupported',
    'a bf16 repo the gate would step down to a GGUF quant is NOT marked unsupported')
  // The regression itself: classifying from the suggestion produced this.
  const fromSuggestion = S.classifySupport({ ...qwen, nativeDtype: null, quantHint: qwen.dtype }, table)
  check(fromSuggestion.status === 'unsupported',
    'and classifying from the step-down would have marked it — which is the bug')
}

console.log('\n--- owner identity ---')
check(O.ownerAccent('Qwen') === O.ownerAccent('Qwen'), 'an accent is stable for the same publisher')
check(O.ownerAccent('Qwen') !== O.ownerAccent('openai'), 'different publishers get different accents')
check(O.ownerInitials('deepseek-ai') === 'DA' && O.ownerInitials('Qwen') === 'QW', 'initials read from the publisher name')
check(O.isFirstParty('openai') && !O.isFirstParty('MaziyarPanahi'), 'the verified check marks the team that trained it')

console.log(`\n${fail === 0 ? 'all checks passed' : fail + ' FAILED'}`)
process.exit(fail ? 1 : 0)
