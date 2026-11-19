// The chart pack's command line: the one implementation the omp extension
// (.omp/extensions/chart-pack) calls through pi.exec, and the one a human runs.
//
//   node scripts/cli.mjs render <type|alias|intent> [data] [--sample basic] [--theme light|dark|high-contrast] [--width 960] [--height px] [--out dir] [--options json]
//   node scripts/cli.mjs recommend <intent> <data>
//   node scripts/cli.mjs lookup <query>
//   node scripts/cli.mjs validate <type> <data|inline json> [--options json]
//   node scripts/cli.mjs datasets [name]
//   node scripts/cli.mjs new <id> [--category c] [--name 'Display name'] [--intents a,b] [--summary text] [--force]
//   node scripts/cli.mjs data-add <name> <file> [--description text]
//   node scripts/cli.mjs sample-add <type> <file> [--name edge-foo] [--description text] [--options json]
//   node scripts/cli.mjs check [type,...] [--suites layout,a11y] [--skip-browser] [--category c]
//   node scripts/cli.mjs gallery [--open]
//
// <data> is a file (JSON rows/object, `{ data, options }` sample format, a datasets/ file
// `{ name, description, data, fields }`, or CSV), `sample:<type>/<name>`, or
// `dataset:<name>`. Every command takes --json (one JSON object on stdout). Exit 1 on
// failure with the reason. CHART_DATASETS_DIR points data-add/datasets at another folder.
// No shebang: vitest imports this file for its unit tests, and run it as `node scripts/cli.mjs`.

import { spawnSync } from 'node:child_process'
import { existsSync, mkdirSync, readdirSync, readFileSync, renameSync, rmSync, statSync, writeFileSync } from 'node:fs'
import { basename, dirname, extname, isAbsolute, join, relative, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

export const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const REPO = resolve(ROOT, '..', '..')
const DIST = join(ROOT, 'dist')
const datasetsDir = () => (process.env.CHART_DATASETS_DIR ? resolve(process.env.CHART_DATASETS_DIR) : join(ROOT, 'datasets'))
const today = () => new Date().toISOString().slice(0, 10)

export class CliError extends Error {}
const fail = (msg) => { throw new CliError(msg) }

/** Repo-relative when inside the repo (".omp/charts/out/..."), else absolute. */
const show = (p) => { const r = relative(REPO, p); return r.startsWith('..') || isAbsolute(r) ? p : r }

// ---------------------------------------------------------------- args

const BOOL = new Set(['json', 'open', 'force', 'skip-browser', 'help'])
export function parseArgs(argv) {
  const pos = [], flags = {}
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]
    if (a.startsWith('--')) {
      const eq = a.indexOf('=')
      const k = eq > 0 ? a.slice(2, eq) : a.slice(2)
      if (eq > 0) flags[k] = a.slice(eq + 1)
      else if (BOOL.has(k)) flags[k] = true
      else if (i + 1 < argv.length) flags[k] = argv[++i]
      else fail(`--${k} needs a value`)
    } else pos.push(a)
  }
  return { pos, flags }
}
const jsonFlag = (flags, k) => {
  if (flags[k] === undefined) return {}
  const v = parseJson(String(flags[k]), `--${k}`)
  if (!v || typeof v !== 'object' || Array.isArray(v)) fail(`--${k} must be a JSON object, e.g. '{"sort":"descending"}'`)
  return v
}

// ---------------------------------------------------------------- processes

function run(cmd, argv, { cwd = ROOT, env } = {}) {
  const r = spawnSync(cmd, argv, { cwd, encoding: 'utf8', maxBuffer: 1 << 28, env: { ...process.env, FORCE_COLOR: '0', ...env } })
  const out = `${r.stdout ?? ''}${r.stderr ? (r.stdout ? '\n' : '') + r.stderr : ''}`.trim()
  return { ok: r.status === 0, code: r.status ?? 1, out: r.error ? `${out}\n${r.error.message}`.trim() : out }
}
const note = (msg) => process.stderr.write(`${msg}\n`)

// ---------------------------------------------------------------- dist

const SOURCE_ROOTS = ['core', 'types', 'chooser', 'adapters', 'time', 'geo', 'theme', 'index.ts']
function newestSource() {
  let best = { t: 0, p: '' }
  const walk = (p) => {
    if (!existsSync(p)) return
    const s = statSync(p)
    if (s.isDirectory()) {
      // a deleted file only shows as its folder's mtime
      if (s.mtimeMs > best.t) best = { t: s.mtimeMs, p }
      for (const n of readdirSync(p)) if (n !== 'node_modules' && n !== 'fonts' && !n.startsWith('.')) walk(join(p, n))
    }
    else if (s.mtimeMs > best.t && !p.endsWith(join('types', 'all.ts'))) best = { t: s.mtimeMs, p }
  }
  for (const r of SOURCE_ROOTS) walk(join(ROOT, r))
  return best
}

/** Rebuild dist/ when missing or older than any source; fall back to the old bundle when the build fails. */
export function ensureDist() {
  const esm = join(DIST, 'charts.esm.js'), iife = join(DIST, 'charts.iife.js')
  const have = existsSync(esm) && existsSync(iife)
  const built = have ? Math.min(statSync(esm).mtimeMs, statSync(iife).mtimeMs) : 0
  const src = newestSource()
  if (have && src.t <= built) return
  note(have ? `dist/ is older than ${relative(ROOT, src.p)}: rebuilding (node scripts/build.mjs)` : 'dist/ is missing: building (node scripts/build.mjs)')
  const r = run('node', ['scripts/build.mjs'])
  if (r.ok) return
  if (have) { note(`build failed; using the existing dist/ bundle:\n${r.out.split('\n').slice(-8).join('\n')}`); return }
  fail(`node scripts/build.mjs failed and there is no dist/ to fall back on:\n${r.out.split('\n').slice(-15).join('\n')}`)
}

let apiPromise = null
/** The built API (dist/charts.esm.js); no DOM is touched at import. */
export function loadApi() {
  if (!apiPromise) { ensureDist(); apiPromise = import(pathToFileURL(join(DIST, 'charts.esm.js')).href + `?t=${statSync(join(DIST, 'charts.esm.js')).mtimeMs}`) }
  return apiPromise
}

// ---------------------------------------------------------------- data

/** Offset of the first character that makes `s` invalid JSON (V8 does not always say). */
function jsonErrorAt(s) {
  let i = 0
  const stop = () => { throw i }
  const ws = () => { while (i < s.length && ' \t\n\r'.includes(s[i])) i++ }
  const str = () => { i++; while (i < s.length) { const c = s[i]; if (c === '"') { i++; return } if (c === '\\') i += 2; else if (c < ' ') stop(); else i++ } stop() }
  const value = () => {
    ws()
    const c = s[i]
    if (c === '{') {
      i++; ws(); if (s[i] === '}') { i++; return }
      for (;;) { ws(); if (s[i] !== '"') stop(); str(); ws(); if (s[i] !== ':') stop(); i++; value(); ws(); if (s[i] === ',') { i++; continue } if (s[i] === '}') { i++; return } stop() }
    }
    if (c === '[') {
      i++; ws(); if (s[i] === ']') { i++; return }
      for (;;) { value(); ws(); if (s[i] === ',') { i++; continue } if (s[i] === ']') { i++; return } stop() }
    }
    if (c === '"') return str()
    const m = /^(?:-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null)/.exec(s.slice(i, i + 400))
    if (!m) stop()
    i += m[0].length
  }
  try { value(); ws(); return i < s.length ? i : -1 } catch (e) { if (typeof e === 'number') return e; throw e }
}

export function parseJson(text, label) {
  try { return JSON.parse(text) } catch (e) {
    const at = jsonErrorAt(text)
    const why = String(e.message).replace(/ in JSON at position \d+.*$/s, '').replace(/, ".*" is not valid JSON$/s, '')
    if (at < 0) fail(`${label}: not valid JSON: ${why}`)
    const before = text.slice(0, at)
    const line = before.split('\n').length, col = at - before.lastIndexOf('\n')
    const src = text.split('\n')[line - 1] ?? ''
    fail(`${label}: not valid JSON at line ${line}, column ${col}: ${why}\n  ${src.slice(Math.max(0, col - 40), col + 40)}\n  ${' '.repeat(Math.min(col - 1, 40))}^`)
  }
}

const NUM = /^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?$/
const cell = (s) => { const t = s.trim(); if (t === '') return null; if (NUM.test(t)) return Number(t); if (t === 'true' || t === 'false') return t === 'true'; return t }

/** CSV text -> rows; numbers, booleans and empty cells (null) typed per cell. */
export function parseCsv(text, label = 'CSV') {
  const lines = []
  let row = [], c = '', quoted = false, line = 1, start = 1
  for (let i = 0; i < text.length; i++) {
    const ch = text[i]
    if (quoted) {
      if (ch === '"') { if (text[i + 1] === '"') { c += '"'; i++ } else quoted = false }
      else { if (ch === '\n') line++; c += ch }
      continue
    }
    if (ch === '"' && c.trim() === '') { quoted = true; c = ''; continue }
    if (ch === ',') { row.push(c); c = ''; continue }
    if (ch === '\n' || ch === '\r') {
      if (ch === '\r' && text[i + 1] === '\n') i++
      row.push(c); lines.push({ line: start, cells: row }); row = []; c = ''; line++; start = line
      continue
    }
    c += ch
  }
  if (quoted) fail(`${label}: line ${start}: a quoted field is never closed`)
  if (c !== '' || row.length) { row.push(c); lines.push({ line: start, cells: row }) }
  const filled = lines.filter((l) => l.cells.some((x) => x.trim() !== ''))
  if (!filled.length) fail(`${label}: no rows (the file is empty)`)
  const header = filled[0].cells.map((h) => h.trim())
  header.forEach((h, i) => { if (!h) fail(`${label}: header column ${i + 1} has no name`) })
  const dup = header.find((h, i) => header.indexOf(h) !== i)
  if (dup) fail(`${label}: header names column '${dup}' twice`)
  const body = filled.slice(1)
  if (!body.length) fail(`${label}: no rows (only a header line)`)
  return body.map(({ line: ln, cells }, r) => {
    if (cells.length !== header.length) fail(`${label}: row ${r + 1} (line ${ln}) has ${cells.length} fields but the header has ${header.length}`)
    return Object.fromEntries(header.map((h, i) => [h, cell(cells[i])]))
  })
}

const WRAP_KEYS = new Set(['data', 'description', 'options', 'name', 'source', 'fields', 'linkFields', '$comment'])
const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v)

function resolveFile(ref) {
  for (const p of [resolve(process.cwd(), ref), resolve(ROOT, ref), resolve(REPO, ref)]) if (existsSync(p) && statSync(p).isFile()) return p
  return null
}

/**
 * Load a data reference: a file (JSON, sample/dataset wrapper, CSV), `sample:<type>/<name>`,
 * `dataset:<name>`, or inline JSON when `inline` allows it.
 * -> { data, csv?, options, description?, fields?, source, label }
 */
export function loadInput(ref, { inline = false } = {}) {
  let m
  if ((m = /^sample:([a-z0-9-]+)\/([a-z0-9-]+)$/.exec(ref))) {
    const p = join(ROOT, 'types', m[1], 'samples', `${m[2]}.json`)
    if (!existsSync(p)) fail(`no sample '${m[2]}' for '${m[1]}' (${show(p)} does not exist)`)
    const s = parseJson(readFileSync(p, 'utf8'), show(p))
    return { data: s.data, options: s.options ?? {}, description: s.description, source: show(p), label: `sample ${m[1]}/${m[2]}` }
  }
  if ((m = /^dataset:([a-z0-9-]+)$/.exec(ref))) {
    const p = join(datasetsDir(), `${m[1]}.json`)
    if (!existsSync(p)) fail(`no dataset '${m[1]}' in ${show(datasetsDir())} (have: ${listDatasetNames().join(', ')})`)
    const d = parseJson(readFileSync(p, 'utf8'), show(p))
    return { data: d.data, options: {}, description: d.description, fields: d.fields, source: show(p), label: `dataset ${m[1]}` }
  }
  const file = resolveFile(ref)
  let text, label
  if (file) { text = readFileSync(file, 'utf8'); label = show(file) }
  else if (inline && /^\s*[[{"]/.test(ref)) { text = ref; label = 'inline JSON' }
  else fail(`no such file: ${ref}${inline ? ' (and it is not inline JSON)' : ''} — give a path, sample:<type>/<name> or dataset:<name>`)
  const trimmed = text.replace(/^\uFEFF/, '').trimStart()
  const asCsv = (file && extname(file).toLowerCase() === '.csv') || (!trimmed.startsWith('[') && !trimmed.startsWith('{') && !trimmed.startsWith('"'))
  if (asCsv) return { data: parseCsv(trimmed, label), csv: trimmed, options: {}, source: label, label }
  const v = parseJson(trimmed, label)
  if (isObj(v) && 'data' in v && Object.keys(v).every((k) => WRAP_KEYS.has(k))) {
    return { data: v.data, options: isObj(v.options) ? v.options : {}, description: v.description, fields: v.fields, linkFields: v.linkFields, source: label, label }
  }
  return { data: v, options: {}, source: label, label }
}

// ---------------------------------------------------------------- catalogue + search

function readRegistry() {
  const p = join(ROOT, 'registry.json')
  return existsSync(p) ? JSON.parse(readFileSync(p, 'utf8')) : { charts: [] }
}
const samplesOf = (id) => { const d = join(ROOT, 'types', id, 'samples'); return existsSync(d) ? readdirSync(d).filter((f) => f.endsWith('.json')).map((f) => f.slice(0, -5)).sort() : [] }
const typeDirs = () => readdirSync(join(ROOT, 'types')).filter((d) => existsSync(join(ROOT, 'types', d, 'index.ts'))).sort()

/** registry.json, plus every chart the current bundle or types/ has that the registry does not list yet. */
export async function catalogue() {
  const api = await loadApi()
  const byId = new Map(readRegistry().charts.map((c) => [c.id, { ...c, inRegistry: true }]))
  for (const c of api.listCharts()) if (!byId.has(c.id)) byId.set(c.id, { ...c, aliases: Object.entries(api.ALIASES).filter(([, v]) => v === c.id).map(([k]) => k), inRegistry: false })
  for (const id of typeDirs()) if (!byId.has(id)) byId.set(id, { id, name: id, category: '?', intents: [], summary: '', aliases: [], inRegistry: false, unbuilt: true })
  return { api, charts: [...byId.values()].map((c) => ({ ...c, aliases: c.aliases ?? [], samples: samplesOf(c.id), readme: existsSync(join(ROOT, 'types', c.id, 'README.md')) ? `.omp/charts/types/${c.id}/README.md` : null, icon: existsSync(join(ROOT, 'icons', 'svg', `${c.id}.svg`)) ? `.omp/charts/icons/svg/${c.id}.svg` : null })) }
}

const STOP = new Set(['chart', 'charts', 'graph', 'graphs', 'plot', 'plots', 'diagram', 'diagrams', 'viz', 'a', 'an', 'the', 'of', 'and', 'or', 'with', 'one', 'by', 'per', 'across', 'in', 'to', 'for', 'as', 'from', 'on', 'each', 'its', 'their', 'is'])
const stem = (w) => (w.length > 4 && w.endsWith('ies') ? w.slice(0, -3) + 'y' : w.length > 3 && w.endsWith('s') && !w.endsWith('ss') ? w.slice(0, -1) : w)
export const tokens = (s) => String(s ?? '').toLowerCase().split(/[^a-z0-9]+/).filter((w) => w && !STOP.has(w)).map(stem)
const kebab = (s) => String(s).trim().toLowerCase().replace(/[\s_]+/g, '-')
const compact = (s) => tokens(s).join('')
const jaccard = (a, b) => { const A = new Set(a), B = new Set(b); if (!A.size || !B.size) return 0; let n = 0; for (const x of A) if (B.has(x)) n++; return n / (A.size + B.size - n) }
const covered = (q, set) => { if (!q.length) return 0; const S = [...new Set(set)]; let n = 0; for (const t of q) n += S.includes(t) ? 1 : S.some((s) => t.length >= 4 && (s.startsWith(t) || t.startsWith(s))) ? 0.5 : 0; return n / q.length }

/** Score one chart against a query: [score, why]. */
export function matchScore(query, c) {
  const q = kebab(query), qt = tokens(query), qc = compact(query)
  const nameT = tokens(c.name)
  const rules = [
    [q === c.id, 100, 'exact id'],
    [c.aliases.includes(q), 95, `alias of ${c.id}`],
    [!!qc && qc === compact(c.id), 92, `same chart as id ${c.id}`],
    [!!qc && c.aliases.some((a) => compact(a) === qc), 90, `same as alias ${c.aliases.find((a) => compact(a) === qc)}`],
    [!!qc && qc === compact(c.name), 90, `same as its name "${c.name}"`],
    [qt.length > 0 && jaccard(qt, tokens(c.id)) === 1, 88, `same words as id ${c.id}`],
    [qt.length > 0 && jaccard(qt, nameT) >= 0.75, 80, `name "${c.name}"`],
    [q.length >= 3 && (c.id.includes(q) || (q.includes(c.id) && c.id.length >= 3)), 70, `id ${c.id} ${c.id.includes(q) ? 'contains' : 'is part of'} "${q}"`],
    [q.length >= 3 && c.aliases.some((a) => a.includes(q)), 65, `alias ${c.aliases.find((a) => a.includes(q))} contains "${q}"`],
  ]
  let best = [0, '']
  for (const [hit, s, why] of rules) if (hit && s > best[0]) best = [s, why]
  const idName = covered(qt, [...tokens(c.id), ...nameT, ...c.aliases.flatMap(tokens)])
  const facets = covered(qt, [...(c.intents ?? []).flatMap(tokens), ...tokens(c.category)])
  const summary = covered(qt, tokens(c.summary))
  const fuzzy = Math.max(60 * idName, 45 * facets, 35 * summary)
  if (fuzzy > best[0]) best = [Math.round(fuzzy), idName * 60 >= Math.max(45 * facets, 35 * summary) ? 'words in its id/name' : facets * 45 >= 35 * summary ? `intent/category ${[...(c.intents ?? []), c.category].filter((x) => tokens(x).some((t) => qt.includes(t))).join(', ')}` : 'words in its summary']
  return best
}

export function search(query, charts, min = 25) {
  return charts.map((c) => { const [score, why] = matchScore(query, c); return { ...c, score, why } }).filter((c) => c.score >= min).sort((a, b) => b.score - a.score || a.id.localeCompare(b.id))
}

// ---------------------------------------------------------------- output

/** Print `human` (string or lines) or `json`, depending on --json. */
function emit(flags, human, json) {
  if (flags.json) process.stdout.write(JSON.stringify(json, null, 2) + '\n')
  else process.stdout.write((Array.isArray(human) ? human.join('\n') : human) + '\n')
}

// ---------------------------------------------------------------- render

const safeScript = (s) => s.replace(/<\/script/gi, '<\\/script')
const safeJson = (v) => JSON.stringify(v).replace(/</g, '\\u003c')

async function chartHtml({ mode, target, data, options, theme, width, height }) {
  const { fontFaceCSS } = await import(pathToFileURL(join(ROOT, 'tests', 'browser', 'lib.mjs')).href)
  const bundle = readFileSync(join(DIST, 'charts.iife.js'), 'utf8')
  const input = { mode, target, data, options: { ...options, theme, animate: false } }
  return `<!doctype html>
<html lang="en" data-theme="${theme}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${target.replace(/[<&"]/g, '')} — chart pack</title>
<style>${fontFaceCSS(true)}
html,body{margin:0;padding:0;font-family:'IBM Plex Sans',sans-serif}
#frame{display:inline-block;padding:16px}
#host{width:${width}px${height ? `;height:${height}px` : ''}}
</style>
<script>${safeScript(bundle)}</script>
</head><body><div id="frame"><div id="host"></div></div>
<script type="application/json" id="chart-input">${safeJson(input)}</script>
<script>
(function () {
  var input = JSON.parse(document.getElementById('chart-input').textContent)
  function go() {
    var host = document.getElementById('host'), r = {}, h
    try {
      if (input.mode === 'type') { h = OmpCharts.chart(input.target, host, input.data, input.options); r.type = h.type }
      else {
        var f = OmpCharts.chartFor(input.target, input.data, host, input.options)
        h = f.handle; r.type = f.typeUsed; r.reason = f.reason
        r.alternatives = f.alternatives.map(function (a) { return { type: a.type, score: a.score, reason: a.reason } })
      }
      window.__h = h
      var bg = getComputedStyle(h.root).getPropertyValue('--oc-surface').trim()
      if (bg) { document.body.style.background = bg; document.getElementById('frame').style.background = bg }
      r.state = h.state; r.errors = h.errors; r.marks = h.markCount(); r.renderer = h.renderer; r.summary = h.summary()
    } catch (e) { r.error = String((e && e.message) || e) }
    window.__result = r
  }
  var faces = ["400 12px 'IBM Plex Sans'", "500 12px 'IBM Plex Sans'", "600 12px 'IBM Plex Sans'", "400 12px 'IBM Plex Mono'"]
  Promise.all(faces.map(function (f) { return document.fonts.load(f) })).then(go, go)
})()
</script>
</body></html>
`
}

async function cmdRender({ pos, flags }) {
  const [target, ref] = pos
  if (!target) fail('usage: render <type|alias|intent> [data] [--sample basic] [--theme light|dark|high-contrast] [--width 960] [--out dir]')
  const api = await loadApi()
  const type = api.resolveType(target)
  const intent = type ? undefined : api.resolveIntent(target)
  if (!type && !intent) {
    const { charts } = await catalogue()
    const near = search(target, charts).slice(0, 5).map((c) => c.id)
    fail(`'${target}' is neither a chart type nor an intent${near.length ? ` — did you mean ${near.join(', ')}?` : ''} (intents: ${api.INTENTS.join(', ')})`)
  }
  let input
  if (ref) input = loadInput(ref)
  else if (type) input = loadInput(`sample:${type}/${flags.sample ?? 'basic'}`)
  else fail(`'${target}' is an intent: give data to choose for — render ${target} <file|dataset:name|sample:type/name>`)
  const theme = flags.theme ?? 'light'
  if (!['light', 'dark', 'high-contrast'].includes(theme)) fail(`--theme must be light, dark or high-contrast (got '${theme}')`)
  const width = Number(flags.width ?? 960), height = flags.height ? Number(flags.height) : undefined
  if (!(width >= 120 && width <= 4000)) fail(`--width must be a number of px between 120 and 4000 (got '${flags.width}')`)
  if (height !== undefined && !(height >= 80 && height <= 4000)) fail(`--height must be a number of px between 80 and 4000 (got '${flags.height}')`)
  const options = { ...input.options, ...jsonFlag(flags, 'options') }
  // a chart type gets the CSV text itself (its own parser); the chooser gets parsed rows
  const data = type && input.csv !== undefined ? input.csv : input.data
  const stamp = new Date().toISOString().replace(/[:.]/g, '-').replace('Z', '')
  let out = resolve(flags.out ?? join(ROOT, 'out', `${stamp}-${type ?? intent}`))
  mkdirSync(out, { recursive: true })
  const files = { html: join(out, 'chart.html'), png: join(out, 'chart.png'), svg: join(out, 'chart.svg'), csv: join(out, 'data.csv') }
  writeFileSync(files.html, await chartHtml({ mode: type ? 'type' : 'intent', target: type ?? intent, data, options, theme, width, height }))

  const { chromium } = await import('playwright-core')
  const browser = await chromium.launch({ headless: true })
  let result
  try {
    const ctx = await browser.newContext({ viewport: { width: width + 64, height: Math.max(600, (height ?? 0) + 64) }, deviceScaleFactor: 2, reducedMotion: 'reduce' })
    const page = await ctx.newPage()
    const pageErrors = []
    page.on('pageerror', (e) => pageErrors.push(String(e)))
    await page.goto(pathToFileURL(files.html).href)
    await page.waitForFunction(() => window.__result !== undefined, null, { timeout: 30_000 })
    result = await page.evaluate(() => window.__result)
    if (result.error) fail(`render failed: ${result.error}`)
    writeFileSync(files.svg, await page.evaluate(() => window.__h.exportSVG()))
    writeFileSync(files.csv, await page.evaluate(() => window.__h.exportCSV()))
    await page.locator('#frame').screenshot({ path: files.png })
    result.pageErrors = pageErrors
  } finally { await browser.close() }

  // the default folder is named after the chart drawn, which for an intent is only known now
  if (!flags.out && intent && result.type) {
    const named = join(ROOT, 'out', `${stamp}-${result.type}`)
    renameSync(out, named)
    out = named
    for (const k of Object.keys(files)) files[k] = join(named, basename(files[k]))
  }
  const ok = result.state !== 'error'
  const json = { ok, input: input.label, type: result.type, intent: intent ?? null, reason: result.reason ?? null, alternatives: result.alternatives ?? [], summary: result.summary, marks: result.marks, state: result.state, renderer: result.renderer, errors: result.errors, pageErrors: result.pageErrors, theme, width, outDir: out, files }
  emit(flags, [
    `type      ${result.type}${intent ? `  (chosen for intent '${intent}')` : ''}`,
    ...(intent ? [`reason    ${result.reason}`, `also      ${(result.alternatives ?? []).slice(0, 4).map((a) => `${a.type} (${a.score})`).join(', ') || '—'}`] : []),
    `data      ${input.label}`,
    `summary   ${result.summary}`,
    `marks     ${result.marks} (${result.renderer})`,
    `state     ${result.state}${result.errors?.length ? `\n${result.errors.map((e) => `  - ${e.path ? `${e.path}: ` : ''}${e.message}`).join('\n')}` : ''}`,
    ...(result.pageErrors.length ? [`console   ${result.pageErrors.join('\n          ')}`] : []),
    `html      ${show(files.html)}`,
    `png       ${show(files.png)}`,
    `svg       ${show(files.svg)}`,
    `csv       ${show(files.csv)}`,
  ], json)
  if (!ok) process.exitCode = 1
}

// ---------------------------------------------------------------- recommend / lookup / validate / datasets

async function recommendFor(intent, ref) {
  const api = await loadApi()
  if (!api.resolveIntent(intent)) fail(`unknown intent '${intent}' (one of: ${api.INTENTS.join(', ')})`)
  const input = loadInput(ref)
  const recs = api.recommend(intent, input.data)
  return { api, input, intent: api.resolveIntent(intent), recs, shape: api.describeData(api.inspect(input.data)) }
}

async function cmdRecommend({ pos, flags }) {
  const [intent, ref] = pos
  if (!intent || !ref) fail('usage: recommend <intent> <data|sample:type/name|dataset:name>')
  const { input, intent: i, recs, shape } = await recommendFor(intent, ref)
  const original = JSON.stringify(input.data)
  const list = recs.map((r) => ({ type: r.type, score: r.score, reason: r.reason, options: r.options, reshaped: r.data !== undefined && JSON.stringify(r.data) !== original }))
  emit(flags, recs.length
    ? [`intent '${i}' for ${input.label} (${shape}):`, ...list.map((r, k) => `${String(k + 1).padStart(2)}. ${r.type.padEnd(22)} ${String(r.score).padStart(3)}  ${r.reason}${Object.keys(r.options).length ? `  options ${JSON.stringify(r.options)}` : ''}`), '', `render the pick: node scripts/cli.mjs render ${i} ${ref}`]
    : `no chart fits ${input.label} for '${i}' (${shape})`, { intent: i, input: input.label, shape, recommendations: list })
}

async function cmdLookup({ pos, flags }) {
  const query = pos.join(' ').trim()
  if (!query) fail('usage: lookup <query>')
  const { api, charts } = await catalogue()
  const hits = search(query, charts).slice(0, 10)
  const intent = api.resolveIntent(query)
  const forIntent = intent ? api.listCharts({ intent }).map((c) => c.id) : []
  const json = { query, matches: hits.map((h) => ({ id: h.id, name: h.name, category: h.category, score: h.score, why: h.why, aliases: h.aliases, summary: h.summary, samples: h.samples, readme: h.readme, icon: h.icon, inRegistry: h.inRegistry })), intent: intent ?? null, chartsForIntent: forIntent }
  const lines = []
  if (!hits.length && !intent) lines.push(`no match for '${query}' among ${charts.length} charts. Not built yet? Start with: node scripts/cli.mjs new <id>`)
  for (const h of hits) {
    lines.push(`${h.id}  [${h.category}] ${h.score} — ${h.why}${h.inRegistry ? '' : '  (not in registry.json yet)'}`)
    if (h.summary) lines.push(`  ${h.name}: ${h.summary}`)
    if (h.aliases.length) lines.push(`  aliases: ${h.aliases.join(', ')}`)
    lines.push(`  samples: ${h.samples.join(', ') || '—'}`, `  readme: ${h.readme ?? '—'}   icon: ${h.icon ?? '—'}`)
  }
  if (intent) lines.push('', `'${query}' is the intent '${intent}': ${forIntent.length} charts serve it (${forIntent.join(', ')}).`, `Rank them for your data: node scripts/cli.mjs recommend ${intent} <data|dataset:name>  (or chartFor('${intent}', data, el))`)
  emit(flags, lines, json)
}

async function cmdValidate({ pos, flags }) {
  const [type, ...rest] = pos
  if (!type || !rest.length) fail('usage: validate <type> <data-file or inline JSON> [--options json]')
  const api = await loadApi()
  const id = api.resolveType(type)
  if (!id) { const { charts } = await catalogue(); const near = search(type, charts).slice(0, 4).map((c) => c.id); fail(`unknown chart type '${type}'${near.length ? ` — did you mean ${near.join(', ')}?` : ''}`) }
  const input = loadInput(rest.join(' '), { inline: true })
  const r = api.validate(id, input.csv ?? input.data, { ...input.options, ...jsonFlag(flags, 'options') })
  const errors = r.errors.map((e) => ({ path: e.path, message: e.message }))
  emit(flags, r.ok ? `ok: ${input.label} is valid ${id} input` : [`${id}: ${input.label} is not valid (${errors.length} problem${errors.length === 1 ? '' : 's'}):`, ...errors.map((e) => `  - ${e.message}`)], { ok: r.ok, type: id, input: input.label, errors })
  if (!r.ok) process.exitCode = 1
}

function readDatasetRegistry(dir = datasetsDir()) {
  const p = join(dir, 'datasets.json')
  if (!existsSync(p)) fail(`${show(p)} not found`)
  const text = readFileSync(p, 'utf8')
  return { path: p, text, reg: parseJson(text, show(p)) }
}
const listDatasetNames = () => { try { return readDatasetRegistry().reg.datasets.map((d) => d.name) } catch { return [] } }

async function cmdDatasets({ pos, flags }) {
  const { reg } = readDatasetRegistry()
  const [name] = pos
  if (!name) {
    emit(flags, [`${reg.datasets.length} datasets in ${show(datasetsDir())}:`, ...reg.datasets.map((d) => `${d.name.padEnd(24)} ${d.kind.padEnd(5)} ${String(d.rows).padStart(4)}  ${d.description}`)], { dir: datasetsDir(), datasets: reg.datasets })
    return
  }
  const entry = reg.datasets.find((d) => d.name === name)
  if (!entry) fail(`no dataset '${name}' (have: ${reg.datasets.map((d) => d.name).join(', ')})`)
  const doc = parseJson(readFileSync(join(datasetsDir(), entry.file), 'utf8'), entry.file)
  emit(flags, [
    `${entry.name} — ${entry.description}`,
    `kind ${entry.kind}, ${entry.rows} ${entry.kind === 'rows' ? 'rows' : 'nodes'}, added ${entry.added}; source: ${doc.source}`,
    'fields:', ...Object.entries(doc.fields ?? {}).map(([k, f]) => `  ${k.padEnd(14)} ${f.type.padEnd(8)} ${f.description}${f.unit ? ` (${f.unit})` : ''}${f.nulls ? `; ${f.nulls} nulls` : ''}`),
    ...(doc.linkFields ? ['link fields:', ...Object.entries(doc.linkFields).map(([k, f]) => `  ${k.padEnd(14)} ${f.type.padEnd(8)} ${f.description}`)] : []),
    `intents: ${entry.intents.join(', ')}`,
    `suggested: ${entry.suggested.join(', ')}`,
    `use: getDataset('${entry.name}') · node scripts/cli.mjs render ${entry.suggested[0]} dataset:${entry.name}`,
  ], { entry, fields: doc.fields, linkFields: doc.linkFields ?? null, source: doc.source })
}

// ---------------------------------------------------------------- check

const ANSI = /\x1b\[[0-9;]*m/g
/** check.mjs's summary block and its failure lines, split into ones naming `ids` and the rest. */
export function summarizeCheck(out, ids = []) {
  const text = out.replace(ANSI, '')
  const at = text.lastIndexOf('══ charts:check')
  const summary = at >= 0 ? text.slice(at).trim() : text.split('\n').slice(-25).join('\n')
  const steps = [...summary.matchAll(/^(pass|FAIL|warn)\s+(\S+)\s+([\d.]+)s$/gm)].map((m) => ({ name: m[2], status: m[1], secs: Number(m[3]) }))
  const fi = summary.indexOf('\nFailures:')
  const failures = fi >= 0 ? summary.slice(fi + 10).split('\n').filter((l) => l.trim() && !/^\d+ step\(s\) failed$|^all green$/.test(l.trim())).map((l) => l.replace(/^ {2}/, '')) : []
  // a failure line plus its indented message
  const grouped = []
  for (const l of failures) if (/^\s{2,}/.test(l) && grouped.length) grouped[grouped.length - 1] += `\n${l}`; else grouped.push(l)
  const names = (l) => ids.some((id) => new RegExp(`types/${id}/|\\b${id}\\b`).test(l))
  return { summary, steps, failures: grouped, mine: ids.length ? grouped.filter(names) : grouped, others: ids.length ? grouped.filter((l) => !names(l)) : [] }
}

function runCheck(types, flags) {
  const argv = ['scripts/check.mjs']
  if (types.length) argv.push('--type', types.join(','))
  if (flags.category) argv.push('--category', flags.category)
  if (flags.suites) argv.push('--suites', flags.suites)
  if (flags['skip-browser']) argv.push('--skip-browser')
  const r = run('node', argv)
  return { ...r, cmd: `node ${argv.join(' ')}`, ...summarizeCheck(r.out, types) }
}

async function cmdCheck({ pos, flags }) {
  const types = pos.flatMap((p) => p.split(',')).map((s) => s.trim()).filter(Boolean)
  const r = runCheck(types, flags)
  const lines = [`$ ${r.cmd}`, r.summary]
  if (types.length && r.failures.length) lines.push('', r.mine.length ? `${r.mine.length} failure(s) name ${types.join(', ')}` : `none of the ${r.failures.length} failure(s) names ${types.join(', ')}: they are elsewhere in the pack`)
  emit(flags, lines, { ok: r.ok, command: r.cmd, steps: r.steps, failures: r.failures, scoped: types, failuresForScope: r.mine, failuresElsewhere: r.others })
  if (!r.ok) process.exitCode = 1
}

async function cmdGallery({ flags }) {
  const steps = [['build', ['scripts/build.mjs']], ['icons', ['icons/lint.mjs', '--write']], ['gallery', ['scripts/gallery.mjs']]]
  const log = []
  for (const [name, argv] of steps) {
    const r = run('node', argv)
    log.push(`${r.ok ? 'ok  ' : 'FAIL'} node ${argv.join(' ')}${r.ok ? '' : `\n${r.out.split('\n').filter((l) => /FAIL|rror/.test(l)).slice(0, 15).join('\n') || r.out.split('\n').slice(-8).join('\n')}`}`)
    // the icon lint fails on any icon in the pack; the gallery still builds without it
    if (!r.ok && name !== 'icons') { emit(flags, log, { ok: false, steps: log }); process.exitCode = 1; return }
  }
  const path = join(ROOT, 'gallery.html')
  let opened = null
  if (flags.open) {
    const opener = process.platform === 'darwin' ? 'open' : process.platform === 'win32' ? null : 'xdg-open'
    const has = opener && run('sh', ['-c', `command -v ${opener}`]).ok
    if (has) { const r = spawnSync(opener, [path], { stdio: 'ignore', detached: true, timeout: 5000 }); opened = r.error ? `${opener} failed: ${r.error.message}` : `opened with ${opener}` }
    else opened = `no ${opener ?? 'opener'} on this machine: open ${path} in a browser`
  }
  emit(flags, [...log, `gallery   ${show(path)}`, ...(opened ? [opened] : [])], { ok: true, path, opened, steps: log })
}

// ---------------------------------------------------------------- new

const RESERVED = new Set(['all', 'index', 'core', 'chooser', 'types', 'adapters', 'time', 'geo', 'theme', 'datasets', 'dist', 'tests', 'samples', 'scripts', 'icons', 'out', 'chart', 'charts', 'new', 'default', 'node-modules', 'shared', 'other', 'none'])
export const KEBAB = /^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/

/** An existing chart that `id` duplicates, or null. Exact ids and aliases always win; --force skips the similarity rules. */
export function reuseOf(id, charts, aliases, force = false) {
  const exact = charts.find((c) => c.id === id)
  if (exact) return { id: exact.id, why: 'a chart with this id already exists' }
  if (aliases[id]) return { id: aliases[id], why: `'${id}' is an alias of ${aliases[id]} (ALIASES in index.ts)` }
  if (force) return null
  let best = null
  for (const c of charts) {
    const [score, why] = matchScore(id, c)
    if (score >= 80 && (!best || score > best.score)) best = { id: c.id, score, why }
  }
  return best ? { id: best.id, why: best.why } : null
}

async function cmdNew({ pos, flags }) {
  const id = pos[0]
  if (!id) fail("usage: new <id> [--category c] [--name 'Display name'] [--intents a,b] [--summary text] [--force]")
  const { api, charts } = await catalogue()
  if (!KEBAB.test(id)) fail(`'${id}' is not a kebab-case id (lowercase words joined by '-', starting with a letter, e.g. 'radial-histogram')`)
  const reuse = reuseOf(id, charts, api.ALIASES, !!flags.force)
  if (reuse) {
    const c = charts.find((x) => x.id === reuse.id)
    emit(flags, [
      `reuse ${reuse.id}: ${reuse.why}`,
      c?.summary ? `  ${c.name}: ${c.summary}` : '',
      `  samples: ${c?.samples.join(', ') || '—'}; readme: ${c?.readme ?? '—'}`,
      `  chart('${reuse.id}', el, data, options)  ·  node scripts/cli.mjs render ${reuse.id}${c?.samples.includes('realistic') ? ' --sample realistic' : ''}`,
      'Nothing was created. If this really is a different chart, extend the existing one with an option, or rerun with a more specific id (--force skips the similarity rules, never an exact id or alias).',
    ].filter(Boolean), { reuse: reuse.id, why: reuse.why, created: false, readme: c?.readme ?? null, samples: c?.samples ?? [] })
    return
  }
  const intentNames = new Set([...api.INTENTS, ...Object.keys(api.INTENT_ALIASES ?? {})])
  if (RESERVED.has(id) || intentNames.has(id) || api.CATEGORIES.includes(id)) fail(`'${id}' is reserved (${RESERVED.has(id) ? 'a pack folder or API word' : 'an intent or category name: render would resolve it as one'}); pick a chart id`)
  const { scaffold, CATEGORIES, CATEGORY_INTENTS } = await import('./scaffold.mjs')
  const category = flags.category ?? 'comparison'
  if (!CATEGORIES.includes(category)) fail(`unknown category '${category}' (one of: ${CATEGORIES.join(', ')})`)
  const intents = flags.intents ? String(flags.intents).split(',').map((s) => s.trim()).filter(Boolean).map((s) => api.resolveIntent(s) ?? fail(`unknown intent '${s}' (one of: ${api.INTENTS.join(', ')})`)) : CATEGORY_INTENTS[category]
  const name = flags.name ?? `${id.replace(/-/g, ' ').replace(/^./, (c) => c.toUpperCase())} chart`
  const near = search(id, charts, 45).slice(0, 3)

  const made = scaffold({ id, name, category, intents, summary: flags.summary })
  const steps = []
  const step = (label, argv) => { const r = run('node', argv); steps.push({ step: label, ok: r.ok, out: r.out }); return r }
  step('edges', ['scripts/edges.mjs', id])
  const lint = step('icons', ['icons/lint.mjs', '--write'])
  const lintMine = lint.out.split('\n').filter((l) => l.startsWith(`FAIL ${id}:`))
  const build = step('build', ['scripts/build.mjs'])
  const check = build.ok ? runCheck([id], { suites: 'layout,a11y' }) : null
  const files = [...made.files, ...readdirSync(join(made.dir, 'samples')).filter((f) => f.startsWith('edge-')).map((f) => join(made.dir, 'samples', f))]
  const green = build.ok && !lintMine.length && check && check.mine.length === 0 && check.steps.length > 0
  const lines = [
    `scaffolded ${id} (${category}; intents ${intents.join(', ')}) — ${name}`,
    ...(near.length ? [`  nearest existing: ${near.map((c) => `${c.id} (${c.score}: ${c.why})`).join('; ')}`] : []),
    `  files: ${files.map(show).join(', ')}`,
    `  edges: ${steps[0].out.split('\n').pop()}`,
    `  icon:  ${lintMine.length ? lintMine.join('; ') : `icons/svg/${id}.svg passes the spec${lint.ok ? '' : ` (the lint has ${lint.out.split('\n').filter((l) => l.startsWith('FAIL')).length} finding(s) elsewhere in the pack)`}`}`,
    `  build: ${build.ok ? build.out.split('\n').pop() : `FAILED\n${build.out.split('\n').slice(-10).join('\n')}`}`,
  ]
  if (check) {
    lines.push('', `$ ${check.cmd}`, check.summary)
    lines.push('', check.mine.length ? `${check.mine.length} failure(s) in ${id}: fix them before reshaping` : check.ok ? `${id}: all green` : `${id}: no failure names ${id}; ${check.others.length} failure(s) elsewhere in the pack are other work in progress`)
  }
  lines.push('', `next: reshape types/${id}/index.ts into the real chart (core/CHART_SPEC.md, types/bar as reference), then node tests/browser/shot.mjs ${id} --widths 320,768,1280 --themes light,dark,high-contrast --hover`)
  emit(flags, lines, { created: true, id, name, category, intents, files: files.map(show), steps: steps.map(({ step: s, ok }) => ({ step: s, ok })), iconFindings: lintMine, check: check && { ok: check.ok, command: check.cmd, steps: check.steps, failuresForId: check.mine, failuresElsewhere: check.others }, ok: !!green })
  if (!green) process.exitCode = 1
}

// ---------------------------------------------------------------- data-add

const ISO = /^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2})(?::(\d{2})(?:\.\d{1,9})?)?(Z|[+-]\d{2}:?\d{2})?)?$/
const isIso = (s) => typeof s === 'string' && ISO.test(s) && !Number.isNaN(Date.parse(s.length === 10 ? `${s}T00:00:00Z` : s))
const kindOf = (v) => (typeof v === 'number' ? (Number.isFinite(v) ? 'number' : 'bad-number') : typeof v === 'boolean' ? 'boolean' : typeof v === 'string' ? (isIso(v) ? 'date' : 'string') : Array.isArray(v) ? 'array' : typeof v === 'object' ? 'object' : typeof v)
const A = { number: 'a number', boolean: 'true/false', date: 'an ISO 8601 date', string: 'text' }
const showV = (v) => (typeof v === 'string' ? `'${v.length > 40 ? v.slice(0, 40) + '…' : v}'` : JSON.stringify(v))

/** Infer field specs for records; throws a CliError listing every inconsistency. */
function inferFields(records, what, given = {}, { tree = false } = {}) {
  const problems = []
  const keys = []
  for (const r of records) for (const k of Object.keys(r)) if (k !== 'children' && !keys.includes(k)) keys.push(k)
  if (!keys.length) fail(`${what}s have no fields`)
  const isLeaf = (r) => r.children === undefined
  const fields = {}
  for (const k of keys) {
    const present = records.map((r, i) => [r, i]).filter(([r]) => k in r)
    const absent = records.map((r, i) => [r, i]).filter(([r]) => !(k in r))
    let leafOnly = false
    if (absent.length) {
      if (tree && present.every(([r]) => isLeaf(r)) && absent.every(([r]) => !isLeaf(r))) leafOnly = true
      else for (const [, i] of absent.slice(0, 5)) problems.push(`${what} ${i + 1}: missing field '${k}' that the other ${what}s have`)
    }
    const counts = {}
    for (const [r] of present) if (r[k] !== null) { const t = kindOf(r[k]); counts[t] = (counts[t] ?? 0) + 1 }
    const ranked = Object.entries(counts).sort((a, b) => b[1] - a[1])
    if (!ranked.length) { problems.push(`field '${k}' is null in every ${what}; its type cannot be inferred`); continue }
    const major = ranked[0][0]
    if (!A[major]) { problems.push(`field '${k}' holds ${major === 'bad-number' ? 'non-finite numbers' : `${major}s`}; a dataset field must be a number, date, boolean or text`); continue }
    for (const [r, i] of present) if (r[k] !== null && kindOf(r[k]) !== major) {
      problems.push(`${what} ${i + 1}: field '${k}' is ${showV(r[k])}, expected ${A[major]} like the other ${what}s`)
      if (problems.length > 25) break
    }
    const nulls = present.filter(([r]) => r[k] === null).length
    const vals = present.map(([r]) => r[k]).filter((v) => v !== null)
    let type = major === 'string' ? 'category' : major
    if (major === 'string') {
      const distinct = new Set(vals).size
      if ((k === 'id' || k === 'parent' || /(^|_)id$|Id$/.test(k)) && (k !== 'id' || distinct === vals.length)) type = 'id'
      else if (distinct > Math.max(12, vals.length * 0.5) || vals.some((v) => v.length > 60)) type = 'text'
    }
    const g = isObj(given[k]) ? given[k] : {}
    fields[k] = { type, description: typeof g.description === 'string' && g.description.trim() ? g.description : `${k.replace(/[_-]+/g, ' ')} (${type}, inferred)`, ...(typeof g.unit === 'string' ? { unit: g.unit } : {}), ...(nulls ? { nulls } : {}), ...(leafOnly ? { on: 'leaf' } : {}) }
  }
  if (problems.length) fail(`${problems.length > 25 ? 'more than 25' : problems.length} problem(s):\n${problems.slice(0, 25).map((p) => `  ${p}`).join('\n')}`)
  return fields
}

/** Shape a loaded input into a dataset document + registry entry (minus intents/suggested). */
export function shapeDataset(name, input) {
  let data = input.data
  if (isObj(data) && !('children' in data) && !('nodes' in data) && Object.values(data).length && Object.values(data).every(Array.isArray)) {
    const cols = Object.entries(data), n = cols[0][1].length
    const bad = cols.find(([, v]) => v.length !== n)
    if (bad) fail(`column '${bad[0]}' has ${bad[1].length} values but column '${cols[0][0]}' has ${n}`)
    data = Array.from({ length: n }, (_, i) => Object.fromEntries(cols.map(([k, v]) => [k, v[i]])))
  }
  const given = isObj(input.fields) ? input.fields : {}
  if (Array.isArray(data)) {
    if (!data.length) fail('no rows: the data is an empty list')
    data.forEach((r, i) => { if (!isObj(r)) fail(`row ${i + 1} is ${showV(r)}, not an object of fields like { "label": "A", "value": 1 }`) })
    const fields = inferFields(data, 'row', given)
    return { kind: 'rows', rows: data.length, data, fields }
  }
  if (isObj(data) && Array.isArray(data.nodes) && Array.isArray(data.links)) {
    if (!data.nodes.length) fail('no rows: the graph has no nodes')
    data.nodes.forEach((r, i) => { if (!isObj(r)) fail(`node ${i + 1} is ${showV(r)}, not an object`) })
    data.links.forEach((r, i) => { if (!isObj(r)) fail(`link ${i + 1} is ${showV(r)}, not an object`) })
    const fields = inferFields(data.nodes, 'node', given)
    const linkFields = inferFields(data.links, 'link', isObj(input.linkFields) ? input.linkFields : {})
    return { kind: 'graph', rows: data.nodes.length, data, fields, linkFields }
  }
  if (isObj(data) && Array.isArray(data.children)) {
    const nodes = []
    const walk = (n, path) => {
      if (!isObj(n)) fail(`tree node at ${path} is ${showV(n)}, not an object`)
      nodes.push(n)
      if (n.children === undefined) return
      if (!Array.isArray(n.children) || !n.children.length) fail(`tree node at ${path} has "children" that is not a non-empty list`)
      n.children.forEach((c, i) => walk(c, `${path}.children[${i}]`))
    }
    walk(data, 'root')
    const fields = inferFields(nodes, 'tree node', given, { tree: true })
    return { kind: 'tree', rows: nodes.length, data, fields }
  }
  fail(`${input.label}: expected rows (a JSON list of objects or CSV), column arrays ({ "col": [...] }), a tree ({ "name", "children": [...] }) or a graph ({ "nodes": [...], "links": [...] }); got ${Array.isArray(data) ? 'a list' : data === null ? 'null' : typeof data}`)
}

async function cmdDataAdd({ pos, flags }) {
  const [name, file] = pos
  if (!name || !file) fail('usage: data-add <name> <file> [--description text]')
  if (!KEBAB.test(name)) fail(`dataset name '${name}' must be kebab-case (lowercase words joined by '-')`)
  const dir = datasetsDir()
  const { path: regPath, text: regText, reg } = readDatasetRegistry(dir)
  const target = join(dir, `${name}.json`)
  if (reg.datasets.some((d) => d.name === name) || existsSync(target)) fail(`dataset '${name}' already exists (${show(target)}); premade inputs are only added to, never replaced — pick another name`)
  const input = loadInput(file)
  const shaped = shapeDataset(name, input)

  const api = await loadApi()
  const ranked = []
  for (const intent of api.INTENTS) {
    let recs = []
    try { recs = api.recommend(intent, shaped.data) } catch { /* an intent the chooser cannot read this shape for */ }
    if (recs[0]) ranked.push({ intent, top: recs[0].score, picks: recs.map((r) => r.type) })
  }
  const good = ranked.filter((r) => r.top >= 60).sort((a, b) => b.top - a.top)
  if (!good.length) {
    const best = ranked.sort((a, b) => b.top - a.top)[0]
    fail(`no intent scores 60 or more for this data${best ? ` (best: ${best.intent} at ${best.top} with ${best.picks[0]})` : ''}: the chooser would not draw it well with any chart, so it is not added`)
  }
  const suggested = []
  for (const depth of [0, 1, 2]) for (const g of good) { const t = g.picks[depth]; if (t && !suggested.includes(t) && suggested.length < 8) suggested.push(t) }

  const description = flags.description ?? input.description ?? `${shaped.rows} ${shaped.kind === 'rows' ? 'rows' : 'nodes'} of ${Object.keys(shaped.fields).join(', ')}, added from ${basename(file)}.`
  const doc = { name, description, source: `added from ${basename(file)} with node scripts/cli.mjs data-add on ${today()}`, data: shaped.data, fields: shaped.fields, ...(shaped.linkFields ? { linkFields: shaped.linkFields } : {}) }
  const entry = { name, file: `${name}.json`, description, kind: shaped.kind, rows: shaped.rows, fields: Object.keys(shaped.fields), intents: good.map((g) => g.intent), suggested, added: today() }

  const validator = join(ROOT, 'datasets', 'validate.mjs')
  const problemsOf = (r) => (r.ok ? [] : r.out.split('\n').filter((l) => /^\s{2}\S/.test(l)).map((l) => l.trim()))
  const before = problemsOf(run('node', [validator, dir]))
  // one row per line, like the hand-written datasets; very large data stays compact
  const body = JSON.stringify(doc.data).length > 200_000 ? JSON.stringify(doc) : JSON.stringify({ ...doc, data: '__DATA__' }, null, 2).replace('"__DATA__"', `[\n${(Array.isArray(doc.data) ? doc.data.map((r) => `    ${JSON.stringify(r)}`).join(',\n') : `    ${JSON.stringify(doc.data)}`)}\n  ]`)
  // datasets/index.ts keeps a typed DATASET_NAMES list and README.md a table, both in registry
  // order (datasets.test.ts compares them): append to both, and restore them on rollback
  const sidecars = ['index.ts', 'README.md'].map((f) => join(dir, f)).filter((p) => existsSync(p)).map((p) => [p, readFileSync(p, 'utf8')])
  writeFileSync(target, body + '\n')
  writeFileSync(regPath, JSON.stringify({ ...reg, datasets: [...reg.datasets, entry] }, null, 2) + '\n')
  for (const [p, text] of sidecars) {
    if (p.endsWith('index.ts')) writeFileSync(p, text.replace(/(export const DATASET_NAMES = \[[\s\S]*?)(\n\] as const)/, `$1\n  '${name}',$2`))
    else {
      const lines = text.split('\n')
      const head = lines.findIndex((l) => /^\| name \| kind \| rows \|/.test(l))
      if (head < 0) continue
      let end = head + 1
      while (end < lines.length && lines[end].startsWith('|')) end++
      lines.splice(end, 0, `| \`${name}\` | ${entry.kind} | ${entry.rows} | ${description.replace(/\|/g, '\\|')} |`)
      writeFileSync(p, lines.join('\n'))
    }
  }
  const after = run('node', [validator, dir])
  const fresh = problemsOf(after).filter((p) => !before.includes(p))
  if (!after.ok && fresh.length) {
    writeFileSync(regPath, regText)
    for (const [p, text] of sidecars) writeFileSync(p, text)
    rmSync(target, { force: true })
    fail(`dataset '${name}' failed validation and was rolled back (nothing written):\n${fresh.map((p) => `  ${p}`).join('\n')}`)
  }
  const rebuilt = dir === join(ROOT, 'datasets') ? run('node', ['scripts/build.mjs']) : null
  emit(flags, [
    `added dataset '${name}': ${entry.kind}, ${entry.rows} ${entry.kind === 'rows' ? 'rows' : 'nodes'}, fields ${Object.entries(shaped.fields).map(([k, f]) => `${k}:${f.type}${f.nulls ? `(${f.nulls} null)` : ''}`).join(', ')}`,
    `  intents: ${good.map((g) => `${g.intent} (${g.top})`).join(', ')}`,
    `  suggested: ${suggested.join(', ')}`,
    `  wrote ${show(target)} and appended the entry to ${show(regPath)}`,
    `  ${after.out.split('\n').pop()}${before.length ? ` (${before.length} problem(s) were already there before this add)` : ''}`,
    ...(rebuilt ? [`  ${rebuilt.ok ? rebuilt.out.split('\n').pop() : `build failed: ${rebuilt.out.split('\n').slice(-3).join(' ')}`}`] : []),
    `  field descriptions marked "(inferred)" are placeholders: edit them in ${show(target)}`,
  ], { ok: true, entry, fields: shaped.fields, file: target, registry: regPath, preexistingProblems: before })
}

// ---------------------------------------------------------------- sample-add

async function cmdSampleAdd({ pos, flags }) {
  const [type, file] = pos
  if (!type || !file) fail('usage: sample-add <type> <file> [--name edge-foo|realistic-2] [--description text] [--options json]')
  const api = await loadApi()
  const id = api.resolveType(type) ?? (existsSync(join(ROOT, 'types', type, 'index.ts')) ? type : null)
  if (!id) fail(`unknown chart type '${type}'`)
  const name = flags.name ?? kebab(basename(file, extname(file))).replace(/[^a-z0-9-]/g, '')
  if (!KEBAB.test(name)) fail(`sample name '${name}' must be kebab-case (e.g. edge-foo, realistic-2); pass --name`)
  const target = join(ROOT, 'types', id, 'samples', `${name}.json`)
  if (existsSync(target)) fail(`${show(target)} already exists; samples are only added to, never replaced — pass another --name`)
  const input = loadInput(file)
  const options = { ...input.options, ...jsonFlag(flags, 'options') }
  const data = input.csv ?? input.data
  const v = api.validate(id, data, options)
  if (!v.ok) fail(`not valid ${id} input, so no sample was written:\n${v.errors.map((e) => `  - ${e.message}`).join('\n')}`)
  const sample = { description: flags.description ?? input.description ?? `Added from ${basename(file)} with cli sample-add.`, data, ...(Object.keys(options).length ? { options } : {}) }
  writeFileSync(target, JSON.stringify(sample, null, 2) + '\n')
  const t = run('npx', ['vitest', 'run', `types/${id}`])
  const tail = t.out.replace(ANSI, '').split('\n').filter((l) => /Test Files|Tests |FAIL|✗|×|AssertionError|Error:/.test(l)).slice(0, 30)
  emit(flags, [`wrote ${show(target)}`, `$ npx vitest run types/${id}`, ...tail, t.ok ? `${id}: green with the new sample` : `${id}: tests FAIL with the new sample — the sample stays (it found a bug); fix the chart, or delete ${show(target)} if the sample is wrong`], { ok: t.ok, file: target, name, type: id, tests: tail })
  if (!t.ok) process.exitCode = 1
}

// ---------------------------------------------------------------- main

const USAGE = `chart pack CLI (cd .omp/charts)
  render <type|alias|intent> [data] [--sample basic] [--theme light|dark|high-contrast] [--width 960] [--height px] [--out dir] [--options json]
  recommend <intent> <data>
  lookup <query>
  validate <type> <data|inline json> [--options json]
  datasets [name]
  new <id> [--category c] [--name 'Display name'] [--intents a,b] [--summary text] [--force]
  data-add <name> <file> [--description text]
  sample-add <type> <file> [--name edge-foo] [--description text] [--options json]
  check [type,...] [--suites layout,a11y] [--skip-browser] [--category c]
  gallery [--open]
<data>: a file (JSON, {data, options}, a datasets/ file, or CSV), sample:<type>/<name>, or dataset:<name>.
--json on any command prints one JSON object. CHART_DATASETS_DIR overrides datasets/.`

const COMMANDS = { render: cmdRender, recommend: cmdRecommend, lookup: cmdLookup, validate: cmdValidate, datasets: cmdDatasets, new: cmdNew, 'data-add': cmdDataAdd, 'sample-add': cmdSampleAdd, check: cmdCheck, gallery: cmdGallery }

async function main(argv) {
  const [cmd, ...rest] = argv
  if (!cmd || cmd === 'help' || cmd === '--help') { process.stdout.write(USAGE + '\n'); return }
  const fn = COMMANDS[cmd]
  let args = { pos: [], flags: {} }
  try {
    args = parseArgs(rest)
    if (!fn) fail(`unknown command '${cmd}'\n${USAGE}`)
    await fn(args)
  } catch (e) {
    const msg = e instanceof CliError ? e.message : String(e?.stack ?? e)
    if (args.flags.json || rest.includes('--json')) process.stdout.write(JSON.stringify({ ok: false, error: msg }, null, 2) + '\n')
    else process.stderr.write(`error: ${msg}\n`)
    process.exitCode = 1
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await main(process.argv.slice(2))
