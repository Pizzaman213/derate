#!/usr/bin/env node
// `npm run ui:new -- <Name>`: a new shared component with everything the pack
// requires of one, so the first `ui:check` after scaffolding is green and the
// work left is the real work (the props, the states, the spec).
//
//   cd .omp && npm run ui:new -- Toast
//   cd .omp && npm run ui:new -- --delete Toast     # reverses every edit below
//
// Writes:  ui/src/components/<Name>.tsx          (props type, className passthrough, tokens + <Icon> only)
// Appends: tests/ui/gallery/states.tsx           between // scaffold:<Name>:start / :end
// Writes:  tests/ui/components/<name>.check.mjs  (all checks pre-wired)
// Appends: design/components/registry.json      (one row)
// Inserts: design/DESIGN.md                      (### <Name> stub under "## Components")
// Rebuilds: design/index.html                   (styleguide:build)
// Appends: design/CHANGELOG.md                   (one dated line)
// Refuses an existing name.

import { readFileSync, writeFileSync, existsSync, unlinkSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'

const here = dirname(fileURLToPath(import.meta.url))
const pack = resolve(here, '..', '..')
const repo = resolve(pack, '..')
const args = process.argv.slice(2)
const del = args.includes('--delete')
const name = args.find((a) => !a.startsWith('--'))
if (!name || !/^[A-Z][A-Za-z0-9]+$/.test(name)) { console.error('usage: ui:new -- <PascalCaseName> | --delete <Name>'); process.exit(2) }
const lower = name.toLowerCase()
const today = new Date().toISOString().slice(0, 10)

const F = {
  component: join(repo, 'ui', 'src', 'components', `${name}.tsx`),
  states: join(here, 'gallery', 'states.tsx'),
  check: join(here, 'components', `${lower}.check.mjs`),
  registry: join(pack, 'design', 'components', 'registry.json'),
  design: join(pack, 'design', 'DESIGN.md'),
  changelog: join(pack, 'design', 'CHANGELOG.md'),
}
const START = `// scaffold:${name}:start`, END = `// scaffold:${name}:end`
const rebuild = () => spawnSync(process.execPath, [join(pack, 'design', 'styleguide.mjs')], { cwd: pack, stdio: 'inherit' })

if (del) {
  if (existsSync(F.component)) unlinkSync(F.component)
  if (existsSync(F.check)) unlinkSync(F.check)
  let s = readFileSync(F.states, 'utf8')
  s = s.replace(new RegExp(`\\n[ \\t]*${START}[\\s\\S]*?${END}[ \\t]*(?=\\n)`), '')
  s = s.replace(new RegExp(`import \\{ ${name} \\} from '../../../../ui/src/components/${name}'\\n`), '')
  writeFileSync(F.states, s)
  const reg = JSON.parse(readFileSync(F.registry, 'utf8'))
  reg.components = reg.components.filter((c) => c.name !== name)
  writeFileSync(F.registry, JSON.stringify(reg, null, 2) + '\n')
  let d = readFileSync(F.design, 'utf8')
  d = d.replace(new RegExp(`\\n### ${name}\\n[\\s\\S]*?(?=\\n### |\\n## )`), '')
  writeFileSync(F.design, d)
  let c = readFileSync(F.changelog, 'utf8')
  c = c.replace(new RegExp(`\\n## \\d{4}-\\d{2}-\\d{2} — scaffold ${name}\\n[\\s\\S]*?(?=\\n## |$)`), '')
  writeFileSync(F.changelog, c)
  rebuild()
  console.log(`removed ${name}: component, gallery states, check, registry row, DESIGN.md stub, CHANGELOG entry`)
  process.exit(0)
}

if (existsSync(F.component) || readFileSync(F.states, 'utf8').includes(START)) { console.error(`${name} already exists (${F.component}); refusing`); process.exit(1) }

writeFileSync(F.component, `import type { ReactNode } from 'react'
import { Icon } from './Icon'

export type ${name}Props = {
  /** What the ${name} is about; announced, so it is text. */
  label: string
  children?: ReactNode
  className?: string
}

/** ${name}. Tokens and <Icon> only: no literal colour, px, duration or SVG --
 *  tests/ui/static/lint.mjs refuses them, and the gallery states in
 *  tests/ui/gallery/states.tsx are what the checks and the style guide render. */
export function ${name}({ label, children, className }: ${name}Props) {
  return (
    <div
      className={className}
      role="group"
      aria-label={label}
      style={{ display: 'grid', gap: 'var(--space-8)', padding: 'var(--space-12)', border: '1px solid var(--rule)', borderRadius: 'var(--radius)', background: 'var(--panel-recessed)' }}
    >
      <span className="label" style={{ display: 'flex', gap: 'var(--space-6)', alignItems: 'center' }}>
        <Icon name="info-circle" size={14} decorative />
        {label}
      </span>
      {children}
    </div>
  )
}
`)

let s = readFileSync(F.states, 'utf8')
s = s.replace(`import { Icon } from '../../../../ui/src/components/Icon'\n`, `import { Icon } from '../../../../ui/src/components/Icon'\nimport { ${name} } from '../../../../ui/src/components/${name}'\n`)
s = s.replace(/\n\]\s*$/, `
  ${START}
  { component: '${name}', variant: 'default', state: 'default', render: () => <${name} label="${name}">Body text that the state shows.</${name}> },
  { component: '${name}', variant: 'default', state: 'empty', render: () => <${name} label="${name}" /> },
  ${END}
]
`)
writeFileSync(F.states, s)

writeFileSync(F.check, `// requires: browser
// ${name}: every variant × state renders, keyboard contract, axe clean.
// See ../lib/component.mjs for what runs; add what is specific to ${name}
// in the \`keyboard\` hook.
//
//   cd ui && node ../.omp/tests/ui/components/${lower}.check.mjs
import { checkComponent } from '../lib/component.mjs'

await checkComponent('${name}')
`)

const reg = JSON.parse(readFileSync(F.registry, 'utf8'))
reg.components.push({ name, kind: 'shared', path: `ui/src/components/${name}.tsx`, props: ['label', 'children', 'className'], variants: {}, states: ['default', 'empty'], tokens: ['space-8', 'space-12', 'rule', 'radius', 'panel-recessed', 'space-6'], icons: ['info-circle'], tests: `.omp/tests/ui/components/${lower}.check.mjs`, spec: `DESIGN.md#${lower}`, added: today })
writeFileSync(F.registry, JSON.stringify(reg, null, 2) + '\n')

let d = readFileSync(F.design, 'utf8')
const stub = `
### ${name}

Scaffolded ${today} by \`npm run ui:new -- ${name}\`; fill this in. **Anatomy:**
label, body. **Variants:** none yet. **States:** default; empty; hover n/a
(not a control); active n/a; focus-visible n/a; disabled n/a; loading —
state the pattern; error — state the pattern. **Keyboard:** none.
**ARIA:** \`role="group" aria-label\`. **Tokens:** \`--space-8\`, \`--space-12\`,
\`--rule\`, \`--radius\`, \`--panel-recessed\`.
`
d = d.replace(/\n### Toast\n/, `${stub}\n### Toast\n`)
if (!d.includes(`### ${name}`)) d = d.replace(/\n## Patterns\n/, `${stub}\n## Patterns\n`)
writeFileSync(F.design, d)

let c = readFileSync(F.changelog, 'utf8')
c = c.replace(/\n## /, `\n## ${today} — scaffold ${name}\n\n- what: new shared component \`${name}\` (scaffolded).\n- files: \`ui/src/components/${name}.tsx\`, \`.omp/tests/ui/gallery/states.tsx\`, \`.omp/tests/ui/components/${lower}.check.mjs\`, \`.omp/design/components/registry.json\`, \`.omp/design/DESIGN.md\`.\n- why: <to be completed>\n\n## `)
writeFileSync(F.changelog, c)
rebuild()
console.log(`scaffolded ${name}: ui/src/components/${name}.tsx, gallery states, tests/ui/components/${lower}.check.mjs, registry row, DESIGN.md stub, CHANGELOG line. Next: fill the spec and the states, then: cd .omp && npm run ui:check -- ui/src/components/${name}.tsx`)
