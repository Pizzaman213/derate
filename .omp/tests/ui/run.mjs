#!/usr/bin/env node
// `npm run ui:check`: the design gate in one command.
//
//   cd .omp && npm run ui:check                 # everything this machine can run
//   cd .omp && npm run ui:check -- --strict     # a skip is a failure (also DERATE_CHECK_STRICT=1)
//   cd .omp && npm run ui:check -- ui/src/tabs  # scope the static lint to a path, and
//                                               # the browser checks to ?only=<Component> when
//                                               # the path names a component file
//
// Order: tokens:check, icons:lint, ui:static (scoped), then the whole
// verifier suite through ui/check.mjs filtered to `.omp/tests` -- which is how
// the gallery, layout, generated and static verifiers run under the same
// discovery, probes and skip accounting as the UI's own. Failures are printed
// as file:line where the verifier gave one.

import { spawnSync } from 'node:child_process'
import { dirname, join, resolve, relative, basename } from 'node:path'
import { fileURLToPath } from 'node:url'
import { existsSync } from 'node:fs'

const here = dirname(fileURLToPath(import.meta.url))
const pack = resolve(here, '..', '..')
const repo = resolve(pack, '..')
const args = process.argv.slice(2)
const strict = args.includes('--strict') || process.env.DERATE_CHECK_STRICT === '1'
// npm runs scripts with cwd = .omp, so a path is resolved against the directory
// the command was typed in (INIT_CWD), then the repo root. A path that resolves
// to nothing is an error: linting a non-existent file "passes" vacuously.
const paths = args.filter((a) => !a.startsWith('--')).map((p) => {
  const hit = [process.env.INIT_CWD, process.cwd(), repo].filter(Boolean).map((b) => resolve(b, p)).find(existsSync)
  if (!hit) { console.error(`ui:check: no such path: ${p}`); process.exit(2) }
  return hit
})

const steps = []
const run = (label, cmd, argv, opts = {}) => {
  const t0 = Date.now()
  const r = spawnSync(cmd, argv, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'], ...opts })
  const out = (r.stdout ?? '') + (r.stderr ?? '')
  const ok = r.status === 0
  steps.push({ label, ok, out, secs: ((Date.now() - t0) / 1000).toFixed(1) })
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${label} (${steps.at(-1).secs}s)`)
  if (!ok) console.log(out.split('\n').filter((l) => /FAIL|error|Error|:\d+:\d+/.test(l)).slice(0, 40).map((l) => '      ' + l).join('\n'))
  return ok
}

run('tokens:check', process.execPath, [join(pack, 'design', 'tokens', 'check.mjs')], { cwd: pack })
run('icons:lint', process.execPath, [join(pack, 'design', 'icons', 'lint.mjs')], { cwd: pack })
run('ui:static' + (paths.length ? ` (${paths.map((p) => relative(repo, p)).join(', ')})` : ''), process.execPath, [join(here, 'static', 'lint.mjs'), ...(paths.length ? paths : [join(repo, 'ui', 'src')])], { cwd: pack })

// Browser checks scoped by component when a path names a component file.
const only = paths.map((p) => basename(p).replace(/\.tsx?$/, '')).filter((n) => /^[A-Z]/.test(n) && existsSync(join(repo, 'ui', 'src', 'components', `${n}.tsx`)))
const env = { ...process.env, ...(only.length ? { DERATE_GALLERY_ONLY: only.join(',') } : {}) }
// layout.check is scoped by the same env var, so a component path still gets the
// overflow / clipping / target-size matrix, not just its own render + axe check.
const filters = only.length ? [...only.map((n) => n.toLowerCase()), 'layout.check'] : ['.omp/tests']
run(`verifiers (ui/check.mjs ${filters.join(' ')}${strict ? ' --strict' : ''})`, process.execPath, [join(repo, 'ui', 'check.mjs'), ...filters, ...(strict ? ['--strict'] : [])], { cwd: join(repo, 'ui'), env })

const failed = steps.filter((s) => !s.ok)
console.log(`\nui:check: ${steps.length - failed.length}/${steps.length} steps passed${failed.length ? ` -- failed: ${failed.map((f) => f.label).join(', ')}` : ''}`)
process.exit(failed.length ? 1 : 0)
