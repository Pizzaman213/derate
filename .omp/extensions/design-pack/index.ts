// derate design pack — the omp extension.
//
// Tools (read-only):  token_lookup, icon_search, component_lookup, graph_query, contrast_check
// Commands:           /tokens /icon /ui-check /graph /impact /design-audit /pack-sync
// Hooks:              session_start  — a short context block (stack, pack paths, usage)
//                     tool_result    — lint touched ui/src files at once; debounce 2 s and
//                                      rebuild when a pack source changed; graph:update
//                                      after any source edit
//                     session_stop   — if ui/src or .omp/design changed since the last
//                                      green ui:check, run it; a failure continues the
//                                      session with the failures as context
//
// Every build and check is one of the pack's own scripts run through pi.exec
// from .omp/, so what the hook says is what `npm run …` would say.
import type { ExtensionAPI, ExtensionContext, ToolResultEvent } from '@oh-my-pi/pi-coding-agent'

type Timer = ReturnType<ExtensionContext['setTimeout']>
import { readFileSync, writeFileSync, existsSync, readdirSync, statSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { join, basename } from 'node:path'
import { CHANGELOG, AUDIT_MD, GRAPH_JSON, PACK, REPO, UI_SRC, DESIGN, abs, rel, isUiSource, isTokenSource, isIconSource, isRegistrySource, isSharedComponent, isGraphSource } from './lib/paths.ts'
import { runScript, runNpm, failureLines } from './lib/run.ts'
import { lookupToken, searchIcons, findComponent, flat, icons, registry, ready, perTheme } from './lib/pack.ts'
import { parseColour, contrast, composite, toHex } from './lib/colour.ts'

const SCRIPTS = {
  lint: join(PACK, 'tests', 'ui', 'static', 'lint.mjs'),
  audit: join(DESIGN, 'audit.mjs'),
  packSync: join(PACK, 'pack-sync.mjs'),
}

export default function designPack(pi: ExtensionAPI) {
  pi.setLabel('derate design pack')
  const z = pi.zod

  // ---------------------------------------------------------------- state
  /** Files touched since the last green ui:check, keyed by repo-relative path. */
  const dirty = new Map<string, string>()
  let lastGreen = ''
  let snapshot = ready() ? takeSnapshot() : null
  const timers = new Map<string, Timer>()

  const digest = () => {
    const h = createHash('sha1')
    for (const [p, v] of [...dirty].sort()) h.update(p).update(v)
    return h.digest('hex')
  }
  const fileHash = (p: string) => { try { return createHash('sha1').update(readFileSync(p)).digest('hex') } catch { return 'missing' } }

  function takeSnapshot() {
    return { tokens: Object.keys(flat()), icons: icons().map((i) => i.name), components: registry().map((c) => c.name) }
  }
  function diffNames(before: string[], after: string[]) {
    const b = new Set(before), a = new Set(after)
    return { added: after.filter((x) => !b.has(x)), removed: before.filter((x) => !a.has(x)) }
  }

  const post = (content: string, display = true) => pi.sendMessage({ customType: 'design-pack', content, display }, { triggerTurn: false })

  // ---------------------------------------------------------------- tools
  pi.registerTool({
    name: 'token_lookup',
    label: 'Token lookup',
    description: 'Look up a design token by CSS name (e.g. "--ink", "ink-muted"), by value ("#1A1917", "rgb(26,25,23)", "12px"), or by nearest colour (ΔE). Returns the name, its value per theme, its layer, description and references. Use before adding any colour, size or spacing literal.',
    parameters: z.object({ query: z.string().describe('token name, CSS value, or colour') }),
    approval: 'read',
    async execute(_id, params: { query: string }) {
      const hits = lookupToken(params.query)
      if (!hits.length) return { content: [{ type: 'text', text: `no token matches "${params.query}" by name or value, and nothing within ΔE 10. If this is a new colour, see .omp/commands/new-color.md.` }] }
      const text = hits.map((h) => `--${h.name}  [${h.layer}${h.match === 'name' ? '' : h.match === 'value' ? `, exact value in ${h.theme}` : `, nearest in ${h.theme}: ΔE ${h.distance}`}]\n  light ${h.values.light} · dark ${h.values.dark} · high-contrast ${h.values['high-contrast']}\n  ${h.description}${h.refs.length ? `\n  references ${h.refs.join(', ')}` : ''}`).join('\n')
      return { content: [{ type: 'text', text }], details: hits }
    },
  })

  pi.registerTool({
    name: 'icon_search',
    label: 'Icon search',
    description: 'Search the icon set by name, tag, category or the glyph it replaced (e.g. "✓", "&#10005;", "close", "warning"). Returns the usage snippet. Use before drawing any SVG or typing any glyph.',
    parameters: z.object({ query: z.string().describe('name, tag, category or glyph') }),
    approval: 'read',
    async execute(_id, params: { query: string }) {
      const hits = searchIcons(params.query)
      if (!hits.length) return { content: [{ type: 'text', text: `no icon matches "${params.query}". The set is in .omp/design/icons/icons.json; to add one see .omp/commands/new-icon.md (spec: .omp/design/icons/README.md).` }] }
      return { content: [{ type: 'text', text: hits.map((i) => `${i.name}  [${i.category}; ${i.tags.join(', ')}${i.replaces.length ? `; replaces ${i.replaces.join(' ')}` : ''}]\n  ${i.usage}`).join('\n') }], details: hits }
    },
  })

  pi.registerTool({
    name: 'component_lookup',
    label: 'Component lookup',
    description: 'Look up a registered UI component: path, props, variants, states, tokens, icons, its check file, its DESIGN.md spec anchor, and what depends on it (graphify affected). Use before creating a component that might already exist.',
    parameters: z.object({ name: z.string().describe('component name, e.g. Readout') }),
    approval: 'read',
    async execute(_id, params: { name: string }, signal) {
      const c = findComponent(params.name)
      if (!c) {
        const near = registry().filter((r) => r.name.toLowerCase().includes(params.name.toLowerCase().slice(0, 4))).map((r) => r.name)
        return { content: [{ type: 'text', text: `no component named "${params.name}" in .omp/design/components/registry.json${near.length ? `; near: ${near.join(', ')}` : ''}. Registered: ${registry().map((r) => r.name).join(', ')}.` }] }
      }
      const affected = await pi.exec('graphify', ['affected', `component:${c.name}`, '--graph', GRAPH_JSON], { cwd: PACK, timeout: 60_000, signal }).catch(() => null)
      const deps = affected?.stdout ? affected.stdout.split('\n').filter((l) => l.startsWith('- ')).slice(0, 25).join('\n') : '(graph unavailable; cd .omp && npm run graph:update)'
      const text = [`${c.name}  [${c.kind}]  ${c.path}`, `props: ${c.props.join(', ') || '—'}`, `variants: ${Object.entries(c.variants).map(([k, v]) => `${k}=${v.join('|')}`).join('; ') || '—'}`, `states: ${c.states.join(', ')}`, `tokens: ${c.tokens.map((t) => `--${t}`).join(', ') || '—'}`, `icons: ${c.icons.join(', ') || '—'}`, `test: ${c.tests ?? 'none'}`, `spec: .omp/design/${c.spec}`, `depends on it:`, deps].join('\n')
      return { content: [{ type: 'text', text }], details: c }
    },
  })

  pi.registerTool({
    name: 'graph_query',
    label: 'Graph query',
    description: 'Query the merged code + design graph (.omp/graph/graph.json) with graphify: mode "query" (a question), "affected" (what depends on target), "explain" (one node), or "path" (shortest path from target to `to`). Query the graph before grepping.',
    parameters: z.object({
      question: z.string().describe('the question, or the target node label/id for affected/explain/path'),
      mode: z.enum(['query', 'affected', 'explain', 'path']).default('query'),
      target: z.string().optional().describe('for affected/explain/path when it differs from question'),
      to: z.string().optional().describe('for path: the destination node'),
    }),
    approval: 'read',
    async execute(_id, params: { question: string; mode: 'query' | 'affected' | 'explain' | 'path'; target?: string; to?: string }, signal) {
      const t = params.target ?? params.question
      const args = params.mode === 'query' ? ['query', params.question, '--budget', '1500'] : params.mode === 'path' ? ['path', t, params.to ?? ''] : [params.mode, t]
      const r = await pi.exec('graphify', [...args, '--graph', GRAPH_JSON], { cwd: PACK, timeout: 90_000, signal })
      const out = `${r.stdout}${r.stderr}`.split('\n').filter((l) => !/skill is from graphify/.test(l)).join('\n').trim()
      return { content: [{ type: 'text', text: out || '(no output)' }], isError: r.code !== 0 }
    },
  })

  pi.registerTool({
    name: 'contrast_check',
    label: 'Contrast check',
    description: 'WCAG 2.2 contrast between a foreground and a background, each a token name (e.g. "ink", "--panel") or a colour. Translucent colours are composited over the background (and the background over --panel). Reports the ratio per theme with AA/AAA verdicts for normal and large text.',
    parameters: z.object({ fg: z.string(), bg: z.string(), theme: z.enum(['light', 'dark', 'high-contrast', 'all']).default('all') }),
    approval: 'read',
    async execute(_id, params: { fg: string; bg: string; theme: 'light' | 'dark' | 'high-contrast' | 'all' }) {
      const f = flat()
      const resolve = (s: string, theme: 'light' | 'dark' | 'high-contrast') => { const n = s.replace(/^var\(|\)$/g, '').replace(/^--/, ''); return n in f ? perTheme(f[n]!)[theme] : s }
      const themes = params.theme === 'all' ? (['light', 'dark', 'high-contrast'] as const) : [params.theme]
      const lines: string[] = []
      for (const theme of themes) {
        const panel = parseColour(perTheme(f['panel']!)[theme]) ?? [255, 255, 255, 1]
        let bg = parseColour(resolve(params.bg, theme)), fg = parseColour(resolve(params.fg, theme))
        if (!bg || !fg) { lines.push(`${theme}: cannot parse ${!fg ? params.fg : params.bg} (${!fg ? resolve(params.fg, theme) : resolve(params.bg, theme)})`); continue }
        if (bg[3] < 1) bg = composite(bg, panel)
        if (fg[3] < 1) fg = composite(fg, bg)
        const ratio = contrast(fg, bg)
        const verdict = (min: number) => (ratio >= min ? 'pass' : 'fail')
        lines.push(`${theme}: ${ratio.toFixed(2)}:1  ${toHex(fg)} on ${toHex(bg)}  AA normal ${verdict(4.5)} · AA large/UI ${verdict(3)} · AAA normal ${verdict(7)} · AAA large ${verdict(4.5)}`)
      }
      return { content: [{ type: 'text', text: lines.join('\n') }] }
    },
  })

  // ---------------------------------------------------------------- commands
  pi.registerCommand('tokens', {
    description: 'Look up a design token by name, value or nearest colour',
    handler: async (args) => {
      const hits = lookupToken(args)
      post(hits.length ? hits.map((h) => `--${h.name} [${h.layer}${h.match === 'nearest' ? `, nearest ΔE ${h.distance}` : h.match === 'value' ? `, exact in ${h.theme}` : ''}]: light ${h.values.light} · dark ${h.values.dark} · hc ${h.values['high-contrast']} — ${h.description}`).join('\n') : `no exact match for "${args}" and nothing within ΔE 10`)
    },
  })
  pi.registerCommand('icon', {
    description: 'Search the icon set by name, tag or the glyph it replaced',
    handler: async (args) => {
      const hits = searchIcons(args)
      post(hits.length ? hits.map((i) => `${i.name} [${i.category}; ${i.tags.join(', ')}] ${i.usage}`).join('\n') : `no icon matches "${args}"; /new-icon ${args} creates one to the spec`)
    },
  })
  pi.registerCommand('ui-check', {
    description: 'Run the design gate (tokens:check, icons:lint, ui:static, gallery/layout verifiers); optional path scope',
    handler: async (args, ctx) => {
      const scope = args.trim() ? args.trim().split(/\s+/).map((p) => abs(p, ctx.cwd)) : []
      post(`running ui:check${scope.length ? ` on ${scope.map(rel).join(' ')}` : ''}…`)
      const r = await runNpm(pi, 'ui:check', scope)
      if (r.ok) { lastGreen = digest(); dirty.clear() }
      post(r.ok ? `ui:check passed\n${r.out.split('\n').slice(-4).join('\n')}` : `ui:check FAILED\n${failureLines(r.out).join('\n')}`)
    },
  })
  pi.registerCommand('graph', {
    description: 'Ask the code + design graph a question (graphify query)',
    handler: async (args) => {
      const r = await pi.exec('graphify', ['query', args, '--graph', GRAPH_JSON, '--budget', '1500'], { cwd: PACK, timeout: 90_000 })
      post(`${r.stdout}${r.stderr}`.split('\n').filter((l) => !/skill is from graphify/.test(l)).join('\n').trim() || '(no output)')
    },
  })
  pi.registerCommand('impact', {
    description: 'What depends on a component or token: graphify affected + tested_by + registry tests',
    handler: async (args) => {
      const target = args.trim()
      const c = findComponent(target)
      const label = c ? `component:${c.name}` : target.startsWith('--') || target in flat() ? `design_token_${target.replace(/^--/, '').replace(/[^A-Za-z0-9]+/g, '_')}` : target
      const r = await pi.exec('graphify', ['affected', label, '--graph', GRAPH_JSON], { cwd: PACK, timeout: 90_000 })
      const lines = [`${r.stdout}${r.stderr}`.split('\n').filter((l) => !/skill is from graphify/.test(l)).join('\n').trim()]
      if (c) lines.push(`tests: ${c.tests ?? 'none'}`, `spec: .omp/design/${c.spec}`, `renders: ${registry().filter((o) => o.tokens.length && o.name !== c.name && o.icons.some((i) => c.icons.includes(i))).map((o) => o.name).join(', ') || '—'}`)
      post(lines.join('\n'))
    },
  })
  pi.registerCommand('design-audit', {
    description: 'Re-run the design audit, show the diff against AUDIT.md, then overwrite it',
    handler: async () => {
      const tmp = join(PACK, 'design', '.audit.tmp.md')
      const r = await runScript(pi, SCRIPTS.audit, ['--out', tmp])
      if (!r.ok) { post(`audit failed:\n${r.out.slice(-800)}`); return }
      const before = existsSync(AUDIT_MD) ? readFileSync(AUDIT_MD, 'utf8') : ''
      const after = readFileSync(tmp, 'utf8')
      const d = await pi.exec('diff', ['-u', '--label', 'AUDIT.md (committed)', '--label', 'AUDIT.md (now)', AUDIT_MD, tmp], { cwd: PACK })
      const diff = d.stdout.split('\n').filter((l) => /^[-+]/.test(l) && !/^[-+]{3}/.test(l) && !/generated by/.test(l))
      writeFileSync(AUDIT_MD, after)
      await pi.exec('rm', ['-f', tmp], { cwd: PACK })
      post(diff.length ? `AUDIT.md updated; ${diff.length} changed line(s):\n${diff.slice(0, 60).join('\n')}` : before === after ? 'AUDIT.md unchanged' : 'AUDIT.md updated (timestamp only)')
    },
  })
  pi.registerCommand('pack-sync', {
    description: 'Reconcile the pack with ui/src: unregistered components, raw icons, literals, unused tokens and icons',
    handler: async () => {
      const r = await runScript(pi, SCRIPTS.packSync)
      post(r.out || '(pack-sync printed nothing)')
    },
  })

  // ---------------------------------------------------------------- hooks
  pi.on('session_start', async () => {
    const f = ready() ? flat() : {}
    const block = [
      'derate design pack (.omp/) — read .omp/RULES.md before editing ui/src.',
      'Stack: Vite 5 + React 18 + TS strict; plain CSS custom properties, no Tailwind.',
      `Tokens: ${Object.keys(f).length} CSS names in .omp/design/tokens/tokens.json → var(--name); three themes (light/dark/high-contrast); never a literal colour/px/duration in ui/src.`,
      `Icons: ${ready() ? icons().length : '?'} in .omp/design/icons/; <Icon name="close" decorative /> or <Icon name="alert-triangle" label="Warning" /> — one of label/decorative is required.`,
      `Components: ${ready() ? registry().length : '?'} in .omp/design/components/registry.json; spec in .omp/design/DESIGN.md; gallery in .omp/tests/ui/gallery/states.tsx.`,
      'Tools: token_lookup, icon_search, component_lookup, graph_query, contrast_check. Commands: /tokens /icon /ui-check /graph /impact /design-audit /pack-sync /new-component /new-icon /new-color.',
      'Scripts (cd .omp): npm run tokens:build|tokens:check|icons:build|icons:lint|styleguide:build|ui:static|ui:new|ui:check|graph:update|audit|pack:sync|check.',
      'Query the graph before grepping: graph_query, or graphify query "<q>" --graph .omp/graph/graph.json.',
      'Hooks: edits under ui/src are linted at once; token/icon/registry edits rebuild and append a CHANGELOG line whose `why` you must complete; a turn that leaves ui:check red is continued with the failures.',
    ].join('\n')
    post(block, false)
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

  const debounce = (key: string, fn: () => Promise<void>, ctx: ExtensionContext, ms = 2000) => {
    const prev = timers.get(key)
    if (prev !== undefined) ctx.clearTimer(prev)
    timers.set(key, ctx.setTimeout(() => { timers.delete(key); void fn() }, ms))
  }

  async function rebuild(kind: 'tokens' | 'icons' | 'registry' | 'component', paths: string[]) {
    const before = snapshot ?? takeSnapshot()
    const steps: string[] = []
    const step = async (name: string, r: Promise<{ ok: boolean; out: string }>) => { const x = await r; steps.push(`${x.ok ? 'ok' : 'FAIL'} ${name}${x.ok ? '' : '\n' + failureLines(x.out, 12).join('\n')}`); return x.ok }
    if (kind === 'tokens') { await step('tokens:build', runNpm(pi, 'tokens:build')); await step('tokens:check', runNpm(pi, 'tokens:check')) }
    if (kind === 'icons') { await step('icons:build', runNpm(pi, 'icons:build')); await step('icons:lint', runNpm(pi, 'icons:lint')) }
    if (kind === 'registry' || kind === 'component') await step('pack:sync --registry', runScript(pi, SCRIPTS.packSync, ['--registry']))
    await step('styleguide:build', runNpm(pi, 'styleguide:build'))
    // CHANGELOG entry from the name diff
    const after = takeSnapshot()
    const what: string[] = []
    for (const [k, label] of [['tokens', 'token'], ['icons', 'icon'], ['components', 'component']] as const) {
      const d = diffNames(before[k], after[k])
      if (d.added.length) what.push(`${label}s added: ${d.added.join(', ')}`)
      if (d.removed.length) what.push(`${label}s removed: ${d.removed.join(', ')}`)
    }
    if (!what.length) what.push(`${kind} changed (values only)`)
    const names = [...new Set([...diffNames(before.tokens, after.tokens).added, ...diffNames(before.tokens, after.tokens).removed])]
    const usages = names.length ? grepUsages(names) : []
    const today = new Date().toISOString().slice(0, 10)
    const entry = `## ${today} — ${kind}: ${what[0]}\n\n- what: ${what.join('; ')}.\n- files: ${[...paths.map(rel), ...usages].join(', ')}\n- why: <to be completed>\n\n`
    const log = readFileSync(CHANGELOG, 'utf8')
    writeFileSync(CHANGELOG, log.replace(/\n## /, `\n${entry}## `))
    snapshot = after
    await step('graph:update', runNpm(pi, 'graph:update'))
    return steps
  }

  function grepUsages(names: string[]): string[] {
    const hits = new Set<string>()
    const walk = (dir: string) => { for (const n of readdirSync(dir)) { const p = join(dir, n); if (statSync(p).isDirectory()) { if (n !== 'node_modules') walk(p) } else if (/\.(tsx?|css)$/.test(n)) { const t = readFileSync(p, 'utf8'); if (names.some((x) => t.includes(`--${x}`))) hits.add(rel(p)) } } }
    walk(UI_SRC)
    return [...hits].sort()
  }

  pi.on('tool_result', async (event, ctx) => {
    if (event.isError) return
    if (!['edit', 'write', 'ast_edit'].includes(event.toolName)) return
    const paths = touchedPaths(event, ctx)
    if (!paths.length) return
    const notes: string[] = []
    const ui = paths.filter(isUiSource)
    for (const p of [...ui, ...paths.filter((p) => isTokenSource(p) || isIconSource(p) || isRegistrySource(p))]) dirty.set(rel(p), fileHash(p))
    if (ui.length) {
      const r = await runScript(pi, SCRIPTS.lint, [...ui, '--json'], { timeout: 60_000 })
      try {
        const findings = JSON.parse(r.out.slice(r.out.indexOf('['))) as { path: string; line: number; col: number; rule: string; message: string }[]
        if (findings.length) notes.push(`design lint (${findings.length}):\n` + findings.slice(0, 30).map((f) => `${f.path}:${f.line}:${f.col} ${f.rule} ${f.message}`).join('\n'))
      } catch { if (!r.ok) notes.push(`design lint could not run: ${r.out.slice(-300)}`) }
    }
    const kinds: Array<['tokens' | 'icons' | 'registry' | 'component', string[]]> = []
    const t = paths.filter(isTokenSource); if (t.length) kinds.push(['tokens', t])
    const i = paths.filter(isIconSource); if (i.length) kinds.push(['icons', i])
    const g = paths.filter(isRegistrySource); if (g.length) kinds.push(['registry', g])
    const c = paths.filter(isSharedComponent); if (c.length && !g.length) kinds.push(['component', c])
    for (const [kind, ps] of kinds) {
      debounce(`rebuild:${kind}`, async () => {
        const steps = await rebuild(kind, ps)
        post(`design pack rebuilt after ${kind} change:\n${steps.join('\n')}\nComplete the \`why\` line of the new entry in .omp/design/CHANGELOG.md.`)
      }, ctx)
    }
    if (!kinds.length && paths.some(isGraphSource)) debounce('graph', async () => { await runNpm(pi, 'graph:update') }, ctx)
    if (kinds.length) notes.push(`${kinds.map(([k]) => k).join(', ')} source changed: rebuild + CHANGELOG entry + graph:update start in 2 s; complete the \`why\` line in .omp/design/CHANGELOG.md.`)
    return notes.length ? { additionalContext: notes.join('\n\n') } : undefined
  })

  // omp bounds every event handler at 30 s, so the stop gate runs what fits:
  // the contrast and icon gates, the static rules over the files this turn
  // touched, generated-output freshness, and -- when a shared component
  // changed -- that component's browser check. The layout matrix (4½ min)
  // cannot fit; the handler says so, and /ui-check (a command: no timeout)
  // is how the agent runs it before claiming done.
  pi.on('session_stop', async (event) => {
    if (event.stop_hook_active) return
    // A file deleted since it was touched (a scaffold removed with `ui:new -- --delete`)
    // has nothing left to check; keeping it re-asks for a verifier that no longer exists.
    for (const p of [...dirty.keys()]) if (!existsSync(join(REPO, p))) dirty.delete(p)
    if (!dirty.size || digest() === lastGreen) return
    const t0 = Date.now()
    const left = () => Math.max(1000, 26_000 - (Date.now() - t0))
    // A file deleted since it was touched (a scaffold reversed, a rename) has
    // nothing left to check; keeping it would ask ui/check.mjs for a verifier
    // that no longer exists.
    for (const p of [...dirty.keys()]) if (!existsSync(join(REPO, p))) dirty.delete(p)
    if (!dirty.size) return
    const touched = [...dirty.keys()]
    const failures: string[] = []
    const skipped: string[] = []
    const gate = async (name: string, run: () => Promise<{ ok: boolean; out: string; code?: number }>) => {
      if (left() < 1500) { skipped.push(name); return }
      const r = await run()
      if (!r.ok) failures.push(`${name}:\n${failureLines(r.out, 15).join('\n') || r.out.slice(-600)}`)
    }
    await gate('tokens:check', () => runScript(pi, join(DESIGN, 'tokens', 'check.mjs'), [], { timeout: left() }))
    await gate('icons:lint', () => runScript(pi, join(DESIGN, 'icons', 'lint.mjs'), [], { timeout: left() }))
    const uiFiles = touched.filter((p) => isUiSource(join(REPO, p))).map((p) => join(REPO, p))
    if (uiFiles.length) await gate('ui:static', () => runScript(pi, SCRIPTS.lint, uiFiles, { timeout: left() }))
    await gate('generated.check', () => runScript(pi, join(PACK, 'tests', 'ui', 'generated.check.mjs'), [], { timeout: left(), cwd: join(REPO, 'ui') }))
    const comps = touched.filter((p) => isSharedComponent(join(REPO, p))).map((p) => basename(p, '.tsx').toLowerCase())
    if (comps.length) await gate(`component checks (${comps.join(', ')})`, () => runScript(pi, join(REPO, 'ui', 'check.mjs'), comps, { timeout: left(), cwd: join(REPO, 'ui') }))
    const notRun = [...skipped, ...(uiFiles.length ? ['layout.check.mjs and the full gallery (4½ min; beyond the 30 s hook budget)'] : [])]
    if (!failures.length && !notRun.length) { lastGreen = digest(); dirty.clear(); return }
    if (!failures.length) {
      // Fast gates green; the slow ones are the agent's to run or to decline with a reason.
      return { continue: true, additionalContext: `Design gates for this turn's edits (${touched.join(', ')}): tokens, icons, static rules${comps.length ? ', component checks' : ''} and generated outputs pass. Not run: ${notRun.join('; ')}. Run /ui-check${comps.length ? ' ' + comps.map((c) => `ui/src/components/${c}.tsx`).join(' ') : ''} before finishing, or state why not.` }
    }
    return { continue: true, additionalContext: `ui:check failed after this turn's edits (${touched.join(', ')}):\n${failures.join('\n')}${notRun.length ? `\nNot run: ${notRun.join('; ')}.` : ''}\nFix them, or state why not before finishing.` }
  })
}
