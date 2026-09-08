// The UI's whole test suite, run by one command.
//
//   npm run check              everything this machine can run
//   npm run check -- --strict  a skip is a failure
//   npm run check -- rows      only verifiers whose path matches "rows"
//
// There is no test runner in `ui/`. The `*.check.mjs` files ARE the suite, and
// until this file existed, running them meant copy-pasting eleven command
// lines out of CLAUDE.md -- a list that had drifted to name ten of the
// seventeen that exist, several of which were not even in the repo.
//
// Two rules hold this together, and both exist because the gate has lied
// before:
//
//   Verifiers are DISCOVERED, never listed. A hard-coded list stops covering a
//   verifier the moment somebody adds or renames one, which is the same
//   failure as not having the verifier at all.
//
//   A SKIP is never a PASS. Some verifiers need a live coordinator, or python,
//   or a browser. When the thing is absent the honest report is "did not run",
//   counted in its own column and never folded into the passes. Two of them
//   used to exit 0 in that case, which made "checked nothing" and "checked
//   everything" the same exit code.
//
// What a verifier needs, it declares itself, on a `// requires:` line at the
// top of the file. That is deliberately in the file rather than here: the CI
// workflow used to carry a second hand-maintained copy of the same fact, and
// it was already wrong -- models/registry.check.mjs needs a coordinator and
// was not on the list, so committing it would have turned CI red.

import { execFile, execFileSync } from 'node:child_process'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'
import { findBrowser } from './src/check/browser.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const src = join(here, 'src')

const args = process.argv.slice(2)
const strict = args.includes('--strict') || process.env.DERATE_CHECK_STRICT === '1'
const filters = args.filter((a) => !a.startsWith('--'))
const ORIGIN = process.env.DERATE_CHECK_ORIGIN ?? 'http://localhost:8088'

// ── discovery ────────────────────────────────────────────────────────────────

function walk(dir) {
  const found = []
  for (const entry of readdirSync(dir)) {
    const p = join(dir, entry)
    if (statSync(p).isDirectory()) found.push(...walk(p))
    else if (entry.endsWith('.check.mjs')) found.push(p)
  }
  return found
}

const all = walk(src).sort()

/** The `// requires:` line, if the file has one. No line means hermetic --
 *  needs nothing but this checkout -- which is the safe default: a verifier
 *  that forgets to declare a dependency FAILS loudly on a machine without it,
 *  rather than being silently skipped everywhere forever. */
function requirement(file) {
  const head = readFileSync(file, 'utf8').slice(0, 4096)
  const m = head.match(/^\/\/ requires:\s*(\S+)(?:\s+(\S+))?/m)
  if (!m) return { kind: 'hermetic' }
  return { kind: m[1], arg: m[2] }
}

// ── probes: asked once, not once per verifier ────────────────────────────────

const probes = {}

async function probe(kind, arg) {
  const key = arg ? `${kind}:${arg}` : kind
  if (key in probes) return probes[key]

  let result
  if (kind === 'hermetic') {
    result = { ok: true }
  } else if (kind === 'coordinator') {
    try {
      const r = await fetch(`${ORIGIN}/api/topology`, { signal: AbortSignal.timeout(2000) })
      result = r.ok ? { ok: true } : { ok: false, why: `${ORIGIN} answered ${r.status}` }
    } catch (e) {
      result = { ok: false, why: `no coordinator on ${ORIGIN} (${e.message})` }
    }
  } else if (kind === 'python') {
    try {
      execFileSync('python3', ['-c', 'import sys'], { stdio: 'ignore' })
      result = { ok: true }
    } catch {
      result = { ok: false, why: 'no python3 on PATH' }
    }
  } else if (kind === 'browser') {
    const b = findBrowser()
    result = b.path ? { ok: true } : { ok: false, why: b.why }
  } else if (kind === 'fixtures') {
    result = process.env[arg]
      ? { ok: true }
      : { ok: false, why: `$${arg} is not set` }
  } else {
    // An unknown requirement is not a licence to skip. Somebody invented a
    // word; the gate should say so rather than quietly stop running the file.
    result = { ok: false, why: `unknown requirement "${kind}"`, fatal: true }
  }
  probes[key] = result
  return result
}

// ── run ──────────────────────────────────────────────────────────────────────

const run = (file) =>
  new Promise((resolve) => {
    execFile('node', [file], { cwd: here, maxBuffer: 32 * 1024 * 1024 },
      (err, stdout, stderr) => resolve({ code: err ? (err.code ?? 1) : 0, out: (stdout || '') + (stderr || '') }))
  })

const chosen = all.filter((f) => filters.length === 0 || filters.some((q) => f.includes(q)))

if (chosen.length === 0) {
  console.error(filters.length
    ? `no verifier matches ${filters.join(', ')}`
    : `no verifiers found under ${src}`)
  process.exit(1)
}

console.log(`${chosen.length} verifier(s)${filters.length ? ` matching ${filters.join(', ')}` : ''}${strict ? ', strict' : ''}\n`)

const passed = []
const failed = []
const skipped = []

for (const file of chosen) {
  const rel = relative(here, file)
  const req = requirement(file)
  const p = await probe(req.kind, req.arg)

  if (!p.ok && !strict && !p.fatal) {
    skipped.push({ rel, why: p.why })
    console.log(`SKIP  ${rel}\n        ${p.why}`)
    continue
  }
  if (!p.ok && (strict || p.fatal)) {
    failed.push({ rel, out: `requirement not met: ${p.why}` })
    console.log(`FAIL  ${rel}\n        requirement not met: ${p.why}`)
    continue
  }

  const { code, out } = await run(file)
  if (code === 0) {
    passed.push(rel)
    const last = out.trim().split('\n').filter(Boolean).pop() ?? ''
    console.log(`PASS  ${rel}\n        ${last.slice(0, 100)}`)
  } else {
    failed.push({ rel, out })
    console.log(`FAIL  ${rel}  (exit ${code})`)
  }
}

// ── report ───────────────────────────────────────────────────────────────────

for (const f of failed) {
  console.log(`\n${'─'.repeat(72)}\nFAILED  ${f.rel}\n${'─'.repeat(72)}`)
  console.log(f.out.trimEnd())
}

// Skips are their own column and are named. A tally that said "12 passed" over
// five verifiers that never ran is the exact report this file exists to stop.
const parts = [`${passed.length} passed`]
if (skipped.length) parts.push(`${skipped.length} skipped`)
parts.push(`${failed.length} failed`)
console.log(`\n${parts.join(', ')}`)
for (const s of skipped) console.log(`  skipped: ${s.rel} -- ${s.why}`)
if (skipped.length && !strict) console.log('\n  `npm run check -- --strict` makes each of those a failure.')

// The glob matching nothing, or the walk quietly missing a directory, is the
// one way this runner could lie about having run. CI kept this guard as a
// shell `[ "$ran" -gt 0 ]`; it belongs here now.
if (passed.length + failed.length + skipped.length !== chosen.length) {
  console.log('\nverifiers were discovered but not accounted for -- refusing to report a result')
  process.exit(1)
}
process.exit(failed.length === 0 ? 0 : 1)
