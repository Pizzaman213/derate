import { useId, useState } from 'react'
import type { FitResult, ParallelismRequest, PlanAlternative } from '../../api/types'
import { Lamp } from '../../components/Lamp'
import { Select, type SelectOption } from '../../components/Select'
import { Verbatim, VerbatimList } from '../../components/Verbatim'
import { toggle, usedSegments, type BoardRow } from './board'
import { repoOf } from './owner'
import type { ModelSession } from './useModelSession'
import { gib } from './verdictView'

/** A plan shape as the split select names it: degrees above one, `dp` as
 *  replicas because that is what it is to somebody choosing. */
function shapeLabel(a: Pick<PlanAlternative, 'tensor_parallel' | 'pipeline_parallel' | 'expert_parallel' | 'data_parallel'>): string {
  const parts = [
    a.tensor_parallel > 1 ? `TP ${a.tensor_parallel}` : null,
    a.pipeline_parallel > 1 ? `PP ${a.pipeline_parallel}` : null,
    a.expert_parallel > 1 ? `EP ${a.expert_parallel}` : null,
    a.data_parallel > 1 ? `${a.data_parallel} replicas` : null,
  ].filter(Boolean)
  return parts.length ? parts.join(' · ') : 'one rank'
}

const VERDICT_WORD: Record<FitResult['verdict'], string> = {
  fits: 'fits',
  fits_degraded: 'fits, decode is slow',
  wont_fit: 'will not fit',
}

const TERMS: { key: keyof FitResult['breakdown']; label: string }[] = [
  { key: 'weights', label: 'weights' },
  { key: 'kv_cache', label: 'kv cache' },
  { key: 'activations', label: 'activations' },
  { key: 'comm_buffers', label: 'comm buffers' },
  { key: 'replicated', label: 'replicated' },
  { key: 'framework_overhead', label: 'framework overhead' },
]

/** Below this share of the bar a segment draws no label: at ~400px a 5%
 *  segment holds three characters of a name, which reads as a glitch. */
const LABEL_MIN_SHARE = 0.12

/** Whether a machine's bar has memory in use that no deployment accounts for,
 *  so the legend only names the hatching when one is drawn. */
function hasOtherUse(r: BoardRow): boolean {
  const used = r.ceiling != null && r.allocatable != null ? Math.max(0, r.ceiling - r.allocatable) : 0
  return usedSegments(r, used).some((s) => s.other)
}

/** Which machines this model would be served on, as memory bars.
 *
 *  It ranks nothing. The rows are in cluster order, and the verdict comes
 *  from the fit gate over whatever ends up ticked: a score computed here
 *  would be a second answer with no arithmetic behind it. What a bar draws is
 *  three facts the server already reported -- what the machine could hold
 *  idle, what is in use now, and what this plan charges per node. */
export function NodeBars({ s }: { s: ModelSession }) {
  const [why, setWhy] = useState(false)
  const { board, result } = s
  const shown = result ? (result.fit_live ?? result.fit) : null
  const perNode = shown?.breakdown.total ?? null
  const many = board.effective.length > 1 || (result?.plan.node_ids.length ?? 0) > 1

  const alternatives = result?.alternatives ?? []
  const matching = s.degrees
    ? alternatives.findIndex(
        (a) =>
          a.tensor_parallel === (s.degrees?.tensor_parallel ?? 1) &&
          a.pipeline_parallel === (s.degrees?.pipeline_parallel ?? 1) &&
          a.expert_parallel === (s.degrees?.expert_parallel ?? 1),
      )
    : -1
  const splitValue = s.degrees == null ? 'auto' : matching >= 0 ? `alt-${matching}` : 'yours'
  const splitOptions: SelectOption<string>[] = [
    { value: 'auto', label: 'Planner picks' },
    ...alternatives.map((a, i) => ({ value: `alt-${i}`, label: shapeLabel(a) })),
    ...(splitValue === 'yours' && s.degrees
      ? [
          {
            value: 'yours',
            label: shapeLabel({
              tensor_parallel: s.degrees.tensor_parallel ?? 1,
              pipeline_parallel: s.degrees.pipeline_parallel ?? 1,
              expert_parallel: s.degrees.expert_parallel ?? 1,
              data_parallel: s.degrees.data_parallel ?? 1,
            }),
          },
        ]
      : []),
  ]
  const pickSplit = (v: string) => {
    if (v === 'auto') return s.setDegrees(null)
    const a = alternatives[Number(v.slice(4))]
    if (!a) return
    const next: ParallelismRequest = {
      tensor_parallel: a.tensor_parallel,
      pipeline_parallel: a.pipeline_parallel,
      expert_parallel: a.expert_parallel,
      data_parallel: a.data_parallel,
    }
    s.setDegrees(next)
  }

  const ownerValue = s.nodeIds != null ? 'you' : 'planner'
  const pickOwner = (v: 'you' | 'planner') => {
    if (v === 'planner') {
      s.setNodeIds(null)
      s.setDegrees(null)
    } else {
      s.setNodeIds([...board.effective])
    }
  }

  const name = repoOf(s.variant?.repo_id ?? s.subjectId) || s.subjectId

  return (
    <div className="mdl-nodes">
      <div className="mdl-nodes-head">
        <span className="label">Where it runs</span>
        <span className="unit">tick one node, or several to split</span>
        <span className="mdl-nodes-controls">
          {many ? (
            <Select aria-label="How it is split" value={splitValue} options={splitOptions} onChange={pickSplit} />
          ) : null}
          <Select<'you' | 'planner'>
            aria-label="Who picks the nodes"
            value={ownerValue}
            options={[
              { value: 'planner', label: 'Planner picks' },
              { value: 'you', label: 'You picked' },
            ]}
            onChange={pickOwner}
          />
        </span>
      </div>

      <div className="mdl-legend unit">
        <span>
          <i className="mdl-swatch mdl-swatch-used" aria-hidden="true" /> already running
        </span>
        <span>
          <i className="mdl-swatch mdl-swatch-model" aria-hidden="true" />{' '}
          {`this model, ${gib(perNode)} per node`}
        </span>
        {board.rows.some(hasOtherUse) ? (
          <span>
            <i className="mdl-swatch mdl-swatch-other" aria-hidden="true" /> in use, not by a model here
          </span>
        ) : null}
      </div>

      {board.rows.map((r) => (
        <NodeBar
          key={r.nodeId}
          row={r}
          checked={r.ticked || (s.nodeIds == null && r.inPlan)}
          onlyTicked={r.ticked && board.effective.length === 1}
          perNode={perNode}
          name={name}
          onToggle={(on) => {
            // Unticking the last machine would mean "plan on nothing", which
            // the gateway can only answer with a 400; the box is disabled for
            // that reason, and this hands the choice back if it ever fires.
            if (!on && board.effective.length === 1) return s.setNodeIds(null)
            // Sorted inside `toggle`, so two tick orders produce one request
            // body, one answer and one server-side memo key.
            s.setNodeIds(toggle(board, r.nodeId, on))
          }}
        />
      ))}

      {shown && result ? (
        <div className="mdl-why">
          <button
            type="button"
            className="mdl-textbtn"
            aria-expanded={why}
            onClick={() => setWhy((v) => !v)}
          >
            {`Why ${gib(perNode)}?`}
          </button>
          {why ? (
            <div className="mdl-why-body">
              <dl className="mdl-terms">
                {TERMS.filter((t) => shown.breakdown[t.key] > 0).map((t) => (
                  <div key={t.key} className={t.key === shown.limiting_term ? 'limiting' : undefined}>
                    <dt className="unit">{t.label}</dt>
                    <dd className="mono">{gib(shown.breakdown[t.key])}</dd>
                    <dd className="unit">{t.key === shown.limiting_term ? 'limiting' : ''}</dd>
                  </div>
                ))}
              </dl>
              {/* Both budgets, each under its own legend and neither merged
                  into the other: they describe different memory. */}
              {result.fit ? (
                <div className="mdl-budget">
                  <span className="unit">{`Against idle hardware: ${VERDICT_WORD[result.fit.verdict]} —`}</span>
                  <Verbatim text={result.fit.reason} size="unit" />
                </div>
              ) : null}
              {result.fit_live ? (
                <div className="mdl-budget">
                  <span className="unit">{`Against what is free now: ${VERDICT_WORD[result.fit_live.verdict]} —`}</span>
                  <Verbatim text={result.fit_live.reason} size="unit" />
                </div>
              ) : null}
              {/* The planner's own argument for the shape, whole, and every
                  shape it turned down. Planner strings are the product. */}
              <div className="mdl-budget">
                <span className="unit">How it is split —</span>
                <Verbatim text={result.plan.reason} size="unit" />
              </div>
              {result.plan.rejected.length ? <VerbatimList items={result.plan.rejected} /> : null}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}

function NodeBar({
  row,
  checked,
  onlyTicked,
  perNode,
  name,
  onToggle,
}: {
  row: BoardRow
  checked: boolean
  onlyTicked: boolean
  perNode: number | null
  name: string
  onToggle: (on: boolean) => void
}) {
  const id = `${useId()}-node`
  const track = row.ceiling ?? row.allocatable
  const used = row.ceiling != null && row.allocatable != null ? Math.max(0, row.ceiling - row.allocatable) : 0
  const model = row.inPlan && perNode != null ? perNode : 0
  const over = row.allocatable != null && model > row.allocatable
  const pct = (bytes: number) => (track ? `${Math.min(100, (bytes / track) * 100)}%` : '0%')

  let note: JSX.Element
  if (row.allocatable == null) note = <span className="unit">not read yet</span>
  else if (row.inPlan && perNode != null)
    note = over ? (
      <span className="unit mdl-sig-fault">{`short ${gib(perNode - row.allocatable)}`}</span>
    ) : (
      <span className="unit">{`${gib(row.allocatable - perNode)} left`}</span>
    )
  else note = <span className="unit">{`${gib(row.allocatable)} free`}</span>

  return (
    <div className="mdl-node">
      <input
        id={id}
        type="checkbox"
        checked={checked}
        disabled={!row.selectable || onlyTicked}
        onChange={(e) => onToggle(e.target.checked)}
        title={onlyTicked ? 'the last machine: it stays ticked' : undefined}
      />
      <label htmlFor={id} className="mdl-node-name">
        <Lamp
          signal={row.inPlan ? 'live' : 'idle'}
          hollow={!row.inPlan}
          size={7}
          label={row.inPlan ? `${row.name} is in the plan` : (row.unselectableReason ?? `${row.name} is not in the plan`)}
        />
        <span className="mono" title={row.unselectableReason ?? ''}>
          {row.name}
        </span>
      </label>
      <div className="mdl-nbar" aria-hidden="true">
        {track ? (
          <>
            {usedSegments(row, used).map((seg, i) => (
              <i
                key={`${seg.title}-${i}`}
                className={`mdl-nbar-used${seg.other ? ' other' : i % 2 ? ' alt' : ''}`}
                style={{ width: pct(seg.bytes) }}
                title={seg.other ? `in use, not by a model here: ${gib(seg.bytes)}` : `${seg.title}: ${gib(seg.bytes)}`}
              >
                {/* A sliver cannot hold its name; it only showed the first
                    three letters. The title carries it either way. */}
                {seg.other || seg.bytes / track < LABEL_MIN_SHARE ? null : (
                  <span>{`${seg.title} ${gib(seg.bytes)}`}</span>
                )}
              </i>
            ))}
            {model > 0 ? (
              <i
                className={`mdl-nbar-model${over ? ' over' : ''}`}
                style={{ width: pct(Math.min(model, Math.max(0, track - used))) }}
                title={`${name}: ${gib(model)}${over ? ', more than is free' : ''}`}
              >
                <span>{`${name} ${gib(model)}${over ? ' ✕' : ''}`}</span>
              </i>
            ) : null}
          </>
        ) : null}
      </div>
      {note}
      {/* The registry's own sentence for why it cannot vouch for this machine,
          and the board's for why it cannot carry a rank. Shown, never hidden:
          "why isn't my other box being used" deserves an answer here. */}
      {!row.selectable && row.unselectableReason ? (
        <div className="mdl-node-note">
          <Verbatim text={row.unselectableReason} size="unit" />
        </div>
      ) : null}
      {!row.eligible && row.ineligibleReason ? (
        <div className="mdl-node-note">
          <Verbatim text={row.ineligibleReason} size="unit" />
        </div>
      ) : null}
    </div>
  )
}
