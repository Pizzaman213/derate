import { useEffect, useMemo, useState } from 'react'
import type { ConcurrencyRequest, ConcurrencyResponse } from '../../api/types'
import { PackChart } from '../../components/PackChart'
import { Verbatim } from '../../components/Verbatim'
import { useBackend } from '../../state/backend'
import { useBenchmarks } from '../../state/resources'
import { useRouter } from '../../state/router'
import { servesOnCluster } from '../../state/runtime'
import { launchId, variantKey } from './ollamaTarget'
import { recommendedPicks } from './quantPicks'
import type { ModelSession } from './useModelSession'
import { MEASURED_SERIES, comparedContexts, ctxLabel as k, presetContexts, toChartRows, toTtftRows } from './usersChart'

type Mode = 'quant' | 'context' | 'ttft'

const MODES: { value: Mode; label: string }[] = [
  { value: 'quant', label: 'By quant' },
  { value: 'context', label: 'By context' },
  { value: 'ttft', label: 'First token' },
]

const secs = (n: number) => (n < 10 ? n.toFixed(2) : n.toFixed(0))

/** The one sentence under the chart, from the response's numbers only. */
function say(resp: ConcurrencyResponse, label: string, seqs: number): string | null {
  const s = resp.series[0]
  if (!s) return null
  const at = s.points.find((p) => p.users === seqs) ?? s.points[0]
  if (!at) return null
  return (
    `At ${k(s.context)}, ${label} fits ${s.wall_users} users before the KV cache is full. ` +
    `Up to ${at.ceiling_tps_each < 10 ? at.ceiling_tps_each.toFixed(1) : at.ceiling_tps_each.toFixed(0)} tok/s each at your ${seqs} sequences, by bandwidth.`
  )
}

/** The First token sentence, for the series at the plan's own context. */
function sayTtft(resp: ConcurrencyResponse, ctx: number | null): string | null {
  const s = resp.series.find((x) => x.context === ctx) ?? resp.series[0]
  if (!s || s.wall_users <= 0) return null
  const a = s.points.find((p) => p.users === 1)?.ttft_floor_s
  const b = s.points.find((p) => p.users === s.wall_users)?.ttft_floor_s
  if (a == null || b == null) return null
  return `No sooner than ${secs(a)} s for one user at ${k(s.context)}, ${secs(b)} s when ${s.wall_users} send at once.`
}

/** "How many users": the KV wall and the bandwidth ceiling per user, from
 *  `POST /api/plan/concurrency`. An estimate; a matching benchmark's measured
 *  points are drawn as dots beside it. */
export function UsersChart({ s }: { s: ModelSession }) {
  const { backend } = useBackend()
  const { navigate } = useRouter()
  const [mode, setMode] = useState<Mode>('quant')
  const [resps, setResps] = useState<{ resp: ConcurrencyResponse; name: string }[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  // Bumped after a compute measurement lands, so the chart refetches.
  const [nonce, setNonce] = useState(0)
  const [measuring, setMeasuring] = useState(false)
  const [measureError, setMeasureError] = useState<string | null>(null)
  const bench = useBenchmarks(s.subjectId)
  const { result, ladder, runtime, variant } = s

  const native = result?.native_window ?? null
  const ctx = result?.context ?? null
  const seqs = result?.concurrency ?? s.liveConcurrency

  // The plan body the verdict was taken with, read off the session's own
  // launch request so engine options size the chart as they size the card.
  // Only the keys `/api/plan` takes; model_id and contexts are per series.
  const reqJson = result ? JSON.stringify(s.currentRequest()) : null
  const base = useMemo(() => {
    if (!reqJson) return null
    const r = JSON.parse(reqJson) as ReturnType<ModelSession['currentRequest']>
    const b: Omit<ConcurrencyRequest, 'model_id' | 'contexts'> = {
      target: r.target,
      runtime: r.runtime,
      concurrency: r.concurrency,
      ...(r.node_ids ? { node_ids: r.node_ids } : {}),
      ...(r.parallelism ? { parallelism: r.parallelism } : {}),
      ...(r.speculative ? { speculative: r.speculative } : {}),
      ...(r.kv_dtype ? { kv_dtype: r.kv_dtype } : {}),
      ...(r.dtype ? { dtype: r.dtype } : {}),
      ...(r.engine_options ? { engine_options: r.engine_options } : {}),
    }
    return b
  }, [reqJson])

  const formats = s.formats
  const picks = useMemo(
    () => (ladder ? recommendedPicks(ladder, runtime, formats).slice(0, 3) : []),
    [ladder, runtime, formats],
  )
  // The compared series are the preset contexts nearest the one in use; the
  // context itself is set by the slider above the chart (`SizingSliders`).
  const presets = useMemo(() => presetContexts(native), [native])
  const contexts = useMemo(() => comparedContexts(ctx, presets), [ctx, presets])
  const pickContext = (c: number | null) => navigate({ context: c }, { replace: true })

  const key = JSON.stringify([mode, base, s.subjectId, ctx, picks.map((p) => launchId(p.variant)), contexts, nonce])
  useEffect(() => {
    if (!base || ctx == null) {
      setResps(null)
      return
    }
    let live = true
    const jobs =
      mode === 'quant'
        ? (picks.length ? picks.map((p) => ({ id: launchId(p.variant), name: p.variant.label })) : [{ id: s.subjectId, name: variant?.label ?? s.subjectId }]).map(
            (j) =>
              backend
                .planConcurrency({ ...base, model_id: j.id, contexts: [ctx] })
                .then((resp) => ({ resp, name: j.name })),
          )
        : [
            backend
              .planConcurrency({ ...base, model_id: s.subjectId, contexts })
              .then((resp) => ({ resp, name: '' })),
          ]
    const id = window.setTimeout(() => {
      Promise.allSettled(jobs).then((settled) => {
        if (!live) return
        const ok = settled.flatMap((r) => (r.status === 'fulfilled' ? [r.value] : []))
        const bad = settled.find((r) => r.status === 'rejected') as PromiseRejectedResult | undefined
        setResps(ok)
        setError(ok.length ? null : bad ? String(bad.reason instanceof Error ? bad.reason.message : bad.reason) : null)
      })
    }, 300)
    return () => {
      live = false
      window.clearTimeout(id)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, backend])

  const measured = useMemo(
    () =>
      (bench.data ?? []).find((b) => b.state === 'done' && b.request.model_id === s.subjectId) ?? null,
    [bench.data, s.subjectId],
  )

  if (!servesOnCluster(runtime) || s.routing) return null
  if (error && !resps?.length) {
    return (
      <section className="mdl-users" aria-label="How many users">
        <div className="mdl-users-head">
          <span className="label">How many users</span>
        </div>
        <Verbatim text={error} size="unit" />
      </section>
    )
  }
  const first = resps?.[0]
  if (!resps || !first) return null

  // Flatten to one response for the chart helper: in quant mode each response
  // carries one series, named by its variant; in context mode one response
  // carries every context.
  const merged: ConcurrencyResponse = {
    ...first.resp,
    series: resps.flatMap((r) => r.resp.series),
  }
  if (!merged.series.some((x) => x.wall_users > 0)) return null
  const names = mode === 'quant' ? resps.map((r) => r.name) : merged.series.map((x) => k(x.context))
  const ttft = mode === 'ttft'
  const picked =
    mode === 'quant' ? (names.find((n) => n === variant?.label) ?? names[0]) : ctx != null ? k(ctx) : names[0]
  const chart = ttft
    ? toTtftRows(merged, names, measured)
    : toChartRows(merged, names, measured, picked ? { x: seqs, series: picked } : null)
  const sentence = ttft
    ? sayTtft(first.resp, ctx)
    : say(first.resp, mode === 'quant' ? first.name : variant?.label ?? s.subjectId, seqs)
  const noCompute = ttft && first.resp.compute == null

  const onPoint = (datum: Record<string, unknown>) => {
    const series = datum.series
    if (typeof series !== 'string' || series === MEASURED_SERIES) return
    if (mode === 'quant') {
      const p = picks.find((x) => x.variant.label === series)
      if (p) s.selectVariant(variantKey(p.variant))
      return
    }
    const c = merged.series.find((x) => k(x.context) === series)?.context
    if (c != null) pickContext(c)
  }

  const measure = () => {
    setMeasuring(true)
    setMeasureError(null)
    backend
      .measureCompute()
      .then(() => setNonce((n) => n + 1))
      .catch((e: unknown) => setMeasureError(e instanceof Error ? e.message : String(e)))
      .finally(() => setMeasuring(false))
  }

  return (
    <section className="mdl-users" aria-label="How many users">
      <div className="mdl-users-head">
        <span className="label">How many users</span>
        <span className="mdl-pill">estimate</span>
        {measured ? <span className="mdl-pill">measured</span> : null}
        <span className="mdl-seg" role="group" aria-label="Compare">
          {MODES.map((m) => (
            <button key={m.value} type="button" aria-pressed={mode === m.value} onClick={() => setMode(m.value)}>
              {m.label}
            </button>
          ))}
        </span>
      </div>
      {noCompute ? (
        <div className="mdl-users-measure">
          <p className="unit">Compute on this machine has not been measured, so there is no first-token estimate.</p>
          <button type="button" className="ghost" disabled={measuring} onClick={measure}>
            {measuring ? 'Measuring compute…' : 'Measure compute'}
          </button>
          {measureError ? <Verbatim text={measureError} size="unit" /> : null}
        </div>
      ) : (
        <PackChart
          type="multi-line"
          title={ttft ? 'Seconds to first token by number of users' : 'Tok/s per user by number of users'}
          height={220}
          data={chart.rows}
          options={{
            xType: 'number',
            xTitle: 'users',
            yTitle: ttft ? 'seconds to first token, no sooner than' : 'tok/s each',
            verticalMarkers: chart.verticalMarkers,
            pointsOnly: chart.pointsOnly,
            endLabels: true,
          }}
          onClick={onPoint}
        />
      )}
      {sentence && !noCompute ? <p className="unit">{sentence}</p> : null}
      {measured && !noCompute ? <p className="unit">Dots are {MEASURED_SERIES} by a benchmark of this variant.</p> : null}
      <Verbatim text={ttft ? merged.ttft_caveat : merged.caveat} size="unit" />
    </section>
  )
}
