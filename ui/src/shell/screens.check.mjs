// requires: browser coordinator -- it opens the real screens in a real browser
//
// The first verifier in this repo that looks at a screen.
//
// Every other *.check.mjs checks a pure function: the URL scheme, the graph
// layout, the quantization ladder, the QR encoder. That is the right shape for
// most of what can go wrong, and it leaves one whole class untouched -- the app
// can pass the entire suite AND a green typecheck and still render a blank
// page, because "it mounted" is not a type and nothing here had ever loaded
// the bundle.
//
//   node src/shell/screens.check.mjs                 assert, and capture
//   node src/shell/screens.check.mjs --capture-only  capture only
//   DERATE_CHECK_ORIGIN=http://localhost:18088 node src/shell/screens.check.mjs
//
// It writes ui/screens/<dest>.png -- one per destination in state/routes.ts,
// which is also the point: `layout.check.mjs` writes layout-preview.svg so a
// shape can be eyeballed with no cluster and no browser, and this is the same
// instinct applied to the whole app. The PNGs are how a person, or Claude,
// sees what the change actually did instead of inferring it from source.
//
// No browser is downloaded to run this. It uses the Chromium already in the
// Playwright cache and says so plainly when there is none, which is why
// playwright-core is the dependency: it never fetches anything, and an
// explicit executablePath means the driver's version does not have to match
// the cached browser's revision.

import { execFileSync } from 'node:child_process'
import { mkdirSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright-core'
import { findBrowser } from '../check/browser.mjs'
import { report } from '../check/harness.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const uiRoot = join(here, '..', '..')
const repoRoot = join(uiRoot, '..')
const outDir = join(uiRoot, 'screens')

const ORIGIN = process.env.DERATE_CHECK_ORIGIN ?? 'http://localhost:8088'
const captureOnly = process.argv.includes('--capture-only')

const { check, note, done } = report()

const browser = findBrowser()
if (!browser.path) {
  console.error(`no browser: ${browser.why}`)
  process.exit(1)
}
note(`chromium: ${browser.why}`)

const get = async (p) => (await fetch(`${ORIGIN}${p}`)).json()

// Ids come off the live coordinator rather than being written down here. A
// hardcoded model id rots the moment the cluster changes, and a verifier that
// silently walks a 404 is worse than one that does not walk it at all.
const [models, topology] = await Promise.all([
  get('/api/models').catch(() => null),
  get('/api/topology').catch(() => null),
])
const someModel = models?.models?.[0]?.model_id ?? models?.[0]?.model_id ?? null
const someNode = topology?.nodes?.[0]?.node_id ?? null

// Whether a secret reached the screen is asked with the server's own
// Redactor, primed with the REAL stored values -- not with a shape heuristic
// run over the page text. The first cut of this used looks_like_secret() on
// every long word and flagged `audeering/wav2vec2-...`: a model id is exactly
// as long and as random-looking as a key, so a shape test over rendered text
// can only cry wolf. Comparing against what is actually in the store cannot.
const secretScan = (text) =>
  JSON.parse(execFileSync('python3', ['-c', [
    'import json, sys',
    'from control_plane.providers.secrets import Redactor, SecretStore',
    'store = SecretStore()',
    'values = [v for v in store._load_file().values() if v]',
    'values += [v for k, v in __import__("os").environ.items() if k.endswith("_API_KEY") and v]',
    'r = Redactor()',
    '[r.remember(v) for v in values]',
    'text = sys.stdin.read()',
    'print(json.dumps({"known": len(values), "leaked": r.contains_secret(text)}))',
  ].join('\n')], { input: text, cwd: repoRoot, encoding: 'utf8', maxBuffer: 32 * 1024 * 1024 }))

const SCREENS = [
  ['dashboard', '/dashboard'],
  ['models', '/models'],
  ['cluster', '/cluster'],
  ['storage', '/storage'],
  ['chat', '/chat'],
  ['spend', '/spend'],
  ['settings', '/settings'],
  ['setup', '/setup'],
]
if (someModel) SCREENS.push(['model-detail', `/models/${someModel}`])
if (someNode) SCREENS.push(['node-sheet', `/cluster?open=node:${someNode}`])

mkdirSync(outDir, { recursive: true })

const engine = await chromium.launch({ executablePath: browser.path })
const page = await engine.newPage({
  viewport: { width: 1440, height: 900 },
  reducedMotion: 'reduce',
  colorScheme: 'dark',
})

const transcript = []

for (const [name, path] of SCREENS) {
  const errors = []
  const badRequests = []
  const offsite = []
  const onConsole = (m) => {
    // 'Failed to load resource' is a response the handler below already judges,
    // by origin. Counting it here too would fail the run on a third party.
    if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(m.text())
  }
  const onPageError = (e) => errors.push(`uncaught: ${e.message}`)
  const onResponse = (r) => {
    if (r.status() < 400) return
    // Only this coordinator's own answers are this UI's fault. The publisher
    // avatars come straight from huggingface.co and come back 429 in bulk, and
    // failing the gate on somebody else's rate limiter would make it useless.
    // They are still recorded -- see screens/console.txt.
    if (r.url().startsWith(ORIGIN)) badRequests.push(`${r.status()} ${r.url()}`)
    else offsite.push(`${r.status()} ${r.url()}`)
  }
  page.on('console', onConsole)
  page.on('pageerror', onPageError)
  page.on('response', onResponse)

  await page.goto(`${ORIGIN}${path}`, { waitUntil: 'domcontentloaded' })
  await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {})
  // The particle field never goes idle; give it one frame and freeze it, so a
  // screenshot is of a screen rather than of an animation mid-stride.
  await page.addStyleTag({ content: '*,*::before,*::after{animation:none !important;transition:none !important}' })

  const shot = join(outDir, `${name}.png`)
  await page.screenshot({ path: shot })

  const rootHeight = await page.evaluate(() => document.getElementById('root')?.getBoundingClientRect().height ?? 0)
  const text = await page.evaluate(() => document.body.innerText ?? '')

  if (!captureOnly) {
    check(rootHeight > 0, `${name}: #root has rendered something (${Math.round(rootHeight)}px)`)
    check(errors.length === 0, `${name}: no console errors${errors.length ? ` -- ${errors[0].slice(0, 120)}` : ''}`)
    check(badRequests.length === 0, `${name}: the coordinator answered everything it was asked${badRequests.length ? ` -- ${badRequests[0]}` : ''}`)
    if (offsite.length) note(`${name}: ${offsite.length} off-site request(s) failed, not gated -- e.g. ${offsite[0].slice(0, 90)}`)

    const scan = secretScan(text)
    if (scan.known === 0) note(`${name}: no stored secrets on this box to look for`)
    else check(!scan.leaked, `${name}: none of the ${scan.known} stored secrets is on the screen`)
  }

  transcript.push(`${name}  ${path}  root=${Math.round(rootHeight)}px  errors=${errors.length}  assets=${badRequests.length}`)
  for (const e of errors) transcript.push(`    console: ${e}`)
  note(`${name} -> screens/${name}.png`)

  page.off('console', onConsole)
  page.off('pageerror', onPageError)
  page.off('response', onResponse)
}

// The deep-path fallback, which routes.ts names and nothing checked: a screen
// URL typed straight into the bar has to be answered with the app. Break it and
// a shared link 404s only on reload -- the failure that makes people stop
// sharing links.
if (!captureOnly && someModel) {
  // With the Accept header a navigation actually sends. A model id is allowed
  // a dot in it (`Llama-3.1-8B`), so a request for one is indistinguishable
  // from a request for a missing file by path alone -- the header is the whole
  // difference, and both halves of that rule are worth pinning.
  const nav = await fetch(`${ORIGIN}/models/${someModel}`, { headers: { accept: 'text/html,application/xhtml+xml' } })
  check(nav.ok, `a deep path reloaded in a browser is answered by the app (${nav.status})`)

  // The dotted case is about the SHAPE of a path, not about whatever this
  // cluster happens to hold, so it uses the id the docstring itself uses. The
  // first cut asked this of a live model id and passed or failed depending on
  // whether that id had a dot in its last segment.
  const dotted = '/models/meta-llama/Llama-3.1-8B'
  const asDoc = await fetch(`${ORIGIN}${dotted}`, { headers: { accept: 'text/html' } })
  check(asDoc.ok, `a dotted model id is a screen when a browser asks (${asDoc.status})`)
  const asFile = await fetch(`${ORIGIN}${dotted}`, { headers: { accept: '*/*' } })
  check(asFile.status === 404, `and a missing file when it is not a navigation (${asFile.status})`)

  // ...and the exception to it, which matters just as much.
  const missing = await fetch(`${ORIGIN}/assets/index-doesnotexist.js`)
  check(missing.status === 404, `a missing hashed asset stays a 404, not index.html (${missing.status})`)
}

writeFileSync(join(outDir, 'console.txt'), transcript.join('\n') + '\n')
await engine.close()

if (captureOnly) {
  console.log(`\ncaptured ${SCREENS.length} screen(s) into ui/screens/`)
  process.exit(0)
}
done()
