// The pack's data files, read fresh on every call (they are small and the
// hooks rebuild them under the session), plus the lookups the tools answer.
import { readFileSync, existsSync } from 'node:fs'
import { TOKENS_FLAT, TOKENS_JSON, ICONS_JSON, REGISTRY_JSON } from './paths.ts'
import { parseColour, deltaE, toHex, type Rgba } from './colour.ts'

export type ThemeValues = { light: string; dark: string; 'high-contrast': string }
export type Flat = Record<string, string | number | ThemeValues>
export type IconMeta = { name: string; category: string; tags: string[]; usage: string; added: string; replaces: string[] }
export type RegistryEntry = { name: string; kind: string; path: string; props: string[]; variants: Record<string, string[]>; states: string[]; tokens: string[]; icons: string[]; tests: string | null; spec: string; added: string; interactive?: boolean }

const json = <T>(p: string): T => JSON.parse(readFileSync(p, 'utf8')) as T

export const flat = (): Flat => json<Flat>(TOKENS_FLAT)
export const icons = (): IconMeta[] => json<{ icons: IconMeta[] }>(ICONS_JSON).icons
export const registry = (): RegistryEntry[] => json<{ components: RegistryEntry[] }>(REGISTRY_JSON).components
export const ready = (): boolean => existsSync(TOKENS_FLAT) && existsSync(ICONS_JSON) && existsSync(REGISTRY_JSON)

export const isThemed = (v: Flat[string]): v is ThemeValues => typeof v === 'object' && v !== null && 'light' in v
export const perTheme = (v: Flat[string]): ThemeValues => (isThemed(v) ? v : { light: String(v), dark: String(v), 'high-contrast': String(v) })

type Leaf = { $value: unknown; $type?: string; $description?: string }
type Tree = { [k: string]: Tree | Leaf | string | undefined }
const isLeaf = (v: unknown): v is Leaf => typeof v === 'object' && v !== null && '$value' in v

/** Layer, description and references for a CSS name, from tokens.json. */
export function describeToken(cssName: string): { layer: 'primitive' | 'semantic' | 'component'; key: string; description: string; refs: string[] } | null {
  const tree = json<Tree>(TOKENS_JSON)
  const walk = (node: Tree, path: string[]): { key: string; leaf: Leaf }[] => {
    const out: { key: string; leaf: Leaf }[] = []
    for (const [k, v] of Object.entries(node)) {
      if (k.startsWith('$') || v === undefined || typeof v === 'string') continue
      if (isLeaf(v)) out.push({ key: [...path, k].join('.'), leaf: v })
      else out.push(...walk(v, [...path, k]))
    }
    return out
  }
  const all = walk(tree, [])
  const hit = all.find(({ key }) => {
    const p = key.split('.')
    if (p[0] === 'semantic') return p[p.length - 1] === cssName
    if (p[0] === 'component') return p.slice(1).join('-') === cssName
    if ((p[0] === 'space' || p[0] === 'opacity') && p.length === 2) return `${p[0]}-${p[1]}` === cssName
    return false
  })
  if (!hit) return null
  const p = hit.key.split('.')
  const raw = typeof hit.leaf.$value === 'string' ? hit.leaf.$value : JSON.stringify(hit.leaf.$value)
  return { layer: p[0] === 'semantic' ? 'semantic' : p[0] === 'component' ? 'component' : 'primitive', key: hit.key, description: hit.leaf.$description ?? '', refs: [...raw.matchAll(/\{([a-zA-Z0-9_.-]+)\}/g)].map((m) => m[1]!) }
}

export type TokenHit = { name: string; values: ThemeValues; match: 'name' | 'value' | 'nearest'; theme?: keyof ThemeValues; distance?: number; layer: string; description: string; refs: string[] }

/** Exact name, then exact value (any theme), then nearest colour by ΔE. */
export function lookupToken(query: string): TokenHit[] {
  const f = flat()
  const q = query.trim().replace(/^var\(|\)$/g, '').replace(/^--/, '')
  const describe = (name: string) => describeToken(name) ?? { layer: '?', description: '', refs: [] }
  if (q in f) return [{ name: q, values: perTheme(f[q]!), match: 'name', ...describe(q) }]
  const byName = Object.keys(f).filter((n) => n.includes(q))
  if (byName.length && !parseColour(q) && !/^\d/.test(q)) return byName.slice(0, 12).map((n) => ({ name: n, values: perTheme(f[n]!), match: 'name' as const, ...describe(n) }))
  const colour = parseColour(q)
  const hits: TokenHit[] = []
  for (const [name, v] of Object.entries(f)) {
    const t = perTheme(v)
    for (const theme of ['light', 'dark', 'high-contrast'] as const) {
      const val = t[theme]
      if (val.toLowerCase() === q.toLowerCase()) hits.push({ name, values: t, match: 'value', theme, ...describe(name) })
      else if (colour) {
        const c = parseColour(val)
        if (c) { const d = deltaE(colour, c); if (d < 10) hits.push({ name, values: t, match: 'nearest', theme, distance: +d.toFixed(2), ...describe(name) }) }
      }
    }
  }
  const exact = hits.filter((h) => h.match === 'value')
  if (exact.length) return dedupe(exact)
  if (!hits.length && colour) {
    // Nothing close: still answer with the nearest three so the caller sees how far off it is.
    const all: TokenHit[] = []
    for (const [name, v] of Object.entries(f)) {
      const t = perTheme(v)
      for (const theme of ['light', 'dark', 'high-contrast'] as const) { const c = parseColour(t[theme]); if (c) all.push({ name, values: t, match: 'nearest', theme, distance: +deltaE(colour, c).toFixed(2), ...describe(name) }) }
    }
    return dedupe(all.sort((a, b) => (a.distance ?? 99) - (b.distance ?? 99))).slice(0, 3)
  }
  return dedupe(hits.sort((a, b) => (a.distance ?? 99) - (b.distance ?? 99))).slice(0, 8)
}
function dedupe(hits: TokenHit[]): TokenHit[] {
  const seen = new Set<string>()
  return hits.filter((h) => { const k = `${h.name}|${h.theme ?? ''}`; if (seen.has(k)) return false; seen.add(k); return true })
}

export function searchIcons(query: string): IconMeta[] {
  const q = query.trim().toLowerCase()
  const all = icons()
  const exact = all.filter((i) => i.name === q || i.replaces.includes(query.trim()))
  if (exact.length) return exact
  return all.filter((i) => i.name.includes(q) || i.tags.some((t) => t.includes(q)) || i.category === q || i.replaces.some((r) => r.toLowerCase().includes(q)))
}

export function findComponent(name: string): RegistryEntry | undefined {
  const n = name.trim().toLowerCase()
  return registry().find((c) => c.name.toLowerCase() === n)
}

export const colourOf = (v: string): Rgba | null => parseColour(v)
export { toHex }
