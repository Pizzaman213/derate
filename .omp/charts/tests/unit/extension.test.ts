// The chart-pack omp extension, loaded with a fake `pi` that records what it registers
// and runs pi.exec for real: every tool and command handler is invoked with real
// arguments against the real CLI, and the hooks with the events omp sends.
//
// The slow end-to-end hooks (the debounced after-edit check, the stop gate's browser
// check, /chart-check and /chart-gallery) run only with CHART_PACK_E2E=1: they run
// charts:check themselves, rewrite registry.json/gallery.html and append to
// CHANGELOG.md (restored afterwards), which the default suite must not do.
import { spawn, spawnSync } from 'node:child_process'
import { cpSync, existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { z } from 'zod'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'

const CHARTS = join(import.meta.dirname, '..', '..')
const REPO = join(CHARTS, '..', '..')
const EXT = join(REPO, '.omp', 'extensions', 'chart-pack', 'index.ts')

type Handler = (event: Record<string, unknown>, ctx: unknown) => unknown
type Tool = { name: string; approval?: string; execute: (id: string, params: unknown, signal: undefined, onUpdate: undefined, ctx: unknown) => Promise<{ content: Array<{ type: string; text?: string }>; details?: unknown; isError?: boolean }> }
type Command = { description?: string; getArgumentCompletions?: (prefix: string) => Array<{ value: string }> | null; handler: (args: string, ctx: unknown) => Promise<void> }
type Message = { customType: string; content: string | Array<{ type: string; text?: string; data?: string; mimeType?: string }>; display: boolean }

const tools = new Map<string, Tool>()
const commands = new Map<string, Command>()
const hooks = new Map<string, Handler[]>()
const messages: Message[] = []
const notified: Array<[string, string]> = []
const timers: Array<() => void> = []

const pi = {
  zod: z,
  setLabel: () => {},
  registerTool: (t: Tool) => tools.set(t.name, t),
  registerCommand: (name: string, c: Command) => commands.set(name, c),
  on: (name: string, h: Handler) => hooks.set(name, [...(hooks.get(name) ?? []), h]),
  sendMessage: (m: Message) => messages.push(m),
  exec: (cmd: string, args: string[], o: { cwd?: string; timeout?: number } = {}) =>
    new Promise<{ stdout: string; stderr: string; code: number; killed: boolean }>((resolve) => {
      const p = spawn(cmd, args, { cwd: o.cwd, env: process.env })
      let stdout = '', stderr = '', killed = false
      const t = o.timeout ? setTimeout(() => { killed = true; p.kill('SIGKILL') }, o.timeout) : undefined
      p.stdout.on('data', (d) => (stdout += d))
      p.stderr.on('data', (d) => (stderr += d))
      p.on('close', (code) => { clearTimeout(t); resolve({ stdout, stderr, code: code ?? 1, killed }) })
    }),
}
const ctx = {
  cwd: REPO,
  hasUI: true,
  ui: { notify: (m: string, t: string) => notified.push([m, t]) },
  setTimeout: (fn: () => void) => { timers.push(fn); return timers.length },
  clearTimer: () => {},
}

const textOf = (m: Message | undefined) => (m ? (typeof m.content === 'string' ? m.content : m.content.map((c) => c.text ?? '').join('\n')) : '')
const lastText = () => textOf(messages[messages.length - 1])
const hook = (name: string, event: Record<string, unknown>) => hooks.get(name)![0]!(event, ctx)

let tmp = ''
beforeAll(async () => {
  // vitest serves nothing outside .omp/charts, so the extension is bundled into tests/.build
  // first (esbuild's CLI: its JS API refuses to run under jsdom); import.meta.url keeps
  // pointing at the real lib/paths.ts so REPO resolves as it does in omp. The import is
  // dynamic so this package's tsc never follows the extension's `.ts` imports.
  const out = join(CHARTS, 'tests', '.build', 'chart-pack-extension.mjs')
  const meta = JSON.stringify(pathToFileURL(join(REPO, '.omp', 'extensions', 'chart-pack', 'lib', 'paths.ts')).href)
  const b = spawnSync(join(CHARTS, 'node_modules', '.bin', 'esbuild'), [EXT, '--bundle', '--platform=node', '--format=esm', `--outfile=${out}`, '--log-level=error', `--define:import.meta.url=${meta}`], { encoding: 'utf8' })
  expect(b.status, b.stderr).toBe(0)
  const path: string = out
  const mod = await import(path)
  mod.default(pi)
  tmp = mkdtempSync(join(tmpdir(), 'chart-pack-ext-'))
  cpSync(join(CHARTS, 'datasets'), join(tmp, 'datasets'), { recursive: true })
  process.env.CHART_DATASETS_DIR = join(tmp, 'datasets')
})
afterAll(() => {
  delete process.env.CHART_DATASETS_DIR
  rmSync(tmp, { recursive: true, force: true })
})

describe('chart-pack extension: registration', () => {
  it('registers the four read-only tools, seven commands and the hooks', () => {
    expect([...tools.keys()].sort()).toEqual(['chart_lookup', 'chart_recommend', 'dataset_lookup', 'schema_validate'])
    for (const t of tools.values()) expect(t.approval).toBe('read')
    expect([...commands.keys()].sort()).toEqual(['chart', 'chart-check', 'chart-data', 'chart-for', 'chart-gallery', 'chart-new', 'chart-sample'])
    expect([...hooks.keys()].sort()).toEqual(['before_agent_start', 'session_start', 'session_stop', 'tool_result'])
  })
  it('completes chart ids, aliases and intents', () => {
    const c = commands.get('chart')!.getArgumentCompletions!('dou')!.map((i) => i.value)
    expect(c).toContain('doughnut')
    expect(commands.get('chart')!.getArgumentCompletions!('tre')!.map((i) => i.value)).toContain('trend')
    expect(commands.get('chart')!.getArgumentCompletions!('bar x')).toBeNull()
  })
})

describe('chart-pack extension: tools', () => {
  it('chart_lookup finds donut by its alias', async () => {
    const r = await tools.get('chart_lookup')!.execute('1', { query: 'doughnut' }, undefined, undefined, ctx)
    expect(r.content[0]!.text).toMatch(/^donut \[part-to-whole\] 95 — alias of donut/)
  })
  it('chart_recommend ranks charts for a dataset and for inline data', async () => {
    const a = await tools.get('chart_recommend')!.execute('1', { intent: 'trend', dataset: 'sales-by-month' }, undefined, undefined, ctx)
    expect(a.isError).toBe(false)
    expect(a.content[0]!.text).toMatch(/^trend — 96 rows/)
    expect(a.content[0]!.text).toMatch(/\n1\. multi-line \d+/)
    const b = await tools.get('chart_recommend')!.execute('1', { intent: 'compare', data: [{ label: 'a', value: 1 }, { label: 'b', value: 2 }] }, undefined, undefined, ctx)
    expect(b.content[0]!.text).toMatch(/\n1\. bar \d+/)
    const c = await tools.get('chart_recommend')!.execute('1', { intent: 'compare' }, undefined, undefined, ctx)
    expect(c.isError).toBe(true)
  })
  it('dataset_lookup lists and shows datasets', async () => {
    const all = await tools.get('dataset_lookup')!.execute('1', {}, undefined, undefined, ctx)
    expect(all.content[0]!.text).toMatch(/^sales-by-month \(rows, 96\)/)
    const one = await tools.get('dataset_lookup')!.execute('1', { name: 'org-tree' }, undefined, undefined, ctx)
    expect(one.content[0]!.text).toMatch(/\n {2}parent: id — /)
    const none = await tools.get('dataset_lookup')!.execute('1', { name: 'nope' }, undefined, undefined, ctx)
    expect(none.isError).toBe(true)
  })
  it('schema_validate returns the chart’s own messages', async () => {
    const bad = await tools.get('schema_validate')!.execute('1', { type: 'bar', data: [{ label: 'A', value: 1 }, { label: 'B', value: 'abc' }] }, undefined, undefined, ctx)
    expect(bad.content[0]!.text).toBe("bar: not valid\n- field 'value' must be a number, got 'abc' at row 2")
    expect((bad.details as { ok: boolean }).ok).toBe(false)
    const mapped = await tools.get('schema_validate')!.execute('1', { type: 'bar', data: [{ plan: 'A', mrr: 3 }], options: { fields: { label: 'plan', value: 'mrr' } } }, undefined, undefined, ctx)
    expect(mapped.content[0]!.text).toBe('ok: valid bar input')
  })
})

describe('chart-pack extension: commands', () => {
  it('/chart renders a type and attaches the PNG', async () => {
    const out = join(tmp, 'render-bar')
    await commands.get('chart')!.handler(`bar --out ${out}`, ctx)
    const m = messages[messages.length - 1]!
    expect(textOf(m)).toMatch(/summary {3}Bar chart, 5 bars, highest Bravo at 340, lowest Delta at 90/)
    expect(Array.isArray(m.content) && m.content.some((c) => c.type === 'image' && c.mimeType === 'image/png' && (c.data?.length ?? 0) > 1000)).toBe(true)
    for (const f of ['chart.html', 'chart.png', 'chart.svg', 'data.csv']) expect(existsSync(join(out, f))).toBe(true)
    expect(readFileSync(join(out, 'data.csv'), 'utf8')).toBe('Label,Value\nAlpha,120\nBravo,340\nCharlie,210\nDelta,90\nEcho,275')
  })
  it('/chart-for posts the pick, reason and alternatives, then renders', async () => {
    const before = messages.length
    await commands.get('chart-for')!.handler(`compare dataset:sales-by-month --out ${join(tmp, 'render-for')}`, ctx)
    const posted = messages.slice(before).map(textOf)
    expect(posted[0]).toMatch(/^\*\*bar\*\* \(\d+\) for 'compare' — .+\ndata: 96 rows.*\nalternatives: horizontal-bar/)
    expect(posted[1]).toMatch(/type {6}bar {2}\(chosen for intent 'compare'\)/)
  })
  it('/chart-new reuses an existing chart and creates nothing', async () => {
    await commands.get('chart-new')!.handler('doughnut', ctx)
    expect(lastText()).toMatch(/reuse donut: 'doughnut' is an alias of donut/)
    expect(existsSync(join(CHARTS, 'types', 'doughnut'))).toBe(false)
  })
  it('/chart-data add rejects a malformed file and adds a valid one (temp datasets dir)', async () => {
    const bad = join(tmp, 'bad.csv'), good = join(tmp, 'good.csv')
    writeFileSync(bad, 'label,value\nA,1\nB,2\nC,abc\n')
    writeFileSync(good, 'team,quarter,deals\nNorth,2026-01-01,42\nNorth,2026-04-01,48\nSouth,2026-01-01,37\nSouth,2026-04-01,40\n')
    notified.length = 0
    await commands.get('chart-data')!.handler(`add ext-probe ${bad}`, ctx)
    expect(lastText()).toMatch(/row 3: field 'value' is 'abc', expected a number like the other rows/)
    expect(notified[0]?.[1]).toBe('error')
    expect(existsSync(join(tmp, 'datasets', 'ext-probe.json'))).toBe(false)
    await commands.get('chart-data')!.handler(`add ext-probe ${good}`, ctx)
    expect(lastText()).toMatch(/added dataset 'ext-probe': rows, 4 rows, fields team:category, quarter:date, deals:number/)
    expect(JSON.parse(readFileSync(join(tmp, 'datasets', 'datasets.json'), 'utf8')).datasets.at(-1).name).toBe('ext-probe')
    await commands.get('chart-data')!.handler('show ext-probe', ctx)
    expect(lastText()).toMatch(/ext-probe — 4 rows of team, quarter, deals/)
  })
  it('/chart-sample add refuses data the chart cannot read', async () => {
    notified.length = 0
    await commands.get('chart-sample')!.handler(`add bar ${join(tmp, 'bad.csv')} --name edge-ext-probe`, ctx)
    expect(lastText()).toMatch(/not valid bar input, so no sample was written/)
    expect(notified[0]?.[1]).toBe('error')
    expect(existsSync(join(CHARTS, 'types', 'bar', 'samples', 'edge-ext-probe.json'))).toBe(false)
  })
})

describe('chart-pack extension: hooks', () => {
  it('injects the context block once per session', async () => {
    messages.length = 0
    await hook('session_start', { type: 'session_start' })
    expect(messages).toHaveLength(1)
    expect(messages[0]!.display).toBe(false)
    expect(textOf(messages[0])).toMatch(/Charts: \d+ in \.omp\/charts\/registry\.json/)
    expect(textOf(messages[0])).toMatch(/chart\(type, el, data, opts\) · chartFor\(intent, data, el\)/)
    expect(await hook('before_agent_start', { type: 'before_agent_start', systemPrompt: [] })).toBeUndefined()
    await hook('session_start', { type: 'session_start' })
    expect(messages).toHaveLength(1)
  })
  it('records chart edits from edit/write and writing bash commands, ignores reads', async () => {
    timers.length = 0
    const r = (await hook('tool_result', { toolName: 'write', isError: false, input: { path: '.omp/charts/types/bar/README.md' }, content: [] })) as { additionalContext: string }
    expect(r.additionalContext).toMatch(/^chart-pack: bar changed — the scoped check/)
    expect(timers).toHaveLength(1)
    const b = (await hook('tool_result', { toolName: 'bash', isError: false, input: { command: 'cd .omp/charts && node scripts/cli.mjs sample-add pie x.json' }, content: [] })) as { additionalContext: string }
    expect(b.additionalContext).toMatch(/chart-pack: pie changed/)
    expect(await hook('tool_result', { toolName: 'bash', isError: false, input: { command: 'cat .omp/charts/types/bar/index.ts' }, content: [] })).toBeUndefined()
    expect(await hook('tool_result', { toolName: 'read', isError: false, input: { path: '.omp/charts/types/bar/index.ts' }, content: [] })).toBeUndefined()
    expect(await hook('tool_result', { toolName: 'write', isError: false, input: { path: 'ui/src/App.tsx' }, content: [] })).toBeUndefined()
  })
  it('the stop gate does not loop', async () => {
    expect(await hook('session_stop', { type: 'session_stop', stop_hook_active: true })).toBeUndefined()
  })
})

describe.runIf(process.env.CHART_PACK_E2E === '1')('chart-pack extension: slow end-to-end (CHART_PACK_E2E=1)', () => {
  const changelog = join(CHARTS, 'CHANGELOG.md')
  let saved: string | null = null
  beforeAll(() => { saved = existsSync(changelog) ? readFileSync(changelog, 'utf8') : null })
  afterAll(() => { if (saved === null) rmSync(changelog, { force: true }); else writeFileSync(changelog, saved) })

  it('after edits settle: scoped check, registry, gallery, a CHANGELOG line; the result rides on the next tool result', async () => {
    expect(timers.length).toBeGreaterThan(0)
    timers[timers.length - 1]!()
    const t0 = Date.now()
    // the debounced step is fire-and-forget inside the extension (it exposes no promise), so wait for its message
    while (!messages.some((m) => textOf(m).includes('appended a line to .omp/charts/CHANGELOG.md')) && Date.now() - t0 < 240_000) await new Promise((r) => setTimeout(r, 500))
    const report = textOf(messages.find((m) => textOf(m).includes('appended a line')))
    expect(report).toMatch(/\$ node scripts\/check\.mjs --type bar,pie --skip-browser → (green|FAILED)\n══ charts:check \(bar, pie\)/)
    expect(readFileSync(changelog, 'utf8')).toMatch(/\n- \d{4}-\d{2}-\d{2}: bar, pie changed \(files: \.omp\/charts\/types\/bar\/README\.md, bash: .*\) — why: <fill in>\n$/)
    const next = (await hook('tool_result', { toolName: 'read', isError: false, input: { path: 'x' }, content: [] })) as { additionalContext: string }
    expect(next.additionalContext).toMatch(/^chart-pack after edits:\n\$ node scripts\/check\.mjs/)
  }, 300_000)
  it('the stop gate runs the scoped layout + a11y check (or says it could not finish)', async () => {
    const r = (await hook('session_stop', { type: 'session_stop', stop_hook_active: false })) as { continue?: boolean; additionalContext?: string } | undefined
    if (r) expect(r.additionalContext).toMatch(/did not finish inside the 30 s stop-hook budget|The chart pack is red after this session's edits/)
  }, 60_000)
  it('/chart-check reports steps and scoped failures', async () => {
    await commands.get('chart-check')!.handler('bar', ctx)
    expect(lastText()).toMatch(/\$ node scripts\/check\.mjs --type bar\n(pass|FAIL|warn) +theme/)
    expect(lastText()).toMatch(/all green|charts:check FAILED/)
  }, 600_000)
  it('/chart-gallery rebuilds and says when it cannot open a browser', async () => {
    const path = process.env.PATH
    process.env.PATH = join(process.execPath, '..')
    try { await commands.get('chart-gallery')!.handler('', ctx) } finally { process.env.PATH = path }
    expect(lastText()).toMatch(/gallery {3}\.omp\/charts\/gallery\.html\nno xdg-open on this machine/)
  }, 300_000)
})
