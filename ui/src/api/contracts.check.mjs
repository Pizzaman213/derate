// requires: python -- computes the expectations by running the contracts
// Verifier for ui/src/api/types.ts against the Python contracts it mirrors.
//
// types.ts opens by saying what it is: "TypeScript mirrors of the frozen
// contracts in 00-architecture.md section 4. Nothing here is invented." That
// was true when it was written and nothing has checked it since. A mirror is a
// copy, and `tsc` cannot see the original -- a union that still lists five
// device classes typechecks perfectly against a Python enum that grew a sixth,
// and the new one arrives at runtime as a value no branch handles.
//
// So the expectations are not restated here. They are computed by running the
// Python, the same way keyfield.check.mjs already keeps looks_like_secret and
// looksLikeSecret honest -- which is the only technique that still fails after
// somebody edits the Python and not the TypeScript.
//
//   node ui/src/api/contracts.check.mjs
import { execFileSync } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const repo = join(here, '..', '..', '..')

const manifest = JSON.parse(
  execFileSync('python3', ['-m', 'control_plane.contracts.manifest'], {
    cwd: repo,
    encoding: 'utf8',
    maxBuffer: 32 * 1024 * 1024,
  }),
)

let failures = 0
const check = (name, actual, expected) => {
  const a = JSON.stringify(actual)
  const e = JSON.stringify(expected)
  const ok = a === e
  if (!ok) failures++
  console.log(
    `${ok ? 'ok  ' : 'FAIL'} ${name}` +
      (ok ? '' : `\n       TypeScript: ${a}\n       Python:     ${e}`),
  )
}

/** Every `export type X = 'a' | 'b'` union in a source file, single or multi
 *  line. Stops at the first token that is not a string literal, a pipe, or
 *  whitespace, so a union of object types is skipped rather than mangled. */
function stringUnions(source) {
  const out = new Map()
  const re = /export type (\w+)\s*=/g
  let m
  while ((m = re.exec(source)) !== null) {
    const members = []
    let i = re.lastIndex
    let expectValue = true
    for (;;) {
      while (i < source.length && /\s/.test(source[i])) i++
      const c = source[i]
      if (c === '|') {
        i++
        expectValue = true
        continue
      }
      if ((c === "'" || c === '"') && expectValue) {
        const end = source.indexOf(c, i + 1)
        if (end === -1) break
        members.push(source.slice(i + 1, end))
        i = end + 1
        expectValue = false
        continue
      }
      break
    }
    if (members.length) out.set(m[1], members)
  }
  return out
}

const types = readFileSync(join(here, 'types.ts'), 'utf8')
const unions = stringUnions(types)

// ---------------------------------------------------------------------------
// Enums. A union that mirrors one must carry exactly its values, in order:
// the order is not load-bearing at runtime, but a reordering is a diff worth
// looking at, and holding it costs nothing.
// ---------------------------------------------------------------------------

const enums = Object.entries(manifest.types).filter(([, t]) => t.kind === 'enum')
const unmirrored = []

for (const [name, spec] of enums) {
  const values = Object.values(spec.members)
  if (!unions.has(name)) {
    unmirrored.push(`${name} (${values.join(' | ')})`)
    continue
  }
  check(`type ${name} mirrors ${spec.module}.${name}`, unions.get(name), values)
}

// ---------------------------------------------------------------------------
// Derived facts the UI forms its own copy of.
// ---------------------------------------------------------------------------

/** The members of `export const NAME: ReadonlySet<T> = new Set<T>([...])`. */
function setLiteral(source, name) {
  const at = source.indexOf(`export const ${name}`)
  if (at === -1) return null
  const open = source.indexOf('[', at)
  const close = source.indexOf(']', open)
  if (open === -1 || close === -1) return null
  return [...source.slice(open + 1, close).matchAll(/'([^']*)'/g)].map((m) => m[1])
}

const rows = readFileSync(join(here, '..', 'tabs', 'models', 'rows.ts'), 'utf8')
check(
  'rows.ts TERMINAL mirrors control_plane.deploy.fsm.TERMINAL',
  setLiteral(rows, 'TERMINAL'),
  manifest.derived.deployment_terminal_states.value,
)

// ---------------------------------------------------------------------------
// Cross-language ports: a TypeScript function that reimplements a Python one.
// Same technique as keyfield.check.mjs -- run the Python, diff the answers --
// because this is the only check that still fails after somebody edits the
// Python and not the TypeScript.
// ---------------------------------------------------------------------------

const DEGREES = []
for (const tp of [1, 2, 4]) {
  for (const pp of [1, 2]) {
    for (const ep of [1, 8]) {
      for (const dp of [1, 2, 3]) {
        DEGREES.push({ tensor_parallel: tp, pipeline_parallel: pp, expert_parallel: ep, data_parallel: dp })
      }
    }
  }
}

const expected = JSON.parse(
  execFileSync(
    'python3',
    [
      '-c',
      [
        'import json, sys, types',
        'from control_plane.gateway.internal_api import _plan_label',
        'cases = json.load(sys.stdin)',
        'print(json.dumps([_plan_label(types.SimpleNamespace(**c)) for c in cases]))',
      ].join('\n'),
    ],
    { cwd: repo, input: JSON.stringify(DEGREES), encoding: 'utf8' },
  ),
)

const outDir = mkdtempSync(join(tmpdir(), 'contracts-'))
const bundle = join(outDir, 'format.mjs')
execFileSync(
  'npx',
  ['esbuild', join(here, '..', 'format.ts'), '--bundle', '--format=esm', `--outfile=${bundle}`],
  { stdio: 'pipe', cwd: join(here, '..', '..') },
)
const { planShortFromDegrees } = await import(pathToFileURL(bundle).href)
rmSync(outDir, { recursive: true, force: true })

const actual = DEGREES.map((d) => planShortFromDegrees(d))
const captionMismatches = DEGREES.filter((_, i) => actual[i] !== expected[i])
if (captionMismatches.length) {
  failures++
  console.log(
    `FAIL planShortFromDegrees mirrors internal_api._plan_label ` +
      `(${captionMismatches.length}/${DEGREES.length} degree combinations differ)`,
  )
  for (const [i, d] of DEGREES.entries()) {
    if (actual[i] === expected[i]) continue
    console.log(
      `       TP${d.tensor_parallel} PP${d.pipeline_parallel} ` +
        `EP${d.expert_parallel} DP${d.data_parallel}: ` +
        `TypeScript ${JSON.stringify(actual[i])} vs Python ${JSON.stringify(expected[i])}`,
    )
  }
} else {
  console.log(
    `ok   planShortFromDegrees mirrors internal_api._plan_label ` +
      `(${DEGREES.length} degree combinations)`,
  )
}

// ---------------------------------------------------------------------------

if (unmirrored.length) {
  console.log(
    `\nnote  ${unmirrored.length} Python enum(s) have no union of the same name in ` +
      `types.ts. Not a failure -- not every contract reaches the wire -- but ` +
      `check that none of these should be mirrored:\n      ` +
      unmirrored.join('\n      '),
  )
}

const mirrored = enums.length - unmirrored.length
console.log(
  `\n${failures === 0 ? 'PASS' : 'FAIL'}: ${mirrored} enum(s), 1 derived set and 1 cross-language port checked` +
    (failures ? `, ${failures} mismatch(es)` : ''),
)
if (failures) {
  console.log(
    'The Python is the original. Regenerate nothing -- edit types.ts to match, ' +
      'then run `cd ui && npm run typecheck` to find the call sites that cared.',
  )
}
process.exit(failures ? 1 : 0)
