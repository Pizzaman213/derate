import type { FitResult, PlanResponse } from '../../api/types'
import { Lamp } from '../../components/Lamp'
import { Readout } from '../../components/Readout'
import { SegmentBar, type Segment } from '../../components/Bars'
import { Verbatim, VerbatimList } from '../../components/Verbatim'
import { gbNum, gbytes, planShortFromDegrees } from '../../format'

interface Props {
  result: PlanResponse
  checking: boolean
  error: string | null
  context: number
  onUseMaxContext: (c: number) => void
  onLaunch: () => void
  launching: boolean
}

const VERDICT_COPY: Record<FitResult['verdict'], { word: string; signal: 'live' | 'warn' | 'fault' }> = {
  fits: { word: 'fits', signal: 'live' },
  fits_degraded: { word: 'fits, degraded', signal: 'warn' },
  wont_fit: { word: 'will not fit', signal: 'fault' },
}

/** The dry run, and the refusal, in the one box mockups-next/js/planner.js
 *  draws as `#verdict` -- transplanted from the old standalone PlanView's
 *  PlanBlock + FitBlock, which is where the real 6-term breakdown and the
 *  refusal copy already lived. Nothing here computes a fit; it renders the
 *  one the fit gate already computed and sent back. */
export function Verdict({ result, checking, error, context, onUseMaxContext, onLaunch, launching }: Props) {
  const p = result.plan
  const fit = result.fit
  const v = VERDICT_COPY[fit.verdict]

  const segments: Segment[] = [
    { key: 'weights', label: 'weights', bytes: fit.breakdown.weights },
    { key: 'kv_cache', label: 'kv cache', bytes: fit.breakdown.kv_cache },
    { key: 'activations', label: 'activations', bytes: fit.breakdown.activations },
    { key: 'comm_buffers', label: 'comm buffers', bytes: fit.breakdown.comm_buffers },
    { key: 'replicated', label: 'replicated', bytes: fit.breakdown.replicated },
    { key: 'framework_overhead', label: 'framework overhead', bytes: fit.breakdown.framework_overhead },
  ]
    .filter((s) => s.bytes > 0)
    .map((s) => ({ ...s, limiting: s.key === fit.limiting_term }))

  const canLaunch = fit.verdict !== 'wont_fit'
  // 0 is the backend's old "nothing helps" sentinel; the corrected backend
  // sends null for that case, but a stray 0 must never render a button that
  // offers to launch at zero context.
  const canAdjust =
    fit.max_context_that_fits != null &&
    fit.max_context_that_fits > 0 &&
    fit.max_context_that_fits !== context

  return (
    <div className={`verdict on${fit.verdict === 'wont_fit' ? ' bad' : ''}`} style={{ opacity: checking ? 0.6 : 1 }}>
      <div className="vhead" style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 8 }}>
        <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <Lamp signal={v.signal} label={v.word} />
          {planShortFromDegrees(p)} · {v.word}
        </span>
        <Readout value={p.measured_link_gbps} decimals={1} width={5} unit="GB/s link" />
      </div>

      <Verbatim text={p.reason} size="label" />

      {p.rejected.length > 0 ? (
        <div style={{ display: 'grid', gap: 6, paddingTop: 6 }}>
          <div className="unit">rejected</div>
          <VerbatimList items={p.rejected} />
        </div>
      ) : null}

      <div className="unit" style={{ margin: '6px 0 10px' }}>
        {p.node_ids.length} {p.node_ids.length === 1 ? 'machine' : 'machines'}: {p.node_ids.join(', ')}
      </div>

      {result.resolver_warnings.length > 0 ? (
        <div style={{ display: 'grid', gap: 6, margin: '6px 0 10px' }}>
          <div className="unit">resolver</div>
          <VerbatimList items={result.resolver_warnings} />
        </div>
      ) : null}

      <Verbatim text={fit.reason} size="label" />

      <div style={{ display: 'grid', gap: 8, marginTop: 8 }}>
        <SegmentBar segments={segments} usable={fit.usable_per_node} />
        <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8 }}>
          <span className="unit">per node</span>
          <span className="unit">usable line at {gbytes(fit.usable_per_node, 1)} GB</span>
        </div>
      </div>

      <dl
        style={{
          margin: '8px 0 0',
          display: 'grid',
          gridTemplateColumns: 'max-content max-content max-content',
          columnGap: 16,
          rowGap: 4,
          alignItems: 'baseline',
        }}
      >
        {segments.map((s) => (
          <BreakdownRow key={s.key} label={s.label} bytes={s.bytes} limiting={s.limiting === true} />
        ))}
        <div style={{ gridColumn: '1 / -1', borderTop: '1px solid var(--rule)', margin: '4px 0' }} />
        <BreakdownRow label="total" bytes={fit.breakdown.total} strong />
        <BreakdownRow label="usable per node" bytes={fit.usable_per_node} />
        <BreakdownRow label="headroom" bytes={fit.headroom} tone={fit.headroom < 0 ? 'fault' : undefined} />
      </dl>

      {fit.predicted_decode_tps != null ? (
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginTop: 8 }}>
          <span className="unit">predicted decode</span>
          <Readout
            value={fit.predicted_decode_tps}
            decimals={1}
            width={5}
            unit="tok/s"
            tone={fit.verdict === 'fits_degraded' ? 'warn' : 'ink'}
          />
        </div>
      ) : null}

      {fit.warnings.length > 0 ? (
        <div style={{ display: 'grid', gap: 6, marginTop: 8 }}>
          {fit.warnings.map((w) => (
            <p key={w} className="label" style={{ margin: 0, fontWeight: 400, color: 'var(--warn)', whiteSpace: 'pre-wrap' }}>
              {w}
            </p>
          ))}
        </div>
      ) : null}

      <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap', marginTop: 10 }}>
        {canLaunch ? (
          <button onClick={onLaunch} disabled={launching}>
            {launching ? 'Launching…' : 'Serve'}
          </button>
        ) : null}

        {/* The gate said what to change; this is that change, already applied. */}
        {canAdjust ? (
          <button onClick={() => onUseMaxContext(fit.max_context_that_fits!)}>
            Use {fit.max_context_that_fits} context
          </button>
        ) : null}

        {/* FITS_DEGRADED loads. The number that makes it a bad idea sits next
            to the button, so the choice is informed rather than blocked. */}
        {fit.verdict === 'fits_degraded' && fit.predicted_decode_tps != null ? (
          <span className="label" style={{ fontWeight: 400, color: 'var(--warn)' }}>
            It will load and decode at {fit.predicted_decode_tps.toFixed(1)} tok/s.
          </span>
        ) : null}

        {fit.verdict === 'wont_fit' && !canAdjust ? (
          <span className="label muted" style={{ fontWeight: 400 }}>
            No context length makes this fit on the current machines.
          </span>
        ) : null}
      </div>

      {/* The refusal, verbatim, when Serve itself was rejected rather than
          the dry run. */}
      {error ? (
        <p className="label" style={{ color: 'var(--fault)', fontWeight: 400, margin: '10px 0 0' }}>
          {error}
        </p>
      ) : null}
    </div>
  )
}

function BreakdownRow({
  label,
  bytes,
  limiting,
  strong,
  tone,
}: {
  label: string
  bytes: number
  limiting?: boolean
  strong?: boolean
  tone?: 'fault'
}) {
  return (
    <>
      <dt className="label" style={{ fontWeight: strong ? 500 : 400, color: strong ? 'var(--ink)' : 'var(--ink-muted)' }}>
        {label}
      </dt>
      <dd style={{ margin: 0 }}>
        <Readout value={gbNum(bytes)} decimals={1} width={7} unit="GB" tone={tone ?? 'ink'} />
      </dd>
      <dd style={{ margin: 0 }}>
        {limiting ? (
          <span className="label" style={{ fontWeight: 400, color: 'var(--ink)' }}>
            limiting
          </span>
        ) : null}
      </dd>
    </>
  )
}
