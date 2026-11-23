// Plan-time helpers for deciding where code goes.
//
//   node .omp/scripts/ownership.mjs                       regenerate .omp/OWNERSHIP.md
//   node .omp/scripts/ownership.mjs --headroom FILE...    lines, budget and room left for each file,
//                                                         with the area that owns it
// Run --headroom while writing the design note (rules/validation.md, step 0): a file with
// little room left is where the change should NOT go.
import { existsSync, writeFileSync } from 'node:fs'
import { areas, MAP, render, responsibility } from './validate/ownership.mjs'
import { budgetOf } from './validate/budget.mjs'
import { read } from './validate/context.mjs'

const args = process.argv.slice(2)
if (args[0] === '--headroom') {
  for (const f of args.slice(1)) {
    if (!existsSync(f)) { console.log(`${f}: new file; default budget ${budgetOf(f)} lines`); continue }
    const n = read(f).length, b = budgetOf(f)
    const area = areas().filter((a) => f.startsWith(a + '/')).sort((x, y) => y.length - x.length)[0]
    console.log(`${f}: ${n}/${b} lines, ${b - n >= 0 ? `${b - n} left` : `${n - b} OVER`}${area ? `  [${area}: ${responsibility(area) ?? 'no responsibility line'}]` : ''}`)
  }
} else {
  writeFileSync(MAP, render())
  console.log(`wrote ${MAP}`)
}
