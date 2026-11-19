#!/usr/bin/env node
// Static design rules for ui/src. No dependencies.
//
//   cd .omp && npm run ui:static                      # ../ui/src
//   node tests/ui/static/lint.mjs <path…> [--json]    # files or directories
//
// Scans *.ts, *.tsx, *.css (not *.check.mjs, *.d.ts, mockups, generated pack
// files). Prints `path:line:col rule message`, exit 1 on any finding; --json
// prints the same as an array. Importable: lint(paths) returns the findings.
//
// Rules, by id:
//   color-literal       hex / rgb() / hsl() / named colour. Allowed only in a
//                       file carrying `// design-pack: computed-color`, and
//                       there only on a line that builds a template literal.
//   px-literal          literal px (other than 0) in a spacing, font-size,
//                       border-radius or letter-spacing declaration, or the
//                       camelCase equivalent in an inline style object (a
//                       number, an 'Npx' string, a shorthand string, or either
//                       as a ternary branch). 1px/2px stay legal on border*,
//                       outline* and box-shadow, which are not in the list.
//   z-index-literal     z-index / zIndex with a number instead of a --z-* token
//   breakpoint-literal  an @media px that is not one of the breakpoints
//   duration-literal    transition / animation with a literal s or ms (the
//                       prefers-reduced-motion clamp block is exempt)
//   font-family-literal font-family / fontFamily not var(--font-*) or inherit
//   raw-svg             <svg outside components/Icon.tsx and files carrying
//                       `// design-pack: data-svg`
//   img-alt             <img without alt
//   icon-button-label   <button with no text and no aria-label / aria-labelledby / title
//   onclick-non-interactive  onClick/onDoubleClick/onKeyDown on div|span|li|td|tr|p|section|g
//                       without role or tabIndex
//   button-type         <button without type=
//   tabindex-positive   tabIndex > 0

import { readFileSync, readdirSync, statSync, existsSync } from 'node:fs'
import { join, relative, resolve, extname, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const repo = resolve(here, '..', '..', '..', '..')

export const BREAKPOINTS = new Set(Object.values(JSON.parse(readFileSync(join(repo, '.omp', 'design', 'tokens', 'tokens.json'), 'utf8')).breakpoint).filter((v) => v && v.$value).map((v) => parseInt(v.$value, 10)))

const NAMED = 'aliceblue|antiquewhite|aqua|aquamarine|azure|beige|bisque|black|blanchedalmond|blue|blueviolet|brown|burlywood|cadetblue|chartreuse|chocolate|coral|cornflowerblue|cornsilk|crimson|cyan|darkblue|darkcyan|darkgoldenrod|darkgray|darkgreen|darkgrey|darkkhaki|darkmagenta|darkolivegreen|darkorange|darkorchid|darkred|darksalmon|darkseagreen|darkslateblue|darkslategray|darkslategrey|darkturquoise|darkviolet|deeppink|deepskyblue|dimgray|dimgrey|dodgerblue|firebrick|floralwhite|forestgreen|fuchsia|gainsboro|ghostwhite|gold|goldenrod|gray|green|greenyellow|grey|honeydew|hotpink|indianred|indigo|ivory|khaki|lavender|lavenderblush|lawngreen|lemonchiffon|lightblue|lightcoral|lightcyan|lightgoldenrodyellow|lightgray|lightgreen|lightgrey|lightpink|lightsalmon|lightseagreen|lightskyblue|lightslategray|lightslategrey|lightsteelblue|lightyellow|lime|limegreen|linen|magenta|maroon|mediumaquamarine|mediumblue|mediumorchid|mediumpurple|mediumseagreen|mediumslateblue|mediumspringgreen|mediumturquoise|mediumvioletred|midnightblue|mintcream|mistyrose|moccasin|navajowhite|navy|oldlace|olive|olivedrab|orange|orangered|orchid|palegoldenrod|palegreen|paleturquoise|palevioletred|papayawhip|peachpuff|peru|pink|plum|powderblue|purple|rebeccapurple|red|rosybrown|royalblue|saddlebrown|salmon|sandybrown|seagreen|seashell|sienna|silver|skyblue|slateblue|slategray|slategrey|snow|springgreen|steelblue|tan|teal|thistle|tomato|turquoise|violet|wheat|white|whitesmoke|yellow|yellowgreen'
const CSS_SPACING = /^(margin(-top|-right|-bottom|-left|-inline|-block)?|padding(-top|-right|-bottom|-left|-inline|-block)?|gap|row-gap|column-gap|inset|top|right|bottom|left|font-size|border-radius|letter-spacing)$/
const TSX_SPACING = /^(margin(Top|Right|Bottom|Left|Inline|Block)?|padding(Top|Right|Bottom|Left|Inline|Block)?|gap|rowGap|columnGap|inset|top|right|bottom|left|fontSize|borderRadius|letterSpacing)$/

function walk(p, out = []) {
  const st = statSync(p)
  if (st.isDirectory()) {
    for (const n of readdirSync(p)) {
      if (n === 'node_modules' || n === 'dist' || n.startsWith('mockups') || n === '.build') continue
      walk(join(p, n), out)
    }
  } else if (/\.(tsx?|css)$/.test(p) && !/\.check\.mjs$|\.d\.ts$/.test(p) && !/\.omp\/design\//.test(p)) out.push(p)
  return out
}

function stripComments(text, css) {
  let out = text.replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, ' '))
  if (!css) out = out.replace(/(^|[^:'"`\\])\/\/[^\n]*/g, (m, p) => p + ' '.repeat(m.length - p.length))
  return out
}
const pos = (text, idx) => {
  let line = 1, last = -1
  for (let i = 0; i < idx; i++) if (text.charCodeAt(i) === 10) { line++; last = i }
  return { line, col: idx - last }
}

/** Every `<tag …>` opener in a JSX source: its attribute text, its index, and
 *  whether it is self-closing. Walks braces and quotes instead of a regex, so
 *  an onClick with nested template literals cannot hide the attributes after
 *  it. */
function openers(text, tag) {
  const out = []
  const re = new RegExp(`<${tag}\\b`, 'g')
  let m
  while ((m = re.exec(text))) {
    let i = m.index + m[0].length, depth = 0, q = null
    for (; i < text.length; i++) {
      const c = text[i]
      if (q) { if (c === q && text[i - 1] !== '\\') q = null; continue }
      if (depth === 0 && (c === '"' || c === "'")) { q = c; continue }
      if (c === '{') depth++
      else if (c === '}') depth--
      else if (c === '`' && depth > 0) { q = '`'; continue }
      else if (c === '>' && depth === 0) break
    }
    const attrs = text.slice(m.index + m[0].length, i)
    out.push({ index: m.index, attrs, selfClosing: attrs.trimEnd().endsWith('/'), end: i + 1 })
  }
  return out
}

export function lintFile(path) {
  const raw = readFileSync(path, 'utf8')
  const css = path.endsWith('.css')
  const tsx = path.endsWith('.tsx')
  const text = stripComments(raw, css)
  const rel = relative(repo, path)
  const out = []
  const add = (idx, rule, message) => { const { line, col } = pos(text, idx); out.push({ path: rel, line, col, rule, message }) }
  const computed = /design-pack: computed-color/.test(raw)
  const dataSvg = /design-pack: data-svg/.test(raw)

  // ---- colours
  const colourRe = new RegExp(`#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?\\b|#[0-9a-fA-F]{3}\\b(?=[;\\s,)])|\\b(?:rgba?|hsla?)\\([^)]*\\)|(?<=[\\s:,('"\`])(?:${NAMED})(?=[\\s;,)'"\`])`, 'g')
  const colourRegions = css
    ? [...text.matchAll(/:\s*([^;{}]+)/g)].map((m) => [m.index + m[0].indexOf(m[1]), m[1]])
    : [[0, text]]
  for (const [start, region] of colourRegions) {
    for (const m of region.matchAll(colourRe)) {
      const v = m[0]
      if (!css && !/^#|^rgb|^hsl/.test(v)) {
        // named colour in TS: only when a whole string literal is the name
        const around = text.slice(start + m.index - 1, start + m.index + v.length + 1)
        if (!/^['"`]\w+['"`]$/.test(around)) continue
      }
      const idx = start + m.index
      if (computed) {
        const lineText = text.slice(text.lastIndexOf('\n', idx) + 1, text.indexOf('\n', idx))
        if (lineText.includes('`')) continue
      }
      add(idx, 'color-literal', `${v} — use a token (var(--…) or tokens.<theme>.*)`)
    }
  }

  if (css) {
    // ---- declarations with media context
    let media = null
    const stack = []
    for (const m of text.matchAll(/@media\s*([^{]+)\{|\{|\}|([a-zA-Z-]+)\s*:\s*([^;{}]+?)\s*(?=[;}])/g)) {
      if (m[0].startsWith('@media')) {
        const q = m[1].trim()
        stack.push({ media: q })
        media = q
        for (const px of q.matchAll(/(\d+(?:\.\d+)?)px/g)) if (!BREAKPOINTS.has(parseFloat(px[1]))) add(m.index, 'breakpoint-literal', `@media ${px[0]} is not a breakpoint (${[...BREAKPOINTS].join('/')})`)
        continue
      }
      if (m[0] === '{') { stack.push({}); continue }
      if (m[0] === '}') { const top = stack.pop(); if (top?.media) media = stack.slice().reverse().find((s) => s.media)?.media ?? null; continue }
      const prop = m[2], value = m[3]
      if (!prop || prop.startsWith('--')) continue
      const before = text.slice(Math.max(0, m.index - 1), m.index)
      if (!/[{;\s]|^$/.test(before)) continue
      const at = m.index + m[0].indexOf(value)
      if (CSS_SPACING.test(prop)) {
        for (const px of value.matchAll(/(-?\d*\.?\d+)px\b/g)) if (parseFloat(px[1]) !== 0) add(at + px.index, 'px-literal', `${prop}: ${px[0]} — use var(--space-*) / var(--size-*) / var(--radius-*) / var(--tracking-*)`)
      } else if (prop === 'z-index') {
        if (/^-?\d+$/.test(value)) add(at, 'z-index-literal', `z-index: ${value} — use var(--z-*)`)
      } else if (/^(transition|animation)(-duration|-delay)?$/.test(prop)) {
        if (!/prefers-reduced-motion/.test(media ?? '')) for (const d of value.matchAll(/(?<![\w-])\d*\.?\d+m?s\b/g)) if (!/^0m?s$/.test(d[0])) add(at + d.index, 'duration-literal', `${prop}: ${d[0]} — use var(--dur*)`)
      } else if (prop === 'font-family') {
        if (!/^(var\(--font-|inherit)/.test(value)) add(at, 'font-family-literal', `font-family: ${value} — use var(--font-sans|--font-mono)`)
      }
    }
    return out
  }

  // ---- tsx / ts
  if (tsx || path.endsWith('.ts')) {
    // inline style objects
    for (const m of text.matchAll(/style=\{\{/g)) {
      let depth = 0, j = m.index + 'style={'.length
      for (; j < text.length; j++) { if (text[j] === '{') depth++; else if (text[j] === '}') { depth--; if (depth === 0) break } }
      const body = text.slice(m.index + 'style={{'.length, j)
      const bodyStart = m.index + 'style={{'.length
      let d = 0, q = null, start = 0
      const parts = []
      for (let k = 0; k <= body.length; k++) {
        const c = body[k]
        if (k === body.length) { parts.push([start, body.slice(start)]); break }
        if (q) { if (c === q && body[k - 1] !== '\\') q = null; continue }
        if (c === "'" || c === '"' || c === '`') { q = c; continue }
        if ('([{'.includes(c)) d++
        if (')]}'.includes(c)) d--
        if (c === ',' && d === 0) { parts.push([start, body.slice(start, k)]); start = k + 1 }
      }
      for (const [off, part] of parts) {
        const pm = part.match(/^(\s*)([A-Za-z]+)\s*:\s*([\s\S]*?)\s*$/)
        if (!pm) continue
        const prop = pm[2], value = pm[3]
        const at = bodyStart + off + pm[1].length
        const numeric = (s) => /^-?\d*\.?\d+$/.test(s.trim()) && parseFloat(s) !== 0
        const pxString = (s) => /^['"][^'"]*\d(?:\.\d+)?px[^'"]*['"]$/.test(s.trim())
        const branches = value.match(/^[^?]+\?([^:]+):([\s\S]+)$/)
        const offenders = branches ? [branches[1], branches[2]] : [value]
        if (TSX_SPACING.test(prop)) {
          for (const o of offenders) if (numeric(o) || pxString(o)) { add(at, 'px-literal', `${prop}: ${o.trim()} — use 'var(--space-*)' / 'var(--size-*)' / 'var(--radius-*)' / 'var(--tracking-*)'`); break }
        } else if (prop === 'zIndex') {
          for (const o of offenders) if (/^-?\d+$/.test(o.trim())) { add(at, 'z-index-literal', `zIndex: ${o.trim()} — use 'var(--z-*)'`); break }
        } else if (/^(transition|animation)(Duration|Delay)?$/.test(prop)) {
          if (/\d\s*m?s\b/.test(value)) add(at, 'duration-literal', `${prop}: ${value.trim()} — use var(--dur*)`)
        } else if (prop === 'fontFamily') {
          if (!/var\(--font-|inherit/.test(value)) add(at, 'font-family-literal', `fontFamily: ${value.trim()} — use var(--font-sans|--font-mono)`)
        }
      }
    }
  }
  if (!tsx) return out

  // ---- JSX structure
  if (!dataSvg && !/components\/Icon\.tsx$/.test(path)) for (const m of text.matchAll(/<svg\b/g)) add(m.index, 'raw-svg', '<svg> outside Icon.tsx — use <Icon name>, or mark a data drawing with `// design-pack: data-svg` on line 1')
  for (const o of openers(text, 'img')) if (!/\balt=/.test(o.attrs)) add(o.index, 'img-alt', '<img> without alt')
  for (const o of openers(text, 'button')) {
    const attrs = o.attrs
    if (!/\btype=/.test(attrs)) add(o.index, 'button-type', '<button> without type= (type="button", or "submit" in a form)')
    if (o.selfClosing) continue
    const close = text.indexOf('</button>', o.index)
    if (close < 0) continue
    const inner = text.slice(o.end, close)
    // Content is any text, entity or `{expression}` once decorative icons and
    // tags are stripped; a button whose only child is an <Icon decorative />
    // has none and needs a label.
    const textish = inner.replace(/<Icon\b[^>]*\/>/g, '').replace(/<[^>]*>/g, '').trim()
    const labelled = /aria-label(?:ledby)?=|\btitle=/.test(attrs)
    if (!textish && !labelled) add(o.index, 'icon-button-label', '<button> with no text and no aria-label / aria-labelledby / title')
  }
  for (const tag of ['div', 'span', 'li', 'td', 'tr', 'p', 'section', 'g']) for (const o of openers(text, tag)) {
    if (/\bon(Click|DoubleClick|KeyDown)=/.test(o.attrs) && !/\brole=/.test(o.attrs) && !/\btabIndex=/.test(o.attrs)) add(o.index, 'onclick-non-interactive', `<${tag}> with a click/key handler and no role or tabIndex — use <button>, or role + tabIndex + key handler`)
  }
  for (const m of text.matchAll(/tabIndex=\{?\s*([1-9]\d*)/g)) add(m.index, 'tabindex-positive', `tabIndex=${m[1]} — positive tabindex reorders the page; use 0 or -1`)
  return out
}

export function lint(paths) {
  const files = paths.flatMap((p) => (existsSync(p) ? walk(resolve(p)) : []))
  return files.flatMap(lintFile).sort((a, b) => a.path.localeCompare(b.path) || a.line - b.line || a.col - b.col)
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const args = process.argv.slice(2)
  const json = args.includes('--json')
  const paths = args.filter((a) => !a.startsWith('--'))
  if (!paths.length) { console.error('usage: lint.mjs <path…> [--json]'); process.exit(2) }
  const findings = lint(paths)
  if (json) process.stdout.write(JSON.stringify(findings, null, 2) + '\n')
  else {
    for (const f of findings) console.log(`${f.path}:${f.line}:${f.col} ${f.rule} ${f.message}`)
    const byRule = {}
    for (const f of findings) byRule[f.rule] = (byRule[f.rule] ?? 0) + 1
    console.log(findings.length ? `\n${findings.length} finding(s): ${Object.entries(byRule).map(([k, v]) => `${k} ${v}`).join(', ')}` : `static: ${paths.join(', ')} clean`)
  }
  process.exitCode = findings.length ? 1 : 0
}
