#!/usr/bin/env node
// Design audit of ui/src. No dependencies. Emits Markdown (default
// .omp/design/AUDIT.md) or, with --json, the same facts as one JSON object for
// /design-audit and pack-sync.
//
//   cd .omp && npm run audit                # writes design/AUDIT.md
//   node design/audit.mjs --out /tmp/a.md   # elsewhere
//   node design/audit.mjs --json            # machine output on stdout
//
// Every section is computed from the files -- nothing here is a hand-kept
// list except the fixed ORDER of the debt ranking at the bottom.

import { readFileSync, readdirSync, statSync, existsSync, writeFileSync } from 'node:fs'
import { join, relative, dirname, basename, extname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'

const here = dirname(fileURLToPath(import.meta.url))
const repo = resolve(here, '..', '..')
const uiDir = join(repo, 'ui')
const srcDir = join(uiDir, 'src')

const args = process.argv.slice(2)
const wantJson = args.includes('--json')
const outIdx = args.indexOf('--out')
const outPath = outIdx >= 0 ? resolve(args[outIdx + 1]) : join(here, 'AUDIT.md')

// ---------------------------------------------------------------- files

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    const st = statSync(p)
    if (st.isDirectory()) {
      if (name === 'node_modules' || name === 'dist' || name.startsWith('mockups')) continue
      walk(p, out)
    } else if (/\.(tsx?|css)$/.test(name) && !name.endsWith('.check.mjs') && !name.endsWith('.d.ts')) {
      out.push(p)
    }
  }
  return out
}

const files = walk(srcDir).sort()
const rel = (p) => relative(repo, p)
const sources = new Map(files.map((p) => [p, readFileSync(p, 'utf8')]))
const isCss = (p) => p.endsWith('.css')
const isTsx = (p) => p.endsWith('.tsx')
const tokensCssPath = [join(srcDir, 'styles', 'tokens.css'), join(here, 'tokens', 'tokens.css')].find(existsSync)

// Strip comments but keep line structure so line numbers survive.
function stripComments(text, lang) {
  let out = text.replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, ' '))
  if (lang !== 'css') out = out.replace(/(^|[^:'"`])\/\/[^\n]*/g, (m, p) => p + ' '.repeat(m.length - p.length))
  return out
}
const clean = new Map([...sources].map(([p, t]) => [p, stripComments(t, isCss(p) ? 'css' : 'ts')]))

function lineOf(text, index) {
  let n = 1
  for (let i = 0; i < index; i++) if (text.charCodeAt(i) === 10) n++
  return n
}
function colOf(text, index) {
  const nl = text.lastIndexOf('\n', index - 1)
  return index - nl
}

// ---------------------------------------------------------------- colour maths

const NAMED = {
  aliceblue: 'f0f8ff', antiquewhite: 'faebd7', aqua: '00ffff', aquamarine: '7fffd4', azure: 'f0ffff', beige: 'f5f5dc', bisque: 'ffe4c4', black: '000000', blanchedalmond: 'ffebcd', blue: '0000ff', blueviolet: '8a2be2', brown: 'a52a2a', burlywood: 'deb887', cadetblue: '5f9ea0', chartreuse: '7fff00', chocolate: 'd2691e', coral: 'ff7f50', cornflowerblue: '6495ed', cornsilk: 'fff8dc', crimson: 'dc143c', cyan: '00ffff', darkblue: '00008b', darkcyan: '008b8b', darkgoldenrod: 'b8860b', darkgray: 'a9a9a9', darkgreen: '006400', darkgrey: 'a9a9a9', darkkhaki: 'bdb76b', darkmagenta: '8b008b', darkolivegreen: '556b2f', darkorange: 'ff8c00', darkorchid: '9932cc', darkred: '8b0000', darksalmon: 'e9967a', darkseagreen: '8fbc8f', darkslateblue: '483d8b', darkslategray: '2f4f4f', darkslategrey: '2f4f4f', darkturquoise: '00ced1', darkviolet: '9400d3', deeppink: 'ff1493', deepskyblue: '00bfff', dimgray: '696969', dimgrey: '696969', dodgerblue: '1e90ff', firebrick: 'b22222', floralwhite: 'fffaf0', forestgreen: '228b22', fuchsia: 'ff00ff', gainsboro: 'dcdcdc', ghostwhite: 'f8f8ff', gold: 'ffd700', goldenrod: 'daa520', gray: '808080', green: '008000', greenyellow: 'adff2f', grey: '808080', honeydew: 'f0fff0', hotpink: 'ff69b4', indianred: 'cd5c5c', indigo: '4b0082', ivory: 'fffff0', khaki: 'f0e68c', lavender: 'e6e6fa', lavenderblush: 'fff0f5', lawngreen: '7cfc00', lemonchiffon: 'fffacd', lightblue: 'add8e6', lightcoral: 'f08080', lightcyan: 'e0ffff', lightgoldenrodyellow: 'fafad2', lightgray: 'd3d3d3', lightgreen: '90ee90', lightgrey: 'd3d3d3', lightpink: 'ffb6c1', lightsalmon: 'ffa07a', lightseagreen: '20b2aa', lightskyblue: '87cefa', lightslategray: '778899', lightslategrey: '778899', lightsteelblue: 'b0c4de', lightyellow: 'ffffe0', lime: '00ff00', limegreen: '32cd32', linen: 'faf0e6', magenta: 'ff00ff', maroon: '800000', mediumaquamarine: '66cdaa', mediumblue: '0000cd', mediumorchid: 'ba55d3', mediumpurple: '9370db', mediumseagreen: '3cb371', mediumslateblue: '7b68ee', mediumspringgreen: '00fa9a', mediumturquoise: '48d1cc', mediumvioletred: 'c71585', midnightblue: '191970', mintcream: 'f5fffa', mistyrose: 'ffe4e1', moccasin: 'ffe4b5', navajowhite: 'ffdead', navy: '000080', oldlace: 'fdf5e6', olive: '808000', olivedrab: '6b8e23', orange: 'ffa500', orangered: 'ff4500', orchid: 'da70d6', palegoldenrod: 'eee8aa', palegreen: '98fb98', paleturquoise: 'afeeee', palevioletred: 'db7093', papayawhip: 'ffefd5', peachpuff: 'ffdab9', peru: 'cd853f', pink: 'ffc0cb', plum: 'dda0dd', powderblue: 'b0e0e6', purple: '800080', rebeccapurple: '663399', red: 'ff0000', rosybrown: 'bc8f8f', royalblue: '4169e1', saddlebrown: '8b4513', salmon: 'fa8072', sandybrown: 'f4a460', seagreen: '2e8b57', seashell: 'fff5ee', sienna: 'a0522d', silver: 'c0c0c0', skyblue: '87ceeb', slateblue: '6a5acd', slategray: '708090', slategrey: '708090', snow: 'fffafa', springgreen: '00ff7f', steelblue: '4682b4', tan: 'd2b48c', teal: '008080', thistle: 'd8bfd8', tomato: 'ff6347', turquoise: '40e0d0', violet: 'ee82ee', wheat: 'f5deb3', white: 'ffffff', whitesmoke: 'f5f5f5', yellow: 'ffff00', yellowgreen: '9acd32',
}

function hexToRgb(h) {
  h = h.replace('#', '')
  if (h.length === 3 || h.length === 4) h = [...h].map((c) => c + c).join('')
  const n = parseInt(h.slice(0, 6), 16)
  const a = h.length === 8 ? parseInt(h.slice(6, 8), 16) / 255 : 1
  return [n >> 16, (n >> 8) & 255, n & 255, a]
}
function hslToRgb(h, s, l) {
  s /= 100; l /= 100
  const k = (n) => (n + h / 30) % 12
  const a = s * Math.min(l, 1 - l)
  const f = (n) => l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)))
  return [f(0) * 255, f(8) * 255, f(4) * 255]
}
export function parseColour(s) {
  s = s.trim().toLowerCase()
  if (s.startsWith('#')) return hexToRgb(s)
  let m = s.match(/^rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)(?:\s*[,/]\s*([\d.]+%?))?\s*\)$/)
  if (m) return [+m[1], +m[2], +m[3], m[4] == null ? 1 : m[4].endsWith('%') ? parseFloat(m[4]) / 100 : +m[4]]
  m = s.match(/^hsla?\(\s*([\d.]+)(?:deg)?[\s,]+([\d.]+)%[\s,]+([\d.]+)%(?:\s*[,/]\s*([\d.]+%?))?\s*\)$/)
  if (m) return [...hslToRgb(+m[1], +m[2], +m[3]), m[4] == null ? 1 : m[4].endsWith('%') ? parseFloat(m[4]) / 100 : +m[4]]
  if (NAMED[s]) return hexToRgb(NAMED[s])
  return null
}
function toLab([r, g, b]) {
  const lin = (c) => { c /= 255; return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4 }
  const [R, G, B] = [lin(r), lin(g), lin(b)]
  const X = (R * 0.4124 + G * 0.3576 + B * 0.1805) / 0.95047
  const Y = (R * 0.2126 + G * 0.7152 + B * 0.0722) / 1.0
  const Z = (R * 0.0193 + G * 0.1192 + B * 0.9505) / 1.08883
  const f = (t) => (t > 0.008856 ? Math.cbrt(t) : 7.787 * t + 16 / 116)
  return [116 * f(Y) - 16, 500 * (f(X) - f(Y)), 200 * (f(Y) - f(Z))]
}
export function deltaE(a, b) {
  const [l1, a1, b1] = toLab(a), [l2, a2, b2] = toLab(b)
  return Math.hypot(l1 - l2, a1 - a2, b1 - b2)
}

// ---------------------------------------------------------------- colour literals

const COLOUR_RE = new RegExp(
  String.raw`#[0-9a-fA-F]{3,8}\b|(?:rgba?|hsla?)\([^)]*\)|\b(?:${Object.keys(NAMED).join('|')})\b`,
  'g',
)

function colourLiterals() {
  const hits = []
  for (const [p, text] of clean) {
    const css = isCss(p)
    // CSS: anything inside a declaration value. TS: hex (6/8 digits), rgb()/hsl()
    // anywhere in the comment-stripped text, and a named colour only when a whole
    // string literal IS the name -- a quote-pairing scanner loses sync on the
    // first apostrophe in JSX text, so string regions are not used for TS.
    const regions = []
    if (css) {
      for (const m of text.matchAll(/:\s*([^;{}]+)/g)) regions.push([m.index + m[0].indexOf(m[1]), m[1]])
    } else {
      for (const m of text.matchAll(/#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?\b|(?:rgba?|hsla?)\([^)]*\)/g)) regions.push([m.index, m[0]])
      for (const m of text.matchAll(new RegExp(`(['"\`])(${Object.keys(NAMED).join('|')})\\1`, 'g'))) regions.push([m.index + 1, m[2]])
    }
    for (const [start, region] of regions) {
      for (const m of region.matchAll(COLOUR_RE)) {
        const v = m[0]
        if (/^#/.test(v) && !/^#[0-9a-fA-F]{3}$|^#[0-9a-fA-F]{4}$|^#[0-9a-fA-F]{6}$|^#[0-9a-fA-F]{8}$/.test(v)) continue
        if (!css && /^#/.test(v) && /^#[0-9a-fA-F]{3,4}$/.test(v)) {
          // `#1A1` style hits in TS strings are usually ids or hashes; require 6/8 digits there.
          continue
        }
        const rgb = parseColour(v)
        if (!rgb) continue
        const idx = start + m.index
        hits.push({ file: rel(p), line: lineOf(text, idx), value: v, rgb, inTokens: p === tokensCssPath })
      }
    }
  }
  return hits
}

function clusterColours(hits) {
  const uniq = [...new Map(hits.map((h) => [h.value.toLowerCase(), h])).values()]
  const parent = uniq.map((_, i) => i)
  const find = (i) => (parent[i] === i ? i : (parent[i] = find(parent[i])))
  for (let i = 0; i < uniq.length; i++)
    for (let j = i + 1; j < uniq.length; j++)
      if (uniq[i].rgb[3] === 1 && uniq[j].rgb[3] === 1 && deltaE(uniq[i].rgb, uniq[j].rgb) < 3) parent[find(i)] = find(j)
  const groups = new Map()
  uniq.forEach((h, i) => {
    const r = find(i)
    if (!groups.has(r)) groups.set(r, [])
    groups.get(r).push(h.value)
  })
  return [...groups.values()].filter((g) => g.length > 1).map((members) => ({
    members,
    locations: hits.filter((h) => members.map((m) => m.toLowerCase()).includes(h.value.toLowerCase())).map((h) => `${h.file}:${h.line}`),
  }))
}

// ---------------------------------------------------------------- scale values

const SPACING_PROPS = ['margin', 'margin-top', 'margin-right', 'margin-bottom', 'margin-left', 'margin-inline', 'margin-block', 'padding', 'padding-top', 'padding-right', 'padding-bottom', 'padding-left', 'padding-inline', 'padding-block', 'gap', 'row-gap', 'column-gap', 'inset', 'top', 'right', 'bottom', 'left']
const SCALE_PROPS = {
  'font-family': ['font-family'],
  'font-size': ['font-size'],
  'font-weight': ['font-weight'],
  'line-height': ['line-height'],
  'border-radius': ['border-radius'],
  'box-shadow': ['box-shadow'],
  'z-index': ['z-index'],
  'letter-spacing': ['letter-spacing'],
  spacing: SPACING_PROPS,
}
const camel = (s) => s.replace(/-([a-z])/g, (_, c) => c.toUpperCase())
const CAMEL_TO_PROP = new Map(Object.values(SCALE_PROPS).flat().map((p) => [camel(p), p]))

function cssDeclarations() {
  // Returns [{file,line,prop,value,media}] for every declaration in every .css file,
  // plus media queries, plus transitions/animations.
  const decls = []
  const medias = []
  for (const [p, text] of clean) {
    if (!isCss(p)) continue
    for (const m of text.matchAll(/@media\s*([^{]+)\{/g)) medias.push({ file: rel(p), line: lineOf(text, m.index), query: m[1].trim() })
    for (const m of text.matchAll(/([a-zA-Z-]+)\s*:\s*([^;{}]+?)\s*(?=[;}])/g)) {
      const prop = m[1]
      if (prop.startsWith('--') || /^(https?|data)$/.test(prop)) continue
      // Skip selector-ish text like "a:hover" -- require that the previous non-space char is { or ; or newline-ish.
      const before = text.slice(Math.max(0, m.index - 2), m.index)
      if (!/[{;\s]$|^$/.test(before)) continue
      decls.push({ file: rel(p), line: lineOf(text, m.index), prop, value: m[2].trim() })
    }
  }
  return { decls, medias }
}

function inlineStyles() {
  // style={{ a: 1, b: '2px', c: expr }} in tsx. Returns [{file,line,prop,value}].
  const out = []
  for (const [p, text] of clean) {
    if (!isTsx(p)) continue
    for (const m of text.matchAll(/style=\{\{/g)) {
      let depth = 0, i = m.index + 'style={'.length, start = i
      for (; i < text.length; i++) {
        const c = text[i]
        if (c === '{') depth++
        else if (c === '}') { depth--; if (depth === 0) break }
      }
      const body = text.slice(start + 1, i)
      // split on top-level commas
      let d = 0, buf = '', parts = []
      for (const c of body) {
        if ('([{'.includes(c)) d++
        if (')]}'.includes(c)) d--
        if (c === ',' && d === 0) { parts.push(buf); buf = '' } else buf += c
      }
      parts.push(buf)
      let offset = start + 1
      for (const part of parts) {
        const pm = part.match(/^\s*([A-Za-z]+)\s*:\s*([\s\S]+?)\s*$/)
        if (pm) {
          const prop = pm[1]
          let value = pm[2]
          if (/^-?\d+(\.\d+)?$/.test(value) && !UNITLESS.has(prop)) value = `${value}px`
          else if (/^(['"`])(.*)\1$/.test(value)) value = value.slice(1, -1)
          else value = `expr: ${value.replace(/\s+/g, ' ').slice(0, 40)}`
          out.push({ file: rel(p), line: lineOf(text, offset + part.indexOf(pm[1])), prop, value })
        }
        offset += part.length + 1
      }
    }
  }
  return out
}

function groupValues(items) {
  const m = new Map()
  for (const it of items) {
    const k = it.value
    if (!m.has(k)) m.set(k, { value: k, count: 0, locations: [] })
    const e = m.get(k)
    e.count++
    if (e.locations.length < 6) e.locations.push(`${it.file}:${it.line}`)
  }
  return [...m.values()].sort((a, b) => b.count - a.count || a.value.localeCompare(b.value))
}
const UNITLESS = new Set(['fontWeight', 'opacity', 'zIndex', 'lineHeight', 'flex', 'flexGrow', 'flexShrink', 'order', 'zoom', 'tabSize', 'columnCount'])

function pxNumbers(value) {
  return [...value.matchAll(/(-?\d*\.?\d+)px\b/g)].map((m) => parseFloat(m[1]))
}
function clusterPx(values) {
  // values: distinct numbers; group within 1px.
  const sorted = [...new Set(values)].sort((a, b) => a - b)
  const clusters = []
  for (const v of sorted) {
    const last = clusters[clusters.length - 1]
    if (last && v - last[last.length - 1] <= 1 && v !== last[last.length - 1]) last.push(v)
    else clusters.push([v])
  }
  return clusters.filter((c) => c.length > 1)
}

// ---------------------------------------------------------------- components

function componentInventory() {
  const comps = []
  for (const [p, text] of sources) {
    if (!isTsx(p)) continue
    for (const m of text.matchAll(/export\s+(?:default\s+)?(?:function|const)\s+([A-Z][A-Za-z0-9]*)/g)) {
      const name = m[1]
      const after = text.slice(m.index + m[0].length, m.index + m[0].length + 1200)
      // first `: Type` inside the first parens
      let props = null
      const paren = after.match(/\(\s*(?:\{[^}]*\}|[A-Za-z_]\w*)\s*:\s*([^)]*?)\)\s*(?::|=>|\{)/s)
      if (paren) props = paren[1].replace(/\s+/g, ' ').trim()
      const variants = []
      if (props) {
        // string-literal union props, either in the literal or in a named type declared in this file
        let scope = props
        const named = props.match(/^([A-Z][A-Za-z0-9]*)$/)
        if (named) {
          const td = text.match(new RegExp(`(?:type|interface)\\s+${named[1]}\\s*=?\\s*(\\{[\\s\\S]*?\\n\\})`))
          if (td) scope = td[1]
        }
        for (const vm of scope.matchAll(/(\w+)\??\s*:\s*((?:'[^']*'\s*\|\s*)+'[^']*')/g)) {
          variants.push({ prop: vm[1], values: [...vm[2].matchAll(/'([^']*)'/g)].map((x) => x[1]) })
        }
        // also `tone?: Tone` where Tone is a union declared in the file
        for (const vm of scope.matchAll(/(\w+)\??\s*:\s*([A-Z]\w*)\b/g)) {
          const td = text.match(new RegExp(`type\\s+${vm[2]}\\s*=\\s*((?:'[^']*'\\s*\\|\\s*)+'[^']*')`))
          if (td && !variants.some((v) => v.prop === vm[1])) variants.push({ prop: vm[1], values: [...td[1].matchAll(/'([^']*)'/g)].map((x) => x[1]) })
        }
      }
      comps.push({ name, path: rel(p), file: basename(p, '.tsx'), props, variants })
    }
  }
  // importers
  for (const c of comps) {
    const stem = c.file
    let n = 0
    for (const [p, text] of sources) {
      if (p.endsWith(`/${stem}.tsx`)) continue
      if (new RegExp(`from\\s+['"][^'"]*/${stem}(?:\\.tsx)?['"]`).test(text) && new RegExp(`\\b${c.name}\\b`).test(text)) n++
    }
    c.importers = n
    const dir = dirname(join(repo, c.path))
    const checks = existsSync(dir) ? readdirSync(dir).filter((f) => f.endsWith('.check.mjs')) : []
    const local = checks.find((f) => new RegExp(`\\b${c.name}\\b`).test(readFileSync(join(dir, f), 'utf8')))
    const packTest = join(here, '..', 'tests', 'ui', 'components', `${c.name.toLowerCase()}.check.mjs`)
    c.tests = local ? rel(join(dir, local)) : existsSync(packTest) ? rel(packTest) : null
  }
  return comps.sort((a, b) => a.path.localeCompare(b.path))
}

// ---------------------------------------------------------------- icons

const GLYPHS = ['✓', '✕', '×', '●', '○', '⌕', '▦', '▤', '↗', '▾', '▸', '⚠', '↔', '→', '+', '–']
function iconInventory() {
  const svgs = [], imgs = [], glyphs = [], entities = []
  for (const [p, text] of sources) {
    if (!isTsx(p)) continue
    for (const m of text.matchAll(/<svg\b([^>]*)>/g)) {
      const attrs = m[1]
      const get = (a) => (attrs.match(new RegExp(`${a}=(?:"([^"]*)"|\\{([^}]*)\\})`)) || []).slice(1).find(Boolean) ?? null
      const ctx = text.slice(Math.max(0, m.index - 200), m.index + 400)
      const aria = (ctx.match(/aria-[a-z]+=(?:"[^"]*"|\{[^}]*\})/g) || []).slice(0, 2)
      const body = text.slice(m.index, text.indexOf('</svg>', m.index))
      const ds = [...body.matchAll(/\bd="([^"]+)"/g)].map((x) => x[1].replace(/\s+/g, ' ').trim())
      svgs.push({ file: rel(p), line: lineOf(text, m.index), viewBox: get('viewBox'), strokeWidth: get('strokeWidth'), fill: get('fill'), stroke: get('stroke'), aria, paths: ds })
    }
    for (const m of text.matchAll(/<(?:img|image)\b[^>]*?(?:src|href)=(?:"([^"]*)"|\{([^}]*)\})/g))
      imgs.push({ file: rel(p), line: lineOf(text, m.index), src: m[1] ?? m[2] })
    // glyphs: only inside JSX text or string literals, i.e. not in comments (clean text)
    const ct = clean.get(p)
    for (const g of GLYPHS) {
      let idx = -1
      while ((idx = ct.indexOf(g, idx + 1)) >= 0) {
        // `+ – × → ↔` are typographic unless a string literal or JSX text node
        // is exactly that character.
        if ('+–×→↔'.includes(g)) {
          const around = ct.slice(Math.max(0, idx - 1), idx + 2)
          if (!/^(['"`][+–×→↔]['"`]|>[+–×→↔]<)$/.test(around)) continue
        }
        glyphs.push({ file: rel(p), line: lineOf(ct, idx), glyph: g })
      }
    }
    for (const m of ct.matchAll(/&#(\d+);|&#x([0-9a-fA-F]+);/g))
      entities.push({ file: rel(p), line: lineOf(ct, m.index), entity: m[0], char: String.fromCodePoint(m[1] ? +m[1] : parseInt(m[2], 16)) })
  }
  // duplicates by identical path set
  const byPaths = new Map()
  for (const s of svgs) {
    if (!s.paths.length) continue
    const k = s.paths.join('|')
    if (!byPaths.has(k)) byPaths.set(k, [])
    byPaths.get(k).push(`${s.file}:${s.line}`)
  }
  const duplicates = [...byPaths.entries()].filter(([, v]) => v.length > 1).map(([k, v]) => ({ paths: k.slice(0, 80), locations: v }))
  return { svgs, imgs, glyphs, entities, duplicates }
}

// ---------------------------------------------------------------- a11y

function a11yFlags() {
  const f = { imgNoAlt: [], buttonNoType: [], iconButtonNoLabel: [], clickableNonInteractive: [], tabindexPositive: [] }
  for (const [p, text] of clean) {
    if (!isTsx(p)) continue
    const loc = (i) => ({ file: rel(p), line: lineOf(text, i) })
    for (const m of text.matchAll(/<img\b([^>]*)>/g)) if (!/\balt=/.test(m[1])) f.imgNoAlt.push(loc(m.index))
    for (const m of text.matchAll(/<button\b((?:[^>{]|\{[^}]*\})*)>/g)) {
      const attrs = m[1]
      if (!/\btype=/.test(attrs)) f.buttonNoType.push(loc(m.index))
      const close = text.indexOf('</button>', m.index)
      const inner = close > 0 ? text.slice(m.index + m[0].length, close) : ''
      const textish = inner.replace(/<[^>]*>/g, '').replace(/\{[^}]*\}/g, (e) => (/['"`]/.test(e) ? 'x' : '')).trim()
      const hasLabel = /aria-label(?:ledby)?=/.test(attrs) || /\btitle=/.test(attrs)
      if (close > 0 && !textish && !hasLabel && !/\{[a-z]\w*\}/.test(inner)) f.iconButtonNoLabel.push(loc(m.index))
    }
    for (const m of text.matchAll(/<(div|span|li|td|tr|p|section|g)\b((?:[^>{]|\{(?:[^{}]|\{[^}]*\})*\})*)>/g)) {
      const attrs = m[2]
      if (/\bon(Click|DoubleClick|KeyDown)=/.test(attrs) && !/\brole=/.test(attrs) && !/\btabIndex=/.test(attrs)) f.clickableNonInteractive.push({ ...loc(m.index), tag: m[1] })
      if (/\btabIndex=\{?\s*(\d+)/.test(attrs) && !/\brole=/.test(attrs) && /\bon(Click|KeyDown)=/.test(attrs)) f.clickableNonInteractive.push({ ...loc(m.index), tag: m[1], note: 'tabIndex without role' })
    }
    for (const m of text.matchAll(/tabIndex=\{?\s*([1-9]\d*)/g)) f.tabindexPositive.push(loc(m.index))
  }
  return f
}

// ---------------------------------------------------------------- token usage

function tokenUsage() {
  if (!tokensCssPath) return { defined: [], unused: [] }
  const defs = [...readFileSync(tokensCssPath, 'utf8').matchAll(/^\s*(--[\w-]+)\s*:/gm)].map((m) => m[1])
  const defined = [...new Set(defs)]
  const all = [...sources.values()].join('\n') + readFileSync(tokensCssPath, 'utf8').replace(/^\s*--[\w-]+\s*:[^;]*;/gm, (m) => m.replace(/^[^:]*:/, ''))
  // `var(--tag-${n})` builds a name at runtime: a token whose numeric suffix is
  // templated counts as used.
  const templated = (t) => { const m = t.match(/^(--[\w-]+-)\d+$/); return m && all.includes(`${m[1]}\${`) }
  const unused = defined.filter((t) => !new RegExp(`var\\(${t}[,)]`).test(all) && !new RegExp(`['"\`]${t}['"\`]`).test(all) && !templated(t))
  return { defined, unused }
}

// ---------------------------------------------------------------- contrast (phase 2)

function contrastFailures() {
  const check = join(here, 'tokens', 'check.mjs')
  if (!existsSync(check)) return { pending: true, failures: [] }
  const r = spawnSync(process.execPath, [check, '--json'], { cwd: here, encoding: 'utf8' })
  try {
    const j = JSON.parse(r.stdout)
    return { pending: false, failures: j.filter((x) => !x.ok) }
  } catch {
    return { pending: false, failures: [{ error: (r.stderr || r.stdout).trim().split('\n').slice(-3).join(' ') }] }
  }
}

// ---------------------------------------------------------------- stack & map

function stackAndMap() {
  const pkg = JSON.parse(readFileSync(join(uiDir, 'package.json'), 'utf8'))
  const folders = readdirSync(srcDir).filter((n) => statSync(join(srcDir, n)).isDirectory()).map((n) => {
    const readme = join(srcDir, n, 'README.md')
    let first = ''
    if (existsSync(readme)) {
      const body = readFileSync(readme, 'utf8').replace(/^#.*$/m, '').trim()
      first = body.split(/(?<=\.)\s/)[0].replace(/\s+/g, ' ').slice(0, 160)
    }
    return { folder: `ui/src/${n}/`, summary: first }
  })
  const main = readFileSync(join(srcDir, 'main.tsx'), 'utf8')
  const styles = [...main.matchAll(/import\s+'([^']+\.css)'/g)].map((m) => m[1])
  const tsconfig = existsSync(join(uiDir, 'tsconfig.app.json')) ? JSON.parse(readFileSync(join(uiDir, 'tsconfig.app.json'), 'utf8').replace(/\/\/.*$/gm, '').replace(/\/\*[\s\S]*?\*\//g, '')) : {}
  return { pkg, folders, styles, tsconfig: tsconfig.compilerOptions ?? {} }
}

// ---------------------------------------------------------------- assemble

const colours = colourLiterals()
const clusters = clusterColours(colours)
const { decls, medias } = cssDeclarations()
const inline = inlineStyles()
const comps = componentInventory()
const icons = iconInventory()
const a11y = a11yFlags()
const tokens = tokenUsage()
const contrast = contrastFailures()
const stack = stackAndMap()

const scales = {}
for (const [label, props] of Object.entries(SCALE_PROPS)) {
  const fromCss = decls.filter((d) => props.includes(d.prop))
  const fromInline = inline.filter((d) => CAMEL_TO_PROP.get(d.prop) && props.includes(CAMEL_TO_PROP.get(d.prop)))
  const items = [...fromCss, ...fromInline]
  const grouped = groupValues(items)
  const nums = items.flatMap((i) => pxNumbers(i.value))
  scales[label] = { values: grouped, pxClusters: clusterPx(nums), total: items.length, inline: fromInline.length }
}
const motion = groupValues(decls.filter((d) => /^(transition|animation)(-duration|-timing-function)?$/.test(d.prop)))
const inlineOpacity = groupValues(inline.filter((d) => d.prop === 'opacity'))
const inlineTotal = inline.length
const inlinePx = inline.filter((d) => /px$/.test(d.value) && d.value !== '0px').length

const literalBreakpoints = groupValues(medias.map((m) => ({ ...m, value: m.query })))

const monogram = icons.duplicates.find((d) => d.locations.some((l) => l.includes('Header.tsx')))
const deadComponents = comps.filter((c) => c.importers === 0 && !/^(App|AppShell)$/.test(c.name) && !c.path.endsWith('main.tsx'))
const literalColoursOutside = colours.filter((c) => !c.inTokens)

const debt = [
  ['Inline px spacing/sizes in `style={{}}`', inlinePx, 'tokenise: `var(--space-*)`, `var(--size-*)`'],
  ['Colour literals outside tokens', literalColoursOutside.length, 'move to tokens.json; computed colours keep the `design-pack: computed-color` marker'],
  ['Glyph and inline-SVG icons', icons.glyphs.length + icons.entities.length + icons.svgs.length, '`<Icon name>`; data drawings keep `design-pack: data-svg`'],
  ['`<button>` without `type`', a11y.buttonNoType.length, '`type="button"` (one `submit`)'],
  ['Clickable non-interactive elements', a11y.clickableNonInteractive.length, 'real `<button>` or `role` + `tabIndex` + key handler'],
  ['Duplicated monogram', monogram ? monogram.locations.length : 0, 'one `derate-mark` icon'],
  ['Unused tokens', tokens.unused.length, tokens.unused.join(', ') || '—'],
  ['Dead components (zero importers)', deadComponents.length, deadComponents.map((c) => c.name).join(', ') || '—'],
  ['Literal breakpoints / transitions', literalBreakpoints.length + motion.length, '`breakpoint.*`, `dur-*`, `ease*` tokens'],
  ['`THEMES` copy in docs/screenshots/brand/build.py', existsSync(join(repo, 'docs/screenshots/brand/build.py')) && /THEMES\s*=\s*\{[\s\S]*?#[0-9A-Fa-f]{6}/.test(readFileSync(join(repo, 'docs/screenshots/brand/build.py'), 'utf8')) ? 1 : 0, 'read tokens.flat.json'],
]

const result = { generated: new Date().toISOString(), stack, colours, clusters, scales, motion, medias, inline: { total: inlineTotal, px: inlinePx, opacity: inlineOpacity }, components: comps, icons, a11y, tokens, contrast, debt }

if (wantJson) {
  // No process.exit here: it truncates a piped stdout at 64 KiB.
  process.stdout.write(JSON.stringify(result, null, 2) + '\n')
} else {
  writeMarkdown()
}

// ---------------------------------------------------------------- markdown

function writeMarkdown() {
const md = []
const h = (n, t) => md.push('', '#'.repeat(n) + ' ' + t, '')
const table = (head, rows) => {
  md.push('| ' + head.join(' | ') + ' |', '|' + head.map(() => '---').join('|') + '|')
  for (const r of rows) md.push('| ' + r.map((c) => String(c ?? '').replace(/\|/g, '\\|')).join(' | ') + ' |')
  md.push('')
}
const code = (s) => '`' + s + '`'
const locs = (l) => l.map(code).join(', ')

md.push(`<!-- generated by .omp/design/audit.mjs on ${result.generated}; rerun: cd .omp && npm run audit -->`)
md.push('# UI design audit — derate', '', `${files.length} source files under \`ui/src\` (\`.ts\`, \`.tsx\`, \`.css\`; verifiers, mockups and \`dist\` excluded).`)

h(2, 'Stack & map')
table(['Fact', 'Value'], [
  ['Framework', `react ${stack.pkg.dependencies.react}, react-dom ${stack.pkg.dependencies['react-dom']}`],
  ['Build', `vite ${stack.pkg.devDependencies.vite}, @vitejs/plugin-react ${stack.pkg.devDependencies['@vitejs/plugin-react']}, typescript ${stack.pkg.devDependencies.typescript}`],
  ['TS', `strict=${stack.tsconfig.strict}, jsx=${stack.tsconfig.jsx}, moduleResolution=${stack.tsconfig.moduleResolution}, noUncheckedIndexedAccess=${stack.tsconfig.noUncheckedIndexedAccess}`],
  ['Styling', 'plain CSS custom properties; no Tailwind, no CSS modules, no CSS-in-JS'],
  ['Runtime deps', Object.keys(stack.pkg.dependencies).join(', ')],
  ['Entry', '`ui/index.html` → `ui/src/main.tsx` → `shell/AppShell.tsx`'],
  ['Stylesheet order (main.tsx)', stack.styles.map(code).join(' → ')],
  ['Verifiers', `${walkCheck(srcDir).length} \`*.check.mjs\` discovered by \`ui/check.mjs\``],
])
table(['Folder', 'Purpose (first sentence of its README)'], stack.folders.map((f) => [code(f.folder), f.summary || '—']))

h(2, 'Colour literals')
const palette = colours.filter((c) => c.inTokens)
md.push(`${palette.length} literals in ${tokensCssPath ? code(rel(tokensCssPath)) : 'tokens'} (the palette) and **${literalColoursOutside.length} outside it**.`, '')
h(3, 'Outside the palette')
table(['Location', 'Value'], literalColoursOutside.map((c) => [code(`${c.file}:${c.line}`), code(c.value)]))
h(3, 'Palette')
const palGrouped = groupValues(palette)
table(['Value', 'Count', 'Lines'], palGrouped.map((g) => [code(g.value), g.count, g.locations.map((l) => l.split(':')[1]).join(', ')]))
h(3, `Near-duplicate clusters (CIE76 ΔE < 3)`)
if (!clusters.length) md.push('None.', '')
else table(['Members', 'Locations'], clusters.map((c) => [c.members.map(code).join(' '), locs(c.locations)]))

h(2, 'Scale values')
md.push(`${decls.length} CSS declarations and ${inlineTotal} inline \`style={{}}\` properties (${inlinePx} of them literal px).`, '')
for (const [label, s] of Object.entries(scales)) {
  h(3, `${label} (${s.total} declarations, ${s.inline} inline)`)
  table(['Value', 'Count', 'Where'], s.values.slice(0, 60).map((v) => [code(v.value), v.count, locs(v.locations.slice(0, 3))]))
  if (s.values.length > 60) md.push(`… ${s.values.length - 60} more values.`, '')
  if (s.pxClusters.length) md.push('Within 1px of each other: ' + s.pxClusters.map((c) => code(c.join(' / '))).join(', '), '')
}
h(3, `@media queries (${medias.length})`)
table(['Query', 'Count', 'Where'], literalBreakpoints.map((v) => [code(v.value), v.count, locs(v.locations.slice(0, 3))]))
h(3, `transition / animation (${motion.reduce((n, v) => n + v.count, 0)})`)
table(['Value', 'Count', 'Where'], motion.map((v) => [code(v.value), v.count, locs(v.locations.slice(0, 3))]))
h(3, 'Inline opacity')
table(['Value', 'Count', 'Where'], inlineOpacity.map((v) => [code(v.value), v.count, locs(v.locations.slice(0, 3))]))

h(2, 'Component inventory')
md.push(`${comps.length} exported PascalCase functions/consts in \`.tsx\` files.`, '')
table(['Component', 'Path', 'Props', 'Variant props', 'Importers', 'Tests'], comps.map((c) => [
  code(c.name), code(c.path), c.props ? code(c.props.slice(0, 60)) : '—',
  c.variants.map((v) => `${v.prop}: ${v.values.join('\\|')}`).join('; ') || '—', c.importers, c.tests ? code(c.tests) : '—',
]))

h(2, 'Icon inventory')
h(3, `Inline \`<svg>\` (${icons.svgs.length})`)
table(['Location', 'viewBox', 'stroke-width', 'fill / stroke', 'aria', 'paths'], icons.svgs.map((s) => [code(`${s.file}:${s.line}`), s.viewBox ?? '—', s.strokeWidth ?? '—', `${s.fill ?? '—'} / ${s.stroke ?? '—'}`, s.aria.join(' ') || '—', s.paths.length]))
h(3, `Duplicate drawings (identical \`d=\` sets)`)
if (!icons.duplicates.length) md.push('None.', '')
else table(['Locations', 'First path'], icons.duplicates.map((d) => [locs(d.locations), code(d.paths)]))
h(3, `\`<img>\` / \`<image>\` (${icons.imgs.length})`)
table(['Location', 'Source'], icons.imgs.map((i) => [code(`${i.file}:${i.line}`), code(i.src)]))
h(3, `Glyph characters used as icons (${icons.glyphs.length})`)
table(['Glyph', 'Location'], icons.glyphs.map((g) => [code(g.glyph), code(`${g.file}:${g.line}`)]))
h(3, `HTML entities (${icons.entities.length})`)
table(['Entity', 'Char', 'Location'], icons.entities.map((e) => [code(e.entity), e.char, code(`${e.file}:${e.line}`)]))

h(2, 'Accessibility red flags')
const flag = (title, list, fmt = (x) => code(`${x.file}:${x.line}`)) => {
  h(3, `${title} (${list.length})`)
  md.push(list.length ? list.map(fmt).join(', ') : 'None.', '')
}
flag('`<img>` without `alt`', a11y.imgNoAlt)
flag('`<button>` without `type`', a11y.buttonNoType)
flag('`<button>` with no text and no label', a11y.iconButtonNoLabel)
flag('Click/key handlers on non-interactive elements without `role`/`tabIndex`', a11y.clickableNonInteractive, (x) => code(`${x.file}:${x.line} <${x.tag}>${x.note ? ' ' + x.note : ''}`))
flag('Positive `tabIndex`', a11y.tabindexPositive)
h(3, 'Computed contrast failures among declared token pairs')
if (contrast.pending) md.push('pending tokens:check (`.omp/design/tokens/check.mjs` does not exist yet).', '')
else if (!contrast.failures.length) md.push('None — every pair in `pairs.json` passes in every theme.', '')
else table(['Pair', 'Theme', 'Ratio', 'Min'], contrast.failures.map((f) => [f.error ?? `${f.fg} on ${f.bg}`, f.theme ?? '', f.ratio ?? '', f.min ?? '']))

h(2, 'Debt, ranked')
table(['#', 'Debt', 'Count', 'Fix'], debt.map((d, i) => [i + 1, d[0], d[1], d[2]]))

h(2, 'Tokens')
md.push(`${tokens.defined.length} custom properties defined; unused: ${tokens.unused.map(code).join(', ') || 'none'}.`, '')

writeFileSync(outPath, md.join('\n') + '\n')
console.log(`wrote ${rel(outPath)} (${md.length} lines; ${literalColoursOutside.length} colour literals, ${inlinePx} inline px, ${comps.length} components)`)
}

function walkCheck(dir, out = []) {
  for (const n of readdirSync(dir)) {
    const p = join(dir, n)
    if (statSync(p).isDirectory()) walkCheck(p, out)
    else if (n.endsWith('.check.mjs')) out.push(p)
  }
  return out
}
