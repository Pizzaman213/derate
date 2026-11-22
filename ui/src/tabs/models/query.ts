// The search box's grammar: plain words, four typed filters, and a pasted
// Hugging Face link. Pure and data-only (suggestions carry actions as data,
// never closures) so `query.check.mjs` can drive it without a browser.

import { band, matches, TERMINAL, type Band, type ModelRow } from './rows'
import { bandWord, gib } from './verdictView'

export interface HfLink {
  repo: string
  quant: string | null
}

// Whole-text match: a link is the only thing in the box or it is not a link.
// `:QUANT` sits directly after the repo (the `hf.co/<repo>:Q4_K_M` form
// llama.cpp and Ollama accept); anything after a `/`, `?` or `#` is a page
// inside the repo and names the same model.
const HF_LINK =
  /^(?:https?:\/\/)?(?:www\.)?(?:huggingface\.co|hf\.co)\/([A-Za-z0-9][A-Za-z0-9._-]*)\/([A-Za-z0-9][A-Za-z0-9._-]*)(?::([A-Za-z0-9._-]+))?(?:[/?#].*)?$/i

// Hub sections that look like an owner and are not one.
const NOT_OWNERS = new Set(['models', 'datasets', 'spaces'])

// A file link: `/resolve/<rev>/<file>.gguf` or `/blob/<rev>/<file>.gguf`.
const FILE_PATH = /^\/(?:resolve|blob)\/[^/]+\/(?:.*\/)?([^/?#]+\.gguf)(?:[?#].*)?$/i
// The quant token at the end of a GGUF filename.
const GGUF_QUANT = /(?:^|[-_.])((?:UD-)?(?:I?Q\d[A-Z0-9_]*|BF16|F16|FP8))\.gguf$/i

export function parseHfLink(text: string): HfLink | null {
  const t = text.trim()
  const m = HF_LINK.exec(t)
  const owner = m?.[1]
  const repo = m?.[2]
  if (!owner || !repo) return null
  if (NOT_OWNERS.has(owner.toLowerCase())) return null
  let quant = m?.[3] ? m[3].toUpperCase() : null
  if (!quant) {
    const rest = t.slice(t.toLowerCase().indexOf(`${owner}/${repo}`.toLowerCase()) + owner.length + 1 + repo.length)
    const file = FILE_PATH.exec(rest)?.[1]
    const q = file ? GGUF_QUANT.exec(file)?.[1] : undefined
    if (q) quant = q.toUpperCase()
  }
  return { repo: `${owner}/${repo}`, quant }
}

export type QueryFilter =
  | { key: 'fits' | 'pinned' }
  | { key: 'on' | 'quant' | 'task' | 'engine'; value: string }

/** What a filter needs beyond the row: the server's pin list. */
export interface FilterContext {
  pins?: ReadonlySet<string>
}

export interface ParsedQuery {
  words: string
  filters: QueryFilter[]
  /** The tokens that became filters, as typed, for the chips. */
  filterTokens: string[]
  link: HfLink | null
}

const VALUE_KEYS = ['on', 'quant', 'task', 'engine'] as const
type ValueKey = (typeof VALUE_KEYS)[number]

function valueKey(token: string): { key: ValueKey; value: string } | null {
  const i = token.indexOf(':')
  if (i < 0) return null
  const key = token.slice(0, i).toLowerCase()
  if (!(VALUE_KEYS as readonly string[]).includes(key)) return null
  return { key: key as ValueKey, value: token.slice(i + 1) }
}

export function parseQuery(text: string): ParsedQuery {
  const link = parseHfLink(text)
  if (link) return { words: '', filters: [], filterTokens: [], link }
  const words: string[] = []
  const filters: QueryFilter[] = []
  const filterTokens: string[] = []
  for (const token of text.split(/\s+/)) {
    if (!token) continue
    const low = token.toLowerCase()
    if (low === 'fits' || low === 'fits:') {
      filters.push({ key: 'fits' })
      filterTokens.push(token)
      continue
    }
    if (low === 'pinned' || low === 'pinned:') {
      filters.push({ key: 'pinned' })
      filterTokens.push(token)
      continue
    }
    const kv = valueKey(token)
    if (kv) {
      // A half-typed `on:` filters nothing yet; searching for the literal
      // text would empty the list while the operator is still typing.
      if (!kv.value) continue
      filters.push({ key: kv.key, value: kv.value.toLowerCase() })
      filterTokens.push(token)
      continue
    }
    words.push(low)
  }
  return { words: words.join(' '), filters, filterTokens, link: null }
}

export function rowPasses(row: ModelRow, f: QueryFilter, fctx: FilterContext = {}): boolean {
  switch (f.key) {
    case 'fits': {
      const b = band(row)
      return b === 'running' || b === 'fits' || b === 'degraded'
    }
    case 'pinned':
      return fctx.pins?.has(row.model_id) === true
    case 'on':
      return (
        row.deployments.some((d) => d.node_ids.some((n) => n.toLowerCase().includes(f.value))) ||
        row.cachedOn.some((n) => n.toLowerCase().includes(f.value))
      )
    case 'quant':
      return [row.dtype, row.nativeDtype, row.quantHint].some(
        (q) => q != null && q.toLowerCase().includes(f.value),
      )
    case 'task':
      return row.pipelineTag?.toLowerCase().startsWith(f.value) === true
    case 'engine':
      return row.deployments.some(
        (d) => !TERMINAL.has(d.state) && (d.runtime ?? '').toLowerCase().startsWith(f.value),
      )
  }
}

export interface Suggestion {
  kind: 'filter' | 'model' | 'link'
  text: string
  note: string
  hit: [number, number] | null
  /** A model suggestion's band, for its verdict lamp. */
  band?: Band
  action: { type: 'query'; query: string } | { type: 'open'; modelId: string; quant: string | null }
}

export interface SuggestionSection {
  title: string
  items: Suggestion[]
}

const TASKS = [
  'text-generation',
  'image-text-to-text',
  'text-to-speech',
  'automatic-speech-recognition',
  'feature-extraction',
]

const ENGINES = ['vllm', 'sglang', 'tts', 'llamacpp']

/** A node as the `on:` completion describes it. */
export interface SuggestNode {
  id: string
  freeBytes: number | null
  running: number
}

const KEYWORDS: { word: string; note: string }[] = [
  { word: 'fits', note: 'fits right now' },
  { word: 'pinned', note: 'pinned by an admin' },
  { word: 'on:', note: 'runs or is cached on a node' },
  { word: 'quant:', note: 'has a quant' },
  { word: 'task:', note: 'kind of model' },
  { word: 'engine:', note: 'running on an engine' },
]

/** Quant spellings across the list, most common first, so the completion
 *  offers what this cluster actually has rather than a fixed table. */
function quantValues(rows: ModelRow[]): [string, number][] {
  const count = new Map<string, number>()
  for (const r of rows) {
    const seen = new Set<string>()
    for (const q of [r.dtype, r.nativeDtype, r.quantHint]) {
      if (!q) continue
      const low = q.toLowerCase()
      if (seen.has(low)) continue
      seen.add(low)
      count.set(low, (count.get(low) ?? 0) + 1)
    }
  }
  return [...count.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
}

const modelCount = (n: number) => `${n} ${n === 1 ? 'model' : 'models'}`

/** One pickable filter in the toolbar's filter dropdown. */
export interface FilterChoice {
  token: string
  note: string
}

export interface FilterMenuSection {
  title: string
  choices: FilterChoice[]
}

/** Most quants the dropdown lists; the rest are a `quant:` away in the box. */
const MENU_QUANTS = 8

/** The filter dropdown's categories, each with the values this cluster
 *  actually has: the same pools the box's completion draws from, so the two
 *  never offer different things. Empty categories are left out. */
export function filterMenu(ctx: { nodes: SuggestNode[]; rows: ModelRow[] }): FilterMenuSection[] {
  const sections: FilterMenuSection[] = [
    {
      title: 'Status',
      choices: [
        { token: 'fits', note: 'fits right now' },
        { token: 'pinned', note: 'pinned by an admin' },
      ],
    },
    {
      title: 'On a machine',
      choices: ctx.nodes.map((n) => ({
        token: `on:${n.id}`,
        note: `${gib(n.freeBytes)} free · ${n.running} running`,
      })),
    },
    {
      title: 'Quant',
      choices: quantValues(ctx.rows)
        .slice(0, MENU_QUANTS)
        .map(([q, n]) => ({ token: `quant:${q}`, note: modelCount(n) })),
    },
    { title: 'Kind', choices: TASKS.map((t) => ({ token: `task:${t}`, note: '' })) },
    { title: 'Engine', choices: ENGINES.map((e) => ({ token: `engine:${e}`, note: '' })) },
  ]
  return sections.filter((s) => s.choices.length > 0)
}

export function suggest(
  text: string,
  ctx: { nodes: SuggestNode[]; rows: ModelRow[]; pins?: ReadonlySet<string> },
): SuggestionSection[] {
  const link = parseHfLink(text)
  if (link) {
    const known = ctx.rows.some((r) => r.model_id === link.repo)
    return [
      {
        title: 'From this link',
        items: [
          {
            kind: 'link',
            text: link.repo + (link.quant ? ' · ' + link.quant : ''),
            note: known
              ? 'already known to this cluster'
              : `Added ${link.repo} from the link. derate reads its config next, then checks the fit.`,
            hit: null,
            action: { type: 'open', modelId: link.repo, quant: link.quant },
          },
        ],
      },
    ]
  }

  const sections: SuggestionSection[] = []
  const last = /\s$/.test(text) ? '' : (text.split(/\s+/).pop() ?? '')
  const head = text.slice(0, text.length - last.length)

  const kv = valueKey(last)
  if (kv) {
    const prefix = kv.value.toLowerCase()
    const pool: { value: string; note: string }[] =
      kv.key === 'on'
        ? ctx.nodes.map((n) => ({
            value: n.id,
            note: `${gib(n.freeBytes)} free · ${n.running} running`,
          }))
        : kv.key === 'quant'
          ? quantValues(ctx.rows).map(([q, n]) => ({ value: q, note: modelCount(n) }))
          : (kv.key === 'engine' ? ENGINES : TASKS).map((v) => ({ value: v, note: '' }))
    const items = pool
      .filter((c) => c.value.toLowerCase().startsWith(prefix) && c.value.toLowerCase() !== prefix)
      .slice(0, 6)
      .map<Suggestion>((c) => ({
        kind: 'filter',
        text: `${kv.key}:${c.value}`,
        note: c.note,
        hit: null,
        action: { type: 'query', query: `${head}${kv.key}:${c.value} ` },
      }))
    if (items.length) sections.push({ title: 'Finish the filter', items })
  }

  if (last.length >= 2) {
    const low = last.toLowerCase()
    const items = KEYWORDS.filter((k) => k.word.startsWith(low) && k.word !== low).map<Suggestion>(
      (k) => ({
        kind: 'filter',
        text: k.word,
        note: k.note,
        hit: null,
        action: { type: 'query', query: head + k.word + (k.word.endsWith(':') ? '' : ' ') },
      }),
    )
    if (items.length) sections.push({ title: 'Filters', items })
  }

  const q = parseQuery(text)
  if (q.words) {
    const hits = ctx.rows.filter(
      (r) => matches(r, q.words) && q.filters.every((f) => rowPasses(r, f, { pins: ctx.pins })),
    )
    const local = (r: ModelRow) => r.where.includes('running') || r.where.includes('ondisk')
    const ordered = [...hits.filter(local), ...hits.filter((r) => !local(r))].slice(0, 6)
    const items = ordered.map<Suggestion>((r) => {
      const b = band(r)
      const nodes =
        b === 'running'
          ? [
              ...new Set(
                r.deployments.filter((d) => !TERMINAL.has(d.state)).flatMap((d) => d.node_ids),
              ),
            ].join(', ')
          : ''
      const i = r.model_id.toLowerCase().indexOf(q.words)
      return {
        kind: 'model',
        text: r.model_id,
        note: bandWord(b, r.checking).label + (nodes ? ' · ' + nodes : ''),
        hit: i < 0 ? null : [i, i + q.words.length],
        band: b,
        action: { type: 'open', modelId: r.model_id, quant: null },
      }
    })
    if (items.length) sections.push({ title: `Models matching “${q.words}”`, items })
  }
  return sections
}
