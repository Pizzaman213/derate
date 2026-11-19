import { describe, expect, it } from 'vitest'
import { spawnSync } from 'node:child_process'
import { cpSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { parseDate } from '../time/parse'
import { DATASET_NAMES, DATASET_REGISTRY, datasetEntry, datasetsFor, type FieldSpec } from './index'

// jsdom rewrites import.meta.url to an http URL; the module's own directory is still on disk.
const DIR = import.meta.dirname
const VALIDATE = join(DIR, 'validate.mjs')

type Row = Record<string, unknown>
interface TreeNode { name: string; value?: number; children?: TreeNode[] }
interface DatasetFile {
  name: string
  description: string
  source: string
  data: unknown
  fields: Record<string, FieldSpec>
  linkFields?: Record<string, FieldSpec>
}

const load = (file: string): DatasetFile => JSON.parse(readFileSync(join(DIR, file), 'utf8')) as DatasetFile
const files = new Map<string, DatasetFile>(DATASET_REGISTRY.datasets.map((e) => [e.name, load(e.file)]))
const file = (name: string): DatasetFile => {
  const f = files.get(name)
  if (!f) throw new Error(`no file for ${name}`)
  return f
}
const rowsOf = (name: string): Row[] => file(name).data as Row[]

function treeNodes(root: TreeNode): TreeNode[] {
  const out: TreeNode[] = []
  const seen = new Set<TreeNode>()
  const walk = (n: TreeNode) => {
    if (seen.has(n)) throw new Error(`cycle at ${n.name}`)
    seen.add(n)
    out.push(n)
    n.children?.forEach(walk)
  }
  walk(root)
  return out
}

/** The records each dataset's `fields` describe: rows, tree nodes, or graph nodes. */
function records(name: string): Row[] {
  const { kind } = datasetEntry(name)
  const { data } = file(name)
  if (kind === 'tree') return treeNodes(data as TreeNode) as unknown as Row[]
  if (kind === 'graph') return (data as { nodes: Row[] }).nodes
  return data as Row[]
}

function checkTyped(recs: Row[], fields: Record<string, FieldSpec>, isLeaf: (r: Row) => boolean = () => true) {
  for (const r of recs) {
    for (const [f, spec] of Object.entries(fields)) {
      if (spec.on === 'leaf' && !isLeaf(r)) continue
      expect(r, `field ${f}`).toHaveProperty(f)
      const v = r[f]
      if (v === null) {
        expect(spec.nullable === true || spec.nulls !== undefined, `null in ${f}`).toBe(true)
        continue
      }
      if (spec.type === 'number') expect(Number.isFinite(v), `${f}=${String(v)}`).toBe(true)
      if (spec.type === 'date') expect(parseDate(v), `${f}=${String(v)}`).toBeInstanceOf(Date)
      if (spec.type === 'boolean') expect(typeof v).toBe('boolean')
      if (['category', 'text', 'id'].includes(spec.type)) expect(typeof v).toBe('string')
    }
  }
}

describe('registry <-> files', () => {
  it('DATASET_NAMES is the registry, in order', () => {
    expect(DATASET_REGISTRY.version).toBe(1)
    expect(DATASET_REGISTRY.datasets.map((d) => d.name)).toEqual([...DATASET_NAMES])
  })

  it('every entry has a file and every file has an entry', () => {
    const onDisk = readdirSync(DIR).filter((f) => f.endsWith('.json') && f !== 'datasets.json').sort()
    expect(DATASET_REGISTRY.datasets.map((d) => d.file).sort()).toEqual(onDisk)
    for (const e of DATASET_REGISTRY.datasets) {
      expect(e.file).toBe(`${e.name}.json`)
      expect(file(e.name).name).toBe(e.name)
      // appended datasets carry their own provenance and date (RULES.md #3: append-only)
      expect(typeof file(e.name).source === 'string' && file(e.name).source.length > 0, `${e.name}: source`).toBe(true)
      expect(e.added).toMatch(/^\d{4}-\d{2}-\d{2}$/)
    }
  })

  it('rows count and field list match each file', () => {
    for (const e of DATASET_REGISTRY.datasets) {
      expect(records(e.name).length, e.name).toBe(e.rows)
      expect(Object.keys(file(e.name).fields), e.name).toEqual(e.fields)
    }
  })

  it('datasetEntry and datasetsFor', () => {
    expect(datasetEntry('ohlc-prices').kind).toBe('rows')
    expect(() => datasetEntry('nope')).toThrow(/unknown dataset "nope"/)
    expect(datasetsFor('network').map((d) => d.name)).toEqual(['network-graph'])
  })
})

describe('fields', () => {
  it.each([...DATASET_NAMES])('%s: every declared field exists and is typed', (name) => {
    const f = file(name)
    checkTyped(records(name), f.fields, (r) => !('children' in r))
    if (f.linkFields) checkTyped((f.data as { links: Row[] }).links, f.linkFields)
  })

  it('declared null counts are exact', () => {
    for (const name of DATASET_NAMES) {
      for (const [f, spec] of Object.entries(file(name).fields)) {
        if (spec.nulls === undefined) continue
        expect(records(name).filter((r) => r[f] === null).length, `${name}.${f}`).toBe(spec.nulls)
      }
    }
  })
})

describe('shapes', () => {
  it('sales-by-month has 24 months x 4 regions', () => {
    const rows = rowsOf('sales-by-month')
    expect(rows).toHaveLength(96)
    expect(new Set(rows.map((r) => r.month)).size).toBe(24)
    expect(new Set(rows.map((r) => r.region))).toEqual(new Set(['North America', 'Europe', 'Asia Pacific', 'Latin America']))
    expect(rows[0]!.month).toBe('2025-01-01')
    expect(rows.at(-1)!.month).toBe('2026-12-01')
  })

  it('ohlc-prices: 120 weekdays and low <= open,close <= high on every row', () => {
    const rows = rowsOf('ohlc-prices') as { date: string; open: number; high: number; low: number; close: number }[]
    expect(rows).toHaveLength(120)
    expect(rows[0]!.date).toBe('2026-06-01')
    for (const r of rows) {
      expect(parseDate(r.date)!.getUTCDay() % 6, r.date).not.toBe(0)
      expect(r.low).toBeLessThanOrEqual(Math.min(r.open, r.close))
      expect(Math.max(r.open, r.close)).toBeLessThanOrEqual(r.high)
    }
  })

  it('budget-breakdown: a 3-level tree with 30 leaves and no cycles, matching the flat rows', () => {
    const root = file('budget-breakdown').data as TreeNode
    const nodes = treeNodes(root)
    const leaves = nodes.filter((n) => !n.children)
    expect(leaves).toHaveLength(30)
    const depth = (n: TreeNode): number => (n.children ? 1 + Math.max(...n.children.map(depth)) : 0)
    expect(depth(root)).toBe(3)
    const flat = rowsOf('budget-breakdown-flat') as { id: string; parent: string | null; value: number | null }[]
    expect(flat).toHaveLength(nodes.length)
    const sum = (rows: { value?: number | null }[]) => rows.reduce((s, r) => s + (r.value ?? 0), 0)
    expect(sum(flat)).toBe(sum(leaves))
  })

  it.each(['budget-breakdown-flat', 'org-tree'])('%s: parents exist, one root, no cycles', (name) => {
    const rows = rowsOf(name) as { id: string; parent: string | null }[]
    const byId = new Map(rows.map((r) => [r.id, r]))
    expect(byId.size).toBe(rows.length)
    expect(rows.filter((r) => r.parent === null)).toHaveLength(1)
    for (const r of rows) {
      const seen = new Set<string>()
      let cur: { id: string; parent: string | null } | undefined = r
      while (cur && cur.parent !== null) {
        expect(byId.has(cur.parent), `${cur.id} -> ${cur.parent}`).toBe(true)
        expect(seen.has(cur.id), `cycle at ${cur.id}`).toBe(false)
        seen.add(cur.id)
        cur = byId.get(cur.parent)
      }
    }
  })

  it('org-tree is four levels under one CEO', () => {
    const rows = rowsOf('org-tree') as { id: string; parent: string | null; title: string }[]
    const byId = new Map(rows.map((r) => [r.id, r]))
    const level = (r: { parent: string | null }): number => (r.parent === null ? 1 : 1 + level(byId.get(r.parent)!))
    expect(Math.max(...rows.map(level))).toBe(4)
    expect(rows.find((r) => r.parent === null)!.title).toBe('Chief Executive Officer')
  })

  it('network-graph: links reference existing node ids, 4 groups', () => {
    const { nodes, links } = file('network-graph').data as { nodes: { id: string; group: string }[]; links: { source: string; target: string }[] }
    const ids = new Set(nodes.map((n) => n.id))
    expect(ids.size).toBe(nodes.length)
    expect(new Set(nodes.map((n) => n.group)).size).toBe(4)
    expect(links.length).toBeGreaterThanOrEqual(55)
    for (const l of links) {
      expect(ids.has(l.source), l.source).toBe(true)
      expect(ids.has(l.target), l.target).toBe(true)
    }
  })

  it('server-metrics: timestamps strictly increase per host and the declared gaps are there', () => {
    const rows = rowsOf('server-metrics') as { timestamp: string; host: string; cpu: number | null }[]
    const last = new Map<string, number>()
    const perHost = new Map<string, number>()
    for (const r of rows) {
      const t = parseDate(r.timestamp)!.getTime()
      expect(r.timestamp.endsWith('Z')).toBe(true)
      expect(t, `${r.host} ${r.timestamp}`).toBeGreaterThan(last.get(r.host) ?? -Infinity)
      last.set(r.host, t)
      perHost.set(r.host, (perHost.get(r.host) ?? 0) + 1)
    }
    expect([...perHost.values()]).toEqual([288, 288, 288])
    const declared = file('server-metrics').fields.cpu!.nulls!
    expect(declared).toBeGreaterThan(0)
    expect(rows.filter((r) => r.cpu === null)).toHaveLength(declared)
  })

  it('project-timeline: dependencies resolve and end >= start', () => {
    const rows = rowsOf('project-timeline') as { id: string; start: string; end: string; dependsOn: string; milestone: boolean }[]
    const ids = new Set(rows.map((r) => r.id))
    for (const r of rows) {
      expect(parseDate(r.end)!.getTime()).toBeGreaterThanOrEqual(parseDate(r.start)!.getTime())
      if (r.milestone) expect(r.end).toBe(r.start)
      for (const d of r.dependsOn ? r.dependsOn.split(',') : []) expect(ids.has(d), `${r.id} depends on ${d}`).toBe(true)
    }
  })
})

describe('validate.mjs', () => {
  const run = (dir?: string) => spawnSync(process.execPath, dir ? [VALIDATE, dir] : [VALIDATE], { encoding: 'utf8' })

  it('exits 0 on the real folder', () => {
    const res = run()
    expect(res.stderr).toBe('')
    expect(res.status).toBe(0)
  })

  it('exits 1 with a clear message on a malformed copy', () => {
    const tmp = mkdtempSync(join(tmpdir(), 'datasets-'))
    try {
      cpSync(DIR, tmp, { recursive: true, filter: (src) => src === DIR.replace(/\/$/, '') || src.endsWith('.json') })
      const ohlc = JSON.parse(readFileSync(join(tmp, 'ohlc-prices.json'), 'utf8'))
      ohlc.data[3].high = ohlc.data[3].low - 1
      delete ohlc.data[7].volume
      writeFileSync(join(tmp, 'ohlc-prices.json'), JSON.stringify(ohlc))
      const reg = JSON.parse(readFileSync(join(tmp, 'datasets.json'), 'utf8'))
      reg.datasets.find((d: { name: string }) => d.name === 'sales-by-month').rows = 95
      writeFileSync(join(tmp, 'datasets.json'), JSON.stringify(reg))
      writeFileSync(join(tmp, 'stray.json'), '{}')

      const res = run(tmp)
      expect(res.status).toBe(1)
      expect(res.stderr).toContain('ohlc-prices.json: row 3: OHLC out of order')
      expect(res.stderr).toContain('ohlc-prices.json: row 7: missing field "volume"')
      expect(res.stderr).toContain('sales-by-month.json: has 96 rows but the registry says 95')
      expect(res.stderr).toContain('stray.json is in the folder but has no registry entry')
    } finally {
      rmSync(tmp, { recursive: true, force: true })
    }
  })
})
