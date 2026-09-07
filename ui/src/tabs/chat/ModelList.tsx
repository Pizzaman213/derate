import { isAudio } from '../../api/types'
import type { DeploymentDTO, ServedModel, TargetKind } from '../../api/types'

export interface ModelRow {
  name: string
  /** Present in `/v1/models`: the gateway will accept it as `model` right now. */
  servable: boolean
  contextLength: number | null
  targetCount: number | null
  kinds: TargetKind[]
  /** Deployment state, set only on a row that is not servable. */
  state: string | null
}

/** `/v1/models` lists every model that can answer, and nothing else. A
 *  deployment still launching, or one that failed, is absent from it — and is
 *  exactly what someone who just pressed launch is looking for here.
 *
 *  So the list is the union of the two: the endpoint first, then any deployed
 *  `served_name` the endpoint did not mention, shown disabled and labelled with
 *  the state that explains why it cannot be picked. The alternative is a list
 *  that silently omits a model the Dashboard says exists.
 *
 *  Audio models are the one thing left out entirely. A provider catalog is
 *  ingested wholesale, so `tts-1` and `whisper-1` arrive here as ordinary
 *  entries; they cannot answer a chat request, and offering one produces a
 *  refusal from the gateway that the picker could have prevented. They are
 *  absent rather than disabled because this is a chat console, and a TTS model
 *  is not a chat model in a state that will change. */
export function buildRows(
  models: ServedModel[],
  deployments: DeploymentDTO[],
): ModelRow[] {
  const rows = new Map<string, ModelRow>()
  for (const m of models) {
    if (isAudio(m.modality)) continue
    rows.set(m.id, {
      name: m.id,
      servable: true,
      contextLength: m.context_length,
      targetCount: m.target_count,
      kinds: m.target_kinds,
      state: null,
    })
  }
  for (const d of deployments) {
    // Several deployments can share one served_name. The first that is not
    // already servable is enough to say the name exists and cannot be used.
    if (rows.has(d.served_name) || isAudio(d.modality)) continue
    rows.set(d.served_name, {
      name: d.served_name,
      servable: false,
      contextLength: d.context_length,
      targetCount: null,
      kinds: [],
      state: d.state,
    })
  }
  return [...rows.values()].sort((a, b) => {
    if (a.servable !== b.servable) return a.servable ? -1 : 1
    return a.name.localeCompare(b.name)
  })
}

/** `local`, `remote`, or both — straight off `target_kinds`, never inferred. */
function describe(r: ModelRow): string {
  if (!r.servable) return r.state ?? 'not serving'
  const kinds = r.kinds.length > 0 ? r.kinds.join(' + ') : '—'
  const targets =
    r.targetCount == null ? '— targets' : r.targetCount === 1 ? '1 target' : `${r.targetCount} targets`
  // Thousands separators: a context length is the one figure here long enough
  // to misread without them.
  const ctx = r.contextLength == null ? '—' : r.contextLength.toLocaleString()
  return `${kinds} · ${targets} · ${ctx} ctx`
}

interface Props {
  rows: ModelRow[]
  selected: string | null
  onSelect: (name: string) => void
  loading: boolean
  error: Error | null
}

export function ModelList({ rows, selected, onSelect, loading, error }: Props) {
  return (
    <div className="chatlist">
      <h2 className="label">Models</h2>
      <hr style={{ margin: '6px 0 var(--s-2)' }} />

      {error ? (
        <div className="unit" style={{ color: 'var(--fault)', whiteSpace: 'pre-wrap' }}>
          {error.message}
        </div>
      ) : null}

      {!error && rows.length === 0 ? (
        <div className="unit">
          {loading ? 'loading' : 'The gateway is serving no models.'}
        </div>
      ) : null}

      {rows.map((r) => (
        <button
          key={r.name}
          type="button"
          aria-pressed={selected === r.name}
          disabled={!r.servable}
          onClick={() => onSelect(r.name)}
        >
          <span className="mono" style={{ display: 'block' }}>
            {r.name}
          </span>
          <span className="unit">{describe(r)}</span>
        </button>
      ))}
    </div>
  )
}
