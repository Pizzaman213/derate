#!/usr/bin/env node
// The chart icon spec, enforced (icons/README.md). Exit 1 on any finding.
//
//   node icons/lint.mjs            lint
//   node icons/lint.mjs --write    also (re)build icons.svg and add missing icons.json entries
//
// Same rules as the design pack's icons (.omp/design/icons/lint.mjs): viewBox, root
// paint attributes, geometry-only children, no literal colour, 2px padding, kebab
// names, metadata, no duplicate geometry, fresh sprite. Plus: one icon per chart type.

import { readFileSync, writeFileSync, readdirSync, existsSync, statSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { dirname, join, basename } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const SVG_DIR = join(here, 'svg')
const META = join(here, 'icons.json')
const SPRITE = join(here, 'icons.svg')
const TYPES = join(here, '..', 'types')
const PAD = 2, BOX = 24
const ROOT_ATTRS = { viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '1.5', 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }
const GEOMETRY = { path: ['d'], circle: ['cx', 'cy', 'r'], rect: ['x', 'y', 'width', 'height', 'rx', 'ry'], line: ['x1', 'y1', 'x2', 'y2'], polyline: ['points'], polygon: ['points'], ellipse: ['cx', 'cy', 'rx', 'ry'] }
const CATEGORIES = ['comparison', 'part-to-whole', 'trend', 'timeline', 'distribution', 'relationship', 'flow-hierarchy', 'matrix', 'geo', 'kpi', 'sets']

const attrsOf = (s) => Object.fromEntries([...s.matchAll(/([a-zA-Z][a-zA-Z0-9:-]*)="([^"]*)"/g)].map((m) => [m[1], m[2]]))
export function parseSvg(text) {
  const root = text.match(/<svg\b([^>]*)>/)
  if (!root) throw new Error('no <svg> root')
  const body = text.slice(text.indexOf(root[0]) + root[0].length, text.lastIndexOf('</svg>'))
  const elements = [...body.matchAll(/<([a-zA-Z]+)\b([^>]*?)\/?>/g)].filter((m) => !m[0].startsWith('</')).map((m) => ({ tag: m[1], attrs: attrsOf(m[2]) }))
  return { attrs: attrsOf(root[1]), elements, body }
}

// Bounding box of path coordinates, control points included (stricter than the curve).
// Same algorithm as .omp/design/icons/lint.mjs pathBounds, extended with arc extents.
/** Points where an SVG arc reaches its extremes (endpoint -> centre parametrisation, SVG F.6.5). */
function arcExtremes(x1, y1, rx, ry, phiDeg, fa, fs, x2, y2) {
  if (rx === 0 || ry === 0) return []
  const phi = (phiDeg * Math.PI) / 180, cos = Math.cos(phi), sin = Math.sin(phi)
  const dx = (x1 - x2) / 2, dy = (y1 - y2) / 2
  const x1p = cos * dx + sin * dy, y1p = -sin * dx + cos * dy
  rx = Math.abs(rx); ry = Math.abs(ry)
  const lam = (x1p * x1p) / (rx * rx) + (y1p * y1p) / (ry * ry)
  if (lam > 1) { rx *= Math.sqrt(lam); ry *= Math.sqrt(lam) }
  const num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
  const den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
  const co = (fa === fs ? -1 : 1) * Math.sqrt(Math.max(0, num / den))
  const cxp = (co * rx * y1p) / ry, cyp = (-co * ry * x1p) / rx
  const cx = cos * cxp - sin * cyp + (x1 + x2) / 2, cy = sin * cxp + cos * cyp + (y1 + y2) / 2
  const ang = (ux, uy, vx, vy) => { const a = Math.atan2(ux * vy - uy * vx, ux * vx + uy * vy); return a }
  const t1 = ang(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
  let dt = ang((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
  if (!fs && dt > 0) dt -= 2 * Math.PI
  if (fs && dt < 0) dt += 2 * Math.PI
  const pts = []
  // sample the swept angle densely enough to catch every extreme of the ellipse
  for (let k = 0; k <= 64; k++) {
    const t = t1 + (dt * k) / 64
    const ex = rx * Math.cos(t), ey = ry * Math.sin(t)
    pts.push([cos * ex - sin * ey + cx, sin * ex + cos * ey + cy])
  }
  return pts
}

export function pathBounds(d) {
  const tokens = d.match(/[a-zA-Z]|-?\d*\.?\d+(?:e-?\d+)?/g) ?? []
  let cmd = null, x = 0, y = 0, sx = 0, sy = 0, i = 0
  const xs = [], ys = []
  const see = (px, py) => { xs.push(px); ys.push(py) }
  const num = () => parseFloat(tokens[i++])
  while (i < tokens.length) {
    if (/[a-zA-Z]/.test(tokens[i])) cmd = tokens[i++]
    const rel = cmd === cmd.toLowerCase(), C = cmd.toUpperCase()
    const pt = (px, py) => (rel ? [x + px, y + py] : [px, py])
    if (C === 'M' || C === 'L' || C === 'T') { const [nx, ny] = pt(num(), num()); x = nx; y = ny; see(x, y); if (C === 'M') { sx = x; sy = y; cmd = rel ? 'l' : 'L' } }
    else if (C === 'H') { x = rel ? x + num() : num(); see(x, y) }
    else if (C === 'V') { y = rel ? y + num() : num(); see(x, y) }
    else if (C === 'C') { for (let k = 0; k < 2; k++) { const [cx, cy] = pt(num(), num()); see(cx, cy) } const [nx, ny] = pt(num(), num()); x = nx; y = ny; see(x, y) }
    else if (C === 'S' || C === 'Q') { const [cx, cy] = pt(num(), num()); see(cx, cy); const [nx, ny] = pt(num(), num()); x = nx; y = ny; see(x, y) }
    else if (C === 'A') {
      const rx0 = num(), ry0 = num(), phi = num(), fa = num(), fs = num()
      const [nx, ny] = pt(num(), num())
      for (const [ax, ay] of arcExtremes(x, y, rx0, ry0, phi, fa, fs, nx, ny)) see(ax, ay)
      x = nx; y = ny; see(x, y)
    }
    else if (C === 'Z') { x = sx; y = sy }
    else throw new Error(`unknown path command ${cmd}`)
  }
  return { minX: Math.min(...xs), maxX: Math.max(...xs), minY: Math.min(...ys), maxY: Math.max(...ys) }
}
function bounds(el) {
  const a = el.attrs, f = (k) => parseFloat(a[k] ?? '0')
  switch (el.tag) {
    case 'path': return pathBounds(a.d ?? '')
    case 'circle': return { minX: f('cx') - f('r'), maxX: f('cx') + f('r'), minY: f('cy') - f('r'), maxY: f('cy') + f('r') }
    case 'ellipse': return { minX: f('cx') - f('rx'), maxX: f('cx') + f('rx'), minY: f('cy') - f('ry'), maxY: f('cy') + f('ry') }
    case 'rect': return { minX: f('x'), maxX: f('x') + f('width'), minY: f('y'), maxY: f('y') + f('height') }
    case 'line': return { minX: Math.min(f('x1'), f('x2')), maxX: Math.max(f('x1'), f('x2')), minY: Math.min(f('y1'), f('y2')), maxY: Math.max(f('y1'), f('y2')) }
    default: {
      const p = (a.points ?? '').match(/-?\d*\.?\d+/g)?.map(Number) ?? []
      const xs = p.filter((_, i) => i % 2 === 0), ys = p.filter((_, i) => i % 2 === 1)
      return { minX: Math.min(...xs), maxX: Math.max(...xs), minY: Math.min(...ys), maxY: Math.max(...ys) }
    }
  }
}

export const svgFiles = () => (existsSync(SVG_DIR) ? readdirSync(SVG_DIR).filter((f) => f.endsWith('.svg')).sort() : [])
export const chartIds = () => readdirSync(TYPES).filter((d) => !d.startsWith('_') && statSync(join(TYPES, d)).isDirectory() && existsSync(join(TYPES, d, 'index.ts'))).sort()
const loadMeta = () => (existsSync(META) ? JSON.parse(readFileSync(META, 'utf8')) : { version: 1, icons: [] })

export function sprite() {
  const symbols = svgFiles().map((f) => {
    const { body } = parseSvg(readFileSync(join(SVG_DIR, f), 'utf8'))
    const a = Object.entries(ROOT_ATTRS).filter(([k]) => k !== 'viewBox').map(([k, v]) => `${k}="${v}"`).join(' ')
    return `<symbol id="${basename(f, '.svg')}" viewBox="0 0 24 24" ${a}>${body.trim()}</symbol>`
  })
  return `<svg xmlns="http://www.w3.org/2000/svg" style="display:none">\n${symbols.join('\n')}\n</svg>\n`
}

/** Metadata for chart ids from their index.ts (no build needed). */
function chartMeta(id) {
  const src = readFileSync(join(TYPES, id, 'index.ts'), 'utf8')
  const str = (k) => src.match(new RegExp(`\\b${k}:\\s*'([^']*)'`))?.[1]
  const intents = src.match(/\bintents:\s*\[([^\]]*)\]/)?.[1]?.match(/'([^']+)'/g)?.map((s) => s.slice(1, -1)) ?? []
  return { name: str('name') ?? id, category: str('category') ?? 'comparison', intents }
}

export function lint({ write = false } = {}) {
  const findings = []
  const add = (icon, rule, message) => findings.push({ icon, rule, message })
  const meta = loadMeta()
  if (write) {
    const have = new Set(meta.icons.map((i) => i.name))
    for (const f of svgFiles()) {
      const name = basename(f, '.svg')
      if (have.has(name) || !existsSync(join(TYPES, name, 'index.ts'))) continue
      const m = chartMeta(name)
      meta.icons.push({ name, chart: name, category: m.category, tags: [...new Set([...name.split('-'), ...m.intents])], usage: `${m.name}: gallery, registry, chart pickers`, added: new Date().toISOString().slice(0, 10) })
    }
    meta.icons.sort((a, b) => a.name.localeCompare(b.name))
    writeFileSync(META, JSON.stringify(meta, null, 2) + '\n')
    writeFileSync(SPRITE, sprite())
  }
  const byName = new Map(meta.icons.map((i) => [i.name, i]))
  const hashes = new Map()
  for (const f of svgFiles()) {
    const name = basename(f, '.svg')
    const text = readFileSync(join(SVG_DIR, f), 'utf8')
    if (!/^[a-z0-9]+(-[a-z0-9]+)*$/.test(name)) add(name, 'name', 'file name must be kebab-case')
    const m = byName.get(name)
    if (!m) add(name, 'metadata', 'no icons.json entry (run: node icons/lint.mjs --write)')
    else {
      for (const k of ['name', 'chart', 'category', 'tags', 'usage', 'added']) if (m[k] === undefined || m[k] === '') add(name, 'metadata', `icons.json entry is missing "${k}"`)
      if (!CATEGORIES.includes(m.category)) add(name, 'metadata', `category "${m.category}" is not a chart category`)
      if (m.added && !/^\d{4}-\d{2}-\d{2}$/.test(m.added)) add(name, 'metadata', '"added" must be YYYY-MM-DD')
    }
    let parsed
    try { parsed = parseSvg(text) } catch (e) { add(name, 'structure', e.message); continue }
    for (const [k, v] of Object.entries(ROOT_ATTRS)) if (parsed.attrs[k] !== v) add(name, 'root', `root ${k} is "${parsed.attrs[k]}", must be "${v}"`)
    const extra = Object.keys(parsed.attrs).filter((k) => !(k in ROOT_ATTRS) && k !== 'xmlns')
    if (extra.length) add(name, 'root', `root carries ${extra.join(', ')}; only the spec attributes are allowed`)
    if (/#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(|\b(?:red|blue|green|black|white|gray|grey)\b/.test(text)) add(name, 'colour', 'literal colour found; use currentColor')
    if (!parsed.elements.length) add(name, 'structure', 'no geometry')
    for (const el of parsed.elements) {
      if (!GEOMETRY[el.tag]) { add(name, 'structure', `<${el.tag}> is not allowed`); continue }
      for (const k of Object.keys(el.attrs)) if (!GEOMETRY[el.tag].includes(k)) add(name, 'child-attr', `<${el.tag} ${k}> — children carry geometry only`)
      let b
      try { b = bounds(el) } catch (e) { add(name, 'structure', e.message); continue }
      if (![b.minX, b.maxX, b.minY, b.maxY].every(Number.isFinite)) add(name, 'structure', `<${el.tag}> has unreadable geometry`)
      else if (b.minX < PAD - 1e-6 || b.minY < PAD - 1e-6 || b.maxX > BOX - PAD + 1e-6 || b.maxY > BOX - PAD + 1e-6)
        add(name, 'padding', `<${el.tag}> reaches x ${b.minX.toFixed(2)}…${b.maxX.toFixed(2)} y ${b.minY.toFixed(2)}…${b.maxY.toFixed(2)}; geometry must stay inside ${PAD}…${BOX - PAD}`)
    }
    const hash = createHash('sha1').update(JSON.stringify(parsed.elements.map((e) => [e.tag, Object.entries(e.attrs).sort()]))).digest('hex')
    if (hashes.has(hash)) add(name, 'duplicate', `identical geometry to ${hashes.get(hash)}`)
    else hashes.set(hash, name)
  }
  const files = new Set(svgFiles().map((f) => basename(f, '.svg')))
  for (const i of meta.icons) if (!files.has(i.name)) add(i.name, 'metadata', 'icons.json entry has no svg file')
  for (const id of chartIds()) if (!files.has(id)) add(id, 'coverage', `chart type '${id}' has no icon (icons/svg/${id}.svg)`)
  for (const n of files) if (!existsSync(join(TYPES, n, 'index.ts'))) add(n, 'coverage', `icon '${n}' matches no chart type`)
  if (!existsSync(SPRITE) || readFileSync(SPRITE, 'utf8') !== sprite()) add('*', 'stale', 'icons.svg is stale — run: node icons/lint.mjs --write')
  return findings
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const findings = lint({ write: process.argv.includes('--write') })
  const json = process.argv.includes('--json')
  if (json) process.stdout.write(JSON.stringify(findings, null, 2) + '\n')
  else {
    for (const f of findings) console.log(`FAIL ${f.icon}: ${f.rule} — ${f.message}`)
    console.log(findings.length ? `\n${findings.length} finding(s)` : `icons: ${svgFiles().length} icons pass the spec, one per chart type`)
  }
  process.exitCode = findings.length ? 1 : 0
}
