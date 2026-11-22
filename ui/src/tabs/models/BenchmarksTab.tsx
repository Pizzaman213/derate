import { useState } from 'react'
import type { BenchPoint, BenchRecord, LaunchRequest } from '../../api/types'
import { Verbatim } from '../../components/Verbatim'
import { useBackend } from '../../state/backend'
import { useBenchmarks } from '../../state/resources'
import { changedKeys, profileSummary } from './profiles'
import type { ModelSession } from './useModelSession'

const at1 = (r: BenchRecord): BenchPoint | null => r.points.find((p) => p.users === 1) ?? r.points[0] ?? null

const ms = (v: number | null | undefined) => (v == null ? '—' : `${Math.round(v)} ms`)
const tps = (v: number | null | undefined) => (v == null ? '—' : `${v.toFixed(1)} tok/s`)

/** Drafted tokens per step the run was launched with, if it speculated. */
function draftK(req: LaunchRequest): number | null {
  const k = req.speculative?.num_speculative_tokens ?? req.speculative_tokens ?? null
  return k != null && k > 0 ? k : null
}

function acceptance(r: BenchRecord): string {
  const acc = r.spec_mean_acceptance
  if (acc == null) return '—'
  const k = draftK(r.request)
  return k != null ? `${(acc * k).toFixed(1)} of ${k}` : `${Math.round(acc * 100)}%`
}

/** The result line a profile or run card shows: the newest finished benchmark
 *  whose request is the same launch (no changed keys), else null. */
export function benchResult(records: BenchRecord[] | null, req: LaunchRequest): string | null {
  const hit = (records ?? []).find((r) => r.state === 'done' && changedKeys(r.request, req).length === 0)
  if (!hit) return null
  const p = at1(hit)
  return `Benchmarked: first token ${ms(p?.ttft_ms_p50)}, ${tps(p?.tps_each_mean)} at ${p?.users ?? 1} user${
    p?.users === 1 ? '' : 's'
  }`
}

/** Measured numbers for this model: each run launches a throwaway copy with
 *  the settings it names, drives it at a few user counts and stops it. */
export function BenchmarksTab({ s, modelId }: { s: ModelSession; modelId: string }) {
  const { backend, invalidate } = useBackend()
  const benches = useBenchmarks(modelId)
  const [error, setError] = useState<string | null>(null)
  const [ticked, setTicked] = useState<Set<string>>(new Set())
  const records = benches.data ?? []
  const running = records.find((r) => r.state === 'launching' || r.state === 'measuring') ?? null

  const act = (p: Promise<unknown>) =>
    p.then(
      () => {
        setError(null)
        invalidate()
      },
      (e: unknown) => setError(e instanceof Error ? e.message : String(e)),
    )

  const run = () => {
    if (
      window.confirm(
        `This launches a temporary copy of ${modelId} with these settings, measures it at 1, 4, 8 and 16 users, then stops it. It needs free memory and takes several minutes.`,
      )
    ) {
      void act(backend.runBenchmark({ request: s.currentRequest() }))
    }
  }

  const tick = (id: string) =>
    setTicked((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  const compared = records.filter((r) => ticked.has(r.bench_id) && r.state === 'done')
  const top = Math.max(1, ...compared.map((r) => at1(r)?.tps_each_mean ?? 0))
  const topTtft = Math.max(1, ...compared.map((r) => at1(r)?.ttft_ms_p50 ?? 0))

  return (
    <div className="mdl-bench">
      <p className="unit">
        A benchmark serves a temporary copy with the current settings, sends real requests at 1, 4, 8 and 16
        concurrent users, records first-token time and tokens per second, then stops the copy.
      </p>
      <div className="mdl-profiles-bar">
        <button type="button" onClick={run} disabled={running != null}>
          Run benchmark with current settings
        </button>
      </div>
      {error ? <Verbatim text={error} size="unit" /> : null}
      {benches.error ? <Verbatim text={benches.error.message} size="unit" /> : null}
      {running ? (
        <div className="mdl-hrow-head">
          <span className="label">{`Benchmark ${running.state}…`}</span>
          <span className="unit">{profileSummary(running.request).join(' · ')}</span>
          <span className="mdl-hrow-actions">
            <button
              type="button"
              className="ghost"
              onClick={() => void act(backend.cancelBenchmark(running.bench_id))}
            >
              Cancel
            </button>
          </span>
        </div>
      ) : null}
      {!records.length ? (
        <p className="unit">No benchmarks for this model yet.</p>
      ) : (
        <table className="mdl-bench-table">
          <thead>
            <tr>
              <th scope="col">
                <span className="sr-only">Compare</span>
              </th>
              <th scope="col">Setup</th>
              <th scope="col">First token</th>
              <th scope="col">tok/s at 1 user</th>
              <th scope="col">Acceptance</th>
              <th scope="col">When</th>
            </tr>
          </thead>
          <tbody>
            {records.map((r) => {
              const p = at1(r)
              return (
                <tr key={r.bench_id}>
                  <td>
                    <input
                      type="checkbox"
                      aria-label={`Compare benchmark ${r.bench_id}`}
                      checked={ticked.has(r.bench_id)}
                      disabled={r.state !== 'done'}
                      onChange={() => tick(r.bench_id)}
                    />
                  </td>
                  <td>
                    <span className="unit">{profileSummary(r.request).join(' · ')}</span>
                    {r.state !== 'done' ? <span className="mdl-pill">{r.state}</span> : null}
                    {r.error ? <Verbatim text={r.error} size="unit" /> : null}
                  </td>
                  <td className="mono">{ms(p?.ttft_ms_p50)}</td>
                  <td className="mono">{tps(p?.tps_each_mean)}</td>
                  <td className="mono">{acceptance(r)}</td>
                  <td className="unit">{new Date(r.started_at * 1000).toLocaleString()}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
      {compared.length >= 2 ? (
        <div className="mdl-bench-compare" aria-label="Compare tok/s at 1 user">
          <div className="sub">Compare · tok/s at 1 user</div>
          {compared.map((r) => {
            const v = at1(r)?.tps_each_mean ?? 0
            return (
              <div key={r.bench_id} className="mdl-bench-bar-row">
                <span className="unit">{profileSummary(r.request).join(' · ')}</span>
                <span className="mdl-bench-bar">
                  <span className="mdl-bench-bar-fill" style={{ width: `${(v / top) * 100}%` }} />
                </span>
                <span className="mono unit">{tps(v)}</span>
              </div>
            )
          })}
          <div className="sub">Compare · first token at 1 user</div>
          {compared.map((r) => {
            const v = at1(r)?.ttft_ms_p50 ?? null
            return (
              <div key={r.bench_id} className="mdl-bench-bar-row">
                <span className="unit">{profileSummary(r.request).join(' · ')}</span>
                <span className="mdl-bench-bar">
                  <span className="mdl-bench-bar-fill" style={{ width: `${((v ?? 0) / topTtft) * 100}%` }} />
                </span>
                <span className="mono unit">{ms(v)}</span>
              </div>
            )
          })}
          <p className="unit">Longer decode bars are better; shorter first-token bars are better.</p>
        </div>
      ) : records.some((r) => r.state === 'done') ? (
        <p className="unit">Tick two or more finished runs to compare them.</p>
      ) : null}
    </div>
  )
}
