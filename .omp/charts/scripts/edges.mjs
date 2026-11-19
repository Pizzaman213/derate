#!/usr/bin/env node
// Derive the standard edge-case samples for a rows-shaped chart from its basic.json.
// Never overwrites: premade inputs are only ever added to (RULES.md #3). Hand-written
// edge files win -- write them first and this script leaves them alone.
//
//   node scripts/edges.mjs <chart-id> [--only empty,nulls] [--skip mixed-tz] [--n 5200] [--dry]
//
// Field kinds are inferred from basic.json: numbers, dates (ISO-like strings), labels
// (other strings). Object-shaped inputs (trees, graphs, KPI objects) get no derived
// edges: write those by hand.

import { readFileSync, writeFileSync, existsSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const args = process.argv.slice(2)
const id = args.find((a) => !a.startsWith('--'))
const flag = (name) => { const i = args.indexOf(`--${name}`); return i >= 0 ? args[i + 1] : undefined }
const only = flag('only')?.split(','), skip = flag('skip')?.split(',') ?? []
const N = Number(flag('n') ?? 5200)
const dry = args.includes('--dry')
if (!id) { console.error('usage: node scripts/edges.mjs <chart-id> [--only a,b] [--skip a,b] [--n 5200] [--dry]'); process.exit(2) }

const dir = join(here, '..', 'types', id, 'samples')
const basicPath = join(dir, 'basic.json')
if (!existsSync(basicPath)) { console.error(`${basicPath} not found`); process.exit(1) }
const basic = JSON.parse(readFileSync(basicPath, 'utf8'))
if (!Array.isArray(basic.data) || !basic.data.every((r) => r && typeof r === 'object' && !Array.isArray(r))) {
  console.error(`${id}: basic.json data is not rows; write edge samples by hand`)
  process.exit(1)
}
const rows = basic.data
const ISO = /^\d{4}-\d{2}(-\d{2})?([T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?)?$/
const keys = [...new Set(rows.flatMap((r) => Object.keys(r)))]
const kind = Object.fromEntries(keys.map((k) => {
  const vals = rows.map((r) => r[k]).filter((v) => v !== null && v !== undefined)
  if (vals.length && vals.every((v) => typeof v === 'number')) return [k, 'number']
  if (vals.length && vals.every((v) => typeof v === 'string' && ISO.test(v))) return [k, 'date']
  if (vals.length && vals.every((v) => typeof v === 'string')) return [k, 'label']
  return [k, 'other']
}))
const nums = keys.filter((k) => kind[k] === 'number')
const dates = keys.filter((k) => kind[k] === 'date')
const labels = keys.filter((k) => kind[k] === 'label')
// the first label column is the category; the rest (series, group) keep their values
const primary = labels[0]

let seed = 7
const rnd = () => { seed = (seed * 1664525 + 1013904223) % 4294967296; return seed / 4294967296 }
const clone = (v) => JSON.parse(JSON.stringify(v))
const spacing = (k) => {
  const ts = rows.map((r) => (r[k] === null || r[k] === undefined ? NaN : Date.parse(r[k]))).filter(Number.isFinite).sort((a, b) => a - b)
  const d = ts.slice(1).map((t, i) => t - ts[i]).filter((x) => x > 0).sort((a, b) => a - b)
  return d[Math.floor(d.length / 2)] ?? 864e5
}
const iso = (t, like) => (like.length <= 10 ? new Date(t).toISOString().slice(0, 10) : new Date(t).toISOString().replace('.000', ''))

const edges = {
  empty: { description: 'No rows: the empty state, never a blank box.', data: [] },
  single: { description: 'One row.', data: [clone(rows[0])] },
  zeros: { description: 'Every number is zero.', data: rows.map((r) => ({ ...r, ...Object.fromEntries(nums.map((k) => [k, 0])) })) },
  negatives: { description: 'Negative values (sign flipped on alternating rows).', data: rows.map((r, i) => ({ ...r, ...Object.fromEntries(nums.map((k) => [k, i % 2 ? -Math.abs(r[k] ?? 0) : r[k]])) })) },
  nulls: { description: 'Missing values: null, empty string and an absent key.', data: rows.map((r, i) => { const o = { ...r }; if (nums[0] && i % 3 === 1) o[nums[0]] = null; if (nums[0] && i % 5 === 2) o[nums[0]] = ''; if (nums[0] && i === rows.length - 1 && rows.length > 2) delete o[nums[0]]; return o }) },
  duplicates: { description: 'Every row appears twice.', data: rows.flatMap((r) => [clone(r), clone(r)]) },
  large: { description: `${N} rows: exercises the canvas path above 5k marks.`, data: Array.from({ length: N }, (_, i) => {
    const r = clone(rows[i % rows.length])
    const cycle = Math.floor(i / rows.length)
    if (primary && cycle > 0 && !dates.length) r[primary] = `${r[primary]} ${cycle}`
    for (const k of dates) {
      // optional date fields (an "end" only some rows have): keep the row's own null
      const first = rows.find((x) => x[k] !== null && x[k] !== undefined)?.[k]
      if (first === undefined || r[k] === null || r[k] === undefined) continue
      r[k] = iso(Date.parse(r[k]) - Date.parse(rows[i % rows.length][k]) + Date.parse(first) + Math.floor(i / rows.length) * rows.length * spacing(k), String(first))
    }
    for (const k of nums) r[k] = Math.round(((rows[i % rows.length][k] ?? 0) * (0.6 + rnd() * 0.8)) * 100) / 100
    return r
  }) },
  'long-labels': { description: 'Very long category labels: they must truncate or wrap, with the full text in the tooltip.', data: rows.map((r, i) => (primary ? { ...r, [primary]: `${r[primary]} — ${['an exceptionally long category name that will not fit', 'Department of Redundancy Department, Northern Region', 'Supercalifragilisticexpialidocious Holdings International'][i % 3]}` } : r)) },
  'unicode-rtl': { description: 'Arabic, Hebrew, CJK, emoji and combining marks in labels.', data: rows.map((r, i) => (primary ? { ...r, [primary]: ['مبيعات الربع', 'הכנסות שנתיות', '売上高 東京', '📈 Growth', 'Zoë Ångström', '수익 서울', 'Ελλάδα', 'नई दिल्ली'][i % 8] + (i >= 8 ? ` ${i}` : '') } : r)) },
  outliers: { description: 'One value a thousand times the rest.', data: rows.map((r, i) => ({ ...r, ...Object.fromEntries(nums.map((k) => [k, i === Math.floor(rows.length / 2) ? (Math.abs(r[k] ?? 1) || 1) * 1000 : r[k]])) })) },
}
if (dates.length) {
  edges['unsorted-dates'] = { description: 'Rows in shuffled date order: the chart must sort.', data: [...rows].map((r, i) => [((i * 7919) % 97) + rnd(), r]).sort((a, b) => a[0] - b[0]).map((x) => x[1]) }
  edges['mixed-tz'] = { description: 'The same instants written with different UTC offsets and as epoch ms.', data: rows.map((r, i) => {
    const o = { ...r }
    for (const k of dates) {
      if (typeof r[k] !== 'string') continue
      const t = Date.parse(r[k].length <= 10 ? `${r[k]}T00:00:00Z` : r[k])
      if (i % 3 === 0) o[k] = new Date(t).toISOString()
      else if (i % 3 === 1) { const d = new Date(t + 5.5 * 3600e3); o[k] = d.toISOString().replace('Z', '+05:30').replace('.000', '') }
      else o[k] = t
    }
    return o
  }) }
}

let wrote = 0
for (const [name, body] of Object.entries(edges)) {
  if (only && !only.includes(name)) continue
  if (skip.includes(name)) continue
  const p = join(dir, `edge-${name}.json`)
  if (existsSync(p)) continue
  // large samples are stored as column arrays: half the bytes, and it exercises that input form
  const data = name === 'large' && body.data.length ? Object.fromEntries(keys.map((k) => [k, body.data.map((r) => r[k] ?? null)])) : body.data
  const out = { ...body, data, ...(basic.options ? { options: basic.options } : {}) }
  if (!dry) writeFileSync(p, JSON.stringify(out, null, name === 'large' ? 0 : 2) + '\n')
  wrote++
  console.log(`${dry ? 'would write' : 'wrote'} ${p}`)
}
console.log(`${id}: ${wrote} edge sample(s) ${dry ? 'pending' : 'written'}; fields: ${keys.map((k) => `${k}:${kind[k]}`).join(', ')}`)
