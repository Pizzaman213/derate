import { useEffect, useRef, useState } from 'react'
import type { FitResult, PlanResponse } from '../api/types'
import { CURATED_MODELS } from '../api/fixtures'
import { useBackend } from '../state/backend'
import { Lamp } from '../components/Lamp'
import { Readout } from '../components/Readout'
import { SegmentBar, type Segment } from '../components/Bars'
import { Verbatim, VerbatimList } from '../components/Verbatim'
import { gbNum, gbytes, planShortFromDegrees } from '../format'

interface Props {
  onLaunched: () => void
}

/** The dry run, and the refusal.
 *
 *  `POST /api/plan` launches nothing, so this screen is safe to poke at. When
 *  the fit gate refuses, the refusal is this screen — a breakdown, the limiting
 *  term, and the gate's own sentence — not an error toast. */
export function PlanView({ onLaunched }: Props) {
  const { backend, invalidate } = useBackend()

  const first = CURATED_MODELS[0]!
  const [modelId, setModelId] = useState(first.model_id)
  const [context, setContext] = useState(first.default_context)
  const [concurrency, setConcurrency] = useState(first.default_concurrency)
  const [target, setTarget] = useState('throughput')
  const [custom, setCustom] = useState('')

  const [result, setResult] = useState<PlanResponse | null>(null)
  const [checking, setChecking] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [launching, setLaunching] = useState(false)

  const seq = useRef(0)

  // Re-plan on any change, debounced. The dry run is cheap and starts nothing,
  // so making someone press a button to see the consequence of a number they
  // just typed is friction for its own sake.
  useEffect(() => {
    if (!backend || !modelId) return
    const mine = ++seq.current
    setChecking(true)
    const id = window.setTimeout(() => {
      backend
        .plan({ model_id: modelId, context, concurrency, target })
        .then((r) => {
          if (seq.current !== mine) return
          setResult(r)
          setError(null)
        })
        .catch((e: unknown) => {
          if (seq.current !== mine) return
          setResult(null)
          setError(e instanceof Error ? e.message : String(e))
        })
        .finally(() => {
          if (seq.current === mine) setChecking(false)
        })
    }, 450)
    return () => window.clearTimeout(id)
  }, [backend, modelId, context, concurrency, target])

  const pickCurated = (m: (typeof CURATED_MODELS)[number]) => {
    setModelId(m.model_id)
    setContext(m.default_context)
    setConcurrency(m.default_concurrency)
    setCustom('')
  }

  const submitCustom = () => {
    const v = custom.trim()
    if (v) setModelId(v)
  }

  const launch = async () => {
    if (!backend || !result) return
    setLaunching(true)
    try {
      await backend.launch({
        model_id: modelId,
        context,
        concurrency,
        target,
        runtime: 'vllm',
      })
      invalidate()
      onLaunched()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLaunching(false)
    }
  }

  return (
    <div style={{ display: 'grid', gap: 'var(--s3)', maxWidth: 780 }}>
      <h1 style={{ fontSize: 20, fontWeight: 500 }}>Plan a model</h1>

      <section style={{ display: 'grid', gap: 'var(--s1)' }}>
        <div style={{ display: 'grid', gap: 2 }}>
          {CURATED_MODELS.map((m) => {
            const on = m.model_id === modelId
            return (
              <button
                key={m.model_id}
                onClick={() => pickCurated(m)}
                aria-pressed={on}
                style={{
                  border: 0,
                  borderLeft: `2px solid ${on ? 'var(--ink)' : 'transparent'}`,
                  borderRadius: 0,
                  background: on ? 'var(--panel-recessed)' : 'transparent',
                  padding: '8px 10px',
                  textAlign: 'left',
                  display: 'grid',
                  gap: 2,
                }}
              >
                <span style={{ fontWeight: 500 }}>{m.label}</span>
                <span className="unit">{m.detail}</span>
              </button>
            )
          })}
        </div>

        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <label className="label muted" style={{ fontWeight: 400 }} htmlFor="hf-id">
            or a HuggingFace ID
          </label>
          <input
            id="hf-id"
            value={custom}
            placeholder="org/model"
            onChange={(e) => setCustom(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') submitCustom()
            }}
            onBlur={submitCustom}
            style={{ flex: 1, minWidth: 0 }}
            className="mono"
          />
        </div>

        {!CURATED_MODELS.some((m) => m.model_id === modelId) ? (
          <div className="unit">resolving {modelId}</div>
        ) : null}
      </section>

      <section
        style={{
          display: 'flex',
          gap: 'var(--s2)',
          flexWrap: 'wrap',
          alignItems: 'flex-end',
        }}
      >
        <NumberField label="context" value={context} onChange={setContext} width={9} />
        <NumberField
          label="concurrent sequences"
          value={concurrency}
          onChange={setConcurrency}
          width={5}
        />
        <label style={{ display: 'grid', gap: 4 }}>
          <span className="label muted" style={{ fontWeight: 400 }}>
            optimise for
          </span>
          <select value={target} onChange={(e) => setTarget(e.target.value)}>
            <option value="throughput">throughput</option>
            <option value="latency">latency</option>
          </select>
        </label>
      </section>

      <hr />

      {error && !result ? (
        <p className="label" style={{ color: 'var(--fault)', fontWeight: 400, margin: 0 }}>
          {error}
        </p>
      ) : null}

      {result ? (
        <div
          style={{
            display: 'grid',
            gap: 'var(--s3)',
            opacity: checking ? 0.5 : 1,
          }}
        >
          <PlanBlock response={result} />
          <hr />
          <FitBlock
            fit={result.fit}
            context={context}
            onUseMaxContext={(c) => setContext(c)}
            onLaunch={() => void launch()}
            launching={launching}
          />
          {error ? (
            <p className="label" style={{ color: 'var(--fault)', fontWeight: 400, margin: 0 }}>
              {error}
            </p>
          ) : null}
        </div>
      ) : (
        <p className="label muted" style={{ fontWeight: 400, margin: 0 }}>
          {checking ? 'Checking…' : 'Pick a model.'}
        </p>
      )}
    </div>
  )
}

function NumberField({
  label,
  value,
  onChange,
  width,
}: {
  label: string
  value: number
  onChange: (n: number) => void
  width: number
}) {
  return (
    <label style={{ display: 'grid', gap: 4 }}>
      <span className="label muted" style={{ fontWeight: 400 }}>
        {label}
      </span>
      <input
        className="mono"
        type="number"
        min={1}
        value={value}
        onChange={(e) => {
          const n = Number(e.target.value)
          if (Number.isFinite(n) && n > 0) onChange(Math.round(n))
        }}
        style={{ width: `${width + 4}ch`, textAlign: 'right' }}
      />
    </label>
  )
}

// ── The plan ─────────────────────────────────────────────────────────────────

function PlanBlock({ response }: { response: PlanResponse }) {
  const p = response.plan
  return (
    <section style={{ display: 'grid', gap: 'var(--s1)' }}>
      <div
        style={{
          display: 'flex',
          alignItems: 'baseline',
          justifyContent: 'space-between',
          gap: 'var(--s2)',
        }}
      >
        <h2 className="label muted" style={{ fontWeight: 400 }}>
          plan
        </h2>
        <span style={{ display: 'flex', alignItems: 'baseline', gap: 'var(--s2)' }}>
          <span style={{ fontWeight: 500 }}>{planShortFromDegrees(p)}</span>
          <Readout value={p.measured_link_gbps} decimals={1} width={5} unit="GB/s link" />
        </span>
      </div>

      <Verbatim text={p.reason} />

      {p.rejected.length > 0 ? (
        <div style={{ display: 'grid', gap: 6, paddingTop: 4 }}>
          <div className="label muted" style={{ fontWeight: 400 }}>
            rejected
          </div>
          <VerbatimList items={p.rejected} />
        </div>
      ) : null}

      <div className="unit">
        {p.node_ids.length} {p.node_ids.length === 1 ? 'machine' : 'machines'}:{' '}
        {p.node_ids.join(', ')}
      </div>
    </section>
  )
}

// ── The fit, and the refusal ─────────────────────────────────────────────────

const VERDICT_COPY: Record<FitResult['verdict'], { word: string; signal: 'live' | 'warn' | 'fault' }> = {
  fits: { word: 'fits', signal: 'live' },
  fits_degraded: { word: 'fits, degraded', signal: 'warn' },
  wont_fit: { word: 'will not fit', signal: 'fault' },
}

function FitBlock({
  fit,
  context,
  onUseMaxContext,
  onLaunch,
  launching,
}: {
  fit: FitResult
  context: number
  onUseMaxContext: (c: number) => void
  onLaunch: () => void
  launching: boolean
}) {
  const v = VERDICT_COPY[fit.verdict]
  const b = fit.breakdown

  const segments: Segment[] = [
    { key: 'weights', label: 'weights', bytes: b.weights },
    { key: 'kv_cache', label: 'kv cache', bytes: b.kv_cache },
    { key: 'activations', label: 'activations', bytes: b.activations },
    { key: 'comm_buffers', label: 'comm buffers', bytes: b.comm_buffers },
    { key: 'replicated', label: 'replicated', bytes: b.replicated },
    { key: 'framework_overhead', label: 'framework overhead', bytes: b.framework_overhead },
  ]
    .filter((s) => s.bytes > 0)
    .map((s) => ({ ...s, limiting: s.key === fit.limiting_term }))

  const canLaunch = fit.verdict !== 'wont_fit'
  const canAdjust =
    fit.max_context_that_fits != null && fit.max_context_that_fits !== context

  return (
    <section style={{ display: 'grid', gap: 'var(--s2)' }}>
      <div
        style={{
          display: 'flex',
          alignItems: 'baseline',
          justifyContent: 'space-between',
          gap: 'var(--s2)',
        }}
      >
        <h2 className="label muted" style={{ fontWeight: 400 }}>
          fit
        </h2>
        <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <Lamp signal={v.signal} label={v.word} />
          <span style={{ fontWeight: 500 }}>{v.word}</span>
        </span>
      </div>

      <Verbatim text={fit.reason} />

      <div style={{ display: 'grid', gap: 8 }}>
        <SegmentBar segments={segments} usable={fit.usable_per_node} />
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            gap: 'var(--s2)',
          }}
        >
          <span className="unit">per node</span>
          <span className="unit">
            usable line at {gbytes(fit.usable_per_node, 1)} GB
          </span>
        </div>
      </div>

      <dl
        style={{
          margin: 0,
          display: 'grid',
          gridTemplateColumns: 'max-content max-content max-content',
          columnGap: 'var(--s2)',
          rowGap: 4,
          alignItems: 'baseline',
        }}
      >
        {segments.map((s) => (
          <BreakdownRow
            key={s.key}
            label={s.label}
            bytes={s.bytes}
            limiting={s.limiting === true}
          />
        ))}
        <div style={{ gridColumn: '1 / -1', borderTop: '1px solid var(--rule)', margin: '4px 0' }} />
        <BreakdownRow label="total" bytes={b.total} strong />
        <BreakdownRow label="usable per node" bytes={fit.usable_per_node} />
        <BreakdownRow
          label="headroom"
          bytes={fit.headroom}
          tone={fit.headroom < 0 ? 'fault' : undefined}
        />
      </dl>

      {fit.predicted_decode_tps != null ? (
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 'var(--s1)' }}>
          <span className="label muted" style={{ fontWeight: 400 }}>
            predicted decode
          </span>
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
        <div style={{ display: 'grid', gap: 6 }}>
          {fit.warnings.map((w) => (
            <p
              key={w}
              className="label"
              style={{
                margin: 0,
                fontWeight: 400,
                color: 'var(--warn)',
                whiteSpace: 'pre-wrap',
              }}
            >
              {w}
            </p>
          ))}
        </div>
      ) : null}

      <div style={{ display: 'flex', gap: 'var(--s1)', alignItems: 'center', flexWrap: 'wrap' }}>
        {canLaunch ? (
          <button onClick={onLaunch} disabled={launching}>
            {launching ? 'Launching…' : 'Launch'}
          </button>
        ) : null}

        {/* The one-click adjustment. The gate said what to change; this is that
            change, already applied. */}
        {canAdjust ? (
          <button onClick={() => onUseMaxContext(fit.max_context_that_fits!)}>
            Use {fit.max_context_that_fits} context
          </button>
        ) : null}

        {/* FITS_DEGRADED loads. The number that makes it a bad idea sits next to
            the button, so the choice is informed rather than blocked. */}
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
    </section>
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
      <dt
        className="label"
        style={{ fontWeight: strong ? 500 : 400, color: strong ? 'var(--ink)' : 'var(--ink-muted)' }}
      >
        {label}
      </dt>
      <dd style={{ margin: 0 }}>
        <Readout
          value={gbNum(bytes)}
          decimals={1}
          width={7}
          unit="GB"
          tone={tone ?? 'ink'}
        />
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
