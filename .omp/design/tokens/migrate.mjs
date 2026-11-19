#!/usr/bin/env node
// One-shot, kept for the record: rewrote ui/src from literal px / colour /
// duration values to the token vocabulary on 2026-11-17. It is pixel-exact --
// every rewrite maps a value to the token that holds exactly that value -- and
// idempotent, so re-running it is harmless. What it could not rewrite it
// prints under "left for review"; tests/ui/static/lint.mjs is the gate that
// keeps the tree clean afterwards.
//
//   cd .omp && node design/tokens/migrate.mjs [--dry]

import { readFileSync, writeFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative, resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const repo = resolve(here, '..', '..', '..')
const src = join(repo, 'ui', 'src')
const dry = process.argv.includes('--dry')

const walk = (d, out = []) => {
  for (const n of readdirSync(d)) {
    const p = join(d, n)
    if (statSync(p).isDirectory()) walk(p, out)
    else if (/\.(tsx?|css)$/.test(n) && !/\.check\.mjs$|\.d\.ts$/.test(n)) out.push(p)
  }
  return out
}

// ------------------------------------------------------------------ tables
const SPACE_OLD = { '--s-1': 4, '--s-2': 8, '--s-3': 12, '--s-4': 16, '--s-5': 24, '--s-6': 32, '--s-7': 48, '--s-half': 6, '--s1': 12, '--s2': 24, '--s3': 36 }
const SPACE_SET = new Set([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 20, 22, 24, 30, 32, 34, 36, 48, 66, 96])
const SIZE = { 10: 'size-badge', 11: 'size-micro', 11.5: 'size-caption', 12: 'size-unit', 13: 'size-label', 13.5: 'size-title-sm', 14: 'size-body-sm', 15: 'size-body', 17: 'size-title', 18: 'size-wordmark', 21: 'size-readout-sm', 22: 'size-plate', 26: 'size-h1', 27: 'size-mono-readout', 28: 'size-readout', 42: 'size-hero', 46: 'size-mono-readout-xl', 48: 'size-readout-xl' }
// 14px in a mono context is the mono body step, not the sans one.
const SIZE_OVERRIDES = { 'ui/src/tabs/setup/setup.css:95': 'size-mono-body', 'ui/src/tabs/setup/setup.css:163': 'size-mono-body' }
const RADIUS = { '2px': 'radius-sm', '3px': 'radius', '6px': 'radius-lg', '50%': 'radius-full' }
const TRACK = { '-0.3px': 'tracking-tight', '-.3px': 'tracking-tight', '-0.2px': 'tracking-snug' }
const Z = { '-1': 'z-below', 0: 'z-base', 1: 'z-sticky', 5: 'z-rail', 9: 'z-overlay' }
const DUR = { '0.12s': 'dur-fast', '120ms': 'dur-fast', '0.16s': 'dur-mid', '160ms': 'dur-mid', '0.18s': 'dur', '180ms': 'dur', '600ms': 'dur-slow', '0.6s': 'dur-slow', '1.4s': 'dur-dots', '1400ms': 'dur-dots', '1.6s': 'dur-sweep', '1600ms': 'dur-sweep' }
const OPACITY = { 0.25: 25, 0.38: 38, 0.45: 45, 0.55: 55, 0.6: 60, 0.62: 62, 0.72: 72, 0.85: 85 }

const CSS_SPACING = /^(margin|margin-top|margin-right|margin-bottom|margin-left|margin-inline|margin-block|padding|padding-top|padding-right|padding-bottom|padding-left|padding-inline|padding-block|gap|row-gap|column-gap|inset|top|right|bottom|left)$/
const TSX_SPACING = /^(margin|marginTop|marginRight|marginBottom|marginLeft|marginInline|marginBlock|padding|paddingTop|paddingRight|paddingBottom|paddingLeft|paddingInline|paddingBlock|gap|rowGap|columnGap|inset|top|right|bottom|left)$/

const review = []
const counts = {}
const bump = (k) => (counts[k] = (counts[k] ?? 0) + 1)

const spaceVar = (n) => {
  const abs = Math.abs(n)
  if (!SPACE_SET.has(abs)) return null
  return n < 0 ? `calc(-1 * var(--space-${abs}))` : `var(--space-${abs})`
}
// Rewrite every `<n>px` inside a spacing value; leave `0` and anything unknown.
const spaceValue = (value, where) => {
  let ok = true
  const out = value.replace(/(-?\d*\.?\d+)px\b/g, (m, n) => {
    const v = spaceVar(parseFloat(n))
    if (!v) { ok = false; return m }
    bump('space'); return v
  })
  if (!ok) review.push(`${where} spacing ${value}`)
  return out
}

// ------------------------------------------------------------------ css
function migrateCss(text, file) {
  const lines = text.split('\n')
  for (let i = 0; i < lines.length; i++) {
    const where = `${file}:${i + 1}`
    let line = lines[i]
    // old spacing names
    line = line.replace(/var\((--s(?:-\d|-half|\d))\)/g, (m, n) => (SPACE_OLD[n] ? (bump('s-rename'), `var(--space-${SPACE_OLD[n]})`) : m))
    // declarations: prop: value;
    line = line.replace(/(^|[{;\s])([a-z-]+)\s*:\s*([^;{}]+?)(\s*)(;|$|})/g, (m, pre, prop, value, ws, end) => {
      if (prop.startsWith('--')) return m
      let v = value
      if (CSS_SPACING.test(prop)) v = spaceValue(value, where)
      else if (prop === 'font-size') {
        v = value.replace(/^(-?\d*\.?\d+)px$/, (mm, n) => { const t = SIZE_OVERRIDES[where] ?? SIZE[parseFloat(n)]; if (!t) { review.push(`${where} font-size ${value}`); return mm } bump('font-size'); return `var(--${t})` })
        v = v.replace(/var\(--size-mono-unit, 11px\)/, 'var(--size-mono-unit)')
      } else if (prop === 'border-radius') {
        v = value.replace(/(\d*\.?\d+(?:px|%))/g, (mm) => (RADIUS[mm] ? (bump('radius'), `var(--${RADIUS[mm]})`) : (review.push(`${where} radius ${value}`), mm)))
      } else if (prop === 'letter-spacing') {
        v = TRACK[value] ? (bump('tracking'), `var(--${TRACK[value]})`) : value
      } else if (prop === 'z-index') {
        v = Z[value] !== undefined ? (bump('z'), `var(--${Z[value]})`) : (review.push(`${where} z-index ${value}`), value)
      } else if (prop === 'font-family') {
        if (/^'IBM Plex Mono', monospace$/.test(value)) { bump('font-family'); v = 'var(--font-mono)' }
        else if (/^'IBM Plex Sans', sans-serif$/.test(value)) { bump('font-family'); v = 'var(--font-sans)' }
      } else if (/^(transition|animation)(-duration|-timing-function)?$/.test(prop)) {
        v = value
          .replace(/\b(\d*\.?\d+m?s)\b(?!\s*!important)/g, (mm) => (DUR[mm] ? (bump('duration'), `var(--${DUR[mm]})`) : mm))
          .replace(/var\(--ease, cubic-bezier\(0\.2, 0\.7, 0\.3, 1\)\)/g, 'var(--ease)')
          .replace(/\bease-in-out\b/g, () => (bump('easing'), 'var(--ease-inout)'))
          .replace(/(^|\s)ease(?=[\s,]|$)/g, (mm, pre) => (bump('easing'), `${pre}var(--ease-default)`))
      } else if (prop === 'color' && value === 'rgb(255 255 255 / 0.94)') { bump('colour'); v = 'var(--on-accent)' }
      else if (prop === 'background' && value === '#f7f4ee') { bump('colour'); v = 'var(--qr-tile)' }
      return `${pre}${prop}: ${v}${ws}${end}`
    })
    lines[i] = line
  }
  return lines.join('\n')
}

// ------------------------------------------------------------------ tsx
// Rewrites the values of known props inside style={{ ... }} objects.
function migrateTsx(text, file) {
  let out = ''
  let i = 0
  while (i < text.length) {
    const at = text.indexOf('style={{', i)
    if (at < 0) { out += text.slice(i); break }
    out += text.slice(i, at + 'style={'.length)
    // find the matching close of the inner object
    let depth = 0, j = at + 'style={'.length
    for (; j < text.length; j++) {
      if (text[j] === '{') depth++
      else if (text[j] === '}') { depth--; if (depth === 0) break }
    }
    const obj = text.slice(at + 'style={'.length, j + 1) // { ... }
    const line = text.slice(0, at).split('\n').length
    out += migrateStyleObject(obj, `${file}:${line}`)
    i = j + 1
  }
  // old spacing names anywhere (template strings, cssText)
  out = out.replace(/var\((--s(?:-\d|-half|\d))\)/g, (m, n) => (SPACE_OLD[n] ? (bump('s-rename'), `var(--space-${SPACE_OLD[n]})`) : m))
  return out
}

function migrateStyleObject(obj, where) {
  // split top-level entries
  const body = obj.slice(1, -1)
  const parts = []
  let d = 0, buf = '', q = null
  for (let k = 0; k < body.length; k++) {
    const c = body[k]
    if (q) { buf += c; if (c === q && body[k - 1] !== '\\') q = null; continue }
    if (c === "'" || c === '"' || c === '`') { q = c; buf += c; continue }
    if ('([{'.includes(c)) d++
    if (')]}'.includes(c)) d--
    if (c === ',' && d === 0) { parts.push(buf); buf = '' } else buf += c
  }
  parts.push(buf)
  const rewritten = parts.map((part) => {
    const m = part.match(/^(\s*)([A-Za-z]+)(\s*:\s*)([\s\S]*?)(\s*)$/)
    if (!m) return part
    const [, pre, prop, sep, value, post] = m
    let v = value
    const num = (s) => (/^-?\d*\.?\d+$/.test(s) ? parseFloat(s) : null)
    const q = (s) => `'${s}'`
    const mapNumeric = (val, fn) => {
      // plain number, 'Npx' string, or a ternary whose branches are those
      const one = (s) => {
        s = s.trim()
        const n = num(s) ?? (/^['"]-?\d*\.?\d+px['"]$/.test(s) ? parseFloat(s.slice(1, -1)) : null)
        if (n === null) return null
        if (n === 0) return s === '0' ? s : '0'
        return fn(n)
      }
      const direct = one(val)
      if (direct !== null) return direct
      const t = val.match(/^([^?]+)\?([^:]+):([\s\S]+)$/)
      if (t) {
        const a = one(t[2]), b = one(t[3])
        if (a !== null && b !== null) return `${t[1].trim()} ? ${a} : ${b}`
      }
      return null
    }
    if (TSX_SPACING.test(prop)) {
      const r = mapNumeric(value, (n) => { const sv = spaceVar(n); if (!sv) return null; bump('space'); return q(sv) })
      if (r !== null && !r.includes('null')) v = r
      else if (/^['"][^'"]*px[^'"]*['"]$/.test(value.trim())) {
        // shorthand string like '0 0 6px'
        const inner = value.trim().slice(1, -1)
        const sv = spaceValue(inner, where)
        v = q(sv)
      } else if (/\d/.test(value) && !/^`/.test(value.trim()) && !/^['"]/.test(value.trim())) review.push(`${where} ${prop}: ${value.trim()}`)
    } else if (prop === 'fontSize') {
      const r = mapNumeric(value, (n) => (SIZE[n] ? (bump('font-size'), q(`var(--${SIZE[n]})`)) : null))
      if (r !== null && !r.includes('null')) v = r
      else if (/\d/.test(value)) review.push(`${where} fontSize: ${value.trim()}`)
    } else if (prop === 'letterSpacing') {
      const s = value.trim().replace(/^['"]|['"]$/g, '')
      if (TRACK[s]) { bump('tracking'); v = q(`var(--${TRACK[s]})`) }
      else if (s === '0px' || s === '0') v = '0'
    } else if (prop === 'borderRadius') {
      const s = value.trim()
      const n = num(s)
      const key = n !== null ? `${n}px` : s.replace(/^['"]|['"]$/g, '')
      if (n === 0 || key === '0px') v = '0'
      else if (RADIUS[key]) { bump('radius'); v = q(`var(--${RADIUS[key]})`) }
      else if (/\d/.test(s)) review.push(`${where} borderRadius: ${s}`)
    } else if (prop === 'zIndex') {
      const s = value.trim()
      if (Z[s] !== undefined) { bump('z'); v = q(`var(--${Z[s]})`) }
    } else if (prop === 'opacity') {
      v = value.replace(/(?<![\w.])(0?\.\d+)(?![\w.])/g, (mm) => { const n = parseFloat(mm); return OPACITY[n] ? (bump('opacity'), q(`var(--opacity-${OPACITY[n]})`)) : mm })
    }
    return `${pre}${prop}${sep}${v}${post}`
  })
  return `{${rewritten.join(',')}}`
}

// ------------------------------------------------------------------ run
let changed = 0
for (const p of walk(src)) {
  const file = relative(repo, p)
  const before = readFileSync(p, 'utf8')
  const after = p.endsWith('.css') ? migrateCss(before, file) : p.endsWith('.tsx') ? migrateTsx(before, file) : before.replace(/var\((--s(?:-\d|-half|\d))\)/g, (m, n) => (SPACE_OLD[n] ? `var(--space-${SPACE_OLD[n]})` : m))
  if (after !== before) {
    changed++
    if (!dry) writeFileSync(p, after)
  }
}
console.log(`${dry ? 'would change' : 'changed'} ${changed} files;`, Object.entries(counts).map(([k, v]) => `${k} ${v}`).join(', '))
if (review.length) console.log('\nleft for review:\n  ' + review.join('\n  '))
