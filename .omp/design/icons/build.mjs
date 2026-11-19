#!/usr/bin/env node
// svg/*.svg + icons.json -> icons.svg (sprite) + components/icons.ts (data for
// ui/src/components/Icon.tsx). Runs SVGO over the sources in place first.
//
//   cd .omp && npm run icons:build
//
// Importable: build({ optimize }) returns { sprite, ts, icons } so lint.mjs and
// generated.check.mjs can byte-compare without writing. Fails on an SVG with
// no icons.json entry or an entry with no SVG.

import { readFileSync, writeFileSync, readdirSync, mkdirSync } from 'node:fs'
import { dirname, join, basename } from 'node:path'
import { fileURLToPath } from 'node:url'
import { optimize } from 'svgo'
import svgoConfig from './svgo.config.mjs'

const here = dirname(fileURLToPath(import.meta.url))
export const SVG_DIR = join(here, 'svg')
export const OUTPUTS = { sprite: 'icons.svg', ts: 'components/icons.ts' }
export const VIEWBOX = '0 0 24 24'

export function loadMeta() {
  return JSON.parse(readFileSync(join(here, 'icons.json'), 'utf8'))
}
export function svgFiles() {
  return readdirSync(SVG_DIR).filter((f) => f.endsWith('.svg')).sort()
}

// Parse the one shape of SVG the set allows: a root element and flat children.
export function parseSvg(text, name) {
  const root = text.match(/<svg\b([^>]*)>([\s\S]*?)<\/svg>/)
  if (!root) throw new Error(`${name}: not an <svg> element`)
  const attrs = parseAttrs(root[1])
  const elements = []
  for (const m of root[2].matchAll(/<([a-zA-Z]+)\b([^>]*?)\/>/g)) elements.push({ tag: m[1], attrs: parseAttrs(m[2]) })
  const nested = root[2].replace(/<([a-zA-Z]+)\b[^>]*?\/>/g, '').trim()
  if (nested) throw new Error(`${name}: only self-closing children are allowed, found ${nested.slice(0, 40)}`)
  return { attrs, elements }
}
function parseAttrs(s) {
  const out = {}
  for (const m of s.matchAll(/([a-zA-Z:-]+)="([^"]*)"/g)) out[m[1]] = m[2]
  return out
}

const camel = (s) => s.replace(/-([a-z])/g, (_, c) => c.toUpperCase())

export function build({ optimizeSources = false } = {}) {
  const meta = loadMeta()
  const byName = new Map(meta.icons.map((i) => [i.name, i]))
  const files = svgFiles()
  const errors = []
  for (const f of files) if (!byName.has(basename(f, '.svg'))) errors.push(`svg/${f} has no entry in icons.json`)
  for (const i of meta.icons) if (!files.includes(`${i.name}.svg`)) errors.push(`icons.json entry "${i.name}" has no svg/${i.name}.svg`)
  if (errors.length) throw new Error(errors.join('\n'))

  const icons = []
  for (const f of files) {
    const name = basename(f, '.svg')
    const raw = readFileSync(join(SVG_DIR, f), 'utf8')
    const optimized = optimize(raw, { ...svgoConfig, path: f }).data + '\n'
    if (optimizeSources && optimized !== raw) writeFileSync(join(SVG_DIR, f), optimized)
    const { attrs, elements } = parseSvg(optimized, name)
    const m = byName.get(name)
    const brandAttrs = {}
    if (m.category === 'brand') {
      if (attrs['stroke-width']) brandAttrs.strokeWidth = parseFloat(attrs['stroke-width'])
      if (attrs['stroke-linecap']) brandAttrs.strokeLinecap = attrs['stroke-linecap']
      if (attrs['stroke-linejoin']) brandAttrs.strokeLinejoin = attrs['stroke-linejoin']
    }
    icons.push({ name, category: m.category, source: optimized, rootAttrs: attrs, elements, brandAttrs })
  }

  const sprite = [
    `<!-- GENERATED from svg/*.svg by .omp/design/icons/build.mjs — edit the SVGs or icons.json, then: cd .omp && npm run icons:build -->`,
    `<svg xmlns="http://www.w3.org/2000/svg" style="display:none">`,
    ...icons.map((i) => {
      // The symbol carries the root's presentation attributes, or <use> paints
      // the geometry with the default black fill.
      const extra = Object.entries(i.rootAttrs).filter(([k]) => k !== 'xmlns' && k !== 'viewBox').map(([k, v]) => ` ${k}="${v}"`).join('')
      const inner = i.source.match(/<svg\b[^>]*>([\s\S]*?)<\/svg>/)[1]
      return `  <symbol id="icon-${i.name}" viewBox="${VIEWBOX}"${extra}>${inner}</symbol>`
    }),
    `</svg>`,
    ``,
  ].join('\n')

  const elementLit = (el) => `{ tag: '${el.tag}', attrs: { ${Object.entries(el.attrs).map(([k, v]) => `${JSON.stringify(camel(k))}: ${JSON.stringify(v)}`).join(', ')} } }`
  const ts = [
    `// GENERATED from svg/*.svg + icons.json by .omp/design/icons/build.mjs — edit the SVGs or icons.json, then: cd .omp && npm run icons:build`,
    `// React-free data. ui/src/components/Icon.tsx renders it.`,
    `export const ICON_VIEWBOX = '${VIEWBOX}'`,
    `export const ICON_STROKE = ${meta.stroke}`,
    `export type IconElement = { tag: 'path' | 'circle' | 'rect' | 'line' | 'polyline' | 'polygon'; attrs: Record<string, string> }`,
    `export type IconCategory = 'action' | 'status' | 'arrow' | 'view' | 'file' | 'nav' | 'brand'`,
    `export type IconData = { category: IconCategory; elements: readonly IconElement[]; attrs?: { strokeWidth?: number; strokeLinecap?: 'round' | 'square' | 'butt'; strokeLinejoin?: 'round' | 'miter' | 'bevel' } }`,
    `export const ICONS = {`,
    ...icons.map((i) => {
      const attrs = Object.keys(i.brandAttrs).length ? `, attrs: ${JSON.stringify(i.brandAttrs).replace(/"([a-zA-Z]+)":/g, '$1: ').replace(/"/g, "'")}` : ''
      return `  '${i.name}': { category: '${i.category}', elements: [${i.elements.map(elementLit).join(', ')}]${attrs} },`
    }),
    `} as const satisfies Record<string, IconData>`,
    `export type IconName = keyof typeof ICONS`,
    `export const ICON_NAMES = Object.keys(ICONS) as IconName[]`,
    ``,
  ].join('\n')

  return { sprite, ts, icons, meta }
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const out = build({ optimizeSources: true })
  mkdirSync(join(here, 'components'), { recursive: true })
  writeFileSync(join(here, OUTPUTS.sprite), out.sprite)
  writeFileSync(join(here, OUTPUTS.ts), out.ts)
  console.log(`icons: ${out.icons.length} optimised; wrote ${Object.values(OUTPUTS).join(', ')}`)
}
