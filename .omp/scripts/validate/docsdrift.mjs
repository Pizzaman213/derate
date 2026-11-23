// READMEs that carry a file table (`| File | Lines | ... |`) go stale first. Judged on what THIS
// change did, so old drift does not block unrelated work: a file ADDED to a directory must
// get a row, and a listed file that changed by more than TOLERANCE of its base size must come
// with an edit to the README. (Found by design-reviewer on the key-profiles change: four new
// files missing and every count stale.)
import { execFileSync } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { base, changed, lineCount, read } from './context.mjs'

const TOLERANCE = 0.2
const SOURCE = /\.(py|tsx?|mjs|css)$/

/** Backticked names in the first column of every table whose header cell is "File". */
const rows = (readme) => {
  const names = new Set()
  let inTable = false
  for (const l of readFileSync(readme, 'utf8').split('\n')) {
    if (/^\|\s*File\s*\|/i.test(l)) inTable = true
    else if (!l.startsWith('|')) inTable = false
    else if (inTable) { const m = l.match(/^\|\s*`([^`]+)`\s*\|/); if (m) names.add(m[1]) }
  }
  return names
}
const baseLines = (p) => {
  try { return lineCount(execFileSync('git', ['show', `${base}:${p}`], { encoding: 'utf8', maxBuffer: 1 << 28, stdio: ['ignore', 'pipe', 'ignore'] })) } catch { return null }
}

export const steps = [
  { name: 'README file tables list new files and follow big changes', run: () => {
    const out = []
    for (const dir of new Set(changed.filter((p) => SOURCE.test(p)).map(dirname))) {
      const readme = join(dir, 'README.md')
      if (!existsSync(readme)) continue
      const listed = rows(readme)
      if (!listed.size) continue
      for (const f of changed.filter((p) => dirname(p) === dir && SOURCE.test(p))) {
        const name = f.split('/').pop(), was = baseLines(f)
        if (was == null && !listed.has(name)) out.push(`${readme}: no row for new file ${name}`)
        else if (was && listed.has(name) && !changed.includes(readme) && Math.abs(read(f).length - was) > TOLERANCE * was)
          out.push(`${readme}: ${name} went ${was} -> ${read(f).length} lines; update its row`)
      }
    }
    return out
  } },
]
