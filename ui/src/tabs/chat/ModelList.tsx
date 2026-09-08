import { ENDPOINT_FOR_MODALITY } from '../../api/types'
import type { DeploymentDTO, Modality, ServedModel, TargetKind } from '../../api/types'

export interface ModelRow {
  name: string
  contextLength: number | null
  targetCount: number | null
  kinds: TargetKind[]
  /** Which endpoint family the gateway advertises this name on, straight off
   *  `/v1/models`. It decides the section the row is filed under AND, in
   *  ChatTab, which endpoint the composer posts to -- one field, so the
   *  heading a person reads and the URL their message goes to cannot
   *  disagree. Absent reads as text, which is what every name was before the
   *  field existed. */
  modality: Modality
}

/** Every name this cluster will answer to, wherever it runs.
 *
 *  `/v1/models` is the router's target index, so it lists every name the
 *  gateway will accept -- a local deployment, an allowlisted provider model,
 *  or both. All of them are rows.
 *
 *  This used to be that endpoint's LOCAL half, and the argument for it was
 *  volume: a coordinator pointed at OpenRouter with no allowlist offered
 *  several hundred entries of somebody else's hardware, and the one deployment
 *  actually running here was a line somewhere inside them. That argument died
 *  with the allowlist. A provider now serves nothing until somebody presses
 *  Serve on that model's own page, so every remote name in this list is one
 *  a person chose, and there is no catalogue left to drown in.
 *
 *  What the filter cost was worse than the noise it prevented: a model could
 *  be switched on, appear in /v1/models, answer a curl, and still be
 *  unselectable in the console shipped to talk to it -- with nothing on screen
 *  saying why. `target_kinds` is still what decides, read server-side off the
 *  same index the router dispatches from (`gateway/targets.py`) and never
 *  inferred here; it now decides what a row SAYS rather than whether it
 *  exists.
 *
 *  Locally-served names sort first. Same reason the cluster floor keeps its
 *  machines at the top: this cluster's own hardware is what somebody came to
 *  this screen for, and a provider model is the alternative to it.
 *
 *  A name served locally AND remotely -- a provider model that backs up a
 *  deployment -- is one row, filed with the local half. Which leg a given
 *  request takes is the routing policy's business, not the picker's.
 *
 *  Deployments that are not serving yet are deliberately NOT listed. An
 *  earlier version added them disabled, captioned with their state, on the
 *  grounds that someone who just pressed launch comes here looking for them;
 *  that was reversed. This is a console for talking to a model, every row in
 *  it can answer, and a launch in flight is reported by `emptyNote` and by the
 *  Dashboard rather than by a row that cannot be clicked.
 *
 *  **Speech models are rows now, and that is a reversal.** This filter used to
 *  drop every audio model with the argument that a TTS model is not a chat
 *  model in a state that will change -- which was true of the *model* and was
 *  the wrong thing to be true of. A person picking `Audio8-TTS-Preview-0.6b`
 *  here is not making a mistake about what it is; they are asking to talk to
 *  the thing this cluster is running, and the console's job is to send that
 *  where it will be answered. `ChatTab` reads the modality and posts to
 *  `/v1/audio/speech` instead, and the turn comes back as a player. Excluding
 *  the row only ever moved the dead end earlier.
 *
 *  **`transcription` is in too, and that is the second half of the same
 *  reversal rather than a repeat of it.** It stayed out one round longer with
 *  the argument that an upload is not a message: a Whisper deployment needs a
 *  file picker, there was no surface for one, and a row that cannot be typed
 *  at is precisely the dead end letting speech in had just removed. That
 *  argument was about the *composer*, not about the model, and the answer was
 *  to fix the composer. It now shows a file chooser and a language field when
 *  the picked row answers on `/v1/audio/transcriptions`, so the row can be
 *  acted on and belongs here.
 *
 *  What is left out is nothing. Every modality `/v1/models` can report has a
 *  row and a composer that speaks its endpoint, and if a fifth is ever added
 *  the honest failure is a section with no controls under it -- which is why
 *  `sections()` files by modality rather than by "is it audio". */
export function buildRows(models: ServedModel[]): ModelRow[] {
  return rowsWhere(models, () => true)
}

function rowsWhere(
  models: ServedModel[],
  keep: (m: ServedModel) => boolean,
): ModelRow[] {
  const rows: ModelRow[] = []
  for (const m of models) {
    if (!keep(m)) continue
    rows.push({
      name: m.id,
      contextLength: m.context_length,
      targetCount: m.target_count,
      kinds: m.target_kinds,
      modality: m.modality ?? 'text',
    })
  }
  const local = (r: ModelRow) => (r.kinds.includes('local') ? 0 : 1)
  return rows.sort((a, b) => local(a) - local(b) || a.name.localeCompare(b.name))
}

/** The states worth naming when the list is empty, in the order a reader
 *  wants them. `stopped` is absent on purpose: a deployment somebody stopped
 *  is what "nothing is running" already says. `ready` and `degraded` are
 *  absent because they are rows. */
const NOTABLE: readonly string[] = ['launching', 'planned', 'stopping', 'failed']

/** An empty picker is a fact, not an error, and it has two causes that read
 *  very differently: nothing has ever been launched here, or something was
 *  launched and is not answering yet. Naming the second is the difference
 *  between a blank panel and a panel that tells you to wait. */
export function emptyNote(deployments: DeploymentDTO[]): string {
  const pending = deployments
    .filter((d) => NOTABLE.includes(d.state))
    .sort(
      (a, b) =>
        NOTABLE.indexOf(a.state) - NOTABLE.indexOf(b.state) ||
        a.served_name.localeCompare(b.served_name),
    )
  if (pending.length === 0) return 'No model is running on this cluster.'
  const named = pending.slice(0, 3).map((d) => `${d.served_name} (${d.state})`)
  const rest = pending.length - named.length
  const tail = rest > 0 ? `, and ${rest} more` : ''
  return `Nothing is serving yet: ${named.join(', ')}${tail}.`
}

/** `local`, `remote`, or both -- straight off `target_kinds`, never inferred. */
function describe(r: ModelRow): string {
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
  /** What to say when there are no rows -- `emptyNote` of the live
   *  deployments, computed by the tab that already polls them. */
  note: string
}

/** The order the sections come out in. Same order and the same reasoning as
 *  `MODALITY_ORDER` in tabs/cluster/layout.ts, which groups the floor's entry
 *  plates: text is what nearly every cluster is, so it goes first, and a
 *  reader looking for the unusual one finds it by scanning down rather than
 *  by finding it interleaved. `transcription` sorts last because it was the
 *  latest modality let into this picker, not because it is filtered -- see
 *  `buildRows` above, which keeps it. */
const SECTION_ORDER: readonly Modality[] = ['text', 'embedding', 'speech', 'transcription']

/** The rows, cut into one section per endpoint family, empty sections dropped.
 *
 *  Exported because it is the interesting half and a pure function of the
 *  rows: `rows.check.mjs` asserts a speech model is filed under
 *  `/v1/audio/speech` and never under the chat heading, which is the claim the
 *  section headers make on screen. */
export function sections(rows: ModelRow[]): { modality: Modality; rows: ModelRow[] }[] {
  const out: { modality: Modality; rows: ModelRow[] }[] = []
  for (const modality of SECTION_ORDER) {
    const members = rows.filter((r) => r.modality === modality)
    if (members.length > 0) out.push({ modality, rows: members })
  }
  return out
}

export function ModelList({ rows, selected, onSelect, loading, error, note }: Props) {
  const groups = sections(rows)
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
        <div className="unit">{loading ? 'loading' : note}</div>
      ) : null}

      {/* One heading per endpoint family, naming the route rather than the
          modality: `POST /v1/audio/speech` is the thing a person can act on --
          it is the URL their message is about to go to, and the same string
          the cluster floor's entry plate carries for that group. "Speech"
          would be a category name they would then have to translate.

          Rendered even when there is only one section, unlike the floor's
          plates. There the drawing is of the routing and a second plate would
          be a second claim about it; here it is a label on a list, and a
          picker that says which endpoint its one family answers on is
          strictly more informative than a bare list of names. */}
      {groups.map((g) => (
        <div key={g.modality} className="chatgroup">
          <div className="chatgrouphead unit mono" aria-hidden>
            POST {ENDPOINT_FOR_MODALITY[g.modality]}
          </div>
          {g.rows.map((r) => (
            <button
              key={r.name}
              type="button"
              aria-pressed={selected === r.name}
              // The heading is aria-hidden, so the endpoint has to reach a
              // screen reader through the row itself or it is a visual-only
              // grouping -- which for "where does this message go" is not a
              // decoration.
              aria-label={`${r.name}, POST ${ENDPOINT_FOR_MODALITY[r.modality]}`}
              onClick={() => onSelect(r.name)}
            >
              <span className="mono" style={{ display: 'block' }}>
                {r.name}
              </span>
              <span className="unit">{describe(r)}</span>
            </button>
          ))}
        </div>
      ))}
    </div>
  )
}
