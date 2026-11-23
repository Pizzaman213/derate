// Briefs (.omp/briefs/*.md) are what an agent reads to design something for derate. They rot
// the way any prose does, so the gate checks the parts a machine can: every section the
// template requires is present, and every `path::symbol` reference names a file that exists and
// a symbol defined in it.
import { existsSync, readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

const DIR = '.omp/briefs'
const SECTIONS = ['## Depth 1', '## Depth 2', '## Depth 3', '## Pitfalls', '## Functions']
const REF = /`([\w./-]+\.(?:py|tsx?|mjs)::[\w.]+)`/g

const defines = (text, symbol, file) => {
  const last = symbol.split('.').pop()
  const re = file.endsWith('.py')
    ? new RegExp(`^\\s*(?:async\\s+def|def|class)\\s+${last}\\b|^${last}\\s*(?::[^=]+)?=`, 'm')
    : new RegExp(`(?:function|const|let|class|interface|type|enum)\\s+${last}\\b`)
  return re.test(text)
}

export const steps = [
  { name: 'briefs have every section and name only code that exists', run: () => {
    if (!existsSync(DIR)) return []
    const out = []
    for (const f of readdirSync(DIR).filter((n) => n.endsWith('.md') && n !== 'TEMPLATE.md' && n !== 'README.md')) {
      const text = readFileSync(join(DIR, f), 'utf8')
      for (const s of SECTIONS) if (!text.includes(s)) out.push(`${DIR}/${f}: missing section "${s}"`)
      for (const [, ref] of text.matchAll(REF)) {
        const [path, symbol] = ref.split('::')
        if (!existsSync(path)) out.push(`${DIR}/${f}: ${ref} -- no such file`)
        else if (!defines(readFileSync(path, 'utf8'), symbol, path)) out.push(`${DIR}/${f}: ${ref} -- ${path} does not define ${symbol}`)
      }
    }
    return out
  } },
]
