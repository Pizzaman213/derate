import type { HubFilters } from './useModelList'
import { Lamp } from '../../components/Lamp'
import { OwnerMark, useAvatars } from './OwnerMark'
import { ownerOf, repoOf } from './owner'
import { band } from './rows'
import type { ModelRow } from './rows'
import { bandWord, formatParams } from './verdictView'

export interface HubCardsProps {
  rows: ModelRow[]
  query: string
  selectedId: string | null
  onOpen(row: ModelRow): void
  loading: boolean
  onlyFits: boolean
  onOnlyFits(v: boolean): void
  source: HubSource
  onSource(v: HubSource): void
  kind: string
  onKind(v: string): void
}

/** Where hub results come from: original publishers, or Unsloth's quants. */
export type HubSource = 'original' | 'unsloth'

/** The hub's own pipeline tags, matched exactly. A kind is a set of tags
 *  because the hub files one kind of model under several names; `task` is the
 *  one the hub search is asked for. */
export const KINDS: { id: string; label: string; task?: string; tags: string[] }[] = [
  { id: 'all', label: 'All', tags: [] },
  { id: 'text', label: 'Text', task: 'text-generation', tags: ['text-generation'] },
  {
    id: 'vision',
    label: 'Vision',
    task: 'image-text-to-text',
    tags: ['image-text-to-text', 'image-to-text'],
  },
  {
    id: 'speech',
    label: 'Speech',
    task: 'text-to-speech',
    tags: ['text-to-speech', 'automatic-speech-recognition'],
  },
  {
    id: 'embedding',
    label: 'Embedding',
    task: 'feature-extraction',
    tags: ['feature-extraction', 'sentence-similarity', 'text-ranking'],
  },
]

/** What the hub search is asked for, from the view's controls. */
export function hubFilters(source: HubSource, kind: string): HubFilters {
  const task = KINDS.find((k) => k.id === kind)?.task
  return {
    ...(source === 'unsloth' ? { author: 'unsloth' } : {}),
    ...(task ? { task } : {}),
  }
}

/** The hub view: cards to browse, because a search across the hub is
 *  browsing, and a card has room for the facts people choose by there --
 *  who published it, what kind it is, how many pulled it. The verdict still
 *  sits on every card, read off the same row the list draws. */
export function HubCards({
  rows,
  query,
  selectedId,
  onOpen,
  loading,
  onlyFits,
  onOnlyFits,
  source,
  onSource,
  kind,
  onKind,
}: HubCardsProps) {
  useAvatars()
  const tags = KINDS.find((k) => k.id === kind)?.tags ?? []
  const shown = rows.filter((r) => {
    if (tags.length && !(r.pipelineTag && tags.includes(r.pipelineTag))) return false
    if (onlyFits) {
      const b = band(r)
      if (b !== 'running' && b !== 'fits' && b !== 'degraded') return false
    }
    return true
  })
  const q = query.trim()

  return (
    <div className="mdl-hub">
      <div className="mdl-hub-filters">
        <div className="chips mdl-chips" role="group" aria-label="Source">
          {(
            [
              ['original', 'Original repos'],
              ['unsloth', 'Unsloth quants'],
            ] as const
          ).map(([id, label]) => (
            <button key={id} type="button" aria-pressed={source === id} onClick={() => onSource(id)}>
              {id === 'unsloth' ? <OwnerMark owner="unsloth" variant="inline" /> : null}
              {label}
            </button>
          ))}
        </div>
        <span className="unit">
          {source === 'unsloth'
            ? 'GGUF and dynamic 4-bit copies from unsloth'
            : 'Repos from the publishers themselves'}
        </span>
        <div className="chips mdl-chips" role="group" aria-label="Kind of model">
          {KINDS.map((k) => (
            <button
              key={k.id}
              type="button"
              aria-pressed={kind === k.id}
              onClick={() => onKind(k.id)}
            >
              {k.label}
            </button>
          ))}
        </div>
        <label className="mdl-check unit">
          <input type="checkbox" checked={onlyFits} onChange={(e) => onOnlyFits(e.target.checked)} />
          Only what fits this cluster
        </label>
      </div>

      {shown.length ? (
        <div className="mdl-hub-grid">
          {shown.map((row) => {
            const owner = ownerOf(row.model_id)
            const word = bandWord(band(row), row.checking)
            const params = formatParams(row.total_params)
            return (
              <button
                key={row.key}
                type="button"
                className="mdl-hub-card"
                aria-current={row.model_id === selectedId ? 'true' : undefined}
                onClick={() => onOpen(row)}
              >
                <span className="mdl-hub-card-head">
                  <OwnerMark owner={owner} variant="card" />
                  <span className="mdl-hub-card-name">
                    <span className="mdl-hub-card-repo mdl-ellipsis">{repoOf(row.model_id)}</span>
                    {owner ? <span className="unit mdl-ellipsis">{owner}</span> : null}
                  </span>
                </span>
                {row.pipelineTag || params ? (
                  <span className="mdl-pills">
                    {row.pipelineTag ? <span className="mdl-pill">{row.pipelineTag}</span> : null}
                    {params ? <span className="mdl-pill">{params}</span> : null}
                  </span>
                ) : null}
                <span className="unit mono">
                  {row.downloads?.toLocaleString() ?? '—'} dl · {row.likes?.toLocaleString() ?? '—'}{' '}
                  likes
                  {row.lastModified ? ` · updated ${new Date(row.lastModified).toLocaleDateString()}` : null}
                </span>
                <span className="mdl-hub-card-foot">
                  <span className="mdl-hub-card-verdict">
                    <Lamp signal={word.signal} hollow={word.signal === 'idle'} label={word.label} />
                    <span className="unit">{word.label}</span>
                  </span>
                  {row.dtype ? <span className="unit mono">{row.dtype}</span> : null}
                </span>
              </button>
            )
          })}
        </div>
      ) : (
        <p className="unit">
          {loading
            ? 'Asking Hugging Face…'
            : q
              ? `Nothing on the hub matches ${q}.`
              : 'Hugging Face sent nothing for this source and kind.'}
        </p>
      )}
    </div>
  )
}
