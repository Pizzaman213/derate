import { Lamp } from '../../components/Lamp'
import { Verbatim } from '../../components/Verbatim'
import { gbytes } from '../../format'
import { BandHeading, useBandCollapse } from './BandSection'
import { band, deploymentSignal, rowFacts } from './rows'
import type { Band, Group, ModelRow } from './rows'

/** The list: fit-banded sections, one row per model.
 *
 *  Rows are real `<button>`s rather than `role="button"` divs with hand-rolled
 *  key handling. That was two bugs waiting -- Enter and Space were reimplemented
 *  per call site, and the focus ring `base.css` gives every native control was
 *  never reaching them.
 *
 *  Every fact on a row is read, not derived. The lamp is the fit gate's verdict,
 *  the sentence under a refusal is the fit gate's own, and a row with no verdict
 *  draws a hollow lamp and says so rather than guessing from the model's name. */
export function CatalogList({
  groups,
  loading,
  error,
  emptyNote,
  onOpen,
  selectedId,
  compact,
  expandAll = false,
}: {
  groups: Group[]
  loading: boolean
  error: Error | null
  emptyNote: string
  onOpen: (row: ModelRow) => void
  /** The model open in the detail pane, marked `aria-current` in the list. */
  selectedId?: string | null
  /** Master-pane width. Four columns do not fit in 360px, so the row stacks:
   *  name on one line, everything describing it on the next. */
  compact?: boolean
  /** Force every collapsible band open. True while a search is running, so a
   *  needle that matched inside the collapsed band shows what it matched. */
  expandAll?: boolean
}) {
  const collapse = useBandCollapse(expandAll)
  if (error) {
    return (
      <p
        className="label"
        style={{ fontWeight: 400, color: 'var(--fault)', whiteSpace: 'pre-wrap' }}
      >
        {error.message}
      </p>
    )
  }
  if (!groups.length) {
    return <p className="unit">{loading ? 'Loading…' : emptyNote}</p>
  }

  return (
    <div>
      {groups.map((g) => (
        <section key={`${g.band}::${g.title}`}>
          {/* The count is on the heading, not implied by the rows: a band that
              does not say its size reads as a short one. */}
          <BandHeading
            group={g}
            collapsible={collapse.collapsible(g.band)}
            open={collapse.isOpen(g.band)}
            onToggle={() => collapse.toggle(g.band)}
          />
          {collapse.isOpen(g.band)
            ? g.rows.map((row) => (
                <Row
                  key={row.key}
                  row={row}
                  onOpen={onOpen}
                  current={row.model_id === selectedId}
                  compact={compact}
                />
              ))
            : null}
        </section>
      ))}
    </div>
  )
}

const LAMP: Record<Band, { signal: 'live' | 'warn' | 'fault' | 'idle'; label: string }> = {
  // `running` is a placeholder: a serving row reads its lamp off the
  // deployment's own state below, because "ready" and "degraded" are different
  // things and one colour for both would hide a failing launch.
  running: { signal: 'live', label: 'running here' },
  fits: { signal: 'live', label: 'fits' },
  degraded: { signal: 'warn', label: 'loads, but decode is slow' },
  wont: { signal: 'fault', label: 'will not fit' },
  unchecked: { signal: 'idle', label: 'not checked' },
  elsewhere: { signal: 'idle', label: 'runs on a provider, not here' },
  // Not a verdict either. Nothing here refused this model -- nobody has asked
  // it to serve it, which is a fact about a choice and not about the hardware.
  unserved: { signal: 'idle', label: 'not served' },
}

const COLUMNS = 'minmax(0, 1.2fr) minmax(0, 1.6fr) auto auto'
const COLUMNS_COMPACT = 'minmax(0, 1fr) auto'

function Row({
  row,
  onOpen,
  current,
  compact,
}: {
  row: ModelRow
  onOpen: (row: ModelRow) => void
  current?: boolean
  compact?: boolean
}) {
  const b = band(row)
  const live = row.deployments.find((d) => d.state === 'ready') ?? row.deployments[0]
  const lamp =
    b === 'running' && live
      ? { signal: deploymentSignal(live.state), label: live.state }
      : row.checking
        // Not a verdict and not a colour: a checking row has no answer yet, so
        // the lamp keeps the shape "unchecked" draws and only the word changes.
        ? { signal: 'idle' as const, label: 'checking' }
        : LAMP[b]
  const cells = compact ? (
    <>
      <span style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 }}>
        {lamp.label ? <Lamp {...lamp} hollow={lamp.signal === 'idle'} /> : null}
        <span className="mono" style={{ wordBreak: 'break-all' }}>
          {row.label}
        </span>
      </span>
      <span className="num" style={{ whiteSpace: 'nowrap' }}>
        <Numbers row={row} />
      </span>
      {/* Second line, spanning both columns: everything that describes the row
          rather than names it. */}
      <span className="unit" style={{ gridColumn: '1 / -1', wordBreak: 'break-all' }}>
        <Facts row={row} />
      </span>
    </>
  ) : (
    <>
      <span
        style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 }}
      >
        {lamp.label ? <Lamp {...lamp} hollow={lamp.signal === 'idle'} /> : null}
        <span className="mono" style={{ wordBreak: 'break-all' }}>
          {row.label}
        </span>
      </span>

      <span className="unit" style={{ wordBreak: 'break-all' }}>
        <Facts row={row} />
      </span>

      <span className="num" style={{ whiteSpace: 'nowrap' }}>
        <Numbers row={row} />
      </span>

      <span className="unit" style={{ whiteSpace: 'nowrap' }}>
        {live
          ? live.state
          : row.checking
            ? 'checking…'
            : row.remoteOnly
              ? 'details ▸'
              : 'quantizations ▸'}
      </span>
    </>
  )

  // Every row opens, provider-only ones included. There is nothing local to
  // resolve them against, but "who serves this, at what context, at what
  // price, and why is there no local verdict" is a real answer and the pane
  // gives it. A row that looked identical to its neighbours and silently did
  // nothing when clicked was the worse option.
  return (
    <>
      <button
        type="button"
        className="deprow"
        aria-current={current ? 'true' : undefined}
        style={{
          gridTemplateColumns: compact ? COLUMNS_COMPACT : COLUMNS,
          rowGap: 2,
          width: '100%',
          textAlign: 'left',
          // Only the three sides a button adds and a row does not. `border: 0`
          // here would also take out `.deprow`'s bottom rule, and setting
          // `background` inline would beat `.deprow:hover` and kill the wash --
          // `font`, `color` and the transparent ground already come from the
          // bare `button` rule in base.css.
          borderTop: 0,
          borderLeft: 0,
          borderRight: 0,
          borderRadius: 0,
        }}
        onClick={() => onOpen(row)}
      >
        {cells}
      </button>
      {/* The refusal, in the gate's own words, under the row it refused. Only
          for a refusal: a "fits" sentence repeats what the lamp already said. */}
      {(row.verdict === 'wont_fit' || row.verdict === null) && row.reason ? (
        <div style={{ padding: '0 var(--s1) 8px' }}>
          <Verbatim text={row.reason} size="unit" />
          {/* The refusal is about ONE set of weights -- the checkpoint as it
              ships. Most refused models are published at several schemes, and
              the ladder on the model's own page is the only place those are
              sized repositories rather than a scheme name: `rowFacts` says
              "would fit at q2_k" because `capacity._walk` is arithmetic and
              never asks whether anybody built that. Enumerating the ladder
              costs a hub search per model, which is what `MAX_CATALOG_MODELS`
              exists to prevent, so it stays one click away and is announced
              instead of run. */}
          {row.verdict === 'wont_fit' ? (
            <button
              type="button"
              className="unit"
              onClick={() => onOpen(row)}
              style={{
                marginTop: 4,
                padding: 0,
                border: 0,
                background: 'transparent',
                textDecoration: 'underline',
                cursor: 'pointer',
              }}
            >
              See the quantizations published for this model
            </button>
          ) : null}
        </div>
      ) : null}
    </>
  )
}

/** The descriptive half of the row. Assembled here rather than upstream so the
 *  list can still sort and band on the parts. */
function Facts({ row }: { row: ModelRow }) {
  // The strings come from `rows.ts::rowFacts`, which is pure and therefore
  // reachable by `rows.check.mjs`. What stays here is what cannot be a string:
  // the gated warning and the cache pill are styled spans.
  const parts = rowFacts(row)

  return (
    <>
      {parts.join(' · ')}
      {row.gated ? (
        <>
          {parts.length ? ' · ' : ''}
          <span style={{ color: 'var(--warn)' }}>gated</span>
        </>
      ) : null}
      {row.cachedOn.length ? (
        <>
          {' '}
          {/* Already on disk: the first launch does not have to pull it. Said
              as a badge rather than a second green lamp, so the one lamp on
              the row keeps meaning fit and nothing else.
              The size is on the badge because presence is not completeness --
              a repository whose cache holds only `config.json` is 0.0 GiB and
              would otherwise read as a downloaded model. There is no expected
              size for a base repository to check against, so the honest thing
              is to show what is actually there. */}
          <span className="pill">
            on {row.cachedOn.length === 1 ? row.cachedOn[0] : `${row.cachedOn.length} nodes`}
            {row.bytesOnDisk != null ? ` · ${gbytes(row.bytesOnDisk)} GiB` : ''}
          </span>
        </>
      ) : null}
    </>
  )
}

/** The numeric half. Measured or absent -- a size nobody measured is not drawn. */
function Numbers({ row }: { row: ModelRow }) {
  const bits: string[] = []
  if (row.total_params != null) bits.push(`${(row.total_params / 1e9).toFixed(1)}B`)
  // The on-disk figure lives on the "on <node>" pill, which every row with
  // cached weights now carries -- one place, not two.
  if (row.predicted_decode_tps != null) bits.push(`${row.predicted_decode_tps.toFixed(0)} tok/s`)
  return <>{bits.join(' · ')}</>
}
