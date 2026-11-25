import { useId, useRef, useState } from 'react'
import { useRouter } from '../../state/router'
import { servesOnCluster } from '../../state/runtime'
import type { ModelSession } from './useModelSession'
import { USER_STOPS, contextStops, ctxLabel, nearestStop, withValue } from './usersChart'
import { gib } from './verdictView'

const CTX_HINT =
  'How many tokens one user’s conversation can hold — prompt and answer together. A longer context reserves more KV cache per user.'
const USERS_HINT = 'How many conversations are decoded at once. Each one holds its own KV cache at the context above.'

/** Under this, a headroom figure rounds to "0.0 GiB" and would read as exact. */
const ROUNDS_TO_ZERO = 0.05 * 2 ** 30

/** Context per user and number of users, as two sliders on the Serve tab.
 *
 *  A drag is held here and written to the URL (`?ctx=`, `?seq=`) once, when
 *  the pointer or key is released: one URL write per gesture, because Chrome
 *  stops applying `replaceState` past ~200 writes in 10 s, after which the
 *  address bar and the screen disagree and the next control builds its URL
 *  from the stale one. The KV line is the fit gate's own
 *  `breakdown.kv_cache`, never extrapolated here, and says "Re-planning…"
 *  until the session plans the URL's values and has answered. Hidden while
 *  this model runs: the sliders describe a launch, and the card describes
 *  the deployment. */
export function SizingSliders({ s }: { s: ModelSession }) {
  const { route, navigate } = useRouter()
  const uid = useId()
  const { result, runtime } = s
  // Remembered across a failed plan, which nulls `result`: the sliders must
  // outlive an error so the value that caused it can be moved back.
  const nativeSeen = useRef<number | null>(null)
  if (result?.native_window != null) nativeSeen.current = result.native_window
  const ctxBase = contextStops(nativeSeen.current)

  const [draft, setDraft] = useState<{ ctx?: number; seqs?: number } | null>(null)

  if (!servesOnCluster(runtime) || s.routing || s.running) {
    // Hidden mid-drag (a deployment came up): the gesture can never release,
    // so it is dropped rather than left to commit when the sliders return.
    if (draft) setDraft(null)
    return null
  }

  const want = { ctx: draft?.ctx ?? s.liveContext, seqs: draft?.seqs ?? s.liveConcurrency }
  // Built from the COMMITTED value, never the draft: a drag must not change
  // the track's length under the pointer. A draft is always one of these.
  const ctxStops = withValue(ctxBase, s.liveContext)
  const userStops = withValue(USER_STOPS, s.liveConcurrency)

  const commit = (next: { context?: number | null; concurrency?: number | null }) => {
    setDraft(null)
    if (
      (next.context === undefined || next.context === route.context) &&
      (next.concurrency === undefined || next.concurrency === route.concurrency)
    )
      return
    navigate(next, { replace: true })
  }
  const release = () => {
    if (!draft) return
    commit({
      ...(draft.ctx != null ? { context: draft.ctx } : {}),
      ...(draft.seqs != null ? { concurrency: draft.seqs } : {}),
    })
  }
  // A cancelled pointer is the browser taking the gesture (a scroll on a
  // phone), not a choice: the draft is dropped, never committed.
  const releaseOn = { onPointerUp: release, onPointerCancel: () => setDraft(null), onKeyUp: release, onBlur: release }

  // Pending until the session plans what the URL says and has the answer;
  // derived, so a move that cancels itself out never leaves it stuck.
  const pending =
    draft != null ||
    s.checking ||
    route.context !== s.plannedContext ||
    route.concurrency !== s.plannedConcurrency
  const fit = result ? (result.fit_live ?? result.fit) : null
  // Only a live reading is room to spare: the static ceiling overstates what
  // a shared machine can hand out, so without one the line states the cache alone.
  const h = result?.fit_live?.headroom ?? null
  const small = h != null && Math.abs(h) < ROUNDS_TO_ZERO
  const room =
    h == null ? null : h >= 0 ? (
      ` · ${small ? 'under 0.1 GiB' : gib(h)} left`
    ) : (
      <>
        {' · '}
        <span className="mdl-sig-fault">short {small ? 'under 0.1 GiB' : gib(-h)}</span>
      </>
    )
  // Auto with no answer yet has no figure: the session's fallback is a
  // placeholder nobody chose, so it is drawn as missing, not as a number.
  const ctxKnown = draft?.ctx != null || route.context != null || result?.context != null
  const seqsKnown = draft?.seqs != null || route.concurrency != null || result?.concurrency != null
  // Labelled with what the plan was sized for, and only when it says.
  const sizedFor =
    result?.context != null && result.concurrency != null
      ? ` for ${result.concurrency} × ${ctxLabel(result.context)} tokens`
      : ''

  return (
    <div className="mdl-size" role="group" aria-label="Context and users">
      <label htmlFor={`${uid}-ctx`} title={CTX_HINT}>
        Context per user
      </label>
      <input
        id={`${uid}-ctx`}
        type="range"
        min={0}
        max={ctxStops.length - 1}
        step={1}
        value={nearestStop(ctxStops, want.ctx)}
        disabled={!ctxKnown || nativeSeen.current == null}
        aria-valuetext={ctxKnown ? `${want.ctx.toLocaleString()} tokens` : 'not planned yet'}
        aria-describedby={`${uid}-ctx-hint`}
        onChange={(e) => setDraft((d) => ({ ...d, ctx: ctxStops[Number(e.target.value)] }))}
        {...releaseOn}
      />
      <span className="mono mdl-size-val">{ctxKnown ? ctxLabel(want.ctx) : '—'}</span>
      <span className="mdl-seg">
        <button
          type="button"
          aria-pressed={route.context == null && draft?.ctx == null}
          title="The planner picks the largest context that fits here, capped at the model's own window."
          onClick={() => commit({ context: null })}
        >
          Auto
        </button>
      </span>
      <span id={`${uid}-ctx-hint`} className="sr-only">
        {CTX_HINT}
      </span>

      <label htmlFor={`${uid}-seqs`} title={USERS_HINT}>
        Users at once
      </label>
      <input
        id={`${uid}-seqs`}
        type="range"
        min={0}
        max={userStops.length - 1}
        step={1}
        value={nearestStop(userStops, want.seqs)}
        disabled={!seqsKnown}
        aria-valuetext={seqsKnown ? `${want.seqs} ${want.seqs === 1 ? 'user' : 'users'}` : 'not planned yet'}
        aria-describedby={`${uid}-seqs-hint`}
        onChange={(e) => setDraft((d) => ({ ...d, seqs: userStops[Number(e.target.value)] }))}
        {...releaseOn}
      />
      <span className="mono mdl-size-val">{seqsKnown ? want.seqs : '—'}</span>
      <span className="mdl-seg">
        <button
          type="button"
          aria-pressed={route.concurrency == null && draft?.seqs == null}
          title="The planner picks the number of sequences."
          onClick={() => commit({ concurrency: null })}
        >
          Auto
        </button>
      </span>
      <span id={`${uid}-seqs-hint`} className="sr-only">
        {USERS_HINT}
      </span>

      <p className="unit mdl-size-sum" aria-live="polite">
        {pending ? (
          'Re-planning…'
        ) : fit ? (
          <>
            {`KV cache ${gib(fit.breakdown.kv_cache)} per node${sizedFor}`}
            {room}
          </>
        ) : null}
      </p>
    </div>
  )
}
