import { useEffect, useRef, useState } from 'react'
import type { DeploymentDTO, DeploymentLogs } from '../../api/types'
import { Banner } from '../../components/Banner'
import { Verbatim } from '../../components/Verbatim'
import { useBackend } from '../../state/backend'
import { useMetrics } from '../../state/metrics'

const WARNING = /\b(WARN|WARNING|ERROR|CRITICAL|Traceback)\b/i

/** What the engine is doing right now, read off the metrics stream and its
 *  own log. Every figure here is a reading, not a prediction: `decode_tps` is
 *  what the engine counted, which is the number the fit gate's prediction in
 *  the verdict box can finally be checked against. */
export function LivePanel({ dep }: { dep: DeploymentDTO }) {
  const { frame } = useMetrics()
  const m = frame?.deployments.find((d) => d.deployment_id === dep.deployment_id) ?? null
  const load = m?.load ?? null

  const decode = load?.decode_tps ?? m?.tokens_per_sec ?? null
  const cells: { label: string; value: string; bar?: number | null }[] = [
    { label: 'decode', value: decode == null ? '—' : `${decode < 10 ? decode.toFixed(1) : decode.toFixed(0)} tok/s` },
    { label: 'first token (recent)', value: m?.ttft_ms == null ? '—' : `${m.ttft_ms.toFixed(0)} ms` },
  ]
  const hit = m?.prefix_cache_hit_pct ?? null
  if (hit != null) cells.push({ label: 'prefix cache hits', value: `${hit.toFixed(1)}%`, bar: hit / 100 })
  // Without a `load` block the engine was not scraped: those three cells are
  // missing readings, not zeroes, so they are left out rather than drawn as
  // dashes that look like an idle engine.
  if (load) {
    cells.push(
      {
        label: 'kv cache used',
        value: load.kv_cache_usage == null ? '—' : `${(load.kv_cache_usage * 100).toFixed(0)}%`,
        bar: load.kv_cache_usage,
      },
      { label: 'running', value: load.requests_running == null ? '—' : String(load.requests_running) },
      { label: 'waiting', value: load.requests_waiting == null ? '—' : String(load.requests_waiting) },
    )
  }
  cells.push({ label: 'queue', value: m?.queue_depth == null ? '—' : String(m.queue_depth) })

  return (
    <section className="mdl-live" aria-label="Live">
      <div className="mdl-live-grid">
        {cells.map((c) => (
          <div key={c.label} className="mdl-live-cell">
            <span className="unit">{c.label}</span>
            <span className="mono mdl-live-value">{c.value}</span>
            {c.bar != null ? (
              <span className="mdl-live-bar" aria-hidden="true">
                <i style={{ width: `${Math.min(100, Math.max(0, c.bar * 100))}%` }} />
              </span>
            ) : null}
          </div>
        ))}
      </div>
      <p className="unit mdl-live-caption">from the engine’s /metrics</p>
      <LogBox deploymentId={dep.deployment_id} />
    </section>
  )
}

/** The deployment's own log tail. Polled only while the source is the live
 *  buffer: an archive or a snapshot is a one-time read of a file on disk, and
 *  re-reading it every three seconds would fetch the same bytes for ever. */
function LogBox({ deploymentId }: { deploymentId: string }) {
  const { backend } = useBackend()
  const [logs, setLogs] = useState<DeploymentLogs | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [warningsOnly, setWarningsOnly] = useState(false)
  const seq = useRef(0)

  useEffect(() => {
    const mine = ++seq.current
    let timer: number | undefined
    const read = () => {
      backend
        .deploymentLogs(deploymentId, 200)
        .then((l) => {
          if (seq.current !== mine) return
          setLogs(l)
          setError(null)
          if (l.source === 'buffer') timer = window.setTimeout(read, 3000)
        })
        .catch((e: unknown) => {
          if (seq.current !== mine) return
          setError(e instanceof Error ? e.message : String(e))
        })
    }
    read()
    return () => {
      seq.current += 1
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [backend, deploymentId])

  const lines = (logs?.lines ?? []).filter((l) => !warningsOnly || WARNING.test(l))

  return (
    <div className="mdl-logs">
      <div className="mdl-logs-head">
        <span className="label">Logs</span>
        {logs ? <span className="unit">{logs.source}</span> : null}
        <label className="unit mdl-check">
          <input type="checkbox" checked={warningsOnly} onChange={(e) => setWarningsOnly(e.target.checked)} />
          Warnings only
        </label>
      </div>
      {error ? (
        <Verbatim text={error} size="unit" />
      ) : !logs ? (
        <p className="unit">Reading the log…</p>
      ) : logs.source === 'unavailable' || logs.source === 'none' ? (
        <p className="unit">{`No log lines (${logs.source}).`}</p>
      ) : (
        <pre className="mdl-logbox">
          {lines.map((l, i) => (
            <span key={i} className={WARNING.test(l) ? 'warn' : undefined}>
              {l}
              {'\n'}
            </span>
          ))}
        </pre>
      )}
    </div>
  )
}

/** The last attempt, when it failed and nothing is running in its place. */
export function FailureBanner({ dep, onDetails }: { dep: DeploymentDTO; onDetails: () => void }) {
  const first = dep.last_error?.split('\n').find((l) => l.trim()) ?? null
  return (
    <div className="mdl-failure">
      <Banner tone="fault">
        <span className="mdl-failure-body">
          <span>{`Last attempt failed: ${dep.state} · ${dep.served_name}`}</span>
          {first ? <Verbatim text={first} size="unit" /> : null}
        </span>
      </Banner>
      <button type="button" className="ghost" onClick={onDetails}>
        Details
      </button>
    </div>
  )
}
