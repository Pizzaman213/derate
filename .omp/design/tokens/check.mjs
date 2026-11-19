#!/usr/bin/env node
// The contrast gate. No dependencies.
//
//   cd .omp && npm run tokens:check          # one line per pair per theme; exit 1 on any failure
//   node design/tokens/check.mjs --json      # [{fg,bg,theme,ratio,min,ok}]
//
// WCAG 2.2 relative luminance and contrast ratio. A translucent colour is
// composited over its background first (and a translucent background over
// `panel`, or over the pair's `over` token), because that is what the eye
// measures. Also fails when tokens.css / tokens.ts / tokens.flat.json are
// stale against tokens.json -- rebuild to memory and byte-compare.

import { readFileSync, existsSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { build, loadTokens, THEMES, OUTPUTS } from './build.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const wantJson = process.argv.includes('--json')

export function parseColour(s) {
  s = String(s).trim().toLowerCase()
  let m
  if ((m = s.match(/^#([0-9a-f]{6})([0-9a-f]{2})?$/))) {
    const n = parseInt(m[1], 16)
    return [n >> 16, (n >> 8) & 255, n & 255, m[2] ? parseInt(m[2], 16) / 255 : 1]
  }
  if ((m = s.match(/^#([0-9a-f]{3})$/))) return [...m[1]].map((c) => parseInt(c + c, 16)).concat([1])
  if ((m = s.match(/^rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)(?:\s*[,/]\s*([\d.]+%?))?\s*\)$/)))
    return [+m[1], +m[2], +m[3], m[4] == null ? 1 : m[4].endsWith('%') ? parseFloat(m[4]) / 100 : +m[4]]
  if ((m = s.match(/^hsla?\(\s*([\d.]+)(?:deg)?[\s,]+([\d.]+)%[\s,]+([\d.]+)%(?:\s*[,/]\s*([\d.]+%?))?\s*\)$/))) {
    const h = +m[1], sat = +m[2] / 100, l = +m[3] / 100
    const k = (n) => (n + h / 30) % 12
    const a = sat * Math.min(l, 1 - l)
    const f = (n) => l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)))
    return [f(0) * 255, f(8) * 255, f(4) * 255, m[4] == null ? 1 : m[4].endsWith('%') ? parseFloat(m[4]) / 100 : +m[4]]
  }
  return null
}
export function composite(fg, bg) {
  const a = fg[3]
  return [0, 1, 2].map((i) => fg[i] * a + bg[i] * (1 - a)).concat([1])
}
export function luminance([r, g, b]) {
  const f = (c) => { c /= 255; return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4 }
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
}
export function contrast(fg, bg) {
  const l1 = luminance(fg), l2 = luminance(bg)
  return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05)
}

export function runPairs(flat, pairsDoc) {
  const value = (name, theme) => {
    const v = flat[name]
    if (v === undefined) throw new Error(`pairs.json names unknown token "${name}"`)
    return typeof v === 'object' ? v[theme] : v
  }
  const colourOf = (name, theme, under) => {
    const c = parseColour(value(name, theme))
    if (!c) throw new Error(`token ${name} (${theme}) is not a plain colour: ${value(name, theme)}`)
    return c[3] < 1 && under ? composite(c, under) : c
  }
  const results = []
  for (const p of pairsDoc.pairs) {
    for (const theme of THEMES) {
      const min = theme === 'high-contrast' ? p['hc-min'] : p.min
      const base = colourOf(p.over ?? 'panel', theme)
      for (const bgName of p.bg) {
        const bg = colourOf(bgName, theme, base)
        for (const fgName of p.fg) {
          const fg = colourOf(fgName, theme, bg)
          const ratio = contrast(fg, bg)
          results.push({ fg: fgName, bg: bgName + (p.over ? ` (over ${p.over})` : ''), theme, kind: p.kind, ratio: +ratio.toFixed(2), min, ok: ratio >= min })
        }
      }
    }
  }
  return results
}

export function staleOutputs() {
  const out = build(loadTokens())
  const stale = []
  for (const [k, file] of Object.entries(OUTPUTS)) {
    const p = join(here, file)
    if (!existsSync(p) || readFileSync(p, 'utf8') !== out[k]) stale.push(file)
  }
  return { stale, out }
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const { stale, out } = staleOutputs()
  const pairs = JSON.parse(readFileSync(join(here, 'pairs.json'), 'utf8'))
  const results = runPairs(out.flat, pairs)
  const failures = results.filter((r) => !r.ok)
  if (wantJson) {
    process.stdout.write(JSON.stringify(results.concat(stale.map((f) => ({ stale: f, ok: false }))), null, 2) + '\n')
  } else {
    for (const r of results) console.log(`${r.ok ? 'ok  ' : 'FAIL'} ${r.theme.padEnd(13)} ${r.fg} on ${r.bg}  ${r.ratio.toFixed(2)}:1  (min ${r.min})`)
    for (const f of stale) console.log(`FAIL stale ${f} — run: cd .omp && npm run tokens:build`)
    console.log(`\n${results.length - failures.length}/${results.length} pairs pass across ${THEMES.length} themes${stale.length ? `; ${stale.length} stale output(s)` : ''}`)
  }
  process.exitCode = failures.length || stale.length ? 1 : 0
}
