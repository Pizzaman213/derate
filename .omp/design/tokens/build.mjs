#!/usr/bin/env node
// tokens.json -> tokens.css, tokens.ts, tokens.flat.json. No dependencies.
//
//   cd .omp && npm run tokens:build
//
// Also importable: `build(tokensJson)` returns every output as a string so
// check.mjs and generated.check.mjs can byte-compare without touching disk, and
// `themeClassCss` gives the style guide class-scoped copies (.theme-light …)
// that are never shipped.
//
// Layers and the rules the build enforces:
//   primitives   color.* space.* font.* radius.* shadow.* duration.* easing.*
//                z.* breakpoint.* opacity.* size.*  -- literal values only.
//   semantic.*   references primitives only. CSS name = last path segment.
//   component.*  references semantic.* (or space.*, the public spacing scale)
//                only; CSS name = --<component>-<part>. A leaf may carry
//                "$extensions": {"derate.pinned": "<reason>"} to reference a
//                primitive directly -- the QR tile is pinned light on purpose.
// A $value that is an object keyed light/dark/high-contrast is themed; every
// theme key must be present. Any other object $value is an error.
//
// CSS emission: semantic and primitive-public tokens are emitted RESOLVED, in
// a :root block (light + invariant) and one block per other theme. Component
// tokens are emitted once, in :root, as var(--<semantic>) references, so they
// follow the cascade -- ModelCard's inline --accent reaches --button-* without
// a second copy.

import { readFileSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
export const THEMES = ['light', 'dark', 'high-contrast']
const PUBLIC_PRIMITIVES = { space: 'space', opacity: 'opacity' }

export function loadTokens() {
  return JSON.parse(readFileSync(join(here, 'tokens.json'), 'utf8'))
}

function isLeaf(v) {
  return v && typeof v === 'object' && '$value' in v
}
export function leaves(tree, path = []) {
  const out = []
  for (const [k, v] of Object.entries(tree)) {
    if (k.startsWith('$')) continue
    if (isLeaf(v)) out.push({ path: [...path, k], leaf: v })
    else if (v && typeof v === 'object') out.push(...leaves(v, [...path, k]))
  }
  return out
}

function layerOf(path) {
  if (path[0] === 'semantic') return 'semantic'
  if (path[0] === 'component') return 'component'
  return 'primitive'
}

export function cssNameOf(path) {
  const layer = layerOf(path)
  if (layer === 'semantic') return `--${path[path.length - 1]}`
  if (layer === 'component') return `--${path.slice(1).join('-')}`
  if (PUBLIC_PRIMITIVES[path[0]] && path.length === 2) return `--${PUBLIC_PRIMITIVES[path[0]]}-${path[1]}`
  return null
}

const REF = /\{([a-zA-Z0-9_.-]+)\}/g

export function build(tokens) {
  const all = leaves(tokens)
  const byPath = new Map(all.map((l) => [l.path.join('.'), l]))
  const errors = []

  const themedValue = (leaf, theme) => {
    const v = leaf.$value
    if (v && typeof v === 'object') {
      const keys = Object.keys(v)
      const missing = THEMES.filter((t) => !(t in v))
      const extra = keys.filter((k) => !THEMES.includes(k))
      if (missing.length || extra.length) throw new Error(`themed value must have exactly ${THEMES.join('/')}: missing ${missing.join(',') || '-'}, extra ${extra.join(',') || '-'}`)
      return v[theme]
    }
    return v
  }
  const isThemed = (leaf) => leaf.$value && typeof leaf.$value === 'object'

  // Resolve a value for one theme, following references.
  function resolve(path, theme, stack = []) {
    const key = path.join('.')
    const entry = byPath.get(key)
    if (!entry) throw new Error(`unresolved reference {${key}}`)
    if (stack.includes(key)) throw new Error(`reference cycle ${[...stack, key].join(' -> ')}`)
    const raw = themedValue(entry.leaf, theme)
    if (typeof raw !== 'string') return raw
    return raw.replace(REF, (_, ref) => {
      const target = byPath.get(ref)
      if (!target) throw new Error(`unresolved reference {${ref}} in ${key}`)
      const fromLayer = layerOf(path), toLayer = layerOf(target.path)
      const pinned = entry.leaf.$extensions && entry.leaf.$extensions['derate.pinned']
      if (fromLayer === 'semantic' && toLayer !== 'primitive') throw new Error(`semantic token ${key} may only reference primitives, not {${ref}}`)
      if (fromLayer === 'component' && !(toLayer === 'semantic' || target.path[0] === 'space') && !pinned) throw new Error(`component token ${key} may only reference semantic tokens (or space.*), not {${ref}}; add $extensions."derate.pinned" with a reason to override`)
      if (fromLayer === 'primitive' && toLayer !== 'primitive') throw new Error(`primitive ${key} may not reference ${ref}`)
      return String(resolve(target.path, theme, [...stack, key]))
    })
  }

  // Does a leaf's resolved value differ across themes (directly or through refs)?
  const resolved = new Map()
  for (const { path } of all) {
    const key = path.join('.')
    try {
      const perTheme = Object.fromEntries(THEMES.map((t) => [t, resolve(path, t)]))
      resolved.set(key, perTheme)
    } catch (e) {
      errors.push(e.message)
    }
  }
  for (const { path, leaf } of all) {
    for (const f of ['$value', '$type', '$description']) if (!(f in leaf)) errors.push(`${path.join('.')} is missing ${f}`)
  }
  if (errors.length) throw new Error('tokens.json:\n  ' + errors.join('\n  '))

  const varies = (key) => { const r = resolved.get(key); return THEMES.some((t) => r[t] !== r.light) }

  // CSS-named tokens, in source order.
  const named = all.map((l) => ({ ...l, key: l.path.join('.'), css: cssNameOf(l.path), layer: layerOf(l.path) })).filter((l) => l.css)
  const seen = new Map()
  for (const n of named) {
    if (seen.has(n.css)) throw new Error(`CSS name collision: ${n.css} from ${seen.get(n.css)} and ${n.key}`)
    seen.set(n.css, n.key)
  }

  const fmt = (v) => (typeof v === 'number' ? String(v) : v)
  const decl = (name, v) => `  ${name}: ${fmt(v)};`

  // Component tokens emit as var() references to whatever they point at.
  const componentDecl = (n) => {
    const raw = n.leaf.$value
    if (typeof raw === 'string' && /^\{[^}]+\}$/.test(raw.trim())) {
      const target = byPath.get(raw.trim().slice(1, -1))
      const tcss = cssNameOf(target.path)
      if (tcss) return decl(n.css, `var(${tcss})`)
    }
    return decl(n.css, resolved.get(n.key).light)
  }

  const nonComponent = named.filter((n) => n.layer !== 'component')
  const components = named.filter((n) => n.layer === 'component')
  const themedNames = nonComponent.filter((n) => varies(n.key))
  const staticNames = nonComponent.filter((n) => !varies(n.key))

  const block = (selector, theme, { includeStatic, includeComponents }) => {
    const lines = [`${selector} {`]
    let group = null
    // A theme block carries only what differs from light in THAT theme.
    const rows = includeStatic ? nonComponent : themedNames.filter((n) => resolved.get(n.key)[theme] !== resolved.get(n.key).light)
    for (const n of rows) {
      const g = (n.layer === 'primitive' ? n.path.slice(0, 1) : n.path.slice(0, 2)).join('.')
      if (g !== group) { group = g; lines.push(`  /* ${g} */`) }
      lines.push(decl(n.css, resolved.get(n.key)[theme]))
    }
    if (includeComponents) {
      lines.push('  /* component */')
      for (const n of components) lines.push(componentDecl(n))
    }
    lines.push('}')
    return lines.join('\n')
  }

  const header = `/* GENERATED from tokens.json by .omp/design/tokens/build.mjs — edit tokens.json, then: cd .omp && npm run tokens:build */`
  const css = [
    header,
    '',
    '/* Light, plus every theme-invariant token and the component layer. */',
    block(':root', 'light', { includeStatic: true, includeComponents: true }),
    '',
    '/* Dark. The surface ramp inverts as a whole so "raised" still means lighter-than-page. */',
    '@media (prefers-color-scheme: dark) {',
    block("  :root:not([data-theme='light']):not([data-theme='high-contrast'])", 'dark', {}).replace(/\n/g, '\n  ').replace(/\n  $/, ''),
    '}',
    '',
    block(":root[data-theme='dark']", 'dark', {}),
    '',
    '/* High contrast: 7:1 text, 3:1 UI, 3px focus ring, hard outlines instead of shadows. */',
    block(":root[data-theme='high-contrast']", 'high-contrast', {}),
    '',
    '@media (prefers-contrast: more) {',
    block('  :root:not([data-theme])', 'high-contrast', {}).replace(/\n/g, '\n  ').replace(/\n  $/, ''),
    '}',
    '',
  ].join('\n')

  // Class-scoped copies for the style guide only.
  const themeClassCss = THEMES.map((t) => block(`.theme-${t}`, t, { includeStatic: true, includeComponents: true })).join('\n\n') + '\n'

  // tokens.ts
  const perTheme = (theme) => Object.fromEntries(named.map((n) => [n.css.slice(2), resolved.get(n.key)[theme]]))
  const staticObj = Object.fromEntries(staticNames.map((n) => [n.css.slice(2), resolved.get(n.key).light]))
  const bp = Object.fromEntries(leaves(tokens.breakpoint).map((l) => [l.path[1], parseInt(l.leaf.$value, 10)]))
  // JSON is valid TS; keys stay quoted because many contain '-'.
  const lit = (o) => JSON.stringify(o, null, 2)
  const ts = [
    `// GENERATED from tokens.json by .omp/design/tokens/build.mjs — edit tokens.json, then: cd .omp && npm run tokens:build`,
    `// Resolved values per theme. Use cssVar() in styles; use tokens.<theme>.* only where CSS cannot reach (canvas, xterm, SVG attributes).`,
    `export const themes = ${lit(THEMES)} as const`,
    `export type ThemeName = (typeof themes)[number]`,
    `export const tokens = {`,
    `  light: ${lit(perTheme('light')).replace(/\n/g, '\n  ')},`,
    `  dark: ${lit(perTheme('dark')).replace(/\n/g, '\n  ')},`,
    `  'high-contrast': ${lit(perTheme('high-contrast')).replace(/\n/g, '\n  ')},`,
    `  static: ${lit(staticObj).replace(/\n/g, '\n  ')},`,
    `} as const`,
    `export type TokenName = keyof typeof tokens.light | keyof typeof tokens.static`,
    `export const cssVar = (n: TokenName): string => \`var(--\${n})\``,
    `export const breakpoints = ${lit(bp)} as const`,
    ``,
  ].join('\n')

  // tokens.flat.json
  const flat = {}
  for (const n of named) {
    const r = resolved.get(n.key)
    flat[n.css.slice(2)] = varies(n.key) ? { light: r.light, dark: r.dark, 'high-contrast': r['high-contrast'] } : r.light
  }
  const flatJson = JSON.stringify(flat, null, 2) + '\n'

  // metadata for other tools
  const meta = named.map((n) => ({ css: n.css.slice(2), key: n.key, layer: n.layer, type: n.leaf.$type, description: n.leaf.$description, themed: varies(n.key), refs: [...String(typeof n.leaf.$value === 'string' ? n.leaf.$value : JSON.stringify(n.leaf.$value)).matchAll(REF)].map((m) => m[1]) }))

  return { css, ts, flatJson, flat, themeClassCss, meta, resolved, named }
}

export const OUTPUTS = { css: 'tokens.css', ts: 'tokens.ts', flatJson: 'tokens.flat.json' }

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const out = build(loadTokens())
  for (const [k, file] of Object.entries(OUTPUTS)) writeFileSync(join(here, file), out[k])
  console.log(`tokens: ${out.named.length} css names (${out.named.filter((n) => n.layer === 'semantic').length} semantic, ${out.named.filter((n) => n.layer === 'component').length} component), ${Object.values(OUTPUTS).join(', ')}`)
}
