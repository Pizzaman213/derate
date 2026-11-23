// Line budgets: files a person has to read stay readable.
//
// Default budget: 300 lines for scripts and hooks, 500 for *.check.mjs verifiers, 800 for
// other code. A file already over its default at the base is recorded in
// .omp/line-budget.json at its BASE size (or smaller) and may not grow. A new file, or one
// that crossed the default in this change, has no record and fails until it is split.
// `--update-budget` re-records and can only lower an entry; growth that is deliberate goes
// in .omp/validate.allow.json as  "budget": { "path": { "max": N, "reason": "..." } }.
import { execFileSync } from 'node:child_process'
import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import { allFiles, allow, base, lineCount, read } from './context.mjs'

const RECORD = '.omp/line-budget.json'
const defaultBudget = (p) =>
  /(^|\/)(scripts|\.githooks)\//.test(p) || p.endsWith('.sh') ? 300 : p.endsWith('.check.mjs') ? 500 : 800
const counted = (p) => /\.(py|tsx?|mjs|sh|css)$/.test(p) && !/(^|\/)(node_modules|dist|__generated__)\//.test(p)

const record = () => (existsSync(RECORD) ? JSON.parse(readFileSync(RECORD, 'utf8')) : {})
const baseSize = (p) => {
  try { return lineCount(execFileSync('git', ['show', `${base}:${p}`], { encoding: 'utf8', maxBuffer: 1 << 28 })) } catch { return null }
}
const sizes = () => Object.fromEntries(allFiles().filter(counted).map((p) => [p, read(p).length]))
const limit = (p, rec) => allow.budget?.[p]?.max ?? rec[p] ?? defaultBudget(p)

export const updateRecord = () => {
  const rec = record()
  const next = {}
  for (const [p, now] of Object.entries(sizes())) {
    if (now <= defaultBudget(p)) continue
    const was = rec[p] ?? baseSize(p)
    const kept = was == null ? null : Math.min(was, now)
    if (kept != null && kept > defaultBudget(p)) next[p] = kept
  }
  writeFileSync(RECORD, JSON.stringify(Object.fromEntries(Object.entries(next).sort()), null, 2) + '\n')
}

export const steps = [
  { name: 'files stay within their line budget', run: () => {
    const rec = record()
    return Object.entries(sizes()).filter(([p, n]) => n > limit(p, rec))
      .map(([p, n]) => `${p}: ${n} lines, budget ${limit(p, rec)}${rec[p] ? ' (recorded at base)' : ''} -- split it, or add a reasoned "budget" entry to .omp/validate.allow.json`)
  } },
]
