// scripts/cli.mjs: its parsers and search as functions, and its commands as the
// processes the extension runs. Writing commands run against a temporary datasets
// folder (CHART_DATASETS_DIR) or a chart that refuses the input, so nothing in the pack
// is changed. `new` with a fresh id (scaffold + build + browser check) is exercised by
// hand, not here: it writes into types/ and runs charts:check.
import { spawnSync } from 'node:child_process'
import { cpSync, existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'

const CHARTS = join(import.meta.dirname, '..', '..')
const CLI_PATH = join(CHARTS, 'scripts', 'cli.mjs')

interface Cli {
  parseCsv(text: string, label?: string): Array<Record<string, unknown>>
  parseJson(text: string, label: string): unknown
  tokens(s: string): string[]
  matchScore(query: string, chart: { id: string; name: string; category: string; intents: string[]; summary: string; aliases: string[] }): [number, string]
  reuseOf(id: string, charts: Array<{ id: string; name: string; category: string; intents: string[]; summary: string; aliases: string[] }>, aliases: Record<string, string>, force?: boolean): { id: string; why: string } | null
  summarizeCheck(out: string, ids?: string[]): { steps: Array<{ name: string; status: string }>; failures: string[]; mine: string[]; others: string[] }
  shapeDataset(name: string, input: { data: unknown; label: string; fields?: unknown }): { kind: string; rows: number; fields: Record<string, { type: string; nulls?: number; on?: string }> }
}
let cli: Cli
const tmp = mkdtempSync(join(tmpdir(), 'chart-cli-'))
const run = (...args: string[]) => {
  const r = spawnSync('node', [CLI_PATH, ...args], { cwd: CHARTS, encoding: 'utf8', env: { ...process.env, CHART_DATASETS_DIR: join(tmp, 'datasets') } })
  return { code: r.status, out: r.stdout, err: r.stderr }
}
const json = (...args: string[]) => JSON.parse(run(...args, '--json').out)

beforeAll(async () => {
  // a .mjs script without declarations: typed here by the Cli interface
  const path: string = CLI_PATH
  cli = (await import(path)) as Cli
  cpSync(join(CHARTS, 'datasets'), join(tmp, 'datasets'), { recursive: true })
})
afterAll(() => rmSync(tmp, { recursive: true, force: true }))

const chart = (id: string, name: string, aliases: string[] = [], extra: Partial<{ category: string; intents: string[]; summary: string }> = {}) => ({ id, name, aliases, category: 'comparison', intents: ['compare'], summary: '', ...extra })
const CATALOGUE = [chart('donut', 'Donut chart', ['doughnut']), chart('pie', 'Pie chart'), chart('stacked-bar', 'Stacked bar chart'), chart('bar', 'Bar chart', ['vertical-bar']), chart('semi-donut', 'Semi-donut chart')]

describe('cli: parsing', () => {
  it('types CSV cells and keeps leading-zero codes as text', () => {
    expect(cli.parseCsv('a,b,c,d\n1,"x, y",,true\n2.5,007,"q""t",false\n')).toEqual([{ a: 1, b: 'x, y', c: null, d: true }, { a: 2.5, b: '007', c: 'q"t', d: false }])
  })
  it('names the CSV problem', () => {
    expect(() => cli.parseCsv('a,b\n1,2\n3\n', 'f.csv')).toThrow("f.csv: row 2 (line 3) has 1 fields but the header has 2")
    expect(() => cli.parseCsv('a,b\n', 'f.csv')).toThrow('f.csv: no rows (only a header line)')
    expect(() => cli.parseCsv('a,a\n1,2\n', 'f.csv')).toThrow("f.csv: header names column 'a' twice")
  })
  it('places a JSON error at its line and column', () => {
    expect(() => cli.parseJson('[\n {"a":1},\n {"a": abc}\n]', 'x.json')).toThrow(/^x\.json: not valid JSON at line 3, column 8: Unexpected token 'a'/)
    expect(() => cli.parseJson('[1,2', 'x.json')).toThrow(/^x\.json: not valid JSON at line 1, column 5/)
  })
})

describe('cli: search and reuse', () => {
  it('tokenises without filler words and plurals', () => {
    expect(cli.tokens('Stacked Bars chart')).toEqual(['stacked', 'bar'])
  })
  it('ranks exact ids, aliases and spelled-out ids', () => {
    expect(cli.matchScore('donut', CATALOGUE[0]!)).toEqual([100, 'exact id'])
    expect(cli.matchScore('doughnut', CATALOGUE[0]!)).toEqual([95, 'alias of donut'])
    expect(cli.matchScore('donut-chart', CATALOGUE[0]!)[0]).toBe(92)
    expect(cli.matchScore('donut', CATALOGUE[4]!)[0]).toBe(70)
  })
  it('new reuses donut for doughnut, donut-chart and pie-chart, and nothing for a fresh id', () => {
    const aliases = { doughnut: 'donut', 'vertical-bar': 'bar' }
    expect(cli.reuseOf('doughnut', CATALOGUE, aliases)?.id).toBe('donut')
    expect(cli.reuseOf('donut-chart', CATALOGUE, aliases)?.id).toBe('donut')
    expect(cli.reuseOf('pie-chart', CATALOGUE, aliases)?.id).toBe('pie')
    expect(cli.reuseOf('bar-stacked', CATALOGUE, aliases)?.id).toBe('stacked-bar')
    expect(cli.reuseOf('radial-histogram', CATALOGUE, aliases)).toBeNull()
    expect(cli.reuseOf('bar-stacked', CATALOGUE, aliases, true)).toBeNull()
    expect(cli.reuseOf('donut', CATALOGUE, aliases, true)?.id).toBe('donut')
  })
})

describe('cli: check output', () => {
  it('splits failures into the scoped chart and the rest of the pack', () => {
    const out = 'noise\n══ charts:check (bar)\npass  theme           0.0s\nFAIL  unit            3.0s\nFAIL  icons           0.1s\n\nFailures:\n  types/bar/bar.test.ts:12  bar: geometry\n    expected 2 to be 3\n  [icons] FAIL pie: padding — too wide\n\n2 step(s) failed\n'
    const s = cli.summarizeCheck(out, ['bar'])
    expect(s.steps.map((x) => `${x.status} ${x.name}`)).toEqual(['pass theme', 'FAIL unit', 'FAIL icons'])
    expect(s.mine).toEqual(['types/bar/bar.test.ts:12  bar: geometry\n  expected 2 to be 3'])
    expect(s.others).toEqual(['[icons] FAIL pie: padding — too wide'])
  })
})

describe('cli: dataset inference', () => {
  it('infers kinds, null counts and leaf-only tree fields', () => {
    const rows = cli.shapeDataset('x', { label: 'x', data: [{ id: 'a', when: '2026-01-01', n: 1, tag: 'p' }, { id: 'b', when: '2026-02-01', n: null, tag: 'q' }] })
    expect(rows.kind).toBe('rows')
    expect(Object.fromEntries(Object.entries(rows.fields).map(([k, f]) => [k, f.type + (f.nulls ? `/${f.nulls}` : '')]))).toEqual({ id: 'id', when: 'date', n: 'number/1', tag: 'category' })
    const tree = cli.shapeDataset('t', { label: 't', data: { name: 'root', children: [{ name: 'a', value: 1 }, { name: 'b', value: 2 }] } })
    expect(tree).toMatchObject({ kind: 'tree', rows: 3, fields: { name: { type: 'category' }, value: { type: 'number', on: 'leaf' } } })
  })
  it('names the inconsistent row', () => {
    expect(() => cli.shapeDataset('x', { label: 'x', data: [{ v: 1 }, { v: 2 }, { v: 'abc' }] })).toThrow("row 3: field 'v' is 'abc', expected a number like the other rows")
    expect(() => cli.shapeDataset('x', { label: 'x', data: [{ v: 1 }, { w: 2 }] })).toThrow("row 2: missing field 'v' that the other rows have")
    expect(() => cli.shapeDataset('x', { label: 'x', data: [] })).toThrow('no rows')
  })
})

describe('cli: commands', () => {
  it('lookup finds donut by alias and says "no match" without failing', () => {
    expect(json('lookup', 'doughnut').matches[0]).toMatchObject({ id: 'donut', score: 95 })
    const none = run('lookup', 'zzqqxx')
    expect(none.code).toBe(0)
    expect(none.out).toMatch(/^no match for 'zzqqxx'/)
  })
  it('lookup of an intent lists the charts that serve it', () => {
    const j = json('lookup', 'trend')
    expect(j.intent).toBe('trend')
    expect(j.chartsForIntent).toContain('multi-line')
  })
  it('recommend ranks a dataset', () => {
    const j = json('recommend', 'trend', 'dataset:sales-by-month')
    expect(j.recommendations[0].type).toBe('multi-line')
    expect(j.recommendations[0].score).toBeGreaterThanOrEqual(60)
  })
  it('validate prints the chart’s own errors and exits 1', () => {
    const bad = run('validate', 'pie', '[{"label":"A","value":1},{"label":"B","value":"abc"}]')
    expect(bad.code).toBe(1)
    expect(bad.out).toBe("pie: inline JSON is not valid (1 problem):\n  - field 'value' must be a number, got 'abc' at row 2\n")
    expect(run('validate', 'pie', 'sample:pie/basic').code).toBe(0)
  })
  it('datasets lists and shows', () => {
    expect(json('datasets').datasets.map((d: { name: string }) => d.name)).toContain('sales-by-month')
    expect(json('datasets', 'sales-by-month').fields.month).toMatchObject({ type: 'date' })
  })
  it('new refuses a duplicate and an invalid id, creating nothing', () => {
    for (const id of ['donut', 'doughnut', 'donut-chart', 'pie-chart']) {
      const r = run('new', id)
      expect(r.code).toBe(0)
      expect(r.out).toMatch(/^reuse (donut|pie): /)
    }
    expect(existsSync(join(CHARTS, 'types', 'donut-chart'))).toBe(false)
    expect(run('new', 'Bad_Id').err).toMatch(/not a kebab-case id/)
    expect(run('new', 'trend').err).toMatch(/reserved/)
  })
  it('data-add rejects malformed input, refuses to replace, and adds a valid file', () => {
    const regPath = join(tmp, 'datasets', 'datasets.json')
    const before = readFileSync(regPath, 'utf8')
    writeFileSync(join(tmp, 'bad.json'), '[\n {"label":"A","value":1},\n {"label":"B", value: 2}\n]')
    writeFileSync(join(tmp, 'bad.csv'), 'label,value\nA,1\nB,2\nC,abc\n')
    expect(run('data-add', 'cli-probe', join(tmp, 'bad.json')).err).toMatch(/not valid JSON at line 3, column 16/)
    expect(run('data-add', 'cli-probe', join(tmp, 'bad.csv')).err).toMatch(/row 3: field 'value' is 'abc', expected a number like the other rows/)
    expect(run('data-add', 'sales-by-month', join(tmp, 'bad.csv')).err).toMatch(/already exists.*never replaced/)
    expect(readFileSync(regPath, 'utf8')).toBe(before)
    expect(existsSync(join(tmp, 'datasets', 'cli-probe.json'))).toBe(false)
    writeFileSync(join(tmp, 'good.csv'), 'region,month,sales\nNorth,2026-01-01,10\nNorth,2026-02-01,12\nSouth,2026-01-01,7\nSouth,2026-02-01,9\n')
    const ok = run('data-add', 'cli-probe', join(tmp, 'good.csv'))
    expect(ok.code).toBe(0)
    const entry = JSON.parse(readFileSync(regPath, 'utf8')).datasets.at(-1)
    expect(entry).toMatchObject({ name: 'cli-probe', file: 'cli-probe.json', kind: 'rows', rows: 4, fields: ['region', 'month', 'sales'] })
    expect(entry.intents.length).toBeGreaterThan(0)
    // the typed name list and the README table follow the registry (datasets.test.ts compares them)
    expect(readFileSync(join(tmp, 'datasets', 'index.ts'), 'utf8')).toMatch(/\n {2}'cli-probe',\n\] as const/)
    expect(readFileSync(join(tmp, 'datasets', 'README.md'), 'utf8')).toMatch(/\n\| `cli-probe` \| rows \| 4 \| /)
    expect(spawnSync('node', [join(CHARTS, 'datasets', 'validate.mjs'), join(tmp, 'datasets')]).status).toBe(0)
  })
  it('data-add rolls back when the folder validator rejects the result', () => {
    const regPath = join(tmp, 'datasets', 'datasets.json')
    const before = readFileSync(regPath, 'utf8')
    const sidecars = ['index.ts', 'README.md'].map((f) => readFileSync(join(tmp, 'datasets', f), 'utf8'))
    writeFileSync(join(tmp, 'orphan.json'), JSON.stringify([{ id: 'a', parent: null, v: 1 }, { id: 'b', parent: 'a', v: 2 }, { id: 'c', parent: 'zz', v: 3 }]))
    const r = run('data-add', 'orphan-probe', join(tmp, 'orphan.json'))
    expect(r.code).toBe(1)
    expect(r.err).toMatch(/failed validation and was rolled back[\s\S]*parent "zz" is not an id/)
    expect(readFileSync(regPath, 'utf8')).toBe(before)
    expect(existsSync(join(tmp, 'datasets', 'orphan-probe.json'))).toBe(false)
    expect(['index.ts', 'README.md'].map((f) => readFileSync(join(tmp, 'datasets', f), 'utf8'))).toEqual(sidecars)
  })
  it('sample-add refuses input the chart cannot read and an existing name', () => {
    writeFileSync(join(tmp, 'edge-cli.csv'), 'label,value\nA,1\nB,abc\n')
    const bad = run('sample-add', 'pie', join(tmp, 'edge-cli.csv'))
    expect(bad.code).toBe(1)
    expect(bad.err).toMatch(/not valid pie input, so no sample was written/)
    expect(existsSync(join(CHARTS, 'types', 'pie', 'samples', 'edge-cli.json'))).toBe(false)
    writeFileSync(join(tmp, 'basic.json'), '[{"label":"A","value":1}]')
    expect(run('sample-add', 'pie', join(tmp, 'basic.json')).err).toMatch(/already exists; samples are only added to/)
  })
  it('an unknown command and a missing argument fail with the reason', () => {
    expect(run('frobnicate').code).toBe(1)
    expect(json('render')).toMatchObject({ ok: false, error: expect.stringMatching(/^usage: render/) })
  })
})
