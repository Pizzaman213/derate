// The mechanical half of "a task is done": the checks a person or an agent forgets.
// Judgement calls (does this match the ask, is it a duplicate in spirit, will it be
// maintainable) belong to the reviewers, see .omp/rules/validation.md. Exit 1 on any failure.
//
//   cd .omp && npm run validate                  everything
//   cd .omp && npm run validate -- --fast        static checks only (no pytest, no UI suite)
//   cd .omp && npm run validate -- --update-budget   lower .omp/line-budget.json (never raises)
//   VALIDATE_BASE=origin/dev npm run validate    measure the change against another base
//   VALIDATE_LENIENT=1 npm run validate          UI verifiers may skip (no coordinator login)
//
// UI verifiers run --strict: a skip is a failure, because "checked nothing" must not be
// green. They need DERATE_CHECK_ADMIN_USER / DERATE_CHECK_ADMIN_PASSWORD (see CLAUDE.md).
// Checks live in scripts/validate/*.mjs, one concern per file, each exporting `steps`.
import { spawnSync } from 'node:child_process'
import { baseRef, base, changed } from './validate/context.mjs'
import * as budget from './validate/budget.mjs'
import * as duplicates from './validate/duplicates.mjs'
import * as statics from './validate/static.mjs'

if (process.argv.includes('--update-budget')) { budget.updateRecord(); process.exit(0) }

const run = (cmd, args) => {
  const r = spawnSync(cmd, args, { encoding: 'utf8', maxBuffer: 1 << 28 })
  return r.status === 0 ? [] : [`${cmd} ${args.join(' ')} exited ${r.status}`, ...(r.stdout + r.stderr).trim().split('\n').slice(-12)]
}
const suites = process.argv.includes('--fast') ? [] : [
  { name: 'contracts, single-source and route-coverage tests', run: () =>
    run('python3', ['-m', 'pytest', '-q', '-x', 'tests/unit/test_contracts_manifest.py', 'tests/unit/test_single_source.py', 'tests/unit/test_gateway_auth_coverage.py']) },
  { name: 'UI typecheck', run: () => run('npm', ['--prefix', 'ui', 'run', 'typecheck']) },
  { name: 'UI verifiers', run: () => run('npm', ['--prefix', 'ui', 'run', 'check', ...(process.env.VALIDATE_LENIENT ? [] : ['--', '--strict'])]) },
  { name: 'design pack (tokens, icons)', run: () => run('npm', ['--prefix', '.omp', 'run', 'check']) },
]

console.log(`base ${baseRef} @ ${base.slice(0, 8)}; ${changed.length} changed file(s)\n`)
const failed = []
for (const { name, run: check } of [...statics.steps, ...duplicates.steps, ...budget.steps, ...suites]) {
  const failures = check()
  console.log(`${failures.length ? 'FAIL' : 'PASS'}  ${name}`)
  failures.slice(0, 40).forEach((f) => console.log(`      ${f}`))
  if (failures.length > 40) console.log(`      ... ${failures.length - 40} more`)
  if (failures.length) failed.push(name)
}
console.log(failed.length ? `\nNot done: ${failed.join('; ')}` : '\nGate green. Reviewers are next: see .omp/rules/validation.md')
process.exit(failed.length ? 1 : 0)
