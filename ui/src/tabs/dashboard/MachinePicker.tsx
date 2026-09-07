import type { NodeStateDTO, PlacementBlock } from '../../api/types'
import { Popover } from '../../components/Popover'
import { Verbatim } from '../../components/Verbatim'
import { shortGpu } from '../../format'

interface Props {
  /** Every machine in the cluster, in cluster order. */
  nodes: NodeStateDTO[]
  /** What the last plan occupied, so the field can show the planner's answer
   *  before anyone has overridden it. */
  plannerChose: string[] | null
  /** How the backend partitioned the machines it can see. Used only to badge
   *  rows -- the client must not re-derive the partition, or there are two
   *  answers to one question. */
  placement: PlacementBlock | null
  /** null means "the planner picks", and sends no `node_ids`. Only an explicit
   *  toggle materialises an array; that is what makes "sending nothing is
   *  exactly the old request" true by construction rather than by care. */
  chosen: string[] | null
  onChange: (next: string[] | null) => void
}

/** The Machines field.
 *
 *  Before anyone touches it the ticks mirror what the planner chose, and the
 *  trigger says `planner` rather than a count -- `0 of 2` before the first plan
 *  lands would be a false number, and who is choosing is the true thing that is
 *  known. The first click adopts the planner's set and applies the edit in one
 *  gesture, which also pins the ticks so they stop moving under the cursor as
 *  450ms plans land.
 */
export function MachinePicker({ nodes, plannerChose, placement, chosen, onChange }: Props) {
  const effective = chosen ?? plannerChose ?? []
  const owned = chosen != null
  const label = owned || plannerChose ? `${effective.length} of ${nodes.length}` : 'planner'

  const eligibleCount = nodes.filter((n) => n.eligible !== false).length

  const toggle = (nodeId: string, on: boolean) => {
    const next = on
      ? [...effective, nodeId]
      : effective.filter((id) => id !== nodeId)
    // Sorted so two different tick orders produce the same request body, and
    // therefore the same answer and the same server-side memo key. The one
    // place order matters -- which host is the pipeline head -- is the
    // planner's to decide from the set.
    next.sort()
    onChange(next)
  }

  return (
    <div className="fld">
      <label htmlFor="pb-machines">Machines</label>
      <Popover
        label="Machines to serve on"
        trigger={
          <span id="pb-machines">
            {label} <span aria-hidden>▾</span>
          </span>
        }
      >
        {() => (
          <>
            {nodes.map((n) => {
              const id = n.profile.node_id
              const ticked = effective.includes(id)
              const ineligible = n.eligible === false
              // Unticking the last machine would mean "plan on nothing", which
              // the backend can only answer with a 400. Better unreachable
              // than a refusal for a gesture with no meaning.
              const isLast = ticked && effective.length === 1
              const disabled = ineligible || isLast
              return (
                <label
                  key={id}
                  className={`poprow${disabled ? ' off' : ''}`}
                  title={isLast ? 'at least one machine' : undefined}
                >
                  <input
                    type="checkbox"
                    checked={ticked}
                    disabled={disabled}
                    onChange={(e) => toggle(id, e.target.checked)}
                    style={{ marginTop: 3 }}
                  />
                  <span style={{ display: 'grid', gap: 2 }}>
                    <span>
                      <span className="mono">{n.profile.hostname || id}</span>{' '}
                      <span className="unit">{shortGpu(n.profile.gpu_name)}</span>
                    </span>
                    {/* The registry's own sentence for why it cannot vouch for
                        this machine. Shown, never hidden: "why isn't my other
                        box being used" deserves an answer on the screen where
                        the question is asked. */}
                    {ineligible && n.ineligible_reason ? (
                      <Verbatim text={n.ineligible_reason} size="label" />
                    ) : null}
                  </span>
                </label>
              )
            })}

            {placement?.mixed_hardware ? (
              <p className="unit" style={{ margin: '6px 4px 0' }}>
                These machines are not alike — serving needs the override below.
              </p>
            ) : null}

            <div className="popfoot">
              <span className="unit">
                {owned ? 'chosen by you' : 'chosen by the planner'}
                {eligibleCount < nodes.length
                  ? ` · ${nodes.length - eligibleCount} unavailable`
                  : ''}
              </span>
              {owned ? (
                <button type="button" onClick={() => onChange(null)}>
                  Reset
                </button>
              ) : null}
            </div>
          </>
        )}
      </Popover>
    </div>
  )
}
