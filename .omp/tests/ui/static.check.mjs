// The static design rules (tests/ui/static/lint.mjs) as a verifier, so
// `ui/check.mjs` discovers and gates them. Hermetic: needs nothing but the
// checkout. One PASS/FAIL per rule, with the first findings named.
//
//   cd ui && node ../.omp/tests/ui/static.check.mjs

import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { report } from '../../../ui/src/check/harness.mjs'
import { lint } from './static/lint.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const src = resolve(here, '..', '..', '..', 'ui', 'src')
const { check, note, done } = report()

const RULES = ['color-literal', 'px-literal', 'z-index-literal', 'breakpoint-literal', 'duration-literal', 'font-family-literal', 'raw-svg', 'img-alt', 'icon-button-label', 'onclick-non-interactive', 'button-type', 'tabindex-positive']
const findings = lint([src])
for (const rule of RULES) {
  const hits = findings.filter((f) => f.rule === rule)
  check(hits.length === 0, `${rule}: ${hits.length} finding(s)${hits.length ? ' -- ' + hits.slice(0, 3).map((h) => `${h.path}:${h.line}:${h.col}`).join(', ') : ''}`)
}
const unknown = findings.filter((f) => !RULES.includes(f.rule))
check(unknown.length === 0, `no findings outside the known rule set (${unknown.length})`)
note(`scanned ui/src; ${findings.length} finding(s) in total`)
done()
