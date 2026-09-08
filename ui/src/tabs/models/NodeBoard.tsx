import { Lamp } from '../../components/Lamp'
import { ProportionBar } from '../../components/Bars'
import { Readout } from '../../components/Readout'
import { Verbatim } from '../../components/Verbatim'
import { deviceClassLabel, gbNum, shortGpu, sizeLabel } from '../../format'
import { toggle, type Board, type BoardRow } from './board'

/** The machine board: which machines this model would be served on, and the
 *  facts you need in order to disagree with the planner about it.
 *
 *  It exists because the planner does not choose machines. `_nodes_for` takes
 *  the alphabetical prefix of the strongest homogeneous group; live memory,
 *  whether the weights are already on disk here, and what is already running
 *  here never enter the decision. Every one of those is knowledge a person has
 *  and the planner does not, and until now there was nowhere on a model's own
 *  screen to apply it.
 *
 *  It ranks nothing. The rows are in cluster order, not an order this file
 *  invented, and the verdict underneath comes from the fit gate over whatever
 *  ends up ticked -- a score computed here would be a second answer with no
 *  arithmetic behind it.
 */
export function NodeBoard({
  board,
  onChange,
}: {
  board: Board
  /** `null` hands the choice back to the planner and sends no `node_ids`. */
  onChange: (next: string[] | null) => void
}) {
  return (
    <div>
      <div className="sub" style={{ display: 'flex', justifyContent: 'space-between' }}>
        <span>machines</span>
        <span className="unit" style={{ letterSpacing: 0 }}>
          {board.owned ? 'chosen by you' : 'chosen by the planner'}
          {board.selectableCount < board.rows.length
            ? ` · ${board.rows.length - board.selectableCount} cannot carry a rank`
            : ''}
        </span>
      </div>

      <div className="nboard-scroll">
        <div className="nboard-head">
          <span />
          <span>machine</span>
          <span>allocatable now / ceiling</span>
          <span>weights</span>
          <span>link</span>
          <span>running</span>
        </div>

        {board.rows.map((r) => (
          <Row
            key={r.nodeId}
            row={r}
            owned={board.owned}
            // Unticking the last machine would mean "plan on nothing", which
            // the gateway can only answer with a 400. Hand the choice back to
            // the planner instead -- that is what the gesture means.
            lastTicked={r.ticked && board.effective.length === 1}
            onToggle={(on) => {
              if (!on && board.effective.length === 1) {
                onChange(null)
                return
              }
              onChange(toggle(board, r.nodeId, on))
            }}
          />
        ))}
      </div>

      {/* One unprobed pair among the ticked machines makes
          `worst_all_reduce` answer for none of them, and the planner then
          plans as though nothing were measured. Said out loud because it
          changes the shape and is otherwise invisible. */}
      {board.unmeasuredPairs.length ? (
        <p className="why" style={{ color: 'var(--warn)' }}>
          {board.unmeasuredPairs.length === 1
            ? `${board.unmeasuredPairs[0]![0]} and ${board.unmeasuredPairs[0]![1]} have never been measured against each other. `
            : `${board.unmeasuredPairs.length} pairs among these machines have never been measured. `}
          The planner reads bandwidth for the whole set or not at all, so it will
          plan this as an unmeasured cluster. Measure them on the Cluster screen
          for a plan that accounts for the wire.
        </p>
      ) : null}

      {board.owned ? (
        <div style={{ marginTop: 'var(--s-2)' }}>
          <button type="button" className="ghost" onClick={() => onChange(null)}>
            Let the planner choose
          </button>
        </div>
      ) : null}
    </div>
  )
}

function Row({
  row,
  owned,
  lastTicked,
  onToggle,
}: {
  row: BoardRow
  /** Whether the ticks are the operator's. Both badges below are only worth
   *  drawing when they say something the tick does not. */
  owned: boolean
  lastTicked: boolean
  onToggle: (on: boolean) => void
}) {
  const id = `nb-${row.nodeId}`
  return (
    <div className={`nboard-row${row.ticked ? ' on' : ''}`}>
      <input
        id={id}
        type="checkbox"
        checked={row.ticked}
        disabled={!row.selectable}
        onChange={(e) => onToggle(e.target.checked)}
        title={lastTicked ? 'the last machine: unticking hands the choice back' : undefined}
      />

      <label htmlFor={id} className={row.selectable ? undefined : 'muted'}>
        <span style={{ display: 'grid', gap: 2 }}>
          <span>
            <span className="mono">{row.name}</span>{' '}
            <span className="unit">{shortGpu(row.gpu) || deviceClassLabel(row.deviceClass)}</span>
          </span>
          {row.subtitle ? <span className="unit">{row.subtitle}</span> : null}
          {/* Whose answer this row is, said only where the tick does not
              already say it. Once the ticks are yours, "in the plan" is on
              every ticked row and reads as decoration. */}
          {!owned && row.inPlan ? <span className="unit">the planner picked this</span> : null}
          {/* The case worth a badge: ticked, and still carrying no rank,
              because the degrees under-fill the set. `unused_node_ids` also
              lists every machine outside the plan when the planner chose, and
              on an unticked row that is what the empty tick already says. */}
          {row.ticked && row.unused ? (
            <span className="unit" style={{ color: 'var(--warn)' }}>
              ticked, but carries no rank
            </span>
          ) : null}
        </span>
      </label>

      <span>
        {/* A machine with no GPU has no pool for these to be a fraction of,
            and both readings are honestly 0 -- which drew as "0.0 / 0.0 GB",
            reading like a GPU with nothing left rather than like a machine
            with no GPU. Same correction `HardwareRows` already carries for
            the same rows. What it can do is said under the row. */}
        {row.ceiling === 0 ? (
          <span className="unit">no GPU memory</span>
        ) : row.allocatable == null ? (
          <span className="unit">no reading</span>
        ) : (
          <span style={{ display: 'grid', gap: 3 }}>
            <span style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
              <Readout
                value={gbNum(row.allocatable)}
                decimals={1}
                width={5}
                stale={row.stale}
                title={
                  row.stale ? 'the last reading; the poll has not refreshed it' : undefined
                }
              />
              <span className="unit">
                / {row.ceiling == null ? '—' : `${gbNum(row.ceiling).toFixed(1)} GB`}
              </span>
            </span>
            {/* Null draws a dashed empty track, never a filled zero: a
                machine nothing has read is not a machine with nothing left. */}
            <ProportionBar
              value={row.ceiling ? row.allocatable / row.ceiling : null}
              label={`${row.name} allocatable against its ceiling`}
            />
          </span>
        )}
      </span>

      <span className="unit">
        {row.cached
          ? row.cachedBytes
            ? `on disk · ${sizeLabel(row.cachedBytes)}`
            : 'on disk'
          : 'pulls'}
      </span>

      <span className="unit">
        {!row.ticked ? (
          '—'
        ) : row.linkUnmeasured ? (
          'never measured'
        ) : row.linkGbps == null ? (
          '—'
        ) : (
          <Readout value={row.linkGbps} decimals={1} width={5} unit="GB/s" />
        )}
      </span>

      <span className="unit">
        {row.occupants.length ? (
          <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <Lamp signal="live" label={`${row.name} is serving`} size={6} />
            {row.occupants.join(', ')}
          </span>
        ) : (
          'nothing'
        )}
      </span>

      {/* The registry's own sentence for why it cannot vouch for this machine,
          and the gateway's for why it cannot carry a rank. Shown, never
          hidden: "why isn't my other box being used" deserves an answer on the
          screen where the question is asked. */}
      {!row.selectable && row.unselectableReason ? (
        <p className="nboard-note">
          <Verbatim text={row.unselectableReason} size="unit" />
        </p>
      ) : null}
      {!row.eligible && row.ineligibleReason ? (
        <p className="nboard-note">
          <Verbatim text={row.ineligibleReason} size="unit" />
        </p>
      ) : null}
    </div>
  )
}
