#!/usr/bin/env node
// charts:check -- everything, in dependency order, with one summary at the end.
//
//   node scripts/check.mjs                         the whole pack
//   node scripts/check.mjs --type bar,pie          scoped to chart types (unit + browser suites for them)
//   node scripts/check.mjs --category trend        scoped to a category
//   node scripts/check.mjs --suites layout,a11y    browser suites to run (default visual,layout,a11y,perf)
//   node scripts/check.mjs --skip-browser          no Playwright (unit + static only)
//
// Steps: theme gates, build (types/all.ts, dist/, icon sprite + icons.json), typecheck,
// icon lint, dataset validation, vitest, browser suites, registry.json, gallery.html.
// Exit 1 if any step fails; failures are listed as file:line where the runner gives one.

import { spawnSync } from 'node:child_process'
import { readFileSync, existsSync, readdirSync } from 'node:fs'
import { join, dirname, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const args = process.argv.slice(2)
const get = (k) => { const i = args.indexOf(`--${k}`); return i >= 0 ? args[i + 1] : undefined }
let types = get('type')?.split(',').map((s) => s.trim()).filter(Boolean)
const category = get('category')
const suites = get('suites') ?? 'visual,layout,a11y,perf'
const skipBrowser = args.includes('--skip-browser')

if (category) {
  types = readdirSync(join(root, 'types')).filter((d) => existsSync(join(root, 'types', d, 'index.ts')) && new RegExp(`category:\\s*'${category}'`).test(readFileSync(join(root, 'types', d, 'index.ts'), 'utf8')))
  if (!types.length) { console.error(`no chart has category '${category}'`); process.exit(2) }
}
if (types) for (const t of types) if (!existsSync(join(root, 'types', t, 'index.ts'))) { console.error(`no chart type '${t}' (types/${t}/index.ts)`); process.exit(2) }
const scoped = !!types

const steps = []
function run(name, cmd, argv, { allowFail = false } = {}) {
  const t0 = Date.now()
  process.stdout.write(`\n── ${name}: ${cmd} ${argv.join(' ')}\n`)
  const r = spawnSync(cmd, argv, { cwd: root, stdio: ['ignore', 'pipe', 'pipe'], encoding: 'utf8', maxBuffer: 1 << 28, env: { ...process.env, FORCE_COLOR: '0' } })
  const out = (r.stdout ?? '') + (r.stderr ?? '')
  const tail = out.trim().split('\n').slice(-12).join('\n')
  process.stdout.write(tail + '\n')
  const ok = r.status === 0
  steps.push({ name, ok: ok || allowFail, failed: !ok, secs: (Date.now() - t0) / 1000, out })
  return ok
}

run('theme', 'node', ['theme/build.mjs', '--check'])
const built = run('build', 'node', ['scripts/build.mjs'])
if (built) run('icons:sync', 'node', ['icons/lint.mjs', '--write'], { allowFail: true })
run('typecheck', 'npx', ['tsc', '-p', 'tsconfig.json'])
run('icons', 'node', ['icons/lint.mjs'])
if (existsSync(join(root, 'datasets', 'validate.mjs'))) run('datasets', 'node', ['datasets/validate.mjs'])
const vitestTargets = scoped ? types.map((t) => `types/${t}`) : []
run('unit', 'npx', ['vitest', 'run', ...vitestTargets])
if (!skipBrowser && built) run('browser', 'node', ['tests/browser/run.mjs', '--suite', suites, ...(scoped ? ['--type', types.join(',')] : [])])
if (built) run('registry', 'node', ['scripts/registry.mjs'])
if (built && existsSync(join(root, 'scripts', 'gallery.mjs'))) run('gallery', 'node', ['scripts/gallery.mjs'])
// the whole gallery, every card in every theme, from disk (full runs only: ~2 min)
if (built && !scoped && !skipBrowser) run('gallery:verify', 'node', ['tests/browser/gallery.mjs'])

// ---- failures with locations
const lines = []
const vj = join(root, 'tests', '.results', 'vitest.json')
if (steps.find((s) => s.name === 'unit')?.failed && existsSync(vj)) {
  const rep = JSON.parse(readFileSync(vj, 'utf8'))
  for (const f of rep.testResults ?? [])
    for (const t of f.assertionResults ?? [])
      if (t.status === 'failed') {
        const loc = t.location ? `:${t.location.line}` : ''
        const msg = (t.failureMessages?.[0] ?? '').split('\n')[0]
        lines.push(`${relative(root, f.name)}${loc}  ${t.fullName}\n    ${msg}`)
      }
    // suites that failed to load
  for (const f of rep.testResults ?? []) if (f.status === 'failed' && !(f.assertionResults ?? []).length) lines.push(`${relative(root, f.name)}  ${String(f.message ?? '').split('\n')[0]}`)
}
for (const s of steps) if (s.failed && s.name !== 'unit') {
  const fl = s.out.split('\n').filter((l) => /FAIL|error|Error/.test(l)).slice(0, 30)
  for (const l of fl) lines.push(`[${s.name}] ${l.trim()}`)
}

console.log('\n══ charts:check' + (scoped ? ` (${types.join(', ')})` : ' (all)'))
for (const s of steps) console.log(`${s.failed ? (s.ok ? 'warn' : 'FAIL') : 'pass'}  ${s.name.padEnd(12)} ${s.secs.toFixed(1).padStart(6)}s`)
if (lines.length) {
  console.log('\nFailures:')
  for (const l of lines.slice(0, 80)) console.log('  ' + l)
  if (lines.length > 80) console.log(`  … ${lines.length - 80} more`)
}
const bad = steps.filter((s) => !s.ok)
console.log(bad.length ? `\n${bad.length} step(s) failed` : '\nall green')
process.exit(bad.length ? 1 : 0)
