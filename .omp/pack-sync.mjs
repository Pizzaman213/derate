#!/usr/bin/env node
// Reconcile the pack with ui/src and print what is out of step. Nothing is
// fixed automatically except the `screen`-kind registry rows (name / path /
// tokens / icons), which are facts rather than decisions; everything else is
// a list for the person or agent to act on, following .omp/RULES.md.
//
//   cd .omp && npm run pack:sync            # report, update screen rows
//   node pack-sync.mjs --registry           # only the screen rows (the tool_result hook)
//   node pack-sync.mjs --json
//
// Reports: exported components in ui/src not in registry.json; raw <svg> and
// glyph icons outside the pack (from the audit); design-rule findings
// (ui:static --json); tokens with zero usages; icons with zero usages.

import { readFileSync, writeFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative, resolve, dirname, basename } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'
import { lint } from './tests/ui/static/lint.mjs'
import { build as buildTokens, loadTokens } from './design/tokens/build.mjs'
import { loadMeta } from './design/icons/build.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const repo = resolve(here, '..')
const src = join(repo, 'ui', 'src')
const registryPath = join(here, 'design', 'components', 'registry.json')
const args = process.argv.slice(2)
const registryOnly = args.includes('--registry')
const json = args.includes('--json')

const walk = (d, out = []) => { for (const n of readdirSync(d)) { const p = join(d, n); if (statSync(p).isDirectory()) { if (n !== 'node_modules') walk(p, out) } else if (/\.(tsx?|css)$/.test(n) && !/\.d\.ts$/.test(n)) out.push(p) } return out }
const files = walk(src)
const text = new Map(files.map((p) => [p, readFileSync(p, 'utf8')]))
const rel = (p) => relative(repo, p)

const registry = JSON.parse(readFileSync(registryPath, 'utf8'))
const tokens = buildTokens(loadTokens())
const tokenNames = tokens.named.map((n) => n.css.slice(2))
const iconNames = loadMeta().icons.map((i) => i.name)

// exported PascalCase components per file
const exported = []
for (const [p, t] of text) if (p.endsWith('.tsx')) for (const m of t.matchAll(/export\s+(?:default\s+)?(?:function|const)\s+([A-Z][A-Za-z0-9]*)/g)) exported.push({ name: m[1], path: rel(p) })
const registered = new Set(registry.components.map((c) => c.name))

// screen rows: one per tsx file under ui/src that exports a component and is not a shared/css entry
const usedTokens = (t) => [...new Set([...t.matchAll(/var\(--([a-z0-9-]+)[,)]/g)].map((m) => m[1]).filter((n) => tokenNames.includes(n)))].sort()
const usedIcons = (t) => [...new Set([...t.matchAll(/name=(?:"([a-z0-9-]+)"|\{[^}]*?'([a-z0-9-]+)'[^}]*\})/g)].flatMap((m) => [m[1], m[2]]).filter((n) => n && iconNames.includes(n)))].sort()
const shared = new Set(registry.components.filter((c) => c.kind !== 'screen').map((c) => c.path))
const screens = []
for (const [p, t] of text) {
  if (!p.endsWith('.tsx') || shared.has(rel(p)) || /\/components\//.test(p) || basename(p) === 'main.tsx') continue
  const names = [...t.matchAll(/export\s+(?:default\s+)?(?:function|const)\s+([A-Z][A-Za-z0-9]*)/g)].map((m) => m[1])
  if (!names.length) continue
  screens.push({ name: names[0], kind: 'screen', path: rel(p), tokens: usedTokens(t), icons: usedIcons(t) })
}
const before = JSON.stringify(registry.components.filter((c) => c.kind === 'screen'))
registry.components = [...registry.components.filter((c) => c.kind !== 'screen'), ...screens.sort((a, b) => a.path.localeCompare(b.path))]
const changed = before !== JSON.stringify(registry.components.filter((c) => c.kind === 'screen'))
if (changed) writeFileSync(registryPath, JSON.stringify(registry, null, 2) + '\n')
if (registryOnly) { console.log(`pack-sync: ${screens.length} screen rows${changed ? ' updated' : ' unchanged'}`); process.exit(0) }

// unregistered components in shared folders (components/, shell/Sheet)
const unregistered = exported.filter((e) => /^ui\/src\/components\//.test(e.path) && !registered.has(e.name) && e.name !== 'Icon' || false)

// audit for raw svg / glyphs
const audit = spawnSync(process.execPath, [join(here, 'design', 'audit.mjs'), '--json'], { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 })
let rawIcons = { svgs: [], glyphs: [], entities: [] }
const marked = (file) => /design-pack: data-svg/.test(readFileSync(join(repo, file), 'utf8').slice(0, 400))
try { const a = JSON.parse(audit.stdout); rawIcons = { svgs: a.icons.svgs.filter((s) => !/Icon\.tsx$/.test(s.file) && !marked(s.file)).map((s) => `${s.file}:${s.line}`), glyphs: a.icons.glyphs.map((g) => `${g.glyph} ${g.file}:${g.line}`), entities: a.icons.entities.map((e) => `${e.entity} ${e.file}:${e.line}`) } } catch {}

const findings = lint([src])
const all = [...text.values()].join('\n')
const unusedTokens = tokenNames.filter((n) => !new RegExp(`var\\(--${n}[,)]|['"\`]${n}['"\`]|tokens\\.(light|dark|static)\\.${n}\\b`).test(all) && !/^(button|input|card|sheet|popover|tooltip|nav|chip|table|lamp|readout|bar|qr|toast|skeleton)-/.test(n) && !(/^(tag|avatar)-\d+$/.test(n) && all.includes(`--${n.replace(/\d+$/, '')}\${`)))
const unusedIcons = iconNames.filter((n) => !new RegExp(`['"]${n}['"]`).test(all))

const report = { unregistered, rawIcons, findings: findings.map((f) => `${f.path}:${f.line}:${f.col} ${f.rule}`), unusedTokens, unusedIcons, screens: screens.length, screenRowsChanged: changed }
if (json) { process.stdout.write(JSON.stringify(report, null, 2) + '\n'); process.exit(0) }
const section = (title, items, hint) => console.log(`${title}: ${items.length}${items.length ? '\n  ' + items.slice(0, 40).join('\n  ') + (items.length > 40 ? `\n  … ${items.length - 40} more` : '') + (hint ? `\n  → ${hint}` : '') : ''}`)
console.log(`pack-sync — ${screens.length} screen rows ${changed ? 'updated' : 'unchanged'}`)
section('unregistered components in ui/src/components', unregistered.map((u) => `${u.name} (${u.path})`), 'npm run ui:new -- <Name> scaffolds the registry row, states, check and spec; or add the row by hand (RULES.md 1)')
section('raw <svg> outside Icon.tsx / data-svg files', rawIcons.svgs, 'replace with <Icon>, or mark a data drawing with // design-pack: data-svg (RULES.md 2)')
section('glyph characters used as icons', rawIcons.glyphs.filter((g) => !/^× .*Verbatim|^\+ .*ClusterGraph/.test(g)), 'replace with <Icon> (RULES.md 2)')
section('HTML entities used as icons', rawIcons.entities, 'replace with <Icon>')
section('design-rule findings (ui:static)', report.findings, 'fix each; npm run ui:static prints the message')
section('tokens with zero usages in ui/src (component-layer and templated names excluded)', unusedTokens, 'remove from tokens.json with a CHANGELOG line, or keep with a reason in $description (RULES.md 3)')
section('icons with zero usages in ui/src', unusedIcons, 'keep if the design doc needs them; otherwise remove from svg/ and icons.json (RULES.md 2)')
