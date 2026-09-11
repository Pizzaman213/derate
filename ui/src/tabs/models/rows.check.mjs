// requires: coordinator -- every assertion is about a fact the gateway reports
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
//   DERATE_CHECK_ORIGIN=http://localhost:18088 node src/tabs/models/rows.check.mjs
//
// Needs a coordinator: :8088 by default, or whatever DERATE_CHECK_ORIGIN names. Every assertion below is about a fact the
// gateway reports, not a shape this file invented.
import { build } from 'esbuild'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

const dir = mkdtempSync(join(tmpdir(), 'rows-'))
const bundle = async (name) => {
  const out = join(dir, `${name}.mjs`)
  await build({
    entryPoints: [fileURLToPath(new URL(`${name}.ts`, import.meta.url))],
    bundle: true, format: 'esm', outfile: out, logLevel: 'silent',
  })
  return import(out)
}
const R = await bundle('rows')
const S = await bundle('support')
const O = await bundle('owner')
const B = await bundle('board')
// The variant table's own derivations. They live outside `QuantLadder.tsx`
// precisely so this file can reach them: what a verdict LOOKS like is a
// judgement, and a green `tsc` says nothing about it.
const L = await bundle('ladder')
// The same normalisation the app applies to `/api/cluster`, imported rather
// than re-typed: a second copy here would agree with any bug that came from
// the same reading of the schema.
const C = await (async () => {
  const out = join(dir, 'client.mjs')
  await build({ entryPoints: [fileURLToPath(new URL('../../api/client.ts', import.meta.url))],
    bundle: true, format: 'esm', outfile: out, logLevel: 'silent' })
  return import(out)
})()
// The live coordinator by default, but overridable: this box's :8088 is a
// shared instance that several sessions bounce, and a check that can only ever
// talk to one port cannot be run against a throwaway on 18xxx.
const ORIGIN = process.env.DERATE_CHECK_ORIGIN ?? 'http://localhost:8088'
const get = async (p) => (await fetch(`${ORIGIN}${p}`)).json()

const [capacity, storage, catalog, hits, ladder, derived, providersPayload] = await Promise.all([
  get('/api/capacity?context=8192&concurrency=1'),
  get('/api/storage'),
  get('/api/catalog'),
  get('/api/models/search?q=qwen3&limit=10'),
  get('/api/models/variants?model_id=Qwen/Qwen3-30B-A3B&context=8192&concurrency=1'),
  // The SAME ladder with no context named, which is what the screen actually
  // asks. Naming one is the query that hid the defect below for as long as it
  // existed, so this fetch deliberately names none.
  get('/api/models/variants?model_id=Qwen/Qwen3-30B-A3B&concurrency=1'),
  get('/api/providers'),
])
const cap = R.capacityIndex(capacity), cache = R.cacheIndex(storage)
let fail = 0
const check = (ok, msg) => { console.log(`${ok ? 'PASS' : 'FAIL'}  ${msg}`); if (!ok) fail++ }

console.log(`basis=${cap.basis}\n`)

// --- catalog banding
const catRows = R.decorate(R.mergeRows([R.catalogRows(catalog)]), cap, cache)
const groups = R.groupRows(catRows, 'fit')
console.log('Recommended:')
for (const g of groups) console.log(`  [${g.title}] ${g.rows.length}: ${g.rows.map(r => r.label).join(', ')}`)
check(groups[0].title === 'Fits here', 'the first band of the curated set is the one that runs')
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
// What this used to assert: `Math.min(servable ranks) > 5`, i.e. that the
// gateway still ranks at least six unservable rows above the first servable
// one. That is not an invariant, it is a snapshot -- of one hub listing, on one
// box, at one set of fit verdicts -- and it failed the moment a KV-margin
// change moved the big GGUFs between fit tiers. Worse, it was inverted: fixing
// `capacity_api.py::_rank_key` to consider `launchable` would improve the
// product and break this line, as a hard FAIL rather than a skip.
//
// The gateway behaviour is real and unchanged: `_rank_key` sorts on fit tier
// then size and never consults `launchable`, so rows vLLM cannot load outrank
// rows it can. What the partition owes is not that the defect persists -- it
// is that the partition never becomes a SECOND ordering. That is what the two
// checks below hold it to, and they hold whether the gateway is fixed or not.
// The ranks themselves are printed as evidence, never gated on.
{
  const dead = reference.filter(v => v.rank < Math.min(...servable.map(v => v.rank))).length
  console.log(`  ${dead} unservable row(s) outrank the first servable one` +
              `${dead ? ' -- the gateway still ranks on fit before servability' : ''}`)
}
const ranks = servable.map(v => v.rank)
check(ranks.every((r, i) => i === 0 || r > ranks[i - 1]), 'partitioning preserves the gateway rank order inside the group')
const rec = ladder.recommended
check(rec == null || servable.some(v => v.repo_id === rec.repo_id), 'the recommended row is in the servable group')
console.log(`  pick -> ${(rec ?? servable[0]).repo_id}`)

// --- the derived context: the gate choosing the question it answers
// The defect this guards, verbatim: `ladder_context` walks the rungs
// best-quality first, and `max_context` answers 0 for a rung whose weights
// alone blow the budget. `_clamp_context` maps that 0 to the model's own
// window, so the first rung cleared the floor trivially and won -- handing a
// 262,144-token context to a ladder of 13 GiB files, every one of which was
// then refused on KV cache, each with a decode figure computed against a
// 10 GiB per-token cache read. Every assertion above this line passes with
// that bug present, because they all name a context.
const chosen = [...new Set(derived.variants.map(v => v.context).filter(c => c != null))]
console.log(`\nDerived context: ${chosen.join(', ')} (${derived.variants.length} variants)`)
check(chosen.length === 1, `one ladder is one question, so one context (got ${chosen.join(', ')})`)
const fitsAt8k = ladder.variants.filter(v => v.fits === true).length
const fitsDerived = derived.variants.filter(v => v.fits === true).length
console.log(`  fits: ${fitsDerived} derived vs ${fitsAt8k} at 8192`)
check(fitsAt8k === 0 || fitsDerived > 0,
  'a ladder with fitting rows at 8192 does not become 0-of-N when the gate picks the context')

// A throughput is about a model that loaded. The gateway sends the figure on
// every row it judged, refusals included -- that is a real property of the
// shape, and the wire does not decide what a table prints. The SCREEN decides,
// so the screen is what gets asserted: "will not fit ... 12 tok/s" is how this
// reached a user.
const refused = { verdict: 'wont_fit', fits: false, predicted_decode_tps: 12.2 }
check(L.decodeLabel(refused) === '—',
  `a row that will not fit still advertises ${L.decodeLabel(refused)}`)
check(L.decodeLabel({ verdict: 'fits_degraded', fits: true, predicted_decode_tps: 9.3 }) === '9 tok/s',
  'the fit-degraded row keeps its decode figure, which is the entire point of that row')
check(L.decodeLabel({ verdict: null, fits: null, predicted_decode_tps: null }) === '—',
  'an unjudged row shows no throughput')
// The wire is still expected to carry it -- if that ever stops, the guard above
// is silently doing nothing and the fit-degraded row loses its number.
check(derived.variants.some(v => v.predicted_decode_tps != null),
  'the gateway still sends a decode figure for the screen to decide about')

// --- a refusal the hardware could have held does not draw as a flat refusal
const lamp = (over) => L.fitLamp({ verdict: 'wont_fit', fits: false, ...over }, true)
check(lamp({ static_fits: true }).label !== lamp({ static_fits: false }).label,
  'a row that fits on spec and is blocked by resident memory reads the same as one that never fit')
check(lamp({ static_fits: true }).signal === 'fault',
  'and it still refuses: the live verdict governs, only the words change')
check(L.fitLamp({ verdict: 'fits' }, false).signal === 'idle',
  'a provider row is not judged by a gate that measured a different machine')

// --- the basis names the placement, and says which budget governed
const on = derived.sized_on
console.log(`  sized on ${on.nodes.join(', ')} at TP=${on.tensor_parallel}, live budget=${on.budget_is_live}`)
check(Array.isArray(on.nodes) && on.nodes.length >= 1, 'the ladder says which machines it was sized on')
check(on.tensor_parallel === 1 ? on.nodes.length === 1 : on.nodes.length === on.tensor_parallel,
  'the basis names exactly the machines the placement used, not everything enrolled')

// --- both budgets travel, and the live one governs
const dual = derived.variants.filter(v => v.static_verdict != null)
const blocked = derived.variants.filter(v => v.fits === false && v.static_fits === true)
console.log(`  ${dual.length}/${derived.variants.length} rows carry a static verdict; ${blocked.length} fit on spec but are blocked right now`)
check(on.budget_basis === 'host_memory' || dual.length === derived.variants.length,
  'every judged row carries the static side too, or the basis says why it cannot')
check(dual.every(v => v.verdict != null),
  'a static verdict never appears without the live one that governs it')
check(derived.variants.every(v => v.fits !== true || v.static_fits !== false),
  'a row that fits live cannot fail against the ceiling that live budget is a slice of')
check(blocked.every(v => v.static_reason),
  "a row blocked only by resident memory carries the gate's own sentence for the other side")

// --- completeness
// The SUBJECT is chosen from the live cache, never named here. This block
// used to hardcode `Qwen/Qwen3-30B-A3B` as a 2 MB metadata stub and
// `openai/gpt-oss-120b` as a finished 195 GB download -- true on the machine
// it was written on, and false the moment somebody pulls or deletes either.
// Qwen3-30B-A3B is 57 GB on this box now, so the stub assertion had been
// failing for who knows how long, which is how a gate stops being read.
//
// The behaviour under test is unchanged and is the whole point: far less than
// expected is NOT complete, at-or-above expected IS, and no expectation is
// unknown rather than a guess. None of those three facts needs a particular
// repository to be in a particular state.
const anyCached = cache.all()[0]
if (!anyCached) {
  console.log('  no repo is cached on any node: completeness has nothing to judge')
} else {
  const { repo_id, bytes } = anyCached
  console.log(`  judging completeness against ${repo_id} (${bytes} bytes on disk)`)
  check(cache.complete(repo_id, bytes * 1000) === false,
    'a repo holding far less than expected is not a finished download')
  check(cache.complete(repo_id, Math.floor(bytes / 2)) === true,
    'a repo holding at least what was expected is')
  check(cache.complete(repo_id, null) === null,
    'with no expected size, completeness is unknown rather than guessed')
  check(cache.complete('nobody/never-pulled-this', bytes) === false,
    'a repo that is not on any node is not complete either')
}

// --- filter
check(catRows.filter(r => R.matches(r, 'mxfp4')).length === 1, 'the filter reaches quantization text')

// --- the card's red dot, read off the server's own runtime table
const table = await get('/api/models/quant-table')
const row = (over) => ({
  model_id: 'x/y', label: 'y', origin: 'hub', group: 'x', verdict: null, reason: null,
  basis: null, predicted_decode_tps: null, headroom: null, total_params: null, dtype: null,
  requantized: false, warnings: [], cachedOn: [], bytesOnDisk: null, downloads: null,
  likes: null, gated: null, tags: [], pipelineTag: null, quantHint: null, detail: '',
  where: ['hub'], servedNames: [], deployments: [], providers: [], offers: [],
  checking: false, remoteOnly: false, unservedOnly: false, ...over,
})
console.log('\n--- support classification (from /api/models/quant-table) ---')
// GGUF used to be marked unsupported here for being GGUF at all, with a
// sentence citing the missing llama.cpp runtime. That stopped being true when
// the `llamacpp` runtime landed -- exactly as the text-to-speech case below
// stopped being true when `tts` did, and it is worth noticing that this file
// has now recorded the same shape of change twice.
//
// Read off the live `/api/models/quant-table`, which is the point: nothing in
// `support.ts` knows what a GGUF is any more, so this passes or fails on what
// the SERVER's runtime table says. A coordinator running a build without the
// runtime will fail this, and correctly -- it is describing that build.
const gguf = S.classifySupport(row({ tags: ['gguf'], model_id: 'unsloth/Qwen3-30B-A3B-GGUF' }), table)
check(gguf.status === 'ok', 'a GGUF repo is servable now that a runtime here loads the format')
check(gguf.reason === null, 'and carries no refusal sentence, because there is nothing to refuse')
// A provider route is still appended when there IS something to refuse. The
// pullable flag must not invent a refusal on a scheme that is supported.
const ggufPullable = S.classifySupport(row({ tags: ['gguf'], model_id: 'unsloth/Qwen3-30B-A3B-GGUF' }), table, true)
check(ggufPullable.status === 'ok' && ggufPullable.reason === null,
  'and a configured provider does not turn a servable scheme back into a refusal')
check(S.classifySupport(row({ quantHint: 'gptq_int4' }), table).status === 'ok', 'a GPTQ-Int4 repo is not marked unsupported')
check(S.classifySupport(row({ quantHint: 'bf16' }), table).status === 'ok', 'plain bf16 is not marked unsupported')
// text-to-speech used to be marked here for being TTS at all. That stopped
// being true when the `tts` runtime landed: the task IS served, on
// /v1/audio/speech, so the dot has to come from whether this checkpoint's
// architecture is one that runtime loads -- which is the support table's
// question, not the pipeline tag's. A red dot on every TTS repository would
// now be a refusal the cluster does not make.
const tts = S.classifySupport(row({ pipelineTag: 'text-to-speech', quantHint: 'bf16' }), table)
check(tts.status === 'ok', 'a text-to-speech model is no longer refused for being one: the tts runtime serves that task')
// A task nothing here serves still says so, and still names itself.
const diffusion = S.classifySupport(row({ pipelineTag: 'text-to-image' }), table)
check(diffusion.status === 'unsupported' && /text-to-image/.test(diffusion.reason), 'a task no runtime serves is marked with its task named')
check(S.classifySupport(row({ pipelineTag: 'text-to-image' }), table, true).reason === diffusion.reason, 'and says the same thing either way — a provider does not serve that task')
// An undetectable quantization answers "unknown", not "fine" -- and only
// "unsupported" draws the red dot, so an unknown row is simply unmarked.
check(S.classifySupport(row({ pipelineTag: 'text-generation' }), table).status !== 'unsupported', 'a text-generation model with no detectable quantization is left unmarked')
check(S.classifySupport(row({}), null).status === 'unknown', 'with no quant table, support is unknown rather than assumed fine')

console.log('\n--- an empty cache directory is not a cached model ---')
// PER NODE, and that is the whole correction. This flattened `repos` across
// nodes and then asserted that a repo empty on ANY node was on NO node --
// which is wrong for exactly the repos that are an empty directory on one
// machine and fully cached on another. Measured here when it was found:
// 18 entries hold no files, and the only three that failed were the three
// that also exist on the other Spark (openai/gpt-oss-20b: 0 blobs on 4d38,
// 11 on 26af). The product was right and the check was wrong, which is the
// expensive direction -- it invites someone to "fix" working code.
//
// Line ~430 below already does the per-node form; this is the same shape.
const raw = storage.nodes.flatMap((n) =>
  (n.models?.repos ?? []).map((r) => ({ ...r, nodeId: n.node_id })))
const empty = raw.filter((r) => r.blob_count === 0)
console.log(`  ${empty.length} of ${raw.length} cache entries hold no files`)
for (const r of empty) {
  check(!cache.nodes(r.repo_id).includes(r.nodeId),
    `${r.repo_id} is not reported as on device at ${r.nodeId}`)
}
check(R.onDeviceRows(cache).length === new Set(raw.filter((r) => r.blob_count > 0).map((r) => r.repo_id)).size,
  'every repo that holds files becomes exactly one row')

console.log('\n--- running source: a live deployment must be findable by repo id ---')
const cluster = await get('/api/cluster')
const deps = await get('/api/deployments')
const depRows = Array.isArray(deps) ? deps : (deps.deployments ?? [])
if (depRows.length === 0) {
  console.log('  (nothing deployed; skipped)')
} else {
  const run = R.runningRows({ deployments: depRows })
  check(run.length === depRows.length, 'every deployment becomes a row')
  // What the ladder's post-launch state depends on: the deployment record is
  // addressable by the repo id a launch was sent with.
  const d = depRows[0]
  check(run.some((r) => r.model_id === d.model_id), 'a deployment row is keyed by the model id the launch used')
  check(typeof d.state === 'string' && d.state === d.state.toLowerCase(), 'deployment state is lower case, as the signal mapping assumes')
  console.log(`  ${d.model_id} -> ${d.served_name} (${d.state})`)
}
void cluster

console.log('\n--- one list: the merge ---')
const deps2 = await get('/api/deployments')
const depRows2 = Array.isArray(deps2) ? deps2 : (deps2.deployments ?? [])
const merged = R.decorate(R.mergeRows([
  R.runningRows({ deployments: depRows2 }),
  R.onDeviceRows(cache),
  R.catalogRows(catalog),
  R.providerRows(providersPayload),
  R.hubRows(hits),
]), cap, cache)

check(new Set(merged.map(r => r.key)).size === merged.length, 'the merged list has no duplicate model ids')

// The merge must not MANUFACTURE a verdict -- but it must let a hub hit
// inherit one the capacity walk already produced. Strictly stronger than the
// old "no hub row carries a verdict".
const side = capacity.live ?? capacity.static
const answered = new Set((side?.rows ?? []).map(r => r.model_id))
check(merged.filter(r => r.verdict !== null).every(r => answered.has(r.model_id)),
  "every verdict on the list is one the capacity report contains")

// CLAUDE.md's rule, and the merge is exactly where it could break.
const invented = merged.filter(r => r.verdict === null && (
  r.predicted_decode_tps !== null || r.total_params !== null ||
  r.headroom !== null || r.dtype !== null))
check(invented.length === 0, 'no row without a verdict carries an invented number')

// A hub hit the walk never resolved has no answer, and says so.
// A hub hit that is ALSO being served is not unanswered -- `band()` returns
// 'running' first, on the stated rule that "reality outranks a prediction",
// and filing a model that is answering requests under anything else would be
// false. Caught by this check flagging Qwen/Qwen3-0.6B as
// `band=running where=running+ondisk+hub`: the product was right and the
// assertion had simply never met a model that was both.
const unresolvedHub = merged.filter(r =>
  r.where.includes('hub') &&
  !answered.has(r.model_id) &&
  !r.deployments.some(d => !['stopped', 'failed'].includes(d.state)))
const misbanded = unresolvedHub.filter(r => R.band(r) !== 'unchecked' || r.verdict !== null)
if (misbanded.length) {
  // A bare FAIL here is unactionable -- the whole question is WHICH row and
  // what it carries instead.
  for (const r of misbanded.slice(0, 5)) {
    console.log(`    misbanded: ${r.model_id} band=${R.band(r)} verdict=${JSON.stringify(r.verdict)} where=${r.where.join('+')}`)
  }
}
check(misbanded.length === 0,
  'a hub hit the capacity walk never resolved bands as unchecked')

// Banding is total, and every heading is a real one now that `plain` is gone.
for (const srt of R.SORTS.map(x => x.id)) {
  const gs = R.groupRows(merged, srt)
  const flat = gs.flatMap(g => g.rows)
  check(flat.length === merged.length, `sort=${srt}: banding loses no row`)
  check(gs.every(g => g.title), `sort=${srt}: every band has a title`)
  const order = ['running','fits','degraded','unchecked','wont','elsewhere']
  const seq = gs.map(g => order.indexOf(g.band))
  check(seq.every((v, i) => i === 0 || v > seq[i - 1]), `sort=${srt}: no sort crosses a band`)
}

// The gateway post-filters hub hits on model_id, which is what lets the tab
// drop the "hub rows are never re-filtered" special case.
const hubOnly = R.hubRows(hits)
check(hubOnly.length > 0 && hubOnly.every(r => R.matches(r, 'qwen3')),
  'the client filter cannot drop a hub hit the gateway returned')

// The three-way merge, live: gpt-oss-120b is curated, cached AND serving.
const gptoss = merged.find(r => r.model_id === 'openai/gpt-oss-120b')
if (gptoss) {
  console.log(`  gpt-oss-120b: where=[${gptoss.where}] verdict=${gptoss.verdict} band=${R.band(gptoss)} served=[${gptoss.servedNames}]`)
  check(gptoss.where.includes('catalog'), 'the curated facet survives the merge')
  if (depRows2.some(d => d.model_id === 'openai/gpt-oss-120b')) {
    check(gptoss.where.includes('running'), 'the running facet survives the merge')
    // Reality outranks a prediction: a model that is serving right now is not
    // filed under "Needs more memory", whatever the walk says at these numbers.
    check(R.band(gptoss) === 'running', 'a serving model bands as running, not by its verdict')
    check(gptoss.servedNames.length > 0, 'a running row carries the name it is served as')
    check(R.matches(gptoss, gptoss.servedNames[0]), 'matches() reaches a served name')
  }
  if (cache.nodes('openai/gpt-oss-120b').length) {
    check(gptoss.where.includes('ondisk'), 'the on-disk facet survives the merge')
    // The fit block must survive a merge whose FIRST contributor carries none.
    check(gptoss.verdict !== null || gptoss.reason !== null,
      'the merge keeps the fit answer even though the running builder has none')
  }
}

console.log('\n--- checking is a state, not a verdict ---')
const someId = merged.find(r => r.verdict === null)?.model_id
if (someId) {
  const marked = R.decorate(R.mergeRows([R.catalogRows(catalog), R.onDeviceRows(cache), R.hubRows(hits)]), cap, cache, new Set([someId]))
  const target = marked.find(r => r.model_id === someId)
  check(target?.checking === true, 'a row named in the in-flight set says it is checking')
  check(marked.filter(r => r.checking && r.verdict !== null).length === 0,
    'a row with a verdict is never also checking')
}

console.log('\n--- capacityIndex folds a poll and a batch ---')
{
  const a = { live: { rows: [{ model_id: 'a/one', verdict: 'fits', reason: 'r1', warnings: [] }] }, unresolved: [] }
  const b = { static: { rows: [{ model_id: 'a/one', verdict: 'wont_fit', reason: 'r2', warnings: [] },
                               { model_id: 'b/two', verdict: 'fits', reason: 'r3', warnings: [] }] }, unresolved: [] }
  const folded = R.capacityIndex(a, b)
  check(folded.row('a/one').verdict === 'wont_fit', 'a later report wins for a model both name')
  check(folded.row('b/two') !== null, 'a model only the later report names survives')
  check(folded.basisOf('a/one') === 'static' && folded.basis === 'live',
    'basisOf is per-report while basis stays the first one')
  const withUnresolved = R.capacityIndex({ live: { rows: [] }, unresolved: [{ model_id: 'c/three', reason: 'gated' }] }, a)
  check(withUnresolved.unresolved('c/three') === 'gated', 'an unresolved sentence survives the fold')
}

console.log('\n--- providers (synthetic: this box has none) ---')
{
  const prov = [{
    provider_id: 'openrouter', display_name: 'OpenRouter', kind: 'openrouter',
    base_url: 'https://openrouter.ai/api/v1', api_key_ref: 'OPENROUTER_API_KEY',
    api_key: 'sk-live-decoy', enabled: true, priority: 10, healthy: true,
    last_error: null, admitting: true, admission_block: null,
    models: [
      { served_name: 'gpt-4o', upstream_id: 'openai/gpt-4o', context_length: 128000,
        supports_streaming: true, supports_tools: true,
        input_cost_per_mtok: 2.5, output_cost_per_mtok: 10 },
      { served_name: 'qwen3-30b', upstream_id: 'Qwen/Qwen3-30B-A3B', context_length: 32768,
        supports_streaming: true, supports_tools: false,
        input_cost_per_mtok: null, output_cost_per_mtok: null },
    ],
  }]
  const withProv = R.decorate(R.mergeRows([
    R.catalogRows(catalog), R.providerRows(prov),
  ]), cap, cache)

  const only = withProv.find(r => r.model_id === 'openai/gpt-4o')
  check(only?.remoteOnly === true, 'a provider-only model is remoteOnly')
  check(only?.verdict === null && R.band(only) === 'elsewhere',
    'a provider-only model bands as elsewhere, not as unchecked')
  check(only?.providers.length === 1 && only.providers[0].context_length === 128000,
    'the provider row carries its context length')
  check(R.matches(only, 'openrouter'), 'matches() reaches a provider display name')

  const both = withProv.find(r => r.model_id === 'Qwen/Qwen3-30B-A3B')
  check(both !== undefined && !both.remoteOnly, 'a model that is both curated and served is not remoteOnly')
  check(both.where.includes('catalog') && both.where.includes('provider'),
    'one row carries both the curated and the provider facet')
  check(withProv.filter(r => r.model_id === 'Qwen/Qwen3-30B-A3B').length === 1,
    'a curated model published by a provider is ONE row')
  check(both.providers[0].input_cost_per_mtok === null,
    'a null price stays null after the merge, never coerced to 0')

  // The one unrecoverable mistake. Cheap to guard.
  const blob = JSON.stringify(withProv)
  check(!blob.includes('sk-live-decoy'), 'no API key reaches a row')
  check(!blob.includes('OPENROUTER_API_KEY'), 'no key reference reaches a row')
}

console.log('\n--- deployment state as a signal ---')
check(R.deploymentSignal('ready') === 'live', 'ready is live')
check(R.deploymentSignal('failed') === 'fault', 'failed is fault')
check(R.deploymentSignal('stopped') === 'fault', 'stopped is fault')
check(R.deploymentSignal('launching') === 'warn', 'launching is warn')
{
  const stoppedOnly = R.mergeRows([R.runningRows({ deployments: [
    { deployment_id: 'd1', model_id: 'x/y', served_name: 'y', state: 'stopped', runtime: 'vllm', node_ids: [] },
  ] })])
  check(R.band(stoppedOnly[0]) !== 'running', 'a finished deployment does not claim the running band')
  check(!R.rowFacts(stoppedOnly[0]).includes('vllm'),
        'and it contributes no runtime label, because nothing is running')
}

console.log('\n--- the row subtitle: distinct live runtimes, never one per record ---')
{
  // The defect, at the scale it actually reached on this box: a crash-looping
  // model accumulated 200 FAILED records and the subtitle printed "vllm" two
  // hundred times. The server keeps terminal deployments on purpose, so the
  // row has to collapse them rather than the store dropping them.
  const dep = (i, state) => ({
    deployment_id: `d${i}`, model_id: 'x/y', served_name: 'y',
    state, runtime: 'vllm', node_ids: [],
  })
  const crashLooped = R.mergeRows([R.runningRows({
    deployments: Array.from({ length: 200 }, (_, i) => dep(i, 'failed')),
  })])
  const facts = R.rowFacts(crashLooped[0])
  console.log(`  200 failed records -> ${facts.filter((f) => f === 'vllm').length} runtime labels`)
  check(facts.filter((f) => f === 'vllm').length === 0,
        '200 finished deployments contribute no runtime labels at all')

  // One live one says it once, however many records sit behind it.
  const busy = R.mergeRows([R.runningRows({
    deployments: [...Array.from({ length: 200 }, (_, i) => dep(i, 'failed')), dep(200, 'ready')],
  })])
  const busyFacts = R.rowFacts(busy[0])
  check(busyFacts.filter((f) => f === 'vllm').length === 1,
        'a live deployment among two hundred dead ones names its runtime exactly once')

  // Distinct, not counted: two runtimes really running are two labels.
  const mixed = R.mergeRows([R.runningRows({ deployments: [
    { deployment_id: 'a', model_id: 'x/y', served_name: 'y', state: 'ready', runtime: 'vllm', node_ids: [] },
    { deployment_id: 'b', model_id: 'x/y', served_name: 'y2', state: 'ready', runtime: 'sglang', node_ids: [] },
    { deployment_id: 'c', model_id: 'x/y', served_name: 'y3', state: 'ready', runtime: 'vllm', node_ids: [] },
  ] })])
  const names = R.rowFacts(mixed[0]).filter((f) => f === 'vllm' || f === 'sglang')
  check(names.length === 2 && names.includes('vllm') && names.includes('sglang'),
        'two distinct runtimes are two labels, deduped but not collapsed to a count')
}

console.log('\n--- the machine board: what you need in order to choose ---')
const [memory, topology] = await Promise.all([get('/api/memory'), get('/api/topology')])
const REPO = 'Qwen/Qwen3-30B-A3B'
const clusterNodes = cluster.nodes.map((n) => C.toNodeState(n, cluster.summary?.coordinator ?? null))
const board = B.buildBoard({
  nodes: clusterNodes,
  memory: memory.nodes ?? [],
  edges: topology.edges ?? [],
  deployments: depRows,
  cache,
  repoId: REPO,
  plannerChose: null,
  placement: null,
  chosen: null,
})
for (const r of board.rows) {
  console.log(`  ${r.nodeId}: selectable=${r.selectable} alloc=${r.allocatable} ceil=${r.ceiling} cached=${r.cached} running=[${r.occupants}]`)
}
check(board.rows.length === clusterNodes.length, 'every machine in the cluster gets a row')

// The gateway refuses a placement naming a machine with no addressable GPU
// memory (400 node_has_no_memory). Offering the tick would be offering a
// refusal, so the board must withhold it -- and say why, in the gateway's own
// words rather than a shrug.
const noMem = clusterNodes.filter((n) => n.profile.addressable_memory === 0)
if (noMem.length === 0) {
  console.log('  (every machine has GPU memory; the withheld-tick case is not exercised)')
} else {
  const rows = board.rows.filter((r) => noMem.some((n) => n.profile.node_id === r.nodeId))
  check(rows.every((r) => !r.selectable), 'a machine with no addressable GPU memory cannot be ticked')
  check(rows.every((r) => !!r.unselectableReason), 'and it says why rather than reading as merely unticked')
  check(board.selectableCount === clusterNodes.length - noMem.length, 'the count of machines that can carry a rank matches')
}

// A missing reading is not a full machine. `?? null` and not `?? 0` is the
// whole difference between "nothing has polled this" and "nothing is left".
const unread = board.rows.filter((r) => !(memory.nodes ?? []).some((m) => m.node_id === r.nodeId))
check(unread.every((r) => r.allocatable === null), 'a machine with no memory report has no allocatable figure, not a zero')

// The cached column is the same join the model cards use, so a cache folder
// with no files in it cannot claim the weights are already here.
const emptyRepos = (storage.nodes ?? []).flatMap((n) =>
  (n.models?.repos ?? []).filter((r) => r.repo_id === REPO && r.blob_count === 0).map(() => n.node_id))
check(board.rows.filter((r) => emptyRepos.includes(r.nodeId)).every((r) => !r.cached),
  'an empty cache folder is not "the weights are on disk here"')
check(board.rows.every((r) => r.cached === cache.nodes(REPO).includes(r.nodeId)),
  'the cached column is the storage join, not a second one')

// Ticking is sorted and idempotent, so two tick orders make one request body,
// one answer, and one server-side memo key.
const tickable = board.rows.filter((r) => r.selectable).map((r) => r.nodeId)
if (tickable.length >= 2) {
  const a = B.toggle({ ...board, effective: [tickable[1]] }, tickable[0], true)
  const b = B.toggle({ ...board, effective: [tickable[0]] }, tickable[1], true)
  check(JSON.stringify(a) === JSON.stringify(b), 'two tick orders produce one node list')
  check(JSON.stringify(a) === JSON.stringify([...a].sort()), 'the node list is sorted')
} else {
  console.log('  (fewer than two tickable machines; the tick-order case is not exercised)')
}

// The link column, and the trap underneath it. `worst_all_reduce` answers for
// the whole set or not at all, so one unprobed pair among the ticked machines
// makes the planner treat every link as unknown -- which changes the plan and
// is invisible unless the board says so.
const twoUp = tickable.length >= 2
  ? B.buildBoard({
      nodes: clusterNodes, memory: memory.nodes ?? [], edges: topology.edges ?? [],
      deployments: depRows, cache, repoId: REPO,
      plannerChose: null, placement: null, chosen: [tickable[0], tickable[1]],
    })
  : null
if (!twoUp) {
  console.log('  (fewer than two tickable machines; the link case is not exercised)')
} else {
  const pairMeasured = B.linkMeasured((topology.edges ?? []).find(
    (e) => [e.src, e.dst].sort().join() === [tickable[0], tickable[1]].sort().join()))
  check(twoUp.unmeasuredPairs.length === (pairMeasured ? 0 : 1),
    'an unmeasured pair among the ticked machines is reported, a measured one is not')
  const ticked = twoUp.rows.filter((r) => r.ticked)
  check(ticked.every((r) => (pairMeasured ? r.linkGbps !== null : r.linkGbps === null)),
    'an unmeasured link carries no figure')
  check(ticked.every((r) => r.linkUnmeasured === !pairMeasured), 'and says it was never measured')
}

// A single ticked machine crosses no wire at all, so there is no bandwidth to
// report and none is invented.
if (tickable.length >= 1) {
  const solo = B.buildBoard({
    nodes: clusterNodes, memory: memory.nodes ?? [], edges: topology.edges ?? [],
    deployments: depRows, cache, repoId: REPO,
    plannerChose: null, placement: null, chosen: [tickable[0]],
  })
  check(solo.rows.every((r) => r.linkGbps === null && !r.linkUnmeasured),
    'one machine crosses no link, so no bandwidth is claimed either way')
  check(solo.owned === true && solo.effective.length === 1, 'an explicit choice is owned by the operator')
}

// Absent is not empty. Sending no machines is what every request before this
// field existed sent, and it must stay reachable.
check(board.owned === false, 'no choice reads as the planner\'s, not as an empty one')
check(board.effective.length === 0, 'and with no plan back yet, nothing is ticked')

console.log('\n--- the machine board, on a cluster this box does not have ---')
// Everything above is a fact the live gateway reports, which is this file's
// rule. These are not: this cluster has ONE machine that can carry a rank, so
// the multi-machine paths -- the link column, the unmeasured-pair trap, tick
// order -- cannot be reached from it at all, and a verifier that quietly skips
// its most important case reads as one that covered it. The inputs below are
// hand-built and say so; the arithmetic under test is the same.
const NODE = (id, mem = 1) => ({
  profile: {
    node_id: id, hostname: id, address: '10.0.0.1', device_class: 'gb10',
    gpu_name: 'NVIDIA GB10', gpu_count: 1, total_memory: mem, addressable_memory: mem,
    memory_bandwidth_gbps: 273, compute_capability: '12.1', driver_version: '580',
  },
  healthy: true, state: 'healthy', role: 'worker', last_seen: 0,
  memory_used: 0, memory_total: mem, power_watts: null, temperature_c: null, utilization_pct: null,
})
const EDGE = (src, dst, gbps) => gbps == null
  ? { src, dst, measured: false, stale: false }
  : { src, dst, measured: true, stale: false, all_reduce_gbps: gbps, sendrecv_gbps: gbps }
const NOCACHE = { nodes: () => [], bytes: () => null, complete: () => null, all: () => [] }
// `runtime` is explicit here and has no default in `buildBoard`, deliberately:
// which machines can carry a rank now DEPENDS on it, and a helper that quietly
// supplied one would test the GPU answer while claiming to test the board.
const synth = (nodes, edges, chosen, extra = {}) => B.buildBoard({
  nodes, edges, memory: [], deployments: [], cache: NOCACHE, repoId: 'x/y',
  plannerChose: null, placement: null, chosen, runtime: 'vllm', ...extra,
})

// A machine with no GPU: what `registry/probe.py::_probe_cpu` actually writes
// -- every memory field 0, no gpu_name, device_class 'cpu'. Not NODE(id, 0),
// which is a GB10 whose probe came back empty and is a different machine with
// a different remedy.
const CPU_NODE = (id) => ({
  profile: {
    node_id: id, hostname: id, address: '10.0.0.9', device_class: 'cpu',
    gpu_name: '', gpu_count: 0, total_memory: 0, addressable_memory: 0,
    memory_bandwidth_gbps: 0, compute_capability: '', driver_version: '',
  },
  healthy: true, state: 'healthy', role: 'worker', last_seen: 0,
  memory_used: 0, memory_total: 8 * 1024 ** 3,
  power_watts: null, temperature_c: null, utilization_pct: null,
})

const three = [NODE('a'), NODE('b'), NODE('c')]

// The trap the screen exists to surface: `worst_all_reduce` answers for the
// whole set or not at all, so ONE unprobed pair among the ticked machines
// makes the planner plan as though nothing were measured.
const oneGap = synth(three, [EDGE('a', 'b', 40), EDGE('a', 'c', 40), EDGE('b', 'c', null)], ['a', 'b', 'c'])
check(oneGap.unmeasuredPairs.length === 1, 'one unprobed pair among three ticked machines is reported')
check(JSON.stringify(oneGap.unmeasuredPairs[0]) === JSON.stringify(['b', 'c']), 'and it names the pair')
check(oneGap.rows.find((r) => r.nodeId === 'b').linkGbps === null,
  'a machine with an unmeasured peer carries no bandwidth figure')
check(oneGap.rows.find((r) => r.nodeId === 'b').linkUnmeasured === true, 'and says it was never measured')

// Worst, not best and not an average: the figure has to be the one the slowest
// leg would actually run at.
const allUp = synth(three, [EDGE('a', 'b', 40), EDGE('a', 'c', 5.5), EDGE('b', 'c', 12)], ['a', 'b', 'c'])
check(allUp.unmeasuredPairs.length === 0, 'a fully measured set reports no gap')
check(allUp.rows.find((r) => r.nodeId === 'a').linkGbps === 5.5, 'the link column is the worst leg, not the best')
check(allUp.rows.find((r) => r.nodeId === 'b').linkGbps === 12, 'and it is per machine, over that machine\'s own legs')

// An unticked machine is not part of this deployment, so a slow link to it
// says nothing and must not drag the figure down.
const two = synth(three, [EDGE('a', 'b', 40), EDGE('a', 'c', 0.1), EDGE('b', 'c', 0.1)], ['a', 'b'])
check(two.unmeasuredPairs.length === 0 && two.rows.find((r) => r.nodeId === 'a').linkGbps === 40,
  'a link to a machine nobody ticked does not enter the figure')
check(two.rows.find((r) => r.nodeId === 'c').linkGbps === null, 'and an unticked machine reports no link at all')

// Sorted and set-like, so two tick orders make one request body, one answer
// and one server-side memo key.
check(JSON.stringify(B.toggle(synth(three, [], ['b']), 'a', true)) === JSON.stringify(['a', 'b']),
  'ticking sorts the node list')
check(JSON.stringify(B.toggle(synth(three, [], ['a']), 'b', true))
   === JSON.stringify(B.toggle(synth(three, [], ['b']), 'a', true)),
  'two tick orders produce one node list')
check(JSON.stringify(B.toggle(synth(three, [], ['a', 'b']), 'a', true)) === JSON.stringify(['a', 'b']),
  'ticking an already-ticked machine changes nothing')

// A missing reading is not a full machine, and not an empty one either. This
// is the case the live cluster cannot produce, because every node it has is
// polled.
const unpolled = synth(three, [], null)
check(unpolled.rows.every((r) => r.allocatable === null && r.ceiling === null),
  'no memory report means no figure, rather than a zero')

// Absence is a request. Sending no machines is byte for byte what every plan
// sent before the field existed, and a chosen list that names a machine which
// cannot carry a rank must not smuggle one in.
check(synth(three, [], null).owned === false, 'no choice is the planner\'s')
check(synth(three, [], []).owned === true && synth(three, [], []).effective.length === 0,
  'an explicit empty list is owned, and the caller decides what that means')
const withDud = synth([...three, NODE('dud', 0)], [], ['a', 'dud'])
check(JSON.stringify(withDud.effective) === JSON.stringify(['a']),
  'a machine that cannot carry a rank is dropped from the effective set')
check(withDud.selectableCount === 3, 'and is not counted among those that can')

// The board answers per RUNTIME, because there are two kinds of serving node
// now. This is the assertion that breaks first if `board.ts` and
// `deploy/flags.py::placement_refusal` ever stop agreeing, and the two have to
// agree exactly: the tick is withheld here precisely so nobody is offered a
// selection the launch would refuse with 400 `node_has_no_memory`.
const mixed = [NODE('spark'), CPU_NODE('pi')]
const onGpu = synth(mixed, [], null)
const onCpu = synth(mixed, [], null, { runtime: 'llamacpp' })
const rowOf = (board, id) => board.rows.find((r) => r.nodeId === id)

check(rowOf(onGpu, 'spark').selectable === true && rowOf(onGpu, 'pi').selectable === false,
  'under vllm the Spark can carry a rank and the Pi cannot')
check(rowOf(onCpu, 'pi').selectable === true && rowOf(onCpu, 'spark').selectable === false,
  'under llamacpp it is exactly the other way round')
check(onGpu.selectableCount === 1 && onCpu.selectableCount === 1,
  'and the count follows the runtime rather than the hardware alone')

// Each refusal names the remedy that exists, which is the half a shared
// sentence could not do. "It cannot carry a rank" was true and useless: under
// llamacpp the Pi is the ONLY machine that can.
check(/pick llamacpp/i.test(rowOf(onGpu, 'pi').unselectableReason ?? ''),
  'a GPU runtime on a GPU-less machine points at the runtime that runs there')
check(/vllm|sglang/i.test(rowOf(onCpu, 'spark').unselectableReason ?? ''),
  'and a CPU runtime on a machine with a GPU points back the other way')
check(rowOf(onCpu, 'pi').unselectableReason === null,
  'a machine that CAN carry the rank carries no refusal at all')

// One node runs one copy of a model. `DeploymentManager._find_conflict`
// refuses a second copy on a node already running the model and the gateway
// answers 409 already_deployed, so the tick has to be withheld here rather
// than offered and then refused -- the rule `_launchable` already follows for
// GGUF, applied to placement.
const DEP = (id, modelId, node, state = 'ready') => ({
  deployment_id: id, served_name: id, model_id: modelId, state, node_ids: [node],
})
const occupied = synth(three, [], null, { deployments: [DEP('d-1', 'x/y', 'a')] })
const busy = occupied.rows.find((r) => r.nodeId === 'a')
check(busy.selectable === false, 'a machine already running this model cannot be ticked')
check(busy.runningThisModel === true, 'and the row names which rule withheld it')
check(/already running this model/.test(busy.unselectableReason ?? ''),
  'and says so, rather than reading as merely unticked')
check(occupied.rows.find((r) => r.nodeId === 'b').selectable === true,
  'a machine that is not running it is untouched')
check(occupied.selectableCount === 3,
  'and the busy machine still counts as one that can carry a rank, because it is carrying one')

// Sharing a machine with a DIFFERENT model is the normal case, and whether
// both fit is the fit gate's answer. This rule must not quietly become a
// second, cruder memory check.
const neighbour = synth(three, [], null, { deployments: [DEP('d-1', 'other/model', 'a')] })
check(neighbour.rows.find((r) => r.nodeId === 'a').selectable === true,
  'a machine running a different model is still offered')
check(neighbour.rows.find((r) => r.nodeId === 'a').occupants.length === 1,
  'and is still reported as occupied, which is a different question')

// History occupies no GPU. `/api/deployments` lists stopped and failed records
// too, and the manager relaunches straight over them.
for (const over of ['stopped', 'failed']) {
  const done = synth(three, [], null, { deployments: [DEP('d-1', 'x/y', 'a', over)] })
  const row = done.rows.find((r) => r.nodeId === 'a')
  check(row.selectable === true, `a ${over} deployment does not hold its machine against a relaunch`)
  check(row.occupants.length === 0, `and is not drawn as an occupant`)
}

// Withheld means withheld: a chosen list naming a busy machine cannot smuggle
// one in, the same way one naming a machine with no memory cannot.
const smuggled = synth(three, [], ['a', 'b'], { deployments: [DEP('d-1', 'x/y', 'a')] })
check(JSON.stringify(smuggled.effective) === JSON.stringify(['b']),
  'a machine already running this model is dropped from the effective set')

// Before the first touch the ticks mirror the planner, so the board is never
// blank next to a plan that named machines.
const mirrored = synth(three, [], null, { plannerChose: ['a', 'b'] })
check(JSON.stringify(mirrored.effective) === JSON.stringify(['a', 'b']), 'an untouched board mirrors the planner')
check(mirrored.owned === false, 'and still reads as the planner\'s choice, not yours')
check(mirrored.rows.filter((r) => r.inPlan).length === 2, 'the planner\'s machines are badged')

console.log('\n--- support reads the native dtype, never the fit gate\'s step-down ---')
// The subject is any row the gate ACTUALLY stepped down, found at run time.
// This used to name Qwen/Qwen3-30B-A3B, which only demonstrates the bug while
// the gate is stepping that particular model down -- and it is not any more
// (native=bf16 suggested=bf16 requantized=false on this box), so the
// demonstration had quietly lost its subject and the check just failed.
// A regression test with no subject must say so, not fail.
const qwen = catRows.find((r) => r.requantized && r.nativeDtype && r.dtype !== r.nativeDtype)
if (!qwen) {
  console.log('  no catalogue row is requantized right now: nothing steps down to classify from')
} else {
  console.log(`  ${qwen.model_id}: native=${qwen.nativeDtype} suggested=${qwen.dtype} requantized=${qwen.requantized}`)
  check(!!qwen.nativeDtype, 'the native dtype is carried through the join')
  const v = S.classifySupport(qwen, table)
  check(v.status !== 'unsupported',
    'a repo the gate would step down to another quant is NOT marked unsupported')
  // The regression itself: classifying from the suggestion produced this.
  //
  // Only demonstrable while the dtype the gate steps DOWN to is one no runtime
  // loads, and that is now a property of the runtime table rather than a
  // constant. The subject found above steps down to q8_0, which every build
  // marked unsupported until `llamacpp` arrived and made the whole GGUF ladder
  // servable -- so on this build the step-down classifies as `ok` and the bug
  // cannot be reproduced through it.
  //
  // Reported rather than failed, by the same rule the comment above states for
  // a missing subject: the property being guarded (support reads the NATIVE
  // dtype) is asserted by the check above it and still holds. What is absent
  // is the counter-example, and a verifier that fails because a refusal got
  // better is one nobody will trust the next time it goes red.
  const fromSuggestion = S.classifySupport({ ...qwen, nativeDtype: null, quantHint: qwen.dtype }, table)
  if (fromSuggestion.status === 'unsupported') {
    check(true, 'and classifying from the step-down would have marked it — which is the bug')
  } else {
    console.log(`  ${qwen.dtype} is servable on this build, so the step-down no longer misclassifies: the counter-example has no subject`)
  }
}

console.log('\n--- the un-served catalogue, the sixth source ---')
// The one unfiltered provider surface in the app, read here exactly as the tab
// reads it. Everything else -- /api/providers, /v1/models, /api/topology --
// already carries only what the allowlist lets through.
const catalogues = {}
for (const p of providersPayload) {
  catalogues[p.provider_id] = await get(`/api/providers/${encodeURIComponent(p.provider_id)}/models`)
}
const published = Object.values(catalogues).flat()
const disabled = published.filter((m) => !m.enabled)
const liveOffered = R.offeredRows(catalogues, providersPayload)
check(liveOffered.length === disabled.length,
  `every switched-off catalogue model becomes a row, and only those (${liveOffered.length} of ${published.length} published)`)
check(liveOffered.every((r) => r.where.length === 1 && r.where[0] === 'offered'),
  'an offered row carries exactly the offered facet')
check(liveOffered.every((r) => r.servedNames.length === 0),
  'and claims no served name -- it does not answer at /v1 yet, and a search that found it there would name an endpoint that 404s')
// The live coordinator may legitimately have nothing switched off -- a record
// written before the allowlist serves its whole catalogue -- so the rules below
// are held to a synthetic catalogue rather than skipped on a green screen.
const CAT = {
  openrouter: [
    { served_name: 'a/one', upstream_id: 'a/one', context_length: 8192, supports_streaming: true,
      supports_tools: true, input_cost_per_mtok: 0.5, output_cost_per_mtok: 1.5, enabled: false },
    { served_name: 'a/two', upstream_id: 'a/two', context_length: 8192, supports_streaming: true,
      supports_tools: false, input_cost_per_mtok: null, output_cost_per_mtok: null, enabled: true },
  ],
}
const PROVS = [{ provider_id: 'openrouter', display_name: 'OpenRouter', models: [] }]
const synthetic = R.offeredRows(CAT, PROVS)
check(synthetic.length === 1 && synthetic[0].model_id === 'a/one',
  'a switched-ON catalogue row produces no offered row: /api/providers already carries it, with health and admission on it')
check(synthetic[0].offers[0].display_name === 'OpenRouter',
  "the offer carries the provider's display name, joined from the listing rather than guessed from the id")
check(synthetic[0].offers[0].input_cost_per_mtok === 0.5,
  'and its price, which is what switching it on costs')

const soloRow = R.mergeRows([synthetic])[0]
check(soloRow.unservedOnly === true && R.band(soloRow) === 'unserved',
  'a row that exists only because a provider publishes it bands as not served')
check(R.matches(soloRow, 'openrouter'),
  'searching the provider name reaches inside the collapsed band')

// Reality outranks a price sheet: the same id, running on a machine here.
const running = {
  ...soloRow, where: ['running'], offers: [],
  deployments: [{ deployment_id: 'd', served_name: 'a/one', state: 'ready', runtime: 'vllm', node_ids: [], last_error: null }],
}
const alsoRunning = R.mergeRows([[running], synthetic])[0]
check(alsoRunning.unservedOnly === false && R.band(alsoRunning) === 'running',
  'and the same model running on a machine here is not in that band at all')

// The two provider endpoints poll on different intervals, so for one tick a
// just-switched-on model appears in both. A row carrying both would draw Serve
// beside Stop serving.
const serving = {
  ...soloRow, where: ['provider'], offers: [],
  providers: [{ provider_id: 'openrouter', display_name: 'OpenRouter', served_name: 'a/one',
    context_length: 8192, input_cost_per_mtok: 0.5, output_cost_per_mtok: 1.5, supports_tools: true,
    supports_streaming: true, healthy: true, last_error: null, admitting: true, admission_block: null }],
}
check(R.mergeRows([[serving], synthetic])[0].offers.length === 0,
  'an offer from a provider that already serves the model is dropped rather than shown beside it')

// Banding and collapse.
const bandOrder = R.groupRows(
  R.decorate(R.mergeRows([R.catalogRows(catalog), synthetic]), cap, cache), 'fit',
).map((g) => g.band)
check(bandOrder[bandOrder.length - 1] === 'unserved',
  'the not-served band sorts below every band that says something about this hardware')
check(R.isCollapsibleBand('unserved') &&
  ['running', 'fits', 'degraded', 'unchecked', 'wont', 'elsewhere'].every((b) => !R.isCollapsibleBand(b)),
  'exactly one band hides itself, and it is the one with no local verdict in it')
check(R.bandSubtitle({ title: 'Not served', band: 'unserved', rows: [soloRow] }) === 'from OpenRouter',
  'the band names who publishes them rather than counting them')
check(R.bandSubtitle({ title: 'Fits here', band: 'fits', rows: [] }) === null,
  'and says nothing about a band that is not about offers')

// The auto-check budget. These rows are verdict-less by nature and there can be
// several hundred of them; the tab's predicate must not spend the batch on them.
const eligible = (r) => r.verdict == null && !r.remoteOnly && !r.unservedOnly && !r.checking
// Merged rows, because `unservedOnly` is a merge-time fact like `remoteOnly`:
// the builder cannot know whether some other source also produced this id, and
// the tab only ever sees merged rows.
check([soloRow, ...R.mergeRows([liveOffered])].every((r) => !eligible(r)),
  'no un-served row is a candidate for the hub auto-check')
check(eligible(synthetic[0]) === true,
  'and the flag really is what excludes them -- the same row before the merge is a candidate')

console.log('\n--- owner identity ---')
check(O.ownerAccent('Qwen') === O.ownerAccent('Qwen'), 'an accent is stable for the same publisher')
check(O.ownerAccent('Qwen') !== O.ownerAccent('openai'), 'different publishers get different accents')
check(O.ownerInitials('deepseek-ai') === 'DA' && O.ownerInitials('Qwen') === 'QW', 'initials read from the publisher name')
check(O.isFirstParty('openai') && !O.isFirstParty('MaziyarPanahi'), 'the verified check marks the team that trained it')

console.log(`\n${fail === 0 ? 'all checks passed' : fail + ' FAILED'}`)
process.exit(fail ? 1 : 0)
