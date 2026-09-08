// Verifier for the launch stepper's rules. There is no test runner in this
// repo -- typecheck plus these scripts are the whole UI gate -- and every rule
// below is one a green `tsc` says nothing about: which step a launch is on is
// a claim about the cluster, and the interesting failures are all claims that
// are merely plausible.
//
//   node src/tabs/cluster/loading.check.mjs
//
// Bundled with the esbuild inside vite rather than imported directly, because
// loading.ts has a value import of ../../format and node's ESM resolver will
// not resolve an extensionless specifier. Same shape as layout.check.mjs.

import { build as bundleWithEsbuild } from 'esbuild'
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const uiRoot = join(here, '..', '..', '..')
const out = mkdtempSync(join(tmpdir(), 'loading-check-'))
const bundle = join(out, 'loading.mjs')

await bundleWithEsbuild({
  entryPoints: [join(here, 'loading.ts')],
  bundle: true,
  format: 'esm',
  outfile: bundle,
  logLevel: 'warning',
})

const L = await import(pathToFileURL(bundle).href)

// The band's own geometry constants, so the drawing checks below measure
// against what the floor actually reserved rather than against numbers retyped
// here -- the exact drift that puts a phase row through the bar above it.
const layoutBundle = join(out, 'layout.mjs')
await bundleWithEsbuild({
  entryPoints: [join(here, 'layout.ts')],
  bundle: true,
  format: 'esm',
  outfile: layoutBundle,
  logLevel: 'warning',
})
const L2 = await import(pathToFileURL(layoutBundle).href)

let failures = 0
function ok(cond, what) {
  if (!cond) {
    failures++
    console.error(`FAIL  ${what}`)
  } else {
    console.log(`ok    ${what}`)
  }
}

const GiB = 1024 ** 3
const dep = (state, model_id = 'meta-llama/Llama-3.1-8B') => ({ state, model_id })
const launch = (since, last_error = null) => ({
  deployment_id: 'd1',
  served_name: 'llama',
  model_id: 'meta-llama/Llama-3.1-8B',
  runtime: 'vllm',
  state: 'launching',
  node_ids: ['spark-01'],
  since,
  last_error,
})
const pull = (over) => ({
  pull_id: 'p1',
  provider_id: 'ollama',
  provider: 'Ollama',
  model: 'meta-llama/Llama-3.1-8B',
  completed: 0,
  total: null,
  status: 'pulling manifest',
  error: null,
  done: false,
  ...over,
})

const at = (view) => view.steps.findIndex((s) => s.state === 'now')
const labels = (view) => view.steps.map((s) => `${s.state}:${s.phase}`)

// ── Which states get the loading form ───────────────────────────────────────

ok(L.isArriving('planned') && L.isArriving('launching'), 'planned and launching are arriving')
ok(
  !L.isArriving('ready') && !L.isArriving('degraded') && !L.isArriving('stopping') && !L.isArriving('failed'),
  'ready, degraded, stopping and failed are not',
)
// DEGRADED is the one that has to be argued rather than assumed: it is a model
// that is up and serving badly, which the band's own word already says, and
// drawing it as arriving would report a live deployment as one that has not
// got here yet. Same allowlist the gateway's _ARRIVING_STATES uses.
ok(!L.isArriving('degraded'), 'degraded is serving badly, not arriving')

// ── The stepper ─────────────────────────────────────────────────────────────

{
  const v = L.launchView(dep('planned'), null, [], 0)
  ok(at(v) === 0, 'planned is on the first step')
  ok(
    labels(v).join(' ') === 'now:preparing todo:downloading todo:loading todo:serving',
    'and nothing after it is claimed',
  )
}

{
  const v = L.launchView(dep('launching'), null, [], 0)
  ok(at(v) === 1, 'launching with nothing reporting holds at Downloading weights')
  ok(v.steps[1].detail === '', 'and shows no size, because none was reported')
  // The load-bearing one. The control plane cannot see inside the runtime's
  // own fetch, so the two middle steps cannot be told apart from here. Holding
  // at the download is the choice that stays true either way: a launch spends
  // its time there, and "Loading onto the GPU" would assert the weights had
  // landed. Never assert the stronger of two claims you cannot check.
  ok(v.steps[2].state === 'todo', 'and never claims the weights are on the GPU')
}

{
  const v = L.launchView(dep('ready'), null, [], 0)
  ok(v.steps.every((s) => s.state === 'done'), 'ready ticks every step')
  ok(at(v) === -1, 'and leaves none current')
}
ok(
  L.launchView(dep('degraded'), null, [], 0).steps.every((s) => s.state === 'done'),
  'degraded is serving, so its steps are done too',
)

// ── What a real pull moves ──────────────────────────────────────────────────

{
  const p = pull({ completed: 1.5 * GiB, total: 4 * GiB, status: 'pulling' })
  const v = L.launchView(dep('launching'), null, [p], 0)
  ok(at(v) === 1, 'a pull in flight keeps it on Downloading weights')
  ok(v.steps[1].detail === '1.5 / 4.0 GB', 'and fills in the real bytes against the real total')
}
{
  const p = pull({ completed: 4 * GiB, total: 4 * GiB, done: true })
  const v = L.launchView(dep('launching'), null, [p], 0)
  ok(at(v) === 2, 'a finished pull is the one thing that advances it to the GPU load')
}
{
  // Sizing: a pull spends its first seconds with no total at all, and a
  // denominator of zero is an invented measurement. Same rule as activity.ts.
  const v = L.launchView(dep('launching'), null, [pull({ completed: 900e6, total: null })], 0)
  ok(v.steps[1].detail === '', 'a pull that has not been sized shows no fraction')
}
{
  const v = L.launchView(dep('launching'), null, [pull({ model: 'some/other-model' })], 0)
  ok(at(v) === 1 && v.steps[1].detail === '', "another model's pull is not this one's")
}
{
  const v = L.launchView(dep('launching'), null, [pull({ error: 'no space left on device' })], 0)
  ok(at(v) === 1, 'a failed pull is not read as progress')
}
{
  const v = L.launchView({ state: 'launching', model_id: '' }, null, [pull({ model: '' })], 0)
  ok(at(v) === 1 && v.steps[1].detail === '', 'an empty model id matches no pull')
}

// ── The clock ───────────────────────────────────────────────────────────────

ok(L.launchView(dep('launching'), null, [], 1000).elapsed === null, 'no launch row is no clock')
// Null is not zero, and this is the whole reason it is null: `0:00` says the
// launch started this second, which is a stronger claim than "we do not know
// when it started". Same rule the sidebar's activity rows are pinned on.
ok(L.launchView(dep('launching'), launch(940), [], 1000).elapsed === 60, 'elapsed counts from `since`')
ok(
  L.launchView(dep('launching'), launch(1001), [], 1000).elapsed === 0,
  'a coordinator clock ahead of this browser never draws a negative age',
)
ok(L.launchView(dep('launching'), launch(0, 'boom'), [], 0).error === 'boom', 'the last error is carried verbatim')

ok(L.clock(0) === '0:00', 'the clock pads seconds')
ok(L.clock(9) === '0:09' && L.clock(61) === '1:01' && L.clock(600) === '10:00', 'and does not pad minutes')
ok(L.clock(-5) === '0:00', 'and never counts backwards')

// ── The drawing ─────────────────────────────────────────────────────────────
//
// The rules above are about what the stepper CLAIMS; these are about whether
// the claim reaches the screen. Both are invisible to `tsc`, and the second
// set is the reason `layout-preview.svg` exists: a band is SVG in authored
// units inside a viewBox, so "does the text land inside the bar" is arithmetic
// nobody can eyeball from the source.
//
// Rendered with react-dom/server against the real components, so this is the
// markup the browser gets and not a copy of it that can drift.

const R = await import('react')
const { renderToStaticMarkup } = await import('react-dom/server')
// Under node_modules rather than in the system temp dir, and this is not
// arbitrary: react is `external` so the components render through the SAME
// react instance this script imported -- two copies do not share a dispatcher
// -- and node resolves a bare specifier by walking up from the importing file,
// which only reaches ui/node_modules from inside the tree.
const cache = join(uiRoot, 'node_modules', '.cache', 'loading-check')
mkdirSync(cache, { recursive: true })
const comp = join(cache, 'LoadingBand.mjs')
await bundleWithEsbuild({
  entryPoints: [join(here, 'LoadingBand.tsx')],
  bundle: true,
  format: 'esm',
  outfile: comp,
  jsx: 'automatic',
  external: ['react', 'react-dom', 'react/jsx-runtime'],
  logLevel: 'warning',
})
const C = await import(pathToFileURL(comp).href)

const band = {
  id: 'd-a8a7d96c',
  deploymentId: 'd-a8a7d96c',
  kind: 'local',
  targetIds: [],
  providers: [],
  offCluster: false,
  servedName: 'Mistral-7B-Instruct-v0.3',
  // The live floor's own numbers, off :8088 while three models were launching.
  x: 120,
  y: 40,
  w: 239,
  h: 103,
  contiguous: true,
  ticks: [],
  members: ['spark-4d38'],
  leads: [],
  plan: 'single node',
  sublabel: '',
  degraded: false,
  loading: true,
  selected: false,
  ny: 91.5,
}

const view = L.launchView(dep('launching'), launch(1000 - 21), [], 1000)

// Rendered INSIDE an <svg>, which is not decoration: React picks the SVG
// namespace off an ancestor, and a `linearGradient` rendered at the top level
// is treated as an HTML element with a casing warning -- so a fragment on its
// own would be checking markup the browser never receives.
const el = R.createElement(
  'svg',
  { xmlns: 'http://www.w3.org/2000/svg', viewBox: '0 0 480 190', width: 960, height: 380 },
  R.createElement('rect', { x: band.x, y: band.y, width: band.w, height: band.h, rx: 3, fill: 'var(--fill)' }),
  // The two lines every band draws, loading or not, so the preview is the
  // whole object and not just the part this change added.
  R.createElement('text', { x: band.x + 11, y: band.y + 15, className: 'm', fontSize: 10, fill: 'var(--on-fill)' }, band.servedName),
  R.createElement('text', { x: band.x + 11, y: band.y + 29, className: 'm', fontSize: 9, fill: 'var(--on-fill-dim)' }, band.plan),
  R.createElement(C.BandSweep, { band }),
  R.createElement(C.BandLaunch, { band, view }),
)
const drawn = renderToStaticMarkup(el)

// The marks, which are the whole stepper: this fixture is LAUNCHING, so
// Preparing is behind it, Downloading weights is where it is held, and the two
// after that are not claimed.
ok((drawn.match(/✓/g) ?? []).length === 1, 'a launching deployment has one step behind it')
ok((drawn.match(/●/g) ?? []).length === 1, 'exactly one step is marked current')
ok((drawn.match(/○/g) ?? []).length === 2, 'and the two it cannot claim are still ahead')
ok(drawn.indexOf('✓') < drawn.indexOf('●') && drawn.indexOf('●') < drawn.indexOf('○'), 'in that order')
ok((drawn.match(/banddot1/g) ?? []).length === 1, 'only the current step gets the waiting dots')
ok(drawn.includes('>0:21<'), 'the clock is drawn where the throughput readout would be')
ok(drawn.includes('>elapsed<'), 'and says what it is a measurement of')
ok(!drawn.includes('tok/s'), 'and never beside a rate for a model that is not answering')
// The one number this must never show. `offset`/`stop-opacity` inside the
// gradient are the sweep's own shape, not a reading, so they are excluded by
// name rather than by hoping the regex misses them.
const claims = drawn.replace(/<linearGradient[\s\S]*?<\/linearGradient>/g, '')
ok(!/[0-9.]+\s*%/.test(claims), 'nothing on a loading band is a percentage')

// Every glyph inside the bar it belongs to. The steps are drawn by a layer
// that sits OVER the bands rather than inside them -- one poll, one clock --
// so nothing structural stops a row being placed past the bar's edge. Only
// this arithmetic does.
const texts = [...drawn.matchAll(/<text[^>]*\bx="([-0-9.]+)"[^>]*\by="([-0-9.]+)"/g)].map((m) => ({
  x: Number(m[1]),
  y: Number(m[2]),
}))
ok(
  texts.every((t) => t.y > band.y && t.y <= band.y + band.h),
  'every line of the band is inside it',
)
ok(
  texts.every((t) => t.x >= band.x + 11 && t.x <= band.x + band.w - 11),
  'and inside its gutters, so nothing is drawn on the bar edge',
)

const rows = [...new Set(texts.map((t) => t.y))].filter((y) => y > band.y + L2.SWEEP_DY).sort((a, b) => a - b)
ok(rows.length === 4, 'four phase rows, one baseline each')
ok(
  rows.every((y, i) => i === 0 || Math.abs(y - rows[i - 1] - L2.PHASE_ROW_H) < 0.001),
  "evenly pitched at the floor's own PHASE_ROW_H",
)
ok(rows[0] > band.y + L2.SWEEP_DY + L2.SWEEP_H, 'and none of them drawn through the sweep bar')
ok(
  rows[rows.length - 1] <= band.y + band.h,
  'the last row is inside the height the floor reserved for it',
)

// Both forms of the bar exist in the markup: the stylesheet picks between them
// on `prefers-reduced-motion`, and the still one is a DIFFERENT SHAPE -- a
// 40%-wide highlight parked at the left edge would read as 40% done.
const still = drawn.match(/class="bandsweep-still"[^>]*width="([0-9.]+)"/)
const move = drawn.match(/class="bandsweep-move"[^>]*width="([0-9.]+)"/)
ok(still && move, 'both bar forms are drawn, and the stylesheet chooses')
ok(
  still && move && Number(still[1]) === band.w - 22 && Number(move[1]) < Number(still[1]),
  'the still bar is the full track and the moving one is not',
)
// The ids are per band and go into url(#...), where a served name's '/' and
// '.' are not legal. Same spelling-out the band's own label clip does.
ok(
  !/url\(#[^)]*[./][^)]*\)/.test(drawn),
  'the sweep references an id with nothing in it a url() cannot carry',
)

writeFileSync(
  join(here, 'loading-preview.svg'),
  drawn
    .replace(/var\(--on-fill-dim\)/g, '#A8A398')
    .replace(/var\(--on-fill\)/g, '#EDE9E0')
    .replace(/var\(--live-solid\)/g, '#3D8B4A')
    .replace(/var\(--flow\)/g, '#4A6FA5')
    .replace(/var\(--fill\)/g, '#191817'),
)

rmSync(out, { recursive: true, force: true })
rmSync(cache, { recursive: true, force: true })
console.log(
  failures
    ? `\n${failures} failed`
    : `\nall good\n  preview: ${join(here, 'loading-preview.svg')}`,
)
process.exit(failures ? 1 : 0)
