// Scaffold a complete, working chart folder: a generic label/value chart drawn with
// core, shaped by its category (bars, or points on a band axis for the time and
// distribution families). It passes chartSuite as written; the chart-smith agent then
// reshapes it into the real chart. `node scripts/cli.mjs new <id>` is the entry point:
// it checks the registry for an existing chart first and runs the edges, icon lint,
// build and scoped check afterwards.
//
//   import { plan, scaffold, remove, iconSvg } from './scaffold.mjs'
//   plan(id, { name, category, intents, summary })  ->  validated spec (throws naming the problem)
//   scaffold({ id, name, category, intents })       ->  { dir, files: [...] }
//
//   node scripts/scaffold.mjs --remove <id> [--force]
//     delete a scaffold (types/<id>/, icons/svg/<id>.svg, its icons.json entry), then rebuild
//     the sprite, dist/, registry.json and gallery.html. Refuses a chart without the
//     scaffold note unless --force.

import { createHash } from 'node:crypto'
import { spawnSync } from 'node:child_process'
import { existsSync, mkdirSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { join, dirname, basename } from 'node:path'
import { fileURLToPath } from 'node:url'
import { parseSvg } from '../icons/lint.mjs'

export const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')

export const CATEGORIES = ['comparison', 'part-to-whole', 'trend', 'timeline', 'distribution', 'relationship', 'flow-hierarchy', 'matrix', 'geo', 'kpi', 'sets']

/** Intents a category's charts serve unless --intents says otherwise. */
export const CATEGORY_INTENTS = {
  comparison: ['compare', 'rank'],
  'part-to-whole': ['composition'],
  trend: ['trend', 'change-over-time'],
  timeline: ['timeline', 'schedule'],
  distribution: ['distribution'],
  relationship: ['relationship', 'correlation'],
  'flow-hierarchy': ['flow', 'hierarchy'],
  matrix: ['compare'],
  geo: ['geo'],
  kpi: ['kpi'],
  sets: ['sets'],
}

/** Points on a band axis for these; bars from zero for the rest. */
const POINT_CATEGORIES = new Set(['trend', 'timeline', 'distribution', 'relationship'])
/** These connect consecutive points with a line. */
const CONNECTED = new Set(['trend', 'timeline'])

/** Realistic sample per category: a field mapping from real-looking column names. */
const REALISTIC = {
  comparison: { description: 'Quarterly revenue by sales region (USD), mapped from export columns.', label: 'region', value: 'revenue_usd', title: 'Revenue (USD)', format: 'currency', rows: [['North America', 1284000], ['Europe', 963500], ['Asia Pacific', 812250], ['Latin America', 301900], ['Middle East', 214300], ['Africa', 98700], ['Oceania', 176400], ['Central Asia', 61250]] },
  'part-to-whole': { description: 'Operating budget by department (USD), mapped from export columns.', label: 'department', value: 'budget_usd', title: 'Budget (USD)', format: 'currency', rows: [['Engineering', 4200000], ['Sales', 2650000], ['Marketing', 1380000], ['Support', 910000], ['Operations', 720000], ['Finance', 480000], ['Legal', 310000], ['People', 405000]] },
  trend: { description: 'Monthly active users (thousands), January to August.', label: 'month', value: 'active_users_k', title: 'Active users (k)', rows: [['Jan', 41.2], ['Feb', 43.8], ['Mar', 47.1], ['Apr', 46.4], ['May', 51.9], ['Jun', 55.3], ['Jul', 54.0], ['Aug', 58.7]] },
  timeline: { description: 'Working days spent in each phase of a product launch.', label: 'phase', value: 'days', title: 'Days', rows: [['Discovery', 12], ['Design', 18], ['Prototype', 9], ['Build', 34], ['Beta', 21], ['Hardening', 8], ['Launch', 3], ['Review', 5]] },
  distribution: { description: 'Requests per response-time bucket over one day.', label: 'bucket', value: 'requests', title: 'Requests', rows: [['0-50 ms', 18240], ['50-100 ms', 30410], ['100-200 ms', 22175], ['200-400 ms', 9620], ['400-800 ms', 3180], ['0.8-1.6 s', 940], ['1.6-3.2 s', 212], ['> 3.2 s', 37]] },
  relationship: { description: 'Average deal size by account segment (USD).', label: 'segment', value: 'avg_deal_usd', title: 'Average deal (USD)', format: 'currency', rows: [['Startup', 4800], ['SMB', 11250], ['Mid-market', 38400], ['Enterprise', 152000], ['Public sector', 96500], ['Education', 22300], ['Nonprofit', 6900], ['Partner', 27600]] },
  'flow-hierarchy': { description: 'Sessions by traffic source in one week.', label: 'source', value: 'sessions', title: 'Sessions', rows: [['Organic search', 48210], ['Direct', 31540], ['Referral', 12870], ['Email', 9420], ['Paid search', 15360], ['Social', 7180], ['Partners', 3260], ['Other', 1150]] },
  matrix: { description: 'Incidents per service in the last quarter.', label: 'service', value: 'incidents', title: 'Incidents', rows: [['API gateway', 14], ['Auth', 6], ['Billing', 9], ['Search', 11], ['Storage', 4], ['Queue', 7], ['Notifications', 3], ['Web app', 12]] },
  geo: { description: 'Population by country (millions), 2026 estimate.', label: 'country', value: 'population_m', title: 'Population (millions)', rows: [['India', 1463.9], ['China', 1416.1], ['United States', 347.3], ['Indonesia', 285.7], ['Pakistan', 255.2], ['Nigeria', 237.5], ['Brazil', 212.8], ['Bangladesh', 175.7]] },
  kpi: { description: 'Share of quarterly target reached per team (%).', label: 'team', value: 'target_pct', title: 'Target reached (%)', rows: [['Platform', 104], ['Mobile', 87], ['Web', 96], ['Data', 112], ['Growth', 78], ['Support', 99], ['Security', 91], ['Infra', 83]] },
  sets: { description: 'Users per feature combination in the last 30 days.', label: 'features', value: 'users', title: 'Users', rows: [['Chat only', 12400], ['Search only', 8650], ['Chat + Search', 6120], ['Docs only', 4310], ['Chat + Docs', 2980], ['Search + Docs', 1870], ['All three', 1420], ['None', 960]] },
}

const BASIC = [['Alpha', 120], ['Bravo', 340], ['Charlie', 210], ['Delta', 90], ['Echo', 275]]

export const camel = (id) => {
  const c = id.replace(/-([a-z0-9])/g, (_, ch) => ch.toUpperCase())
  return /^[0-9]/.test(c) ? `c${c}` : c
}
const pascal = (id) => { const c = camel(id); return c[0].toUpperCase() + c.slice(1) }
const q = (s) => `'${String(s).replace(/\\/g, '\\\\').replace(/'/g, "\\'")}'`
const reEscape = (s) => s.replace(/[.*+?^${}()|[\]\\/]/g, '\\$&')

export function mode(category) {
  return { points: POINT_CATEGORIES.has(category), connected: CONNECTED.has(category) }
}

function schemaTs(id) {
  const P = pascal(id)
  return `import { z } from 'zod'
import { fields } from '../../core'

/** One mark per row. Duplicate labels are summed (and noted under the chart). */
export const input = fields.rows({
  label: fields.label('category on the x axis'),
  value: fields.numOrNull('the measured value; null leaves a gap'),
}, 'rows of { label, value }')

export const options = z.object({
  sort: z.enum(['none', 'ascending', 'descending']).default('none').describe('mark order along the x axis'),
  showValues: z.boolean().default(false).describe('print each value beside its mark'),
  xTitle: z.string().optional(),
  yTitle: z.string().optional(),
})

export type ${P}Row = z.output<typeof input>[number]
export type ${P}Options = z.output<typeof options>
`
}

function indexTs({ id, name, category, intents, summary }) {
  const c = camel(id), P = pascal(id)
  const { points, connected } = mode(category)
  const role = points ? 'point' : 'bar'
  const markCode = points
    ? `      const cx = f.px(d.label) + f.bw / 2, cy = f.py(d.value)
      const fill = theme.cat(0)
      marks.push({
        type: 'circle', role: 'point', key: d.label, cx, cy, r, fill,
        datum: d, label: \`\${d.label}: \${fmt.value(d.value)}\`, tooltip: { title: d.label, rows: [['Value', fmt.value(d.value)]], color: fill },
      })
      if (o.showValues && f.bw >= valueSpace * 0.6) {
        marks.push({ type: 'text', layer: 'labels', key: \`v:\${d.label}\`, x: cx, y: cy - r - 4, text: fmt.value(d.value), anchor: 'middle', baseline: 'alphabetic', fill: theme.ink, size: 11, halo: true, inert: true })
      }`
    : `      const x = f.px(d.label), yv = f.py(d.value)
      const fill = theme.cat(0)
      marks.push({
        type: 'rect', role: 'bar', key: d.label, x, width: Math.max(0.5, f.bw), y: d.value === 0 ? y0 - 0.5 : Math.min(y0, yv), height: d.value === 0 ? 1 : Math.abs(yv - y0),
        rx: Math.min(2, f.bw / 4), fill,
        datum: d, label: \`\${d.label}: \${fmt.value(d.value)}\`, tooltip: { title: d.label, rows: [['Value', fmt.value(d.value)]], color: fill },
      })
      if (o.showValues && f.bw >= valueSpace * 0.6) {
        marks.push({ type: 'text', layer: 'labels', key: \`v:\${d.label}\`, x: x + f.bw / 2, y: d.value >= 0 ? yv - 4 : yv + 4, text: fmt.value(d.value), anchor: 'middle', baseline: d.value >= 0 ? 'alphabetic' : 'hanging', fill: theme.ink, size: 11, inert: true })
      }`
  const connector = connected
    ? `
    // consecutive points joined by one decorative path (no datum: not a data mark); a null breaks it
    let path = ''
    let pen = false
    for (const it of items) {
      if (it.value === null) { pen = false; continue }
      path += \`\${pen ? 'L' : 'M'}\${(f.px(it.label) + f.bw / 2).toFixed(2)},\${f.py(it.value).toFixed(2)}\`
      pen = true
    }
    if (path) marks.splice(f.marks.length, 0, { type: 'path', key: 'connector', d: path, fill: 'none', stroke: theme.cat(0), strokeWidth: 1.5, strokeOpacity: 0.6, linejoin: 'round', inert: true })
`
    : ''
  return `// ${name}: a generic label/value chart. ${MARKER} (\`node scripts/cli.mjs new ${id}\`).
// ${points ? 'One point per category on a band axis' : 'One bar per category, from zero'}. Reshape it into the real chart per core/CHART_SPEC.md.

import { defineChart, phrase } from '../../core'
import { collapseLabels, finiteExtent, notes, sortItems } from '../../core/helpers'
import type { Mark } from '../../core'
import { input, options, type ${P}Options, type ${P}Row } from './schema'

export const ${c} = defineChart<${P}Row[], ${P}Options>({
  id: ${q(id)},
  name: ${q(name)},
  category: ${q(category)},
  intents: [${intents.map(q).join(', ')}],
  summary: ${q(summary)},
  input,
  options,
  shape: 'rows',
  fields: ['label', 'value'],
  numberFields: ['value'],
  draw(ctx) {
    const { theme, options: o, fmt } = ctx
    const { items: raw, duplicates } = collapseLabels(ctx.data)
    const items = sortItems(raw, o.sort)
    const ext = finiteExtent(items.map((d) => d.value)) ?? [0, 1]
    const valueSpace = o.showValues ? ctx.measure('000.0k') : 0
    const f = ctx.cartesian({
      x: { type: 'band', domain: items.map((d) => d.label), padding: ${points ? '0.1' : '0.2'} },
      y: { type: 'linear', domain: [Math.min(0, ext[0]), Math.max(0, ext[1])], zero: true },
      xTitle: o.xTitle,
      yTitle: o.yTitle,
      pad: { top: o.showValues ? 14 : ${points ? '6' : '0'} },
    })
${points ? '    const r = Math.max(1.5, Math.min(5, f.bw / 3))\n' : '    const y0 = f.py(0)\n'}    const marks: Mark[] = [...f.marks]
    items.forEach((d) => {
      if (d.value === null) return
${markCode}
    })${connector}
    const nulls = items.filter((d) => d.value === null).length
    return {
      marks,
      plot: f.plot,
      note: notes([[duplicates, 'duplicate labels summed'], [nulls, 'missing values left blank']]),
    }
  },
  describe({ data, fmt, name }) {
    const { items } = collapseLabels(data)
    return \`\${name}, \${phrase.count(items.length, ${q(role)})}, \${phrase.extremes(items, fmt)}\`
  },
  table(data) {
    const { items } = collapseLabels(data)
    return { columns: ['Label', 'Value'], rows: items.map((d) => [d.label, d.value]) }
  },
})

export default ${c}
`
}

function testTs({ id, name, category }) {
  const c = camel(id)
  const { points } = mode(category)
  const role = points ? 'point' : 'bar'
  const geometry = points
    ? `  it('points sit higher for larger values, on one shared zero-based scale', () => {
    const el = host(640, 400)
    const h = ${c}.render(el, [{ label: 'a', value: 10 }, { label: 'b', value: 20 }, { label: 'c', value: 0 }], { animate: false })
    const ys = [...el.querySelectorAll('[data-role="point"]')].map((p) => Number(p.getAttribute('cy')))
    expect(ys).toHaveLength(3)
    // linear: the 0 -> 10 drop equals the 10 -> 20 drop
    expect(ys[2]! - ys[0]!).toBeCloseTo(ys[0]! - ys[1]!, 1)
    expect(ys[1]!).toBeLessThan(ys[0]!)
    h.destroy()
  })`
    : `  it('bar heights are proportional to values and start at zero', () => {
    const el = host(640, 400)
    const h = ${c}.render(el, [{ label: 'a', value: 10 }, { label: 'b', value: 20 }, { label: 'c', value: -5 }], { animate: false })
    const rects = [...el.querySelectorAll('[data-role="bar"]')]
    const hs = rects.map((r) => Number(r.getAttribute('height')))
    expect(hs[1]! / hs[0]!).toBeCloseTo(2, 1)
    expect(hs[2]! / hs[0]!).toBeCloseTo(0.5, 1)
    // the negative bar hangs from the zero line
    expect(Number(rects[2]!.getAttribute('y'))).toBeCloseTo(Number(rects[0]!.getAttribute('y')) + hs[0]!, 1)
    h.destroy()
  })`
  return `import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import ${c} from './index'

chartSuite({
  module: ${c},
  dir: import.meta.dirname,
  role: ${q(role)},
  marks: { basic: 5, realistic: 8, 'edge-single': 1, 'edge-duplicates': 5, 'edge-zeros': 5, 'edge-large': 5200 },
  summary: /^${reEscape(name)}, 5 ${role}s, highest Bravo at 340, lowest Delta at 90$/,
  malformed: [
    { name: 'a text value', data: [{ label: 'A', value: 1 }, { label: 'B', value: 'abc' }], message: /field 'value' must be a number, got 'abc' at row 2/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
    { name: 'mismatched columns', data: { label: ['a', 'b'], value: [1] }, message: /column 'value' has 1 values but column 'label' has 2/ },
  ],
})

describe('${id}: geometry', () => {
${geometry}
  it('sums duplicate labels and says so', () => {
    const el = host()
    const h = ${c}.render(el, [{ label: 'a', value: 1 }, { label: 'a', value: 2 }], { animate: false })
    expect(h.markCount()).toBe(1)
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/1 duplicate labels summed/)
    expect(h.exportCSV()).toBe('Label,Value\\na,3')
    h.destroy()
  })
  it('sorts descending', () => {
    const el = host()
    const h = ${c}.render(el, [{ label: 'a', value: 1 }, { label: 'b', value: 3 }, { label: 'c', value: 2 }], { animate: false, sort: 'descending' })
    expect([...el.querySelectorAll('[data-role="${role}"]')].map((m) => m.getAttribute('data-key'))).toEqual(['${points ? 'circle' : 'rect'}:b', '${points ? 'circle' : 'rect'}:c', '${points ? 'circle' : 'rect'}:a'])
    h.destroy()
  })
})
`
}

function readmeMd({ id, name, category, intents, summary }) {
  const { points, connected } = mode(category)
  const r = REALISTIC[category]
  return `# ${name} (\`${id}\`)

${summary}

## When to use
- ${points ? 'One value per category where the position of a point carries the reading' : 'Comparing one value across a handful to a few dozen categories'} (${category}; intents: ${intents.join(', ')}).

## When not to use
- Many categories or long names: \`horizontal-bar\`.
- Several series per category: \`grouped-bar\` or \`stacked-bar\`.
- Values over continuous time: \`line\` or \`area\`.
- Parts of a whole: \`stacked-bar-100\`, \`pie\` (≤ 6 parts) or \`treemap\`.

## Input
Rows of \`{ label, value }\`. \`value\` may be \`null\` (left blank, noted).
\`\`\`json
[{ "label": "Alpha", "value": 120 }, { "label": "Bravo", "value": 340 }]
\`\`\`
Also accepted: CSV text (\`"label,value\\nAlpha,120"\`), column arrays (\`{ "label": [...], "value": [...] }\`),
and any column names through a field mapping: \`{ fields: { label: '${r.label}', value: '${r.value}' } }\`.
Duplicate labels are summed and noted under the chart.

## Options
| Option | Type | Default | |
|---|---|---|---|
| \`sort\` | \`none \\| ascending \\| descending\` | \`none\` | mark order |
| \`showValues\` | boolean | \`false\` | value beside each mark (dropped when marks are too narrow) |
| \`xTitle\`, \`yTitle\` | string | — | axis titles |

Every chart also takes the base options (\`valueFormat\`, \`locale\`, \`currency\`, \`theme\`,
\`patterns\`, \`title\`, \`onClick\`, ... — see \`core/fields.ts\`).

## Example
\`\`\`ts
import { chart } from '.omp/charts'
chart('${id}', el, rows, { fields: { label: '${r.label}', value: '${r.value}' }, sort: 'descending' })
\`\`\`

## Edge cases
Empty → empty state. Zeros → ${points ? 'points on the baseline' : '1px stubs on the baseline (still hoverable)'}. Negatives ${points ? 'sit below the zero line' : 'hang below the zero line'}.
Nulls → gaps${connected ? ' (the connecting line breaks)' : ''} + note. Duplicates → summed + note. 5,200 rows → canvas.
Long and RTL labels rotate, truncate, and show the full text on hover.
`
}

function realisticJson(category) {
  const r = REALISTIC[category]
  return {
    description: r.description,
    data: r.rows.map(([l, v]) => ({ [r.label]: l, [r.value]: v })),
    options: { fields: { label: r.label, value: r.value }, ...(r.format ? { valueFormat: r.format } : {}), yTitle: r.title },
  }
}

// ---------------------------------------------------------------- icon

const geometryHash = (text) => {
  const { elements } = parseSvg(text)
  return createHash('sha1').update(JSON.stringify(elements.map((e) => [e.tag, Object.entries(e.attrs).sort()]))).digest('hex')
}

/** The icon for `id`: a small bar or dot layout derived from a hash of the id, unique among icons/svg. */
export function iconSvg(id, category, existing = existingIconHashes(id)) {
  const { points, connected } = mode(category)
  for (let salt = 0; salt < 64; salt++) {
    const b = createHash('sha1').update(`${id}#${salt}`).digest()
    const n = (points ? 4 : 3) + (b[0] % 3)
    const span = 16, gap = span / n
    const parts = ['<path d="M3 21h18"/>']
    if (points) {
      const pts = Array.from({ length: n }, (_, i) => [+(4 + gap * i + gap / 2 - 0.5).toFixed(1), 5 + (b[i + 1] % 13)])
      if (connected) parts.push(`<polyline points="${pts.map(([x, y]) => `${x} ${y}`).join(' ')}"/>`)
      for (const [x, y] of pts) parts.push(`<circle cx="${x}" cy="${y}" r="1.5"/>`)
    } else {
      const w = +(gap * 0.6).toFixed(1)
      for (let i = 0; i < n; i++) {
        const h = 4 + (b[i + 1] % 14)
        parts.push(`<rect x="${(4 + gap * i + (gap - w) / 2).toFixed(1)}" y="${19 - h}" width="${w}" height="${h}" rx="0.5"/>`)
      }
    }
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">\n  ${parts.join('\n  ')}\n</svg>\n`
    if (!existing.has(geometryHash(svg))) return svg
  }
  throw new Error(`could not derive a unique icon for '${id}'`)
}

function existingIconHashes(skip) {
  const dir = join(ROOT, 'icons', 'svg')
  const out = new Set()
  if (!existsSync(dir)) return out
  for (const f of readdirSync(dir)) if (f.endsWith('.svg') && basename(f, '.svg') !== skip) {
    try { out.add(geometryHash(readFileSync(join(dir, f), 'utf8'))) } catch { /* the icon lint reports unparseable files */ }
  }
  return out
}

// ---------------------------------------------------------------- folder

/**
 * Write types/<id>/ (index.ts, schema.ts, samples/basic.json + realistic.json, <id>.test.ts,
 * README.md) and icons/svg/<id>.svg. Refuses when any of them exists.
 */
export function scaffold({ id, name, category, intents, summary, root = ROOT }) {
  if (!CATEGORIES.includes(category)) throw new Error(`unknown category '${category}' (one of: ${CATEGORIES.join(', ')})`)
  const dir = join(root, 'types', id)
  const icon = join(root, 'icons', 'svg', `${id}.svg`)
  for (const p of [dir, icon]) if (existsSync(p)) throw new Error(`${p} already exists; scaffold never overwrites`)
  const spec = { id, name, category, intents, summary: summary ?? `${name}: one ${mode(category).points ? 'point' : 'bar'} per category, from a zero baseline.` }
  const files = {
    [join(dir, 'schema.ts')]: schemaTs(id),
    [join(dir, 'index.ts')]: indexTs(spec),
    [join(dir, `${id}.test.ts`)]: testTs(spec),
    [join(dir, 'README.md')]: readmeMd(spec),
    [join(dir, 'samples', 'basic.json')]: JSON.stringify({ description: 'Five categories, one value each.', data: BASIC.map(([label, value]) => ({ label, value })) }, null, 2) + '\n',
    [join(dir, 'samples', 'realistic.json')]: JSON.stringify(realisticJson(category), null, 2) + '\n',
    [icon]: iconSvg(id, category),
  }
  mkdirSync(join(dir, 'samples'), { recursive: true })
  for (const [p, text] of Object.entries(files)) writeFileSync(p, text)
  return { dir, icon, files: Object.keys(files) }
}

// ---------------------------------------------------------------- plan / remove / command line

export const MARKER = 'Scaffolded by scripts/scaffold.mjs'
export const INTENTS = ['compare', 'rank', 'composition', 'trend', 'timeline', 'schedule', 'distribution', 'relationship', 'correlation', 'flow', 'hierarchy', 'network', 'geo', 'kpi', 'change-over-time', 'deviation', 'sets']
export const ID_RE = /^[a-z][a-z0-9]*(-[a-z0-9]+)*$/

/** Validate an id and options and fill in the defaults; throws an Error naming the problem. */
export function plan(id, o = {}) {
  if (!id || !ID_RE.test(id)) throw new Error(`'${id ?? ''}' is not a kebab-case id starting with a letter (e.g. 'lollipop', 'radial-bar')`)
  if (INTENTS.includes(id) || CATEGORIES.includes(id)) throw new Error(`'${id}' is reserved: it is ${INTENTS.includes(id) ? 'an intent' : 'a category'} name`)
  const category = o.category ?? 'comparison'
  if (!CATEGORIES.includes(category)) throw new Error(`unknown category '${category}' (one of: ${CATEGORIES.join(', ')})`)
  const intents = o.intents ? (Array.isArray(o.intents) ? o.intents : String(o.intents).split(',')).map((s) => s.trim()).filter(Boolean) : CATEGORY_INTENTS[category]
  const bad = intents.filter((i) => !INTENTS.includes(i))
  if (bad.length) throw new Error(`unknown intent${bad.length > 1 ? 's' : ''} ${bad.map((b) => `'${b}'`).join(', ')} (one of: ${INTENTS.join(', ')})`)
  if (!intents.length) throw new Error('at least one intent is needed')
  const words = id.split('-').join(' ')
  const name = o.name ?? `${words[0].toUpperCase()}${words.slice(1)} chart`
  return { id, name, category, intents, summary: o.summary }
}

/** Delete a scaffold: types/<id>/, icons/svg/<id>.svg and its icons.json entry. Refuses a real chart unless forced. */
export function remove(id, { force = false, root = ROOT } = {}) {
  if (!id || !ID_RE.test(id)) throw new Error(`'${id ?? ''}' is not a kebab-case id`)
  const dir = join(root, 'types', id)
  const index = join(dir, 'index.ts')
  if (existsSync(index) && !force && !readFileSync(index, 'utf8').includes(MARKER)) throw new Error(`types/${id}/index.ts is not a scaffold (no "${MARKER}" note); pass --force to delete a real chart`)
  const removed = []
  if (existsSync(dir)) { rmSync(dir, { recursive: true, force: true }); removed.push(`types/${id}/`) }
  const icon = join(root, 'icons', 'svg', `${id}.svg`)
  if (existsSync(icon)) { rmSync(icon); removed.push(`icons/svg/${id}.svg`) }
  const metaPath = join(root, 'icons', 'icons.json')
  if (existsSync(metaPath)) {
    const meta = JSON.parse(readFileSync(metaPath, 'utf8'))
    const n = meta.icons.length
    meta.icons = meta.icons.filter((i) => i.name !== id)
    if (meta.icons.length !== n) { writeFileSync(metaPath, JSON.stringify(meta, null, 2) + '\n'); removed.push(`icons/icons.json entry '${id}'`) }
  }
  return removed
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const args = process.argv.slice(2)
  const at = args.indexOf('--remove')
  try {
    if (at < 0) throw new Error('usage: node scripts/scaffold.mjs --remove <id> [--force]   (create with: node scripts/cli.mjs new <id>)')
    const id = args[at + 1]
    const removed = remove(id, { force: args.includes('--force') })
    // regenerate everything the folder fed: the sprite, the bundle, the registry and the gallery
    const steps = [['icons', ['icons/lint.mjs', '--write']], ['build', ['scripts/build.mjs']], ['registry', ['scripts/registry.mjs']], ['gallery', ['scripts/gallery.mjs']]]
    const done = steps.map(([name, argv]) => `${name} ${spawnSync('node', argv, { cwd: ROOT, stdio: 'ignore' }).status === 0 ? 'ok' : 'FAIL'}`)
    console.log(removed.length ? `removed ${removed.join(', ')}\n${done.join(', ')}` : `nothing to remove for '${id}'`)
  } catch (e) {
    console.error(`error: ${e.message}`)
    process.exit(1)
  }
}
