// Where the chart pack is, relative to the repo root, and what a touched path belongs to.
import { join, relative, resolve, isAbsolute, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = fileURLToPath(new URL('.', import.meta.url))
/** The repo root: this file is .omp/extensions/chart-pack/lib/paths.ts. */
export const REPO = resolve(here, '..', '..', '..', '..')
export const CHARTS = join(REPO, '.omp', 'charts')
export const REGISTRY_JSON = join(CHARTS, 'registry.json')
export const CHANGELOG = join(CHARTS, 'CHANGELOG.md')

export const rel = (p: string): string => relative(REPO, p).split(sep).join('/')
export const abs = (p: string, cwd = REPO): string => (isAbsolute(p) ? p : resolve(cwd, p))

export type Target = { kind: 'chart'; id: string } | { kind: 'dataset'; name: string }

/** The chart folder or dataset a repo path belongs to (types/_shared and types/all.ts belong to none). */
export function targetOf(p: string): Target | null {
  const r = rel(p)
  let m = /^\.omp\/charts\/types\/([a-z0-9][a-z0-9-]*)\//.exec(r)
  if (m) return { kind: 'chart', id: m[1]! }
  m = /^\.omp\/charts\/datasets\/([^/]+)$/.exec(r)
  if (m) return { kind: 'dataset', name: m[1]!.replace(/\.(json|mjs|ts|md)$/, '') }
  return null
}

const WRITES = /(^|[\s;&|(])(>|>>|tee|mv|cp|rm|touch|truncate|install|patch|sed\s+-i|perl\s+-[a-z]*i)(\s|$)|\bgit\s+(checkout|restore|apply|stash\s+pop)\b|cli\.mjs\s+(new|sample-add|data-add)\b|edges\.mjs\b|writeFile/

/** Chart ids and datasets a bash command plausibly wrote, or [] when it only reads. */
export function bashTargets(command: string, cwd: string): Target[] {
  if (!WRITES.test(command)) return []
  const out: Target[] = []
  const inCharts = rel(cwd).startsWith('.omp/charts') || /\bcd\s+\S*\.omp\/charts\b/.test(command)
  for (const m of command.matchAll(/(?:^|[\s'"=(/])((?:\.omp\/charts\/)?(?:types\/[a-z0-9][a-z0-9-]*\/|datasets\/[a-z0-9][a-z0-9.-]*))/g)) {
    const p = m[1]!.startsWith('.omp/') ? join(REPO, m[1]!) : inCharts ? join(CHARTS, m[1]!) : join(abs(cwd), m[1]!)
    const t = targetOf(p.endsWith('/') ? p + 'x' : p)
    if (t) out.push(t)
  }
  for (const m of command.matchAll(/cli\.mjs\s+(new|sample-add)\s+([a-z0-9][a-z0-9-]*)/g)) out.push({ kind: 'chart', id: m[2]! })
  for (const m of command.matchAll(/cli\.mjs\s+data-add\s+([a-z0-9][a-z0-9-]*)/g)) out.push({ kind: 'dataset', name: m[1]! })
  for (const m of command.matchAll(/edges\.mjs\s+([a-z0-9][a-z0-9-]*)/g)) out.push({ kind: 'chart', id: m[1]! })
  return out
}
