// Verifier for the URL scheme. There is no test runner in this repo (see
// layout.check.mjs), and routes.ts is pure -- a string in, a route out, a
// string back -- so it is checkable without a browser:
//
//   node src/state/router.check.mjs
//
// What it is guarding: a URL is the one piece of this UI that leaves the
// machine. Somebody pastes it into a message and it has to still mean the same
// screen an hour later, on a different cluster, in a different build. The two
// properties below are what "means the same screen" reduces to:
//
//   parse(href(r)) === r        a route survives being written down
//   href(parse(u)) is stable    a URL has one canonical spelling
//
// routes.ts is bundled with the esbuild inside vite rather than imported
// directly, for the same reason layout.check.mjs does it: node's ESM resolver
// will not resolve an extensionless specifier.

import { build as bundleWithEsbuild } from 'esbuild'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const uiRoot = join(here, '..', '..')
const out = mkdtempSync(join(tmpdir(), 'router-check-'))
const bundle = join(out, 'routes.mjs')

// esbuild's JS API rather than the launcher under node_modules/.bin:
// that shim is a POSIX script with no .cmd twin, so spawning it by path
// fails on Windows. rows.check.mjs already bundles this way.
await bundleWithEsbuild({
  entryPoints: [join(here, 'routes.ts')],
  bundle: true,
  format: 'esm',
  outfile: bundle,
  logLevel: 'warning',
})

const { parse, href, DESTINATIONS, DEFAULT_CONTEXT, DEFAULT_CONCURRENCY } = await import(pathToFileURL(bundle).href)

let failures = 0
function check(what, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want)
  if (!ok) {
    failures += 1
    console.error(`FAIL ${what}\n  got  ${JSON.stringify(got)}\n  want ${JSON.stringify(want)}`)
  }
  return ok
}

// ── The destinations, and the two ways in ────────────────────────────────────

check('root is the dashboard', parse('/').dest, 'dash')
check('root canonicalises', href(parse('/')), '/dashboard')
check('unknown path is the dashboard', parse('/nonsense/deeper').dest, 'dash')
check('unknown path canonicalises', href(parse('/nonsense')), '/dashboard')

for (const [path, dest] of [
  ['/dashboard', 'dash'],
  ['/models', 'models'],
  ['/cluster', 'cluster'],
  ['/chat', 'chat'],
  ['/spend', 'spend'],
  ['/settings', 'settings'],
  // Not in the header's nav, and still a destination like any other. The
  // first-run screen has to survive being reloaded and typed, so it has to
  // round trip through the scheme like the rest.
  ['/setup', 'setup'],
]) {
  check(`${path} -> ${dest}`, parse(path).dest, dest)
  check(`${path} round trips`, href(parse(path)), path)
}

// Every destination the type admits is reachable, walked from routes.ts's own
// `DESTINATIONS` rather than from the pairs above -- so a `Dest` added with no
// `SEGMENT` entry is caught here even though the loop above, being a list,
// would never have heard of it. Such a Dest parses as the dashboard and
// href()s to `/undefined`, silently, in both directions.
for (const dest of DESTINATIONS) {
  const url = href({ ...parse('/'), dest })
  check(`${dest} has a segment`, url.includes('undefined'), false)
  check(`${dest} survives its own URL`, parse(url).dest, dest)
}

// ── Model ids, which are the only ids with a slash in them ───────────────────

const HF = 'meta-llama/Llama-3.1-8B-Instruct'

// Setup has no subject of its own, so a model id does not follow it there --
// same rule as every other destination that is not /models.
//
// A `?node=` selection DOES survive, and that is deliberate rather than an
// oversight worth asserting away: the scheme's own comment says a selection is
// not owned by a destination, and the setup screen simply reads no selection.
// Pinning the opposite here would invent a rule the scheme does not have.
check('setup drops a model', href({ ...parse('/models'), dest: 'setup', model: HF }), '/setup')
check('hub id is the whole tail', parse(`/models/${HF}`).model, HF)
check('hub id round trips', href(parse(`/models/${HF}`)), `/models/${HF}`)
check('single-segment id', parse('/models/qwen3-30b-a3b').model, 'qwen3-30b-a3b')
check('no id', parse('/models').model, null)
// A model id is meaningless anywhere else, and canonicalising drops it rather
// than carrying it into every subsequent URL.
check('model id only on /models', href({ ...parse('/cluster'), model: HF }), '/cluster')
// Percent-encoding survives intact: the id that comes back is the id that went
// in, character for character, not one that merely re-encodes to the same URL.
const ODD = 'someone/model with spaces+and%20stuff'
check('odd id round trips', parse(href({ ...parse('/models'), model: ODD })).model, ODD)

// ── Selections ───────────────────────────────────────────────────────────────

check('node', parse('/cluster?node=spark-01').node, 'spark-01')
check('link', parse('/cluster?link=spark-01~spark-02').link, 'spark-01~spark-02')
check('a tilde is not escaped', href(parse('/cluster?link=spark-01~spark-02')), '/cluster?link=spark-01~spark-02')
check('dep', parse('/dashboard?dep=qwen3-30b-a3b').dep, 'qwen3-30b-a3b')
check('empty parameter is no selection', parse('/cluster?node=').node, null)

check('sheet', parse('/cluster?open=node:spark-01').sheet, { kind: 'node', id: 'spark-01' })
check('sheet reads unescaped', href(parse('/cluster?open=node:spark-01')), '/cluster?open=node:spark-01')
check('sheet reads escaped too', parse('/cluster?open=node%3Aspark-01').sheet, { kind: 'node', id: 'spark-01' })
check('sheet on a model keeps its slashes', parse(`/dashboard?open=model:${HF}`).sheet, {
  kind: 'model',
  id: HF,
})
check('unknown sheet kind is no sheet', parse('/cluster?open=wat:x').sheet, null)
check('sheet with no id is no sheet', parse('/cluster?open=node:').sheet, null)

// Selections outlive a destination change: they are what the whole shell is
// about, not one screen's private state.
check(
  'selection survives the destination',
  href({ ...parse('/cluster?node=spark-01'), dest: 'dash' }),
  '/dashboard?node=spark-01',
)

// ── The two numbers ──────────────────────────────────────────────────────────

check('context', parse('/models?ctx=32768').context, 32768)
check('sequences', parse('/models?seq=4').concurrency, 4)
check('numbers round trip', href(parse('/models?ctx=32768&seq=4')), '/models?ctx=32768&seq=4')
// A REVERSAL, and the reason it is not a regression.
//
// These three used to assert the opposite: `?ctx=8192` was normalised away,
// because absence and 8192 were the same request and one screen must not have
// two URLs. They are not the same request any more. Absence means the
// coordinator picks the context per model from what actually fits -- which is
// what lets the models screen band every row on a fresh install with nothing
// typed anywhere -- and `?ctx=8192` means somebody overrode that and every
// verdict is taken at 8192 instead. Two questions, so two URLs, and the rule
// is intact: one spelling per meaning.
//
// Dropping an explicit 8192 on the way out would silently rewrite the first
// into the second on a link somebody shared, which is the failure the old
// assertion was written to prevent, pointed the other way.
check('an explicit default is kept', href(parse(`/models?ctx=${DEFAULT_CONTEXT}`)), `/models?ctx=${DEFAULT_CONTEXT}`)
check('an explicit default parses as itself', parse(`/models?ctx=${DEFAULT_CONTEXT}`).context, DEFAULT_CONTEXT)
check('an explicit one sequence is kept', href(parse(`/models?seq=${DEFAULT_CONCURRENCY}`)), `/models?seq=${DEFAULT_CONCURRENCY}`)
check('no context is the coordinator choosing', parse('/models').context, null)
check('no sequences is the coordinator choosing', parse('/models').concurrency, null)
// Unparseable is still absent, not 8192: a URL nobody can read is not an
// override somebody made.
check('garbage context defers to the coordinator', parse('/models?ctx=abc').context, null)
check('zero context defers to the coordinator', parse('/models?ctx=0').context, null)
check('negative sequences defer to the coordinator', parse('/models?seq=-3').concurrency, null)
check('fractional context rounds', parse('/models?ctx=4096.4').context, 4096)
// The numbers belong to the fit question, so they ride along wherever that
// question is being asked -- the models tab, or a model sheet over any screen.
check('numbers are dropped off the models tab', href({ ...parse('/spend'), context: 32768 }), '/spend')
check(
  'a model sheet keeps them',
  href({ ...parse(`/spend?open=model:${HF}`), context: 32768 }),
  `/spend?open=model:${HF}&ctx=32768`,
)

// ── The deployment shape: which machines, at which degrees ───────────────────
//
// These ride with ctx/seq because they are the other half of the same
// question. A verdict is only worth sending to somebody if what it was taken
// at travels with it, and "on which machines" is as much a part of that as
// "at what context".

check('machines parse', parse('/models?on=spark-01,spark-02').on, ['spark-01', 'spark-02'])
check('machines round trip', href(parse('/models?on=spark-01,spark-02')), '/models?on=spark-01,spark-02')
// Sorted on the way in, so two people who ticked the same machines in a
// different order are looking at the same URL -- and send the same request
// body, and hit the same server-side memo key.
check('machines sort', parse('/models?on=spark-02,spark-01').on, ['spark-01', 'spark-02'])
check('machine order is one spelling', href(parse('/models?on=spark-02,spark-01')), '/models?on=spark-01,spark-02')
check('a repeated machine is one machine', parse('/models?on=spark-01,spark-01').on, ['spark-01'])
check('blank entries are dropped', parse('/models?on=spark-01,,%20').on, ['spark-01'])
// `null` and `[]` are different requests: absent means "the planner picks",
// which is what every URL written before this field existed meant, and the
// empty list is a 400 with no honest answer.
check('no machines is the planner', parse('/models').on, null)
check('an empty list is the planner', parse('/models?on=').on, null)
check('an empty list is not written', href(parse('/models?on=')), '/models')
check('machines are dropped off the models tab', href({ ...parse('/spend'), on: ['spark-01'] }), '/spend')

check(
  'degrees parse',
  [parse('/models?tp=2&pp=1&ep=1').tp, parse('/models?tp=2&pp=1&ep=1').pp],
  [2, 1],
)
check('degrees round trip', href(parse('/models?tp=2&pp=1&ep=1')), '/models?tp=2&pp=1&ep=1')
// The one that `positive()` would get wrong. TP=1 against a planner that wants
// TP=2 is an override, and collapsing it to "unset" would hand the axis back
// to the planner it was overruling -- silently, and only in the URL.
check('tp=1 is not the absence of tp', parse('/models?tp=1&pp=1').tp, 1)
check(
  'tp=1 survives being written down',
  href(parse('/models?tp=1&pp=1')),
  '/models?tp=1&pp=1&ep=1',
)
check(
  'no degrees is the planner',
  [parse('/models').tp, parse('/models').pp, parse('/models').ep],
  [null, null, null],
)
// Adopted as a SET, because `parallelism` is one object on the wire: with an
// omitted key meaning 1, "TP mine, PP the planner's" cannot be expressed at
// all, so a partial set is completed rather than half-honoured.
check(
  'one axis adopts the set',
  [parse('/models?tp=4').tp, parse('/models?tp=4').pp, parse('/models?tp=4').ep],
  [4, 1, 1],
)
check('one axis writes the set', href(parse('/models?tp=4')), '/models?tp=4&pp=1&ep=1')
// Expert parallel is the axis the planner can never pick on this class of
// hardware -- `EP_VIABLE_THRESHOLD` is 40 GB/s and a Spark tops out at 23.15 --
// so the field is the only way anyone asks for it, and the URL is the only way
// the ask survives being shared.
check(
  'ep adopts the set on its own',
  [parse('/models?ep=2').tp, parse('/models?ep=2').pp, parse('/models?ep=2').ep],
  [1, 1, 2],
)
check('ep writes the set', href(parse('/models?ep=2')), '/models?tp=1&pp=1&ep=2')
check('ep=1 is not the absence of ep', parse('/models?ep=1').ep, 1)
check('garbage degrees are the planner', parse('/models?tp=abc&pp=x&ep=!').tp, null)
check('zero degrees are the planner', parse('/models?tp=0&pp=0&ep=0').tp, null)
check(
  'degrees are dropped off the models tab',
  href({ ...parse('/spend'), tp: 2, pp: 1, ep: 1 }),
  '/spend',
)

check('spec parses', parse('/models?spec=ngram:5').spec, { method: 'ngram', tokens: 5 })
// A bare colon, not %3A -- the same reading `open=node:spark-01` gets above.
// `enc` leaves it alone deliberately: a colon is legal in a query value and
// escaping it would make the one part of this UI that people paste to each
// other harder to read for nothing.
check('spec round trips', href(parse('/models?spec=ngram:5')), '/models?spec=ngram:5')
check('an escaped colon reads the same', parse('/models?spec=ngram%3A5').spec, {
  method: 'ngram',
  tokens: 5,
})
check('no spec is one token per step', parse('/models').spec, null)
// An unknown method is NOT dropped here. Which methods a checkpoint offers is
// the coordinator's answer, and it gives it as a sentence naming what this
// model does support; swallowing the value in the parser would turn a shared
// link into a silently different launch.
check('an unknown method survives the parser', parse('/models?spec=eagle:3').spec, {
  method: 'eagle',
  tokens: 3,
})
// A count, unlike a method, has no honest reading when it is unreadable --
// there is no server-side answer to "draft NaN tokens".
check('a spec with no count is no spec', parse('/models?spec=ngram').spec, null)
check('a spec with no method is no spec', parse('/models?spec=:5').spec, null)
check('a garbage count is no spec', parse('/models?spec=ngram:abc').spec, null)
check('a zero count is no spec', parse('/models?spec=ngram:0').spec, null)
check('a head rides beside its method', parse('/models?spec=eagle3:3&head=Angel/Q_eagle3').spec, {
  method: 'eagle3',
  tokens: 3,
  model: 'Angel/Q_eagle3',
})
// Bare slashes and a bare colon, like every other id in this scheme -- a
// model id keeps its slashes in the path, and a head repository keeps them
// here for the same reason: these URLs get pasted into messages.
check(
  'a head round trips',
  href(parse('/models?spec=eagle3:3&head=Angel/Q_eagle3')),
  '/models?spec=eagle3:3&head=Angel/Q_eagle3',
)
// A head with no method and no count is not a request the fit gate can price,
// so it is ignored rather than half-honoured -- the same rule `spec=ngram`
// with no count follows.
check('a head alone is no spec', parse('/models?head=Angel/Q_eagle3').spec, null)
check('a head alone is not written', href(parse('/models?head=Angel/Q_eagle3')), '/models')
check(
  'spec is dropped off the models tab',
  href({ ...parse('/spend'), spec: { method: 'ngram', tokens: 5 } }),
  '/spend',
)

// ── The properties themselves, over every URL above ──────────────────────────

const URLS = [
  '/',
  '/dashboard',
  '/dashboard?dep=qwen3-30b-a3b',
  '/models',
  `/models/${HF}`,
  `/models/${HF}?ctx=32768&seq=4`,
  '/cluster?node=spark-01',
  '/cluster?link=spark-01~spark-02',
  '/cluster?node=spark-01&open=node:spark-02',
  `/spend?open=model:${HF}&ctx=131072`,
  '/settings?node=spark-01&dep=qwen3-30b-a3b&open=dep:qwen3-30b-a3b',
  '/nonsense?ctx=99',
  `/models/${HF}?ctx=32768&seq=8&on=spark-4d38`,
  '/models?on=spark-01,spark-02&tp=2&pp=1&ep=1',
  // The shape the EP field exists to produce: DP attention with the experts
  // sharded across it. `?dp=` is deliberately absent -- placement.ts pairs the
  // data-parallel degree to this one, because vLLM's expert-parallel size IS
  // `dp * tp` and no other pairing means EP=2 across machines.
  '/models?on=spark-01,spark-02&tp=1&pp=1&ep=2',
  // An override that happens to equal the number the disclosure shows. It has
  // to survive the round trip like any other, or a shared link quietly becomes
  // "let the coordinator choose".
  `/models/${HF}?ctx=8192&seq=1`,
  `/spend?open=model:${HF}&on=spark-01&tp=1&pp=2&ep=1`,
  `/models/${HF}?ctx=32768&spec=mtp:1`,
  '/models?on=spark-4d38&tp=2&pp=1&ep=1&spec=ngram:5',
  `/models/${HF}?spec=eagle3:3&head=AngelSlim/Qwen3-4B_eagle3`,
]

for (const url of URLS) {
  const route = parse(url)
  const written = href(route)
  // Both directions, composed. Comparing a route to itself would pass with any
  // encoding at all; this fails the moment writing a route down and reading it
  // back changes a single field of it.
  check(`route survives being written down: ${url}`, parse(written), route)
  check(`one canonical spelling: ${url}`, href(parse(written)), written)
}

rmSync(out, { recursive: true, force: true })

if (failures) {
  console.error(`\n${failures} check(s) failed.`)
  process.exit(1)
}
console.log(`router: ${URLS.length} URLs, all checks pass.`)
