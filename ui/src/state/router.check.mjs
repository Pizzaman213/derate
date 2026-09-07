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

import { execFileSync } from 'node:child_process'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const uiRoot = join(here, '..', '..')
const out = mkdtempSync(join(tmpdir(), 'router-check-'))
const bundle = join(out, 'routes.mjs')

execFileSync(
  join(uiRoot, 'node_modules', '.bin', 'esbuild'),
  [join(here, 'routes.ts'), '--bundle', '--format=esm', `--outfile=${bundle}`, '--log-level=warning'],
  { stdio: 'inherit' },
)

const { parse, href, DEFAULT_CONTEXT, DEFAULT_CONCURRENCY } = await import(bundle)

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
  ['/storage', 'storage'],
  ['/chat', 'chat'],
  ['/spend', 'spend'],
  ['/settings', 'settings'],
]) {
  check(`${path} -> ${dest}`, parse(path).dest, dest)
  check(`${path} round trips`, href(parse(path)), path)
}

// ── Model ids, which are the only ids with a slash in them ───────────────────

const HF = 'meta-llama/Llama-3.1-8B-Instruct'
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
// A URL spelling out the default says nothing a bare /models does not, and two
// spellings of one screen is exactly what a shared link must not have.
check('default context is omitted', href(parse(`/models?ctx=${DEFAULT_CONTEXT}`)), '/models')
check('default sequences omitted', href(parse(`/models?seq=${DEFAULT_CONCURRENCY}`)), '/models')
check('default context parses as null', parse(`/models?ctx=${DEFAULT_CONTEXT}`).context, null)
check('garbage context is the default', parse('/models?ctx=abc').context, null)
check('zero context is the default', parse('/models?ctx=0').context, null)
check('negative sequences is the default', parse('/models?seq=-3').concurrency, null)
check('fractional context rounds', parse('/models?ctx=4096.4').context, 4096)
// The numbers belong to the fit question, so they ride along wherever that
// question is being asked -- the models tab, or a model sheet over any screen.
check('numbers are dropped off the models tab', href({ ...parse('/spend'), context: 32768 }), '/spend')
check(
  'a model sheet keeps them',
  href({ ...parse(`/spend?open=model:${HF}`), context: 32768 }),
  `/spend?open=model:${HF}&ctx=32768`,
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
