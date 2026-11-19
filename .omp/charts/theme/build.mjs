#!/usr/bin/env node
// theme/tokens.json (+ the design pack's resolved tokens) -> chart-tokens.css and
// tokens.generated.ts, then the palette gates. `--check` regenerates in memory and
// fails if either output is stale or any gate fails.
//
//   cd .omp/charts && npm run theme:build            # write
//   node theme/build.mjs --check                      # verify only

import { readFileSync, writeFileSync, existsSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { ramp, contrast, minSeparation, CVD_KINDS } from './color.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const DESIGN_FLAT = join(here, '../../design/tokens/tokens.flat.json')
export const THEMES = ['light', 'dark', 'high-contrast']
export const SEQ_STEPS = 9
export const DIV_STEPS = 11
// Gates. Contrast is WCAG 1.4.11 (graphics 3:1); high-contrast promises text-level 4.5:1.
// Separation is the smallest pairwise OKLab distance x100 under each simulated vision kind;
// tritanopia (~0.01% of people) has its own, lower floor. Okabe-Ito itself scores ~7.6.
export const GATES = {
  contrast: { light: 3, dark: 3, 'high-contrast': 4.5 },
  separation: { light: 8, dark: 8, 'high-contrast': 3.4 },
  tritanopia: { light: 6, dark: 6, 'high-contrast': 3.4 },
}

function loadDesign() {
  if (!existsSync(DESIGN_FLAT)) throw new Error(`design tokens not found at ${DESIGN_FLAT} — run: cd .omp && npm run tokens:build`)
  return JSON.parse(readFileSync(DESIGN_FLAT, 'utf8'))
}

function resolveScalar(v, theme, design, path) {
  if (typeof v !== 'string') return v
  const m = v.match(/^\{design\.([a-z0-9-]+)\}$/)
  if (!m) return v
  const d = design[m[1]]
  if (d === undefined) throw new Error(`${path}: alias ${v} does not exist in the design pack`)
  return typeof d === 'object' ? d[theme] : d
}
function resolve(value, theme, design, path) {
  if (value && typeof value === 'object' && !Array.isArray(value)) return resolveScalar(value[theme], theme, design, path)
  return resolveScalar(value, theme, design, path)
}

/** Flatten the token tree to { name: { light, dark, high-contrast } }. */
export function resolveTokens() {
  const design = loadDesign()
  const src = JSON.parse(readFileSync(join(here, 'tokens.json'), 'utf8'))
  const out = {}
  const walk = (node, path) => {
    if (node && typeof node === 'object' && '$value' in node) {
      out[path.join('-')] = Object.fromEntries(THEMES.map((t) => [t, resolve(node.$value, t, design, path.join('.'))]))
      return
    }
    for (const [k, v] of Object.entries(node)) if (!k.startsWith('$')) walk(v, [...path, k])
  }
  walk(src.chart, [])
  const final = {}
  for (const [k, v] of Object.entries(out)) {
    if (k.startsWith('seq-') || k.startsWith('div-')) continue
    final[k] = v
  }
  for (const t of THEMES) {
    const seq = ramp([out['seq-start'][t], out['seq-end'][t]], SEQ_STEPS)
    seq.forEach((c, i) => ((final[`seq-${i}`] ??= {})[t] = c))
    const div = ramp([out['div-low'][t], out['div-mid'][t], out['div-high'][t]], DIV_STEPS)
    div.forEach((c, i) => ((final[`div-${i}`] ??= {})[t] = c))
  }
  return final
}

export function gates(tokens) {
  const findings = [], report = []
  const cats = Object.keys(tokens).filter((k) => /^cat-\d+$/.test(k)).sort((a, b) => +a.slice(4) - +b.slice(4))
  for (const t of THEMES) {
    const bg = tokens.surface[t]
    const colors = cats.map((k) => tokens[k][t])
    const crs = colors.map((c) => contrast(c, bg))
    crs.forEach((cr, i) => {
      if (cr < GATES.contrast[t]) findings.push(`${t}: cat-${i + 1} ${colors[i]} is ${cr.toFixed(2)}:1 on surface ${bg}, needs ${GATES.contrast[t]}:1`)
    })
    const seps = Object.fromEntries(CVD_KINDS.map((k) => [k, minSeparation(colors, k)]))
    for (const [k, s] of Object.entries(seps)) {
      const floor = k === 'tritanopia' ? GATES.tritanopia[t] : GATES.separation[t]
      if (s.min < floor) findings.push(`${t}: under ${k} cat-${s.pair[0]} and cat-${s.pair[1]} are ${s.min.toFixed(1)} apart, needs ${floor}`)
    }
    report.push({ theme: t, minContrast: Math.min(...crs), separation: Object.fromEntries(Object.entries(seps).map(([k, s]) => [k, +s.min.toFixed(1)])) })
  }
  return { findings, report }
}

const block = (sel, vars) => `${sel} {\n${vars.map(([k, v]) => `  --oc-${k}: ${v};`).join('\n')}\n}\n`

export function build() {
  const tokens = resolveTokens()
  const names = Object.keys(tokens)
  const invariant = names.filter((k) => THEMES.every((t) => tokens[k][t] === tokens[k].light))
  const variant = names.filter((k) => !invariant.includes(k))
  const forTheme = (t) => variant.map((k) => [k, tokens[k][t]])
  const header = '/* GENERATED from theme/tokens.json by theme/build.mjs — edit tokens.json, then: cd .omp/charts && npm run theme:build */\n'
  const css =
    header +
    block('.oc-root', [...invariant.map((k) => [k, tokens[k].light]), ...forTheme('light')]) +
    block(`[data-theme='dark'] .oc-root:not([data-theme]),\n.oc-root[data-theme='dark']`, forTheme('dark')) +
    block(`[data-theme='high-contrast'] .oc-root:not([data-theme]),\n.oc-root[data-theme='high-contrast']`, forTheme('high-contrast')) +
    `@media (prefers-color-scheme: dark) {\n` +
    block(`:root:not([data-theme]) .oc-root:not([data-theme])`, forTheme('dark')).replace(/^/gm, '  ').trimEnd() +
    `\n}\n@media (prefers-contrast: more) {\n` +
    block(`:root:not([data-theme]) .oc-root:not([data-theme])`, forTheme('high-contrast')).replace(/^/gm, '  ').trimEnd() +
    `\n}\n`
  const values = Object.fromEntries(THEMES.map((t) => [t, Object.fromEntries(names.map((k) => [`--oc-${k}`, tokens[k][t]]))]))
  const ts =
    '// GENERATED from theme/tokens.json by theme/build.mjs — do not edit.\n' +
    `export const THEMES = ${JSON.stringify(THEMES)} as const\n` +
    `export type ThemeName = (typeof THEMES)[number]\n` +
    `export const SEQ_STEPS = ${SEQ_STEPS}\nexport const DIV_STEPS = ${DIV_STEPS}\n` +
    `export const CAT_COUNT = ${names.filter((k) => /^cat-\d+$/.test(k)).length}\n` +
    `export const CHART_TOKENS_CSS = ${JSON.stringify(css)}\n` +
    `/** Resolved values per theme, for where CSS cannot reach (canvas before attach, exports). */\n` +
    `export const THEME_VALUES: Record<ThemeName, Record<string, string>> = ${JSON.stringify(values, null, 2)}\n`
  return { css, ts, tokens }
}

const OUT = { css: join(here, 'chart-tokens.css'), ts: join(here, 'tokens.generated.ts') }

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const check = process.argv.includes('--check')
  const { css, ts, tokens } = build()
  const { findings, report } = gates(tokens)
  if (check) {
    for (const [k, p] of Object.entries(OUT)) {
      const want = k === 'css' ? css : ts
      if (!existsSync(p) || readFileSync(p, 'utf8') !== want) findings.push(`${p} is stale — run: npm run theme:build`)
    }
  } else {
    writeFileSync(OUT.css, css)
    writeFileSync(OUT.ts, ts)
  }
  for (const r of report)
    console.log(`${r.theme.padEnd(14)} min contrast ${r.minContrast.toFixed(2)}:1  separation ${Object.entries(r.separation).map(([k, v]) => `${k} ${v}`).join(', ')}`)
  for (const f of findings) console.log(`FAIL ${f}`)
  console.log(findings.length ? `\n${findings.length} finding(s)` : `theme: ${Object.keys(tokens).length} tokens, gates pass${check ? ', outputs fresh' : ', written'}`)
  process.exitCode = findings.length ? 1 : 0
}
