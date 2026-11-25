import { useId, useState } from 'react'
import type { FitResult, ParallelismRequest, PlanAlternative } from '../../api/types'
import { Lamp } from '../../components/Lamp'
import { Select, type SelectOption } from '../../components/Select'
import { Verbatim, VerbatimList } from '../../components/Verbatim'
import { toggle, type BoardRow } from './board'
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

/** Which machines this model would be served on.
 *
 *  It ranks nothing. The rows are in cluster order, and the verdict comes
 *  from the fit gate over whatever ends up ticked: a score computed here
 *  would be a second answer with no arithmetic behind it. Each row says what
 *  the server already reported: what the machine has free, and what this plan
 *  would leave or lack there. */
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

  // While copies run, the board answers "which machine am I looking at": a
  // tick on a machine running a copy shows that copy, a tick anywhere else
  // plans another copy there. One machine at a time; splitting is for planning.
  const running = s.running
  const copyOn = (nodeId: string) => s.live.find((d) => d.node_ids.includes(nodeId)) ?? null

  return (
    <div className="mdl-nodes">
      <div className="mdl-nodes-head">
        <span className="label">Where it runs</span>
        <span className="unit">
          {running
            ? 'pick a machine to see its copy, or to serve another copy there'
            : 'tick one node, or several to split'}
        </span>
        {running ? null : (
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
        )}
      </div>

      {board.rows.map((r) =>
        running ? (
          <NodeBar
            key={r.nodeId}
            row={{ ...r, selectable: r.selectable || copyOn(r.nodeId) != null }}
            checked={running.node_ids.includes(r.nodeId)}
            onlyTicked={running.node_ids.includes(r.nodeId)}
            perNode={perNode}
            onToggle={(on) => {
              if (on) s.pickNode(r.nodeId)
            }}
          />
        ) : (
          <NodeBar
            key={r.nodeId}
            row={{ ...r, selectable: r.selectable || copyOn(r.nodeId) != null }}
            checked={r.ticked || (s.nodeIds == null && r.inPlan)}
            onlyTicked={r.ticked && board.effective.length === 1}
            perNode={perNode}
            onToggle={(on) => {
              // A machine already running a copy is not a place to plan one:
              // the coordinator refuses that. Ticking it shows that copy.
              if (on && copyOn(r.nodeId)) return s.pickNode(r.nodeId)
              // Unticking the last machine would mean "plan on nothing", which
              // the gateway can only answer with a 400; the box is disabled for
              // that reason, and this hands the choice back if it ever fires.
              if (!on && board.effective.length === 1) return s.setNodeIds(null)
              // Sorted inside `toggle`, so two tick orders produce one request
              // body, one answer and one server-side memo key.
              s.setNodeIds(toggle(board, r.nodeId, on))
            }}
          />
        ),
      )}

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
  onToggle,
}: {
  row: BoardRow
  checked: boolean
  onlyTicked: boolean
  perNode: number | null
  onToggle: (on: boolean) => void
}) {
  const id = `${useId()}-node`
  const over = row.allocatable != null && perNode != null && perNode > row.allocatable

  let note: JSX.Element
  if (row.allocatable == null) note = <span className="unit">{row.noHostPool ? 'no host pool' : 'not read yet'}</span>
  else if (row.inPlan && perNode != null)
    note = over ? (
      <span className="unit mdl-sig-fault">{`short ${gib(perNode - row.allocatable)}`}</span>
    ) : (
      <span className="unit">{`${gib(row.allocatable - perNode)} left`}</span>
    )
  else if (row.hostPool) note = <span className="unit mdl-node-pool">{`${gib(row.allocatable)} host RAM free`}</span>
  else note = <span className="unit">{`${gib(row.allocatable)} free`}</span>

  return (
    <div className="mdl-node">
      <input
        id={id}
        type="checkbox"
        checked={checked}
        disabled={!row.selectable || onlyTicked}
        onChange={(e) => onToggle(e.target.checked)}
        aria-describedby={!row.selectable && row.unselectableReason ? `${id}-why` : undefined}
        title={onlyTicked ? 'the last machine: it stays ticked' : undefined}
      />
      <label htmlFor={id} className="mdl-node-name">
        <Lamp
          signal={row.inPlan ? 'live' : 'idle'}
          hollow={!row.inPlan}
          size={7}
          label={row.inPlan ? `${row.name} is in the plan` : `${row.name} is not in the plan`}
        />
        <span className="mono">{row.name}</span>
      </label>
      {note}
      {/* The registry's own sentence for why it cannot vouch for this machine,
          and the board's for why it cannot carry a rank. Shown, never hidden:
          "why isn't my other box being used" deserves an answer here. */}
      {!row.selectable && row.unselectableReason ? (
        <div className="mdl-node-note" id={`${id}-why`}>
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
