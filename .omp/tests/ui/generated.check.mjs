// Generated outputs are fresh, and every registered component is covered.
// Hermetic. Rebuilds tokens, icons, the style guide and the architecture
// diagram to memory and byte-compares them with what is on disk, then checks
// that every registry.json component names a check file that exists, a
// DESIGN.md anchor that exists, and at least one gallery state.
//
//   cd ui && node ../.omp/tests/ui/generated.check.mjs

import { readFileSync, existsSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { report } from '../../../ui/src/check/harness.mjs'
import { build as buildTokens, loadTokens, OUTPUTS as TOKEN_OUTPUTS } from '../../design/tokens/build.mjs'
import { build as buildIcons, OUTPUTS as ICON_OUTPUTS } from '../../design/icons/build.mjs'
import { buildStyleGuide, OUTPUT as STYLEGUIDE } from '../../design/styleguide.mjs'
import { render as renderDiagram } from '../../graph/diagram.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const pack = resolve(here, '..', '..')
const repo = resolve(pack, '..')
const { check, note, done } = report()

const same = (file, text) => existsSync(file) && readFileSync(file, 'utf8') === text

const tokens = buildTokens(loadTokens())
for (const [k, f] of Object.entries(TOKEN_OUTPUTS)) check(same(join(pack, 'design', 'tokens', f), tokens[k]), `tokens: ${f} matches tokens.json (else: cd .omp && npm run tokens:build)`)
const icons = buildIcons()
for (const [k, f] of Object.entries(ICON_OUTPUTS)) check(same(join(pack, 'design', 'icons', f), icons[k]), `icons: ${f} matches svg/ + icons.json (else: npm run icons:build)`)
check(same(STYLEGUIDE, await buildStyleGuide()), 'style guide: design/index.html matches its sources (else: npm run styleguide:build)')
const design = readFileSync(join(pack, 'design', 'DESIGN.md'), 'utf8')
const archNow = design.match(/<!-- arch:start -->[\s\S]*?<!-- arch:end -->/)?.[0]
check(archNow === renderDiagram(), 'architecture diagram in DESIGN.md matches graph.json (else: npm run graph:update)')

// registry coverage
// GitHub's heading slug: lowercase, drop everything but letters, digits,
// spaces and hyphens, then spaces to hyphens (so "A / B" is "a--b").
const anchors = new Set([...design.matchAll(/^#{2,4}\s+(.+)$/gm)].map((m) => m[1].toLowerCase().replace(/[^a-z0-9 -]/g, '').trim().replace(/ /g, '-')))
const registry = JSON.parse(readFileSync(join(pack, 'design', 'components', 'registry.json'), 'utf8'))
const states = readFileSync(join(pack, 'tests', 'ui', 'gallery', 'states.tsx'), 'utf8')
for (const c of registry.components.filter((c) => c.kind !== 'screen')) {
  check(!!c.tests && existsSync(join(repo, c.tests)), `${c.name}: tests points at an existing check (${c.tests})`)
  const anchor = (c.spec.split('#')[1] ?? '').toLowerCase()
  check(anchors.has(anchor), `${c.name}: spec anchor DESIGN.md#${anchor} exists`)
  check(new RegExp(`component: '${c.name}'`).test(states), `${c.name}: has gallery states`)
  check(existsSync(join(repo, c.path)), `${c.name}: path exists (${c.path})`)
}
note(`${registry.components.filter((c) => c.kind !== 'screen').length} registered components (+${registry.components.filter((c) => c.kind === 'screen').length} screen rows); ${anchors.size} headings in DESIGN.md`)
done()
