import { useState } from 'react'
import type { ReactNode } from 'react'
import type { CapacityReport, MetricsDeploymentFrame, ModelSearchResponse } from '../../api/types'
import { Icon } from '../../components/Icon'
import { Lamp } from '../../components/Lamp'
import type { Resource } from '../../state/backend'
import { OwnerMark, useAvatars } from './OwnerMark'
import { ownerOf, repoOf } from './owner'
import { BAND_TITLE, TERMINAL, band, bandSubtitle, isCollapsibleBand, rowFacts } from './rows'
import type { Band, CapacityIndex, Group, ModelRow } from './rows'
import { AUTO_CHECK, machinesPhrase } from './useModelList'
import type { ListView } from './useModelList'
import { bandWord, formatParams, gib } from './verdictView'

export interface ModelRowsProps {
  groups: Group[]
  /** Rows an admin pinned, drawn first under `Pinned`. */
  pinnedGroup?: Group | null
  hubOnlyGroup: Group | null
  pins?: ReadonlySet<string>
  onTogglePin?(modelId: string): void
  view: ListView
  words: string
  selectedId: string | null
  onOpen(row: ModelRow): void
  loading: boolean
  fatal: Error | null
  emptyNote: string
  wontCount: number
  showWont: boolean
  onShowWont(v: boolean): void
  caption: ReactNode
  /** The one metrics subscription's frames, by deployment id, so a running
   *  row can state what its engine is doing without subscribing itself. */
  live?: ReadonlyMap<string, MetricsDeploymentFrame>
}

export interface ListCaptionProps {
  capacity: Resource<CapacityReport>
  cap: CapacityIndex
  problems: { name: string; message: string }[]
  hits: ModelSearchResponse | null
}

export function emptyNote(query: string): string {
  const q = query.trim()
  if (q) return `Nothing here matches ${q}.`
  return 'Nothing is running, cached, curated, or published by a provider yet.'
}

/** What sits above the rows: where the verdicts came from, and which feeds
 *  failed. Its own component so the hub view can show it too. */
export function ListCaption({ capacity, cap, problems, hits }: ListCaptionProps) {
  return (
    <>
      {/* Where the verdicts came from, said once at the top rather than
          repeated on every row.

          Read off the REPORT, never off anything this screen holds. There
          are no fields here any more, but the rule is older than that and
          is the reason there are none: a caption built from what was asked
          names a context the verdicts beneath it were not taken at, for as
          long as the answer takes to arrive.

          `context` is null when nobody named one, which is the default
          path. The gate then chose per model -- so the caption says which
          machine and which budget, and each row carries its own number. */}
      <p className="unit" style={{ margin: '0 0 var(--space-6)' }}>
        {capacity.data && cap.basis
          ? `Fit on ${machinesPhrase(capacity.data)}, ` +
            (capacity.data.context
              ? `at ${capacity.data.context.toLocaleString()} context and ` +
                `${capacity.data.concurrency} ` +
                `${capacity.data.concurrency === 1 ? 'sequence' : 'sequences'}, `
              : 'each at the largest context it can hold up to its own window, ') +
            (capacity.data.budget_basis === 'host_memory'
              ? capacity.data.local_serving === false
                ? 'against host memory — no GPU was found, so nothing here can be ' +
                  'served from this machine. '
                : 'against host memory — no GPU was found, so these are sized ' +
                  'for the CPU runtime. '
              : cap.basis === 'live'
                ? 'against what the nodes can hand out right now. '
                : 'against the idle-hardware ceiling — there is no live memory reading. ') +
            `The first ${AUTO_CHECK} rows with no verdict are checked as the ` +
            'list settles; the rest say “not checked” rather than guessing.'
          : 'No capacity answer yet, so nothing here is banded by fit.'}
      </p>

      {/* A feed failing greys nothing and empties nothing. It says which
          feed, and why, in that server's own words. */}
      {problems.map((p) => (
        <p
          key={p.name}
          className="label"
          style={{
            fontWeight: 400,
            color: 'var(--warn)',
            whiteSpace: 'pre-wrap',
            margin: '0 0 var(--space-6)',
          }}
        >
          {p.name}: {p.message}
        </p>
      ))}

      {hits ? (
        <>
          {Object.entries(hits.sources)
            .filter(([, v]) => !v.ok && v.note)
            .map(([name, v]) => (
              <p
                key={name}
                className="label"
                style={{
                  fontWeight: 400,
                  color: 'var(--warn)',
                  whiteSpace: 'pre-wrap',
                  margin: '0 0 var(--space-6)',
                }}
              >
                {name}: {v.note}
              </p>
            ))}
          {hits.notes.map((n) => (
            <p key={n} className="unit" style={{ margin: '0 0 var(--space-6)' }}>
              {n}
            </p>
          ))}
        </>
      ) : null}
    </>
  )
}

/** The list: fit-banded sections, one row per model, six columns.
 *
 *  Rows are real `<button type="button">`s, so Enter, Space and the focus
 *  ring come from the platform rather than from per-row key handling.
 *
 *  Every fact on a row is read, not derived: the word is the fit gate's
 *  verdict, the size is what the gate sized, and a row with no verdict draws
 *  a hollow lamp and says so rather than guessing from the model's name. */
export function ModelRows({
  groups,
  pinnedGroup = null,
  hubOnlyGroup,
  pins,
  onTogglePin,
  view,
  words,
  selectedId,
  onOpen,
  loading,
  fatal,
  emptyNote,
  wontCount,
  showWont,
  onShowWont,
  caption,
  live,
}: ModelRowsProps) {
  useAvatars()
  // Which collapsible bands somebody opened. A search forces them open
  // without touching this, so clearing it puts the band back as it was --
  // a needle that matched inside a collapsed band has to show what it found.
  const [opened, setOpened] = useState<ReadonlySet<Band>>(() => new Set<Band>())
  const searching = words !== ''
  const isOpen = (b: Band) => !isCollapsibleBand(b) || searching || opened.has(b)
  const toggle = (b: Band) =>
    setOpened((prev) => {
      const next = new Set(prev)
      if (next.has(b)) next.delete(b)
      else next.add(b)
      return next
    })

  const empty = !groups.length && !hubOnlyGroup && !pinnedGroup
  const pinProps = (id: string) =>
    onTogglePin ? { pinned: pins?.has(id) === true, onTogglePin } : {}

  return (
    <div className="mdl-list">
      {caption}

      {fatal ? (
        <p className="label mdl-fatal">{fatal.message}</p>
      ) : empty ? (
        <p className="unit">{loading ? 'Loading…' : emptyNote}</p>
      ) : (
        <div className="mdl-table">
          <div className="mdl-row-grid mdl-thead" aria-hidden="true">
            <span>verdict</span>
            <span>model</span>
            <span className="mdl-num">size</span>
            <span className="mdl-num">decode / seq</span>
            <span>layout</span>
            <span className="mdl-num">per node</span>
          </div>

          {pinnedGroup ? (
            <section>
              <div className="mdl-group-head">
                <span className="mdl-group-title">Pinned</span>
                <span className="unit">{pinnedGroup.rows.length.toLocaleString()}</span>
              </div>
              {pinnedGroup.rows.map((row) => (
                <Row
                  key={row.key}
                  row={row}
                  live={live}
                  words={words}
                  selected={row.model_id === selectedId}
                  onOpen={onOpen}
                  {...pinProps(row.model_id)}
                />
              ))}
            </section>
          ) : null}

          {groups.map((g) => {
            const collapsible = isCollapsibleBand(g.band) && !searching
            const open = isOpen(g.band)
            return (
              <section key={`${g.band}::${g.title}`}>
                <GroupHead
                  group={g}
                  collapsible={collapsible}
                  open={open}
                  onToggle={() => toggle(g.band)}
                />
                {open
                  ? g.rows.map((row) => (
                      <Row
                        key={row.key}
                        row={row}
                        live={live}
                        words={words}
                        selected={row.model_id === selectedId}
                        onOpen={onOpen}
                        {...pinProps(row.model_id)}
                      />
                    ))
                  : null}
              </section>
            )
          })}

          {hubOnlyGroup ? (
            <section className="mdl-hubgroup">
              <div className="mdl-group-head">
                <span className="mdl-group-title">
                  On Hugging Face
                  <span className="unit"> · not downloaded yet, checked against your nodes</span>
                </span>
                <span className="unit">{hubOnlyGroup.rows.length.toLocaleString()}</span>
              </div>
              {hubOnlyGroup.rows.map((row) => (
                <Row
                  key={row.key}
                  row={row}
                  live={live}
                  words={words}
                  selected={row.model_id === selectedId}
                  onOpen={onOpen}
                  {...pinProps(row.model_id)}
                />
              ))}
            </section>
          ) : null}
        </div>
      )}

      {view === 'fits' && wontCount > 0 ? (
        <p className="unit mdl-wont">
          {wontCount.toLocaleString()} more need more memory than all nodes have together.{' '}
          <button type="button" className="mdl-textbtn" onClick={() => onShowWont(!showWont)}>
            {showWont ? 'Hide them' : 'Show them'}
          </button>
        </p>
      ) : null}
    </div>
  )
}

/** A band's heading: a plain line, or a disclosure when the band collapses.
 *  The count is on the heading because a band that does not say its size
 *  reads as a short one. */
function GroupHead({
  group,
  collapsible,
  open,
  onToggle,
}: {
  group: Group
  collapsible: boolean
  open: boolean
  onToggle: () => void
}) {
  const subtitle = bandSubtitle(group)
  const title = BAND_TITLE[group.band] ?? group.title
  const count = group.rows.length.toLocaleString()
  const body = (
    <>
      <span className="mdl-group-title">
        {collapsible ? (
          <Icon name={open ? 'chevron-down' : 'chevron-right'} size={12} decorative />
        ) : null}
        {title}
        {subtitle ? <span className="unit"> · {subtitle}</span> : null}
      </span>
      <span className="unit">{count}</span>
    </>
  )
  if (!collapsible) return <div className="mdl-group-head">{body}</div>
  return (
    <button type="button" className="mdl-group-head mdl-group-toggle" aria-expanded={open} onClick={onToggle}>
      {body}
    </button>
  )
}

/** The id with the first occurrence of the words marked. */
function Marked({ text, words }: { text: string; words: string }) {
  if (!words) return <>{text}</>
  const at = text.toLowerCase().indexOf(words.toLowerCase())
  if (at < 0) return <>{text}</>
  return (
    <>
      {text.slice(0, at)}
      <mark>{text.slice(at, at + words.length)}</mark>
      {text.slice(at + words.length)}
    </>
  )
}

function Row({
  row,
  words,
  selected,
  onOpen,
  pinned,
  onTogglePin,
  live,
}: {
  row: ModelRow
  live?: ReadonlyMap<string, MetricsDeploymentFrame>
  words: string
  selected: boolean
  onOpen: (row: ModelRow) => void
  pinned?: boolean
  onTogglePin?: (modelId: string) => void
}) {
  const b = band(row)
  const word = bandWord(b, row.checking)
  const running = b === 'running'
  const layout = running
    ? [...new Set(row.deployments.flatMap((d) => d.node_ids))].join(', ')
    : (row.dtype ?? row.nativeDtype ?? '')
  // The publisher moves to the muted line: the mark beside the name already
  // says who, and at the split's default width the full id leaves the repo
  // name -- the part that tells two Qwen rows apart -- three characters.
  const owner = ownerOf(row.model_id)
  const facts = [owner, ...rowFacts(row)].filter(Boolean).join(' · ')
  // Its own line: appended to the facts above, the ellipsis ate every live
  // figure at the split's default width.
  const liveLine = liveFacts(row, live).join(' · ')
  const button = (
    <button
      type="button"
      className="mdl-row mdl-row-grid"
      aria-current={selected ? 'true' : undefined}
      onClick={() => onOpen(row)}
    >
      <span className="mdl-cell-verdict">
        <Lamp signal={word.signal} hollow={word.signal === 'idle'} label={word.label} />
        <span className="unit">{word.label}</span>
      </span>
      <span className="mdl-cell-model">
        <span className="mdl-cell-name">
          <OwnerMark owner={owner} variant="inline" reserve />
          <span className="mono mdl-id" title={row.model_id}>
            <Marked text={repoOf(row.model_id) || row.model_id} words={words} />
          </span>
        </span>
        {facts ? <span className="unit mdl-ellipsis mdl-facts">{facts}</span> : null}
        {liveLine ? <span className="unit mdl-ellipsis mdl-facts">{liveLine}</span> : null}
      </span>
      <span className="unit mdl-num">{formatParams(row.total_params)}</span>
      <span className="unit mdl-num">
        {row.predicted_decode_tps == null
          ? '—'
          : row.predicted_decode_tps.toFixed(row.predicted_decode_tps < 10 ? 1 : 0)}
      </span>
      <span className="unit mono mdl-ellipsis">{layout}</span>
      <span className="unit mdl-num">{running ? '—' : gib(row.needBytes)}</span>
    </button>
  )
  if (!onTogglePin) return button
  // A sibling of the row button, never inside it: a button may not hold one.
  return (
    <div className="mdl-row-wrap">
      {button}
      <button
        type="button"
        className="mdl-pin"
        aria-label={`${pinned ? 'Unpin' : 'Pin'} ${row.model_id}`}
        aria-pressed={pinned === true}
        onClick={() => onTogglePin(row.model_id)}
      >
        <Icon name="pin" size={14} decorative />
      </button>
    </div>
  )
}

/** What a running row's engine is doing now, from the metrics frame: kv cache
 *  in use, recent first token, requests running. A part with no reading is
 *  left out rather than drawn as a zero. */
function liveFacts(row: ModelRow, live: ReadonlyMap<string, MetricsDeploymentFrame> | undefined): string[] {
  if (!live) return []
  const d = row.deployments.find((x) => !TERMINAL.has(x.state) && live.has(x.deployment_id))
  const m = d ? live.get(d.deployment_id) : undefined
  if (!m) return []
  const parts: string[] = []
  if (m.load?.kv_cache_usage != null) parts.push(`kv ${Math.round(m.load.kv_cache_usage * 100)}%`)
  if (m.ttft_ms != null) parts.push(`first token ${Math.round(m.ttft_ms)} ms`)
  if (m.load?.requests_running != null) parts.push(`${m.load.requests_running} running`)
  return parts
}
