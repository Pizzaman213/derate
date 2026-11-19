#!/usr/bin/env node
// The icon spec, enforced. Exit 1 on any finding; `--json` for machines.
//
//   cd .omp && npm run icons:lint
//
// Rules (see README.md): viewBox 0 0 24 24; root fill="none" and
// stroke="currentColor"; stroke-width 1.5, round caps and joins (brand may set
// its own width and caps); children carry geometry only (brand children may
// carry `opacity`); no literal colour anywhere; every coordinate inside the
// 2…22 box; kebab-case name; icons.json entry with name, category, tags,
// usage, added; no two icons with identical geometry; generated outputs fresh.

import { readFileSync, existsSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { dirname, join, basename } from 'node:path'
import { fileURLToPath } from 'node:url'
import { build, loadMeta, svgFiles, parseSvg, SVG_DIR, OUTPUTS } from './build.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const wantJson = process.argv.includes('--json')
const CATEGORIES = ['action', 'status', 'arrow', 'view', 'file', 'nav', 'brand']
const GEOMETRY_ATTRS = { path: ['d'], circle: ['cx', 'cy', 'r'], rect: ['x', 'y', 'width', 'height', 'rx', 'ry'], line: ['x1', 'y1', 'x2', 'y2'], polyline: ['points'], polygon: ['points'] }
const PAD = 2, BOX = 24

// Bounding box of a path's coordinates (control points included: an over-estimate
// that only ever makes the lint stricter).
export function pathBounds(d) {
  const tokens = d.match(/[a-zA-Z]|-?\d*\.?\d+(?:e-?\d+)?/g) ?? []
  let cmd = null, x = 0, y = 0, sx = 0, sy = 0
  const xs = [], ys = []
  const see = (px, py) => { xs.push(px); ys.push(py) }
  let i = 0
  const num = () => parseFloat(tokens[i++])
  while (i < tokens.length) {
    if (/[a-zA-Z]/.test(tokens[i])) cmd = tokens[i++]
    const rel = cmd === cmd.toLowerCase()
    const C = cmd.toUpperCase()
    const pt = (px, py) => (rel ? [x + px, y + py] : [px, py])
    if (C === 'M' || C === 'L' || C === 'T') { const [nx, ny] = pt(num(), num()); x = nx; y = ny; see(x, y); if (C === 'M') { sx = x; sy = y; cmd = rel ? 'l' : 'L' } }
    else if (C === 'H') { x = rel ? x + num() : num(); see(x, y) }
    else if (C === 'V') { y = rel ? y + num() : num(); see(x, y) }
    else if (C === 'C') { for (let k = 0; k < 2; k++) { const [cx, cy] = pt(num(), num()); see(cx, cy) } const [nx, ny] = pt(num(), num()); x = nx; y = ny; see(x, y) }
    else if (C === 'S' || C === 'Q') { const [cx, cy] = pt(num(), num()); see(cx, cy); const [nx, ny] = pt(num(), num()); x = nx; y = ny; see(x, y) }
    else if (C === 'A') { num(); num(); num(); num(); num(); const [nx, ny] = pt(num(), num()); x = nx; y = ny; see(x, y) }
    else if (C === 'Z') { x = sx; y = sy; i += 0 }
    else throw new Error(`unknown path command ${cmd}`)
  }
  return { minX: Math.min(...xs), maxX: Math.max(...xs), minY: Math.min(...ys), maxY: Math.max(...ys) }
}
function elementBounds(el) {
  const a = el.attrs, f = (k) => parseFloat(a[k] ?? '0')
  switch (el.tag) {
    case 'path': return pathBounds(a.d ?? '')
    case 'circle': return { minX: f('cx') - f('r'), maxX: f('cx') + f('r'), minY: f('cy') - f('r'), maxY: f('cy') + f('r') }
    case 'rect': return { minX: f('x'), maxX: f('x') + f('width'), minY: f('y'), maxY: f('y') + f('height') }
    case 'line': return { minX: Math.min(f('x1'), f('x2')), maxX: Math.max(f('x1'), f('x2')), minY: Math.min(f('y1'), f('y2')), maxY: Math.max(f('y1'), f('y2')) }
    default: {
      const pts = (a.points ?? '').match(/-?\d*\.?\d+/g)?.map(Number) ?? []
      const xs = pts.filter((_, i) => i % 2 === 0), ys = pts.filter((_, i) => i % 2 === 1)
      return { minX: Math.min(...xs), maxX: Math.max(...xs), minY: Math.min(...ys), maxY: Math.max(...ys) }
    }
  }
}

export function lint() {
  const findings = []
  const add = (icon, rule, message) => findings.push({ icon, rule, message })
  const meta = loadMeta()
  const byName = new Map(meta.icons.map((i) => [i.name, i]))
  const hashes = new Map()

  for (const f of svgFiles()) {
    const name = basename(f, '.svg')
    const text = readFileSync(join(SVG_DIR, f), 'utf8')
    if (!/^[a-z0-9]+(-[a-z0-9]+)*$/.test(name)) add(name, 'name', 'file name must be kebab-case')
    const m = byName.get(name)
    if (!m) add(name, 'metadata', 'no icons.json entry')
    else {
      for (const field of ['name', 'category', 'tags', 'usage', 'added']) if (m[field] === undefined || m[field] === '') add(name, 'metadata', `icons.json entry is missing "${field}"`)
      if (!CATEGORIES.includes(m.category)) add(name, 'metadata', `category "${m.category}" is not one of ${CATEGORIES.join('|')}`)
      if (m.added && !/^\d{4}-\d{2}-\d{2}$/.test(m.added)) add(name, 'metadata', `"added" must be YYYY-MM-DD`)
    }
    const brand = m?.category === 'brand'

    let parsed
    try { parsed = parseSvg(text, name) } catch (e) { add(name, 'structure', e.message); continue }
    const { attrs, elements } = parsed
    if (attrs.viewBox !== '0 0 24 24') add(name, 'viewbox', `viewBox is "${attrs.viewBox}", must be "0 0 24 24"`)
    if (attrs.fill !== 'none') add(name, 'fill', `root fill is "${attrs.fill}", must be "none"`)
    if (attrs.stroke !== 'currentColor') add(name, 'stroke', `root stroke is "${attrs.stroke}", must be "currentColor"`)
    if (!brand && attrs['stroke-width'] !== '1.5') add(name, 'stroke-width', `stroke-width is "${attrs['stroke-width']}", must be 1.5 outside brand`)
    if (!brand && attrs['stroke-linecap'] !== 'round') add(name, 'linecap', `stroke-linecap must be round outside brand`)
    if (attrs['stroke-linejoin'] !== 'round') add(name, 'linejoin', `stroke-linejoin must be round`)
    if (/#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(|\b(?:red|blue|green|black|white|gray|grey)\b/.test(text)) add(name, 'colour', 'literal colour found; use currentColor')
    if (!elements.length) add(name, 'structure', 'no geometry')

    for (const el of elements) {
      if (!GEOMETRY_ATTRS[el.tag]) { add(name, 'structure', `<${el.tag}> is not allowed`); continue }
      const allowed = new Set([...GEOMETRY_ATTRS[el.tag], ...(brand ? ['opacity'] : [])])
      for (const k of Object.keys(el.attrs)) if (!allowed.has(k)) add(name, 'child-attr', `<${el.tag} ${k}> — children carry geometry only`)
      const b = elementBounds(el)
      if (b.minX < PAD - 1e-6 || b.minY < PAD - 1e-6 || b.maxX > BOX - PAD + 1e-6 || b.maxY > BOX - PAD + 1e-6)
        add(name, 'padding', `<${el.tag}> reaches x ${b.minX.toFixed(2)}…${b.maxX.toFixed(2)} y ${b.minY.toFixed(2)}…${b.maxY.toFixed(2)}; geometry must stay inside ${PAD}…${BOX - PAD}`)
    }
    const hash = createHash('sha1').update(JSON.stringify(elements.map((e) => [e.tag, Object.entries(e.attrs).sort()]))).digest('hex')
    if (hashes.has(hash)) add(name, 'duplicate', `identical geometry to ${hashes.get(hash)}`)
    else hashes.set(hash, name)
  }
  for (const i of meta.icons) if (!existsSync(join(SVG_DIR, `${i.name}.svg`))) add(i.name, 'metadata', 'icons.json entry has no svg file')

  // staleness
  try {
    const out = build()
    for (const [k, file] of Object.entries(OUTPUTS)) {
      const p = join(here, file)
      if (!existsSync(p) || readFileSync(p, 'utf8') !== out[k]) add('*', 'stale', `${file} is stale — run: cd .omp && npm run icons:build`)
    }
  } catch (e) {
    add('*', 'build', e.message)
  }
  return findings
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const findings = lint()
  if (wantJson) process.stdout.write(JSON.stringify(findings, null, 2) + '\n')
  else {
    for (const f of findings) console.log(`FAIL ${f.icon}: ${f.rule} — ${f.message}`)
    console.log(findings.length ? `\n${findings.length} finding(s)` : `icons: ${svgFiles().length} icons pass the spec`)
  }
  process.exitCode = findings.length ? 1 : 0
}
