// derate chart pack — the omp extension.
//
// Tools (read-only):  chart_recommend, chart_lookup, dataset_lookup, schema_validate
// Commands:           /chart /chart-for /chart-new /chart-data /chart-sample /chart-check /chart-gallery
// Hooks:              session_start / before_agent_start — one short context block per session
//                     tool_result   — edits under .omp/charts/types/<id>/ or datasets/ are recorded;
//                                     2 s after the last one: the scoped fast check, registry,
//                                     gallery and a dated CHANGELOG line; the result reaches the
//                                     model with its next tool result
//                     session_stop  — charts changed since the last green check: the scoped
//                                     layout + a11y check; a failure continues the session
//
// Everything runs the pack's one CLI (.omp/charts/scripts/cli.mjs) through pi.exec, with
// --json where a tool needs structure, so the extension and a human see the same thing.
import type { ExtensionAPI, ExtensionContext, ToolResultEvent } from '@oh-my-pi/pi-coding-agent'
import { appendFileSync, existsSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { CHANGELOG, CHARTS, REGISTRY_JSON, REPO, abs, bashTargets, rel, targetOf, type Target } from './lib/paths.ts'
import { runCli, runCliJson, splitArgs } from './lib/cli.ts'

type Timer = ReturnType<ExtensionContext['setTimeout']>

interface RegistryChart { id: string; name: string; category: string; intents: string[]; aliases: string[]; summary: string }
interface Registry { count: number; categories: Record<string, string[]>; charts: RegistryChart[] }

const INTENTS = ['compare', 'rank', 'composition', 'trend', 'timeline', 'schedule', 'distribution', 'relationship', 'correlation', 'flow', 'hierarchy', 'network', 'geo', 'kpi', 'change-over-time', 'deviation', 'sets']

let registryCache: { mtime: number; reg: Registry } | null = null
function registry(): Registry {
  try {
    const mtime = statSync(REGISTRY_JSON).mtimeMs
    if (!registryCache || registryCache.mtime !== mtime) registryCache = { mtime, reg: JSON.parse(readFileSync(REGISTRY_JSON, 'utf8')) as Registry }
    return registryCache.reg
  } catch { return { count: 0, categories: {}, charts: [] } }
}

/** A file argument made absolute against the session cwd; sample:/dataset: refs pass through. */
const dataRef = (arg: string, cwd: string) => (/^(sample|dataset):/.test(arg) ? arg : abs(arg, cwd))
const fence = (s: string) => '```\n' + s + '\n```'
const tail = (s: string, n = 60) => s.split('\n').slice(-n).join('\n')

export default function chartPack(pi: ExtensionAPI) {
  pi.setLabel('derate chart pack')
  const z = pi.zod

  // ---------------------------------------------------------------- state
  /** Chart ids / datasets touched since the last green layout+a11y check, with their files. */
  const dirty = new Map<string, Set<string>>()
  /** Touched since the last background run. */
  const pending = new Map<string, Set<string>>()
  /** Background results waiting to ride along with the next tool result. */
  const notes: string[] = []
  let injected = false
  let timer: Timer | undefined

  const key = (t: Target) => (t.kind === 'chart' ? t.id : `dataset:${t.name}`)
  const post = (content: string, display = true) => pi.sendMessage({ customType: 'chart-pack', content, display }, { triggerTurn: false })

  // ---------------------------------------------------------------- context block
  function contextBlock(): string {
    const reg = registry()
    const cats = Object.entries(reg.categories).map(([k, v]) => `${k} ${v.length}`).join(', ')
    return [
      'derate chart pack (.omp/charts/) — read .omp/charts/RULES.md before building any chart; the authoring contract is .omp/charts/core/CHART_SPEC.md (reference: types/bar).',
      `Charts: ${reg.count} in .omp/charts/registry.json (${cats || 'registry not built: cd .omp/charts && node scripts/registry.mjs'}). Search it before creating one: reuse beats new.`,
      "API (.omp/charts/index.ts): chart(type, el, data, opts) · chartFor(intent, data, el) · recommend(intent, data) → [{type, score, reason}] · listCharts({category, intent}) · getSample(type, name) · getDataset(name).",
      `Intents: ${INTENTS.join(', ')}.`,
      'Tools: chart_lookup, chart_recommend, dataset_lookup, schema_validate. Commands: /chart <type|intent> [data] · /chart-for <intent> <data> · /chart-new <id> · /chart-data add <name> <file> · /chart-sample add <type> <file> · /chart-check [type] · /chart-gallery.',
      'CLI (cd .omp/charts): node scripts/cli.mjs render|recommend|lookup|validate|datasets|new|data-add|sample-add|check|gallery — --json for machines. Agent: chart-smith builds or extends a chart type to the full spec.',
      'Hooks: edits under .omp/charts/types/<id>/ or datasets/ run the scoped check, rebuild registry.json + gallery.html and append a CHANGELOG.md line whose `why` you complete; a turn that leaves them red is continued with the failures.',
    ].join('\n')
  }

  // ---------------------------------------------------------------- completions
  const firstArg = (prefix: string, values: Array<{ value: string; description?: string }>) => {
    if (/\s/.test(prefix)) return null
    const hits = values.filter((v) => v.value.startsWith(prefix.toLowerCase())).slice(0, 40)
    return hits.length ? hits.map((v) => ({ value: v.value, label: v.value, description: v.description })) : null
  }
  const chartItems = () => registry().charts.flatMap((c) => [{ value: c.id, description: `${c.category} — ${c.summary}` }, ...c.aliases.map((a) => ({ value: a, description: `alias of ${c.id}` }))])
  const intentItems = () => INTENTS.map((i) => ({ value: i, description: 'intent: the chooser picks the chart' }))

  // ---------------------------------------------------------------- tools
  /** Inline data goes to a temp file so the CLI reads it like any other file. */
  async function withData<T>(data: unknown, fn: (path: string) => Promise<T>): Promise<T> {
    const p = join(tmpdir(), `chart-pack-${process.pid}-${Date.now()}-${Math.random().toString(36).slice(2)}.json`)
    writeFileSync(p, typeof data === 'string' ? data : JSON.stringify(data))
    try { return await fn(p) } finally { rmSync(p, { force: true }) }
  }
  const text = (t: string, details?: unknown, isError = false) => ({ content: [{ type: 'text' as const, text: t }], details, isError })

  pi.registerTool({
    name: 'chart_recommend',
    label: 'Chart recommend',
    description: 'Rank chart types for an intent (compare, rank, composition, trend, timeline, schedule, distribution, relationship, correlation, flow, hierarchy, network, geo, kpi, change-over-time, deviation, sets) and a dataset. Give exactly one of: `data` (inline rows/object or CSV text), `dataset` (a name from datasets/datasets.json), or `file` (a JSON/CSV path). Returns [{type, score, reason, options}]. Use before choosing or building any chart.',
    parameters: z.object({
      intent: z.string().describe('what the chart is for, e.g. "compare" or "trend"'),
      data: z.any().optional().describe('inline data: rows, an object, or CSV text'),
      dataset: z.string().optional().describe('a shared dataset name, e.g. "sales-by-month"'),
      file: z.string().optional().describe('path to a JSON or CSV file'),
    }),
    approval: 'read',
    async execute(_id, params: { intent: string; data?: unknown; dataset?: string; file?: string }, signal, _onUpdate, ctx) {
      const run = (ref: string) => runCliJson<{ intent: string; shape: string; recommendations: Array<{ type: string; score: number; reason: string; options: Record<string, unknown> }>; error?: string }>(pi, ['recommend', params.intent, ref], { signal })
      const r = params.dataset ? await run(`dataset:${params.dataset}`) : params.file ? await run(abs(params.file, ctx.cwd)) : params.data !== undefined ? await withData(params.data, run) : null
      if (!r) return text('give one of data, dataset or file', undefined, true)
      if (!r.json || r.json.error) return text(r.json?.error ?? r.out, r.json, true)
      const recs = r.json.recommendations
      return text(recs.length ? [`${r.json.intent} — ${r.json.shape}`, ...recs.map((x, i) => `${i + 1}. ${x.type} ${x.score} — ${x.reason}${Object.keys(x.options).length ? ` options ${JSON.stringify(x.options)}` : ''}`)].join('\n') : `no chart fits this data for '${r.json.intent}' (${r.json.shape})`, r.json)
    },
  })

  pi.registerTool({
    name: 'chart_lookup',
    label: 'Chart lookup',
    description: 'Search the chart registry (.omp/charts/registry.json) by id, alias, name, category, intent or summary words, e.g. "doughnut", "stacked bars", "trend". Returns ranked matches with their samples, README and icon; an intent also lists the charts that serve it. Run before building any chart: reuse beats new.',
    parameters: z.object({ query: z.string().describe('chart name, alias, intent or words') }),
    approval: 'read',
    async execute(_id, params: { query: string }, signal) {
      const r = await runCliJson<{ matches: Array<{ id: string; category: string; score: number; why: string; summary: string; samples: string[]; readme: string | null; icon: string | null; inRegistry: boolean }>; intent: string | null; chartsForIntent: string[]; error?: string }>(pi, ['lookup', params.query], { signal })
      if (!r.json || r.json.error) return text(r.json?.error ?? r.out, r.json, true)
      const lines = r.json.matches.map((m) => `${m.id} [${m.category}] ${m.score} — ${m.why}${m.inRegistry ? '' : ' (not in registry.json yet)'}\n  ${m.summary}\n  samples: ${m.samples.join(', ') || '—'}; readme: ${m.readme ?? '—'}; icon: ${m.icon ?? '—'}`)
      if (r.json.intent) lines.push(`intent '${r.json.intent}': ${r.json.chartsForIntent.join(', ')} — rank them for your data with chart_recommend`)
      return text(lines.length ? lines.join('\n') : `no chart matches '${params.query}'. If nothing fits, /chart-new <id> scaffolds one (it checks for a duplicate first).`, r.json)
    },
  })

  pi.registerTool({
    name: 'dataset_lookup',
    label: 'Dataset lookup',
    description: 'List the shared datasets (.omp/charts/datasets/datasets.json) or show one: description, kind, rows, fields with types, intents and suggested charts. Use a dataset as chart_recommend/schema_validate input or getDataset(name) in code.',
    parameters: z.object({ name: z.string().optional().describe('dataset name; omit to list all') }),
    approval: 'read',
    async execute(_id, params: { name?: string }, signal) {
      const r = await runCliJson<Record<string, unknown> & { error?: string }>(pi, ['datasets', ...(params.name ? [params.name] : [])], { signal })
      if (!r.json || r.json.error) return text(String(r.json?.error ?? r.out), r.json, true)
      if (!params.name) {
        const ds = r.json.datasets as Array<{ name: string; kind: string; rows: number; description: string; suggested: string[] }>
        return text(ds.map((d) => `${d.name} (${d.kind}, ${d.rows}) — ${d.description} → ${d.suggested.slice(0, 4).join(', ')}`).join('\n'), r.json)
      }
      const e = r.json.entry as { name: string; kind: string; rows: number; description: string; intents: string[]; suggested: string[] }
      const fields = (r.json.fields ?? {}) as Record<string, { type: string; description: string; unit?: string; nulls?: number }>
      return text([`${e.name} (${e.kind}, ${e.rows}) — ${e.description}`, ...Object.entries(fields).map(([k, f]) => `  ${k}: ${f.type}${f.unit ? ` [${f.unit}]` : ''}${f.nulls ? `, ${f.nulls} nulls` : ''} — ${f.description}`), `intents: ${e.intents.join(', ')}`, `suggested: ${e.suggested.join(', ')}`].join('\n'), r.json)
    },
  })

  pi.registerTool({
    name: 'schema_validate',
    label: 'Schema validate',
    description: "Validate data against a chart type's input schema without rendering. Give `data` (inline rows/object or CSV text) or `file` (JSON/CSV path), and optional `options` (e.g. a field mapping {fields: {label: 'plan', value: 'mrr'}}). Returns ok or the human-readable errors the chart would show.",
    parameters: z.object({
      type: z.string().describe('chart id or alias'),
      data: z.any().optional().describe('inline data'),
      file: z.string().optional().describe('path to a JSON or CSV file'),
      options: z.record(z.any()).optional().describe('chart options, e.g. a field mapping'),
    }),
    approval: 'read',
    async execute(_id, params: { type: string; data?: unknown; file?: string; options?: Record<string, unknown> }, signal, _onUpdate, ctx) {
      const opt = params.options ? ['--options', JSON.stringify(params.options)] : []
      const run = (ref: string) => runCliJson<{ ok: boolean; type: string; errors: Array<{ path: string; message: string }>; error?: string }>(pi, ['validate', params.type, ref, ...opt], { signal })
      const r = params.file ? await run(abs(params.file, ctx.cwd)) : params.data !== undefined ? await withData(params.data, run) : null
      if (!r) return text('give data or file', undefined, true)
      if (!r.json || r.json.error) return text(r.json?.error ?? r.out, r.json, true)
      return text(r.json.ok ? `ok: valid ${r.json.type} input` : `${r.json.type}: not valid\n${r.json.errors.map((e) => `- ${e.message}`).join('\n')}`, r.json)
    },
  })

  // ---------------------------------------------------------------- commands
  async function render(args: string[], ctx: ExtensionContext): Promise<boolean> {
    const r = await runCli(pi, ['render', ...args], { timeout: 180_000 })
    if (!r.ok) { ctx.ui.notify(`render failed: ${r.out.split('\n').find((l) => l.startsWith('error')) ?? `exit ${r.code}`}`, 'error'); post(fence(tail(r.out))); return false }
    const png = /^png\s+(.+)$/m.exec(r.out)?.[1]?.trim()
    const pngPath = png ? abs(png, REPO) : null
    if (ctx.hasUI && pngPath && existsSync(pngPath)) {
      pi.sendMessage({ customType: 'chart-pack', display: true, content: [{ type: 'text', text: fence(r.out) }, { type: 'image', data: readFileSync(pngPath).toString('base64'), mimeType: 'image/png' }] }, { triggerTurn: false })
    } else post(fence(r.out))
    return true
  }

  pi.registerCommand('chart', {
    description: 'Render a chart type (its basic sample, or your data) or let the chooser pick for an intent: /chart <type|intent> [data path|dataset:name] [--theme dark] [--width 960]',
    getArgumentCompletions: (prefix) => firstArg(prefix, [...chartItems(), ...intentItems()]),
    handler: async (args, ctx) => {
      const [target, ...rest] = splitArgs(args)
      if (!target) { ctx.ui.notify('usage: /chart <type|intent> [data path]', 'warning'); return }
      const data = rest[0] && !rest[0].startsWith('--') ? [dataRef(rest.shift()!, ctx.cwd)] : []
      await render([target, ...data, ...rest], ctx)
    },
  })

  pi.registerCommand('chart-for', {
    description: 'Ask the chooser: the pick, the reason and the alternatives for an intent and your data, then render the pick: /chart-for <intent> <data path|dataset:name>',
    getArgumentCompletions: (prefix) => firstArg(prefix, intentItems()),
    handler: async (args, ctx) => {
      const [intent, ref, ...rest] = splitArgs(args)
      if (!intent || !ref) { ctx.ui.notify('usage: /chart-for <intent> <data path|dataset:name>', 'warning'); return }
      const data = dataRef(ref, ctx.cwd)
      const r = await runCliJson<{ intent: string; shape: string; recommendations: Array<{ type: string; score: number; reason: string }>; error?: string }>(pi, ['recommend', intent, data])
      if (!r.json || r.json.error || !r.json.recommendations) { ctx.ui.notify(String(r.json?.error ?? 'recommend failed'), 'error'); post(fence(tail(r.out))); return }
      const [pick, ...alts] = r.json.recommendations
      if (!pick) { post(`no chart fits this data for '${r.json.intent}' (${r.json.shape})`); return }
      post([`**${pick.type}** (${pick.score}) for '${r.json.intent}' — ${pick.reason}`, `data: ${r.json.shape}`, alts.length ? `alternatives: ${alts.map((a) => `${a.type} (${a.score}: ${a.reason})`).join('; ')}` : 'no alternatives'].join('\n'))
      await render([intent, data, ...rest], ctx)
    },
  })

  pi.registerCommand('chart-new', {
    description: 'Scaffold a new chart type (checks the registry for an existing one first): /chart-new <id> [--category c] [--name "Display name"] [--intents a,b]',
    handler: async (args, ctx) => {
      const argv = splitArgs(args)
      if (!argv[0]) { ctx.ui.notify('usage: /chart-new <id> [--category c] [--name "Display name"] [--intents a,b]', 'warning'); return }
      post(`/chart-new ${argv[0]}: checking the registry, then scaffold + edges + icon + build + scoped check…`)
      const r = await runCli(pi, ['new', ...argv], { timeout: 900_000 })
      post(fence(tail(r.out, 80)))
      if (!r.ok) ctx.ui.notify(`chart-new ${argv[0]}: ${r.out.split('\n').find((l) => l.startsWith('error')) ?? 'failures — see the transcript'}`, 'error')
      else if (!/^reuse /m.test(r.out)) dirty.set(argv[0], new Set([`.omp/charts/types/${argv[0]}/`]))
    },
  })

  pi.registerCommand('chart-data', {
    description: 'Shared datasets: /chart-data add <name> <file> (validated, never replaces) · /chart-data [list] · /chart-data show <name>',
    getArgumentCompletions: (prefix) => firstArg(prefix, [{ value: 'add', description: 'add <name> <file>' }, { value: 'list' }, { value: 'show', description: 'show <name>' }]),
    handler: async (args, ctx) => {
      const [sub = 'list', ...rest] = splitArgs(args)
      let r
      if (sub === 'add') {
        const [name, file, ...flags] = rest
        if (!name || !file) { ctx.ui.notify('usage: /chart-data add <name> <file>', 'warning'); return }
        r = await runCli(pi, ['data-add', name, abs(file, ctx.cwd), ...flags])
      } else if (sub === 'list' || sub === 'show') r = await runCli(pi, ['datasets', ...rest.slice(0, 1)])
      else { ctx.ui.notify(`unknown /chart-data subcommand '${sub}' (add, list, show)`, 'warning'); return }
      post(fence(tail(r.out, 80)))
      if (!r.ok) ctx.ui.notify(r.out.split('\n').find((l) => l.startsWith('error')) ?? `chart-data ${sub} failed`, 'error')
    },
  })

  pi.registerCommand('chart-sample', {
    description: 'Add a sample to a chart (validated against its schema, never overwrites, then its tests run): /chart-sample add <type> <file> [--name edge-foo]',
    getArgumentCompletions: (prefix) => (/^add\s+\S*$/.test(prefix) ? firstArg(prefix.replace(/^add\s+/, ''), chartItems())?.map((i) => ({ ...i, value: `add ${i.value}` })) ?? null : firstArg(prefix, [{ value: 'add', description: 'add <type> <file>' }])),
    handler: async (args, ctx) => {
      const [sub, type, file, ...flags] = splitArgs(args)
      if (sub !== 'add' || !type || !file) { ctx.ui.notify('usage: /chart-sample add <type> <file> [--name edge-foo]', 'warning'); return }
      const r = await runCli(pi, ['sample-add', type, abs(file, ctx.cwd), ...flags], { timeout: 300_000 })
      post(fence(tail(r.out)))
      if (!r.ok) ctx.ui.notify(r.out.split('\n').find((l) => l.startsWith('error')) ?? `sample-add ${type}: tests fail with the new sample`, 'error')
    },
  })

  pi.registerCommand('chart-check', {
    description: 'Run charts:check (theme, build, typecheck, icons, datasets, unit, browser, registry, gallery); scoped to chart ids when given: /chart-check [type,...]',
    getArgumentCompletions: (prefix) => firstArg(prefix, chartItems()),
    handler: async (args, ctx) => {
      const types = splitArgs(args).flatMap((a) => a.split(',')).filter(Boolean)
      post(`running charts:check${types.length ? ` for ${types.join(', ')}` : ' on the whole pack'}…`)
      const r = await runCliJson<{ ok: boolean; command: string; steps: Array<{ name: string; status: string; secs: number }>; failures: string[]; failuresForScope: string[]; failuresElsewhere: string[] }>(pi, ['check', ...(types.length ? [types.join(',')] : [])], { timeout: 1_800_000 })
      const j = r.json
      if (!j) { ctx.ui.notify('charts:check did not report', 'error'); post(fence(tail(r.out))); return }
      // green for the scope: nothing failing names these charts (red elsewhere is other work)
      const scopedGreen = j.ok || (types.length > 0 && j.failuresForScope.length === 0 && j.steps.length > 0)
      if (scopedGreen) for (const k of [...dirty.keys()]) if (!types.length || types.includes(k)) dirty.delete(k)
      const lines = [`$ ${j.command}`, ...j.steps.map((s) => `${s.status.padEnd(4)}  ${s.name.padEnd(12)} ${s.secs.toFixed(1)}s`)]
      if (j.failures.length) {
        if (types.length) lines.push('', `failures in ${types.join(', ')}:`, ...(j.failuresForScope.length ? j.failuresForScope : ['(none)']), ...(j.failuresElsewhere.length ? ['', 'failures elsewhere in the pack:', ...j.failuresElsewhere] : []))
        else lines.push('', 'failures:', ...j.failures)
      }
      lines.push('', j.ok ? 'all green' : 'charts:check FAILED')
      post(fence(lines.join('\n')))
      if (!j.ok) ctx.ui.notify(`charts:check failed (${j.failures.length} failure line(s))`, 'error')
    },
  })

  pi.registerCommand('chart-gallery', {
    description: 'Rebuild dist, icons and gallery.html, then open it',
    handler: async (_args, ctx) => {
      const r = await runCli(pi, ['gallery', '--open'], { timeout: 300_000 })
      post(fence(r.out))
      if (!r.ok) ctx.ui.notify('gallery build failed', 'error')
    },
  })

  // ---------------------------------------------------------------- hooks
  pi.on('session_start', async () => {
    if (injected) return
    injected = true
    post(contextBlock(), false)
  })
  pi.on('before_agent_start', async () => {
    if (injected) return
    injected = true
    return { message: { customType: 'chart-pack', content: contextBlock(), display: false } }
  })

  const touchedPaths = (event: ToolResultEvent, ctx: ExtensionContext): string[] => {
    const out = new Set<string>()
    const input = event.input as Record<string, unknown>
    if (typeof input.path === 'string') out.add(abs(input.path, ctx.cwd))
    if (Array.isArray(input.paths)) for (const p of input.paths) if (typeof p === 'string') out.add(abs(p, ctx.cwd))
    if (typeof input.input === 'string') for (const m of input.input.matchAll(/^\[([^\]#]+)#[0-9A-Fa-f]{4}\]/gm)) out.add(abs(m[1]!, ctx.cwd))
    const details = event.details as { path?: string; perFileResults?: { path: string }[] } | undefined
    if (details && typeof details === 'object') {
      if (typeof details.path === 'string') out.add(abs(details.path, ctx.cwd))
      if (Array.isArray(details.perFileResults)) for (const r of details.perFileResults) if (r?.path) out.add(abs(r.path, ctx.cwd))
    }
    return [...out]
  }

  function record(t: Target, file: string) {
    for (const m of [dirty, pending]) { const s = m.get(key(t)) ?? new Set<string>(); s.add(file); m.set(key(t), s) }
  }

  /** The background step after edits settle: fast scoped check, registry, gallery, CHANGELOG. */
  async function afterEdits(): Promise<string> {
    const batch = new Map(pending)
    pending.clear()
    const ids = [...batch.keys()].filter((k) => !k.startsWith('dataset:') && existsSync(join(CHARTS, 'types', k, 'index.ts')))
    const gone = [...batch.keys()].filter((k) => !k.startsWith('dataset:') && !ids.includes(k))
    const datasets = [...batch.keys()].filter((k) => k.startsWith('dataset:')).map((k) => k.slice(8))
    const report: string[] = []
    let ranRegistry = false
    if (ids.length || datasets.length) {
      const r = await pi.exec('node', ['scripts/check.mjs', ...(ids.length ? ['--type', ids.join(',')] : []), '--skip-browser'], { cwd: CHARTS, timeout: 900_000 })
      const out = `${r.stdout}${r.stderr}`
      ranRegistry = /── registry:/.test(out)
      const at = out.lastIndexOf('══ charts:check')
      report.push(`$ node scripts/check.mjs ${ids.length ? `--type ${ids.join(',')} ` : ''}--skip-browser → ${r.code === 0 ? 'green' : 'FAILED'}`, at >= 0 ? out.slice(at).trim() : tail(out, 30))
    }
    if (!ranRegistry) {
      for (const s of ['scripts/registry.mjs', 'scripts/gallery.mjs']) {
        const r = await pi.exec('node', [s], { cwd: CHARTS, timeout: 300_000 })
        report.push(`${r.code === 0 ? 'ok  ' : 'FAIL'} node ${s}${r.code === 0 ? '' : `\n${tail(`${r.stdout}${r.stderr}`, 8)}`}`)
      }
    }
    const files = [...new Set([...batch.values()].flatMap((s) => [...s]))]
    const what = [...ids, ...gone.map((g) => `${g} (removed)`), ...datasets.map((d) => `dataset ${d}`)].join(', ')
    const day = new Date().toISOString().slice(0, 10)
    if (!existsSync(CHANGELOG)) writeFileSync(CHANGELOG, '# Chart pack changelog\n\nOne dated line per change (RULES.md #5): what, why, which files. The chart-pack extension appends a line after edits; complete its `why`.\n\n')
    appendFileSync(CHANGELOG, `- ${day}: ${what} changed (files: ${files.join(', ')}) — why: <fill in>\n`)
    report.push(`appended a line to .omp/charts/CHANGELOG.md for ${what}: complete its \`why\``)
    return report.join('\n')
  }

  pi.on('tool_result', async (event, ctx) => {
    const out: string[] = []
    if (notes.length) out.push(...notes.splice(0))
    if (!event.isError) {
      let targets: Array<[Target, string]> = []
      if (['edit', 'write', 'ast_edit'].includes(event.toolName)) targets = touchedPaths(event, ctx).flatMap((p) => { const t = targetOf(p); return t ? [[t, rel(p)] as [Target, string]] : [] })
      else if (event.toolName === 'bash') {
        const input = event.input as { command?: unknown; cwd?: unknown }
        if (typeof input.command === 'string') targets = bashTargets(input.command, typeof input.cwd === 'string' ? abs(input.cwd, ctx.cwd) : ctx.cwd).map((t) => [t, `bash: ${input.command as string}`.slice(0, 120)])
      }
      if (targets.length) {
        for (const [t, f] of targets) record(t, f)
        if (timer !== undefined) ctx.clearTimer(timer)
        timer = ctx.setTimeout(() => {
          timer = undefined
          void afterEdits().then((report) => { notes.push(`chart-pack after edits:\n${report}`); post(fence(report)) }, (e: unknown) => { notes.push(`chart-pack after-edit step failed: ${String(e)}`) })
        }, 2000)
        out.push(`chart-pack: ${[...new Set(targets.map(([t]) => key(t)))].join(', ')} changed — the scoped check, registry, gallery and a CHANGELOG line run 2 s after your last edit; the result arrives with a later tool result.`)
      }
    }
    return out.length ? { additionalContext: out.join('\n\n') } : undefined
  })

  // omp bounds a hook at 30 s: the browser check gets 25 of them; when it cannot finish,
  // the continuation asks the agent to run it (a command has no such bound).
  pi.on('session_stop', async (event) => {
    if (event.stop_hook_active) return
    if (!dirty.size) return
    const ids = [...dirty.keys()].filter((k) => !k.startsWith('dataset:') && existsSync(join(CHARTS, 'types', k, 'index.ts')))
    const datasets = [...dirty.keys()].filter((k) => k.startsWith('dataset:'))
    const failures: string[] = []
    if (datasets.length) {
      const v = await pi.exec('node', ['datasets/validate.mjs'], { cwd: CHARTS, timeout: 20_000 })
      if (v.code !== 0) failures.push(`datasets/validate.mjs:\n${tail(`${v.stdout}${v.stderr}`, 25)}`)
    }
    if (ids.length) {
      const r = await pi.exec('node', ['scripts/cli.mjs', 'check', ids.join(','), '--suites', 'layout,a11y', '--json'], { cwd: CHARTS, timeout: 25_000, signal: event.signal })
      if (r.killed) {
        return { continue: true, additionalContext: `Charts changed this session (${ids.join(', ')}) and their layout + a11y check did not finish inside the 30 s stop-hook budget. Run /chart-check ${ids.join(',')} (or: cd .omp/charts && node scripts/cli.mjs check ${ids.join(',')} --suites layout,a11y), fix what fails, or state why not before finishing.${failures.length ? `\n${failures.join('\n')}` : ''}` }
      }
      let j: { ok: boolean; failuresForScope: string[]; failuresElsewhere: string[] } | null = null
      try { j = JSON.parse(r.stdout.slice(r.stdout.indexOf('{'))) } catch { j = null }
      if (!j) failures.push(`check did not report:\n${tail(`${r.stdout}${r.stderr}`, 20)}`)
      else if (j.failuresForScope.length) failures.push(`charts:check ${ids.join(', ')} (layout, a11y):\n${j.failuresForScope.join('\n')}`)
      else if (!j.ok && !j.failuresElsewhere.length) failures.push(`charts:check ${ids.join(', ')} failed without a failure line:\n${tail(r.stdout, 20)}`)
    }
    if (!failures.length) { dirty.clear(); return }
    return { continue: true, additionalContext: `The chart pack is red after this session's edits (${[...dirty.keys()].join(', ')}):\n${failures.join('\n\n')}\nFix them, or state why not before finishing (RULES.md #8).` }
  })
}
