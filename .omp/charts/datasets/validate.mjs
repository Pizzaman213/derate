#!/usr/bin/env node
/**
 * Validates a datasets folder: the registry (datasets.json) against the files beside it,
 * every file's required keys, declared fields against every row / node / link, field types,
 * declared null counts, row counts, and the structural invariants of each kind
 * (tree: shape and leaf-only fields; graph: links resolve; id/parent rows: parents resolve,
 * one root, no cycles; open/high/low/close rows: low <= open,close <= high).
 *
 *   node datasets/validate.mjs [folder]     # default: the folder this script is in
 *
 * Exit 0 when clean; exit 1 with one human-readable line per problem.
 * Node built-ins only, so `/chart-data add` can run it before anything is installed.
 */
import { readFileSync, readdirSync, existsSync } from 'node:fs'
import { join, dirname, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

export const REGISTRY = 'datasets.json'
export const FIELD_TYPES = ['date', 'number', 'category', 'text', 'id', 'boolean']
export const KINDS = ['rows', 'tree', 'graph']
export const INTENTS = [
  'compare', 'rank', 'composition', 'trend', 'timeline', 'schedule', 'distribution', 'relationship',
  'correlation', 'flow', 'hierarchy', 'network', 'geo', 'kpi', 'change-over-time', 'deviation', 'sets',
]
const ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2})(?::(\d{2})(?:\.\d{1,9})?)?(Z|[+-]\d{2}:?\d{2})?)?$/
const DAY = /^\d{4}-\d{2}-\d{2}$/
const SLUG = /^[a-z0-9]+(?:-[a-z0-9]+)*$/
const MAX_PER_FILE = 25

const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v)
const show = (v) => (typeof v === 'string' ? JSON.stringify(v) : String(v))

/** True for an ISO 8601 date or date-time whose calendar fields exist. */
export function isIsoDate(s) {
  if (typeof s !== 'string') return false
  const m = ISO_DATE.exec(s)
  if (!m) return false
  const [y, mo, d] = [Number(m[1]), Number(m[2]), Number(m[3])]
  if (mo < 1 || mo > 12 || d < 1 || d > new Date(Date.UTC(y, mo, 0)).getUTCDate()) return false
  if (m[4] !== undefined && (Number(m[4]) > 23 || Number(m[5]) > 59 || Number(m[6] ?? 0) > 59)) return false
  return true
}

function checkValue(spec, value) {
  if (value === null) return spec.nullable || spec.nulls !== undefined ? null : 'is null but the field declares no nulls'
  switch (spec.type) {
    case 'number': return typeof value === 'number' && Number.isFinite(value) ? null : `${show(value)} is not a finite number`
    case 'date': return isIsoDate(value) ? null : `${show(value)} is not an ISO 8601 date`
    case 'boolean': return typeof value === 'boolean' ? null : `${show(value)} is not a boolean`
    default: return typeof value === 'string' ? null : `${show(value)} is not a string`
  }
}

function checkFieldSpecs(fields, label, err) {
  if (!isObj(fields) || Object.keys(fields).length === 0) return err(`"${label}" must be a non-empty object of field specs`)
  for (const [name, spec] of Object.entries(fields)) {
    if (!isObj(spec)) { err(`${label}.${name} must be an object`); continue }
    if (!FIELD_TYPES.includes(spec.type)) err(`${label}.${name}.type is ${show(spec.type)}; expected one of ${FIELD_TYPES.join(', ')}`)
    if (typeof spec.description !== 'string' || !spec.description.trim()) err(`${label}.${name}.description is missing`)
    if (spec.unit !== undefined && typeof spec.unit !== 'string') err(`${label}.${name}.unit must be a string`)
    if (spec.nulls !== undefined && !(Number.isInteger(spec.nulls) && spec.nulls >= 0)) err(`${label}.${name}.nulls must be a non-negative integer`)
    if (spec.nullable !== undefined && typeof spec.nullable !== 'boolean') err(`${label}.${name}.nullable must be a boolean`)
    if (spec.on !== undefined && spec.on !== 'leaf') err(`${label}.${name}.on must be "leaf" when present`)
  }
}

/** Every declared field present, typed, and (when declared) null exactly `nulls` times; no undeclared keys. */
function checkRecords(records, fields, what, err, isLeaf = () => true) {
  const nulls = {}
  records.forEach((rec, i) => {
    const at = `${what} ${i}`
    if (!isObj(rec)) return err(`${at} is not an object`)
    for (const [name, spec] of Object.entries(fields)) {
      const required = spec.on !== 'leaf' || isLeaf(rec)
      if (!(name in rec)) { if (required) err(`${at}: missing field "${name}"`); continue }
      if (!required) { err(`${at}: field "${name}" is declared leaf-only but is set on a non-leaf`); continue }
      const problem = checkValue(spec, rec[name])
      if (problem) err(`${at}: field "${name}" ${problem}`)
      if (rec[name] === null) nulls[name] = (nulls[name] ?? 0) + 1
    }
    for (const key of Object.keys(rec)) {
      if (key === 'children') continue
      if (!(key in fields)) err(`${at}: key "${key}" is not declared in fields`)
    }
  })
  for (const [name, spec] of Object.entries(fields)) {
    if (spec.nulls !== undefined && (nulls[name] ?? 0) !== spec.nulls) {
      err(`field "${name}" declares ${spec.nulls} nulls but ${what}s contain ${nulls[name] ?? 0}`)
    }
  }
}

function checkParents(rows, err) {
  const ids = new Map()
  rows.forEach((r, i) => {
    if (ids.has(r.id)) err(`row ${i}: duplicate id ${show(r.id)}`)
    ids.set(r.id, r)
  })
  const roots = rows.filter((r) => r.parent === null)
  if (roots.length !== 1) err(`expected exactly one root (parent null), found ${roots.length}`)
  rows.forEach((r, i) => {
    if (r.parent !== null && !ids.has(r.parent)) err(`row ${i}: parent ${show(r.parent)} is not an id in this dataset`)
  })
  for (const r of rows) {
    const seen = new Set([r.id])
    let cur = r
    while (cur && cur.parent !== null && ids.has(cur.parent)) {
      if (seen.has(cur.parent)) { err(`cycle through id ${show(r.id)}`); break }
      seen.add(cur.parent)
      cur = ids.get(cur.parent)
    }
  }
}

function checkOhlc(rows, err) {
  rows.forEach((r, i) => {
    const { open: o, high: h, low: l, close: c } = r
    if (![o, h, l, c].every((v) => typeof v === 'number')) return
    if (!(l <= Math.min(o, c) && Math.max(o, c) <= h)) err(`row ${i}: OHLC out of order (low ${l}, open ${o}, close ${c}, high ${h})`)
  })
}

function flattenTree(root, err) {
  const out = []
  const walk = (node, path) => {
    if (!isObj(node)) return err(`tree node at ${path} is not an object`)
    out.push(node)
    if (node.children === undefined) return
    if (!Array.isArray(node.children) || node.children.length === 0) return err(`tree node at ${path} has "children" that is not a non-empty array`)
    node.children.forEach((c, i) => walk(c, `${path}.children[${i}]`))
  }
  walk(root, 'data')
  return out
}

function checkFile(dir, entry, err) {
  const path = join(dir, entry.file)
  let doc
  try { doc = JSON.parse(readFileSync(path, 'utf8')) } catch (e) { return err(`cannot parse: ${e.message}`) }
  if (!isObj(doc)) return err('top level must be an object')
  if (doc.name !== entry.name) err(`"name" is ${show(doc.name)} but the registry says ${show(entry.name)}`)
  if (typeof doc.description !== 'string' || !doc.description.trim()) err('"description" is missing')
  if (typeof doc.source !== 'string' || !doc.source.trim()) err('"source" is missing')
  checkFieldSpecs(doc.fields, 'fields', err)
  if (!isObj(doc.fields)) return
  const declared = Object.keys(doc.fields)
  if (JSON.stringify(declared) !== JSON.stringify(entry.fields)) {
    err(`fields are [${declared.join(', ')}] but the registry lists [${(entry.fields ?? []).join(', ')}]`)
  }
  const { data } = doc
  if (data === undefined) return err('"data" is missing')

  if (entry.kind === 'rows') {
    if (!Array.isArray(data)) return err('kind "rows" needs "data" to be an array')
    if (data.length !== entry.rows) err(`has ${data.length} rows but the registry says ${entry.rows}`)
    checkRecords(data, doc.fields, 'row', err)
    if (doc.fields.id?.type === 'id' && doc.fields.parent?.type === 'id') checkParents(data, err)
    if (['open', 'high', 'low', 'close'].every((k) => doc.fields[k]?.type === 'number')) checkOhlc(data, err)
  } else if (entry.kind === 'tree') {
    if (!isObj(data)) return err('kind "tree" needs "data" to be a root node object')
    const nodes = flattenTree(data, err)
    if (nodes.length !== entry.rows) err(`tree has ${nodes.length} nodes but the registry says ${entry.rows}`)
    checkRecords(nodes, doc.fields, 'tree node', err, (n) => n.children === undefined)
  } else if (entry.kind === 'graph') {
    if (!isObj(data) || !Array.isArray(data.nodes) || !Array.isArray(data.links)) return err('kind "graph" needs "data" = { nodes: [], links: [] }')
    if (data.nodes.length !== entry.rows) err(`graph has ${data.nodes.length} nodes but the registry says ${entry.rows}`)
    checkRecords(data.nodes, doc.fields, 'node', err)
    if (doc.linkFields === undefined) err('kind "graph" needs "linkFields" describing each link')
    else { checkFieldSpecs(doc.linkFields, 'linkFields', err); if (isObj(doc.linkFields)) checkRecords(data.links, doc.linkFields, 'link', err) }
    const ids = new Set()
    data.nodes.forEach((n, i) => { if (ids.has(n?.id)) err(`node ${i}: duplicate id ${show(n?.id)}`); ids.add(n?.id) })
    data.links.forEach((l, i) => {
      for (const end of ['source', 'target']) if (!ids.has(l?.[end])) err(`link ${i}: ${end} ${show(l?.[end])} is not a node id`)
    })
  }
}

function checkEntry(entry, i, err) {
  if (!isObj(entry)) return err(`datasets[${i}] is not an object`)
  const at = `datasets[${i}]${typeof entry.name === 'string' ? ` (${entry.name})` : ''}`
  if (typeof entry.name !== 'string' || !SLUG.test(entry.name)) err(`${at}: "name" must be a kebab-case string`)
  if (entry.file !== `${entry.name}.json`) err(`${at}: "file" is ${show(entry.file)}; expected "${entry.name}.json"`)
  if (typeof entry.description !== 'string' || !entry.description.trim()) err(`${at}: "description" is missing`)
  if (!KINDS.includes(entry.kind)) err(`${at}: "kind" is ${show(entry.kind)}; expected one of ${KINDS.join(', ')}`)
  if (!Number.isInteger(entry.rows) || entry.rows < 1) err(`${at}: "rows" must be a positive integer`)
  if (!Array.isArray(entry.fields) || entry.fields.length === 0 || !entry.fields.every((f) => typeof f === 'string')) err(`${at}: "fields" must be a non-empty array of names`)
  if (!Array.isArray(entry.intents) || entry.intents.length === 0) err(`${at}: "intents" must be a non-empty array`)
  else for (const t of entry.intents) if (!INTENTS.includes(t)) err(`${at}: unknown intent ${show(t)}; expected one of ${INTENTS.join(', ')}`)
  if (!Array.isArray(entry.suggested) || entry.suggested.length === 0) err(`${at}: "suggested" must be a non-empty array`)
  else for (const s of entry.suggested) if (typeof s !== 'string' || !SLUG.test(s)) err(`${at}: suggested chart id ${show(s)} is not a kebab-case id`)
  if (typeof entry.added !== 'string' || !DAY.test(entry.added) || !isIsoDate(entry.added)) err(`${at}: "added" must be a YYYY-MM-DD date`)
}

/** All problems in `dir`, as `file: message` lines. Empty means valid. */
export function validateFolder(dir) {
  const problems = []
  const scoped = (file) => {
    let n = 0
    return (msg) => {
      n += 1
      if (n <= MAX_PER_FILE) problems.push(`${file}: ${msg}`)
      else if (n === MAX_PER_FILE + 1) problems.push(`${file}: ... further problems in this file suppressed`)
    }
  }
  const regErr = scoped(REGISTRY)
  const regPath = join(dir, REGISTRY)
  if (!existsSync(regPath)) return [`${REGISTRY}: not found in ${dir}`]
  let reg
  try { reg = JSON.parse(readFileSync(regPath, 'utf8')) } catch (e) { return [`${REGISTRY}: cannot parse: ${e.message}`] }
  if (!isObj(reg)) return [`${REGISTRY}: top level must be an object`]
  if (reg.version !== 1) regErr(`"version" is ${show(reg.version)}; expected 1`)
  if (!Array.isArray(reg.datasets)) { regErr('"datasets" must be an array'); return problems }

  const names = new Set()
  reg.datasets.forEach((entry, i) => {
    checkEntry(entry, i, regErr)
    if (isObj(entry) && names.has(entry.name)) regErr(`datasets[${i}]: duplicate name ${show(entry.name)}`)
    if (isObj(entry)) names.add(entry.name)
  })

  const onDisk = readdirSync(dir).filter((f) => f.endsWith('.json') && f !== REGISTRY)
  const registered = new Set(reg.datasets.filter(isObj).map((e) => e.file))
  for (const f of onDisk) if (!registered.has(f)) regErr(`${f} is in the folder but has no registry entry`)
  for (const entry of reg.datasets) {
    if (!isObj(entry) || typeof entry.file !== 'string') continue
    if (!existsSync(join(dir, entry.file))) { regErr(`entry ${show(entry.name)} names ${entry.file}, which does not exist`); continue }
    if (KINDS.includes(entry.kind)) checkFile(dir, entry, scoped(entry.file))
  }
  return problems
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  const dir = resolve(process.argv[2] ?? dirname(fileURLToPath(import.meta.url)))
  const problems = validateFolder(dir)
  if (problems.length) {
    console.error(`datasets: ${problems.length} problem${problems.length === 1 ? '' : 's'} in ${dir}`)
    for (const p of problems) console.error(`  ${p}`)
    process.exit(1)
  }
  const reg = JSON.parse(readFileSync(join(dir, REGISTRY), 'utf8'))
  console.log(`datasets: ${reg.datasets.length} datasets valid in ${dir}`)
}
