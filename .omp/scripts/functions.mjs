// Find what already exists before writing it.
//
//   node .omp/scripts/functions.mjs <term> [<term> ...] [--in <dir>] [--all]
//
// Lists functions, classes and exported consts whose NAME or one-line doc contains every term
// (case-insensitive), as `file:line  signature -- doc`. Python (def/class) and TypeScript
// (function, exported const, class, interface, type). Output stops at 40 hits unless --all.
// Run it with the verb you are about to implement ("mint", "merge", "clamp", "paginate").
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { root } from './validate/context.mjs'

const args = process.argv.slice(2)
const inIdx = args.indexOf('--in')
const scope = inIdx >= 0 ? args[inIdx + 1] : null
const all = args.includes('--all')
const terms = args.filter((a, i) => !a.startsWith('--') && args[i - 1] !== '--in').map((t) => t.toLowerCase())
if (!terms.length) { console.error('usage: functions.mjs <term>... [--in <dir>] [--all]'); process.exit(2) }

const SKIP = new Set(['node_modules', 'dist', '__pycache__', '.worktrees', '.git'])
const files = (d) => readdirSync(d).flatMap((n) => {
  if (SKIP.has(n)) return []
  const p = join(d, n)
  return statSync(p).isDirectory() ? files(p) : /\.(py|tsx?)$/.test(n) ? [p] : []
})

const PY = /^(\s*)(?:async\s+)?(def|class)\s+(\w+)\s*(\([^)]*\)?)?/
const TS = /^\s*(?:export\s+)?(?:async\s+)?(function|const|class|interface|type)\s+(\w+)/
const docAfter = (lines, i) => {
  const m = (lines[i + 1] ?? '').trim().match(/^(?:r?"""|''')\s*(.*?)(?:"""|''')?$/)
  return m ? m[1] : ''
}
const docBefore = (lines, i) => (lines[i - 1] ?? '').trim().match(/^(?:\/\*\*|\*|\/\/)\s*(.*?)(?:\*\/)?$/)?.[1] ?? ''

const hits = []
for (const f of files(scope ?? '.').filter((p) => !/(^|\/)tests?\//.test(p) && !/\.check\.mjs$/.test(p))) {
  const lines = readFileSync(join(root, f), 'utf8').split('\n')
  lines.forEach((l, i) => {
    const py = f.endsWith('.py') ? l.match(PY) : null
    const ts = !py && !f.endsWith('.py') ? l.match(TS) : null
    const name = py?.[3] ?? ts?.[2]
    if (!name) return
    const doc = (py ? docAfter(lines, i) : docBefore(lines, i)).slice(0, 110)
    const hay = `${name.replace(/_/g, ' ').replace(/([a-z])([A-Z])/g, '$1 $2')} ${doc}`.toLowerCase()
    if (terms.every((t) => hay.includes(t))) hits.push(`${f}:${i + 1}  ${py ? `${py[2]} ${name}${py[4] ?? ''}` : `${ts[1]} ${name}`}${doc ? ` -- ${doc}` : ''}`)
  })
}
const shown = all ? hits : hits.slice(0, 40)
console.log(shown.join('\n') || 'nothing matches: search a synonym before writing it')
if (hits.length > shown.length) console.log(`... ${hits.length - shown.length} more (use --all or --in <dir>)`)
