import type { ParallelismPlan, ParallelismRequest } from '../../api/types'
import { planShortFromDegrees } from '../../format'

type Axis = 'tensor_parallel' | 'pipeline_parallel' | 'expert_parallel'

interface Props {
  /** null means "the planner picks", and sends no `parallelism`. */
  degrees: ParallelismRequest | null
  onChange: (next: ParallelismRequest | null) => void
  /** The degrees currently in force, whoever chose them. Shown as placeholder
   *  text while `degrees` is null, so an untouched field displays the planner's
   *  answer in muted type -- visibly present, visibly not yours. */
  effective: {
    tensor_parallel: number
    pipeline_parallel: number
    expert_parallel: number
  } | null
  /** The planner's own pick. Rendered as a caption only when it differs from
   *  what will launch. */
  recommended: ParallelismPlan | null
  overruled: boolean
  /** Prefix for this instance's element ids. `ModelInspector` is mounted from
   *  two places -- the Models tab and the sheet -- and `AppShell` keeps every
   *  destination mounted at once, so more than one of these can be in the DOM
   *  together; with one hardcoded id, clicking "TP" in one would focus the
   *  other's field. Defaults to the Serve panel's own prefix. */
  idPrefix?: string
}

/** TP, PP and EP, owned as a set.
 *
 *  Per-axis handback reads well and encodes badly: with `parallelism` sent as
 *  an object, an omitted key means 1, so "TP mine, PP the planner's" cannot be
 *  expressed on the wire at all. Rather than invent a second meaning for an
 *  empty field, the whole object is adopted on the first edit and handed back
 *  by one Reset -- the same first-touch-adopts gesture the machine picker uses.
 *
 *  There is no DP field and that is not an omission. vLLM builds no rank group
 *  for expert parallel -- its size IS `dp * tp` -- so a DP box would have
 *  exactly one legal value given EP and no meaning without it;
 *  `state/placement.ts` pairs them on the way out. EP is here at all because
 *  the planner cannot pick it on this class of hardware (`EP_VIABLE_THRESHOLD`
 *  is 40 GB/s and a Spark tops out at 23.15) and without a field there was no
 *  way to ask for it short of writing the request body by hand.
 *
 *  Nothing here checks legality. Head divisibility, layer counts and the
 *  bandwidth thresholds are the planner's arithmetic, and a second copy in the
 *  browser is a second answer that can disagree -- the same reason this bar
 *  does not compute fit. The field takes any positive integer and lets the
 *  planner say no, in its own words.
 */
export function DegreeFields({
  degrees,
  onChange,
  effective,
  recommended,
  overruled,
  idPrefix = 'sp',
}: Props) {
  const adopt = (axis: Axis, raw: string) => {
    const base: ParallelismRequest = degrees ?? {
      tensor_parallel: effective?.tensor_parallel ?? 1,
      pipeline_parallel: effective?.pipeline_parallel ?? 1,
      expert_parallel: effective?.expert_parallel ?? 1,
    }
    if (raw.trim() === '') return
    const n = Number(raw)
    // Same guard the Context and Seqs fields use: a value that is not a
    // positive number is not applied, and the last good one stays.
    if (!Number.isFinite(n) || n < 1) return
    onChange({ ...base, [axis]: Math.round(n) })
  }

  const field = (axis: Axis, id: string, text: string) => (
    <div className="fld" style={{ width: 58 }}>
      <label htmlFor={id}>{text}</label>
      <input
        id={id}
        className="mono"
        type="number"
        min={1}
        value={degrees ? String(degrees[axis] ?? 1) : ''}
        placeholder={effective ? String(effective[axis]) : ''}
        onChange={(e) => adopt(axis, e.target.value)}
      />
    </div>
  )

  return (
    <>
      {field('tensor_parallel', `${idPrefix}-tp`, 'TP')}
      {field('pipeline_parallel', `${idPrefix}-pp`, 'PP')}
      {field('expert_parallel', `${idPrefix}-ep`, 'EP')}
      {degrees ? (
        <div className="fld">
          {/* Occupies the label row so the control lines up with the fields. */}
          <label htmlFor={`${idPrefix}-degrees-reset`}>&nbsp;</label>
          <button
            id={`${idPrefix}-degrees-reset`}
            type="button"
            onClick={() => onChange(null)}
          >
            Reset
          </button>
        </div>
      ) : null}
      {overruled && recommended ? (
        <div className="fld" style={{ justifyContent: 'flex-end' }}>
          <span className="unit">planner recommends {planShortFromDegrees(recommended)}</span>
        </div>
      ) : null}
    </>
  )
}
