import { useState } from 'react'
import type { GpuProcessDTO } from '../../api/types'
import { Verbatim } from '../../components/Verbatim'
import { useBackend } from '../../state/backend'
import { useNodeProcesses } from '../../state/resources'
import { useSelection } from '../../state/selection'
import { gbytes } from '../../format'

/** What is actually holding the GPU, whether or not we launched it.
 *
 *  The gap this closes: `nvidia-smi --query-compute-apps` is the only memory
 *  number GB10 will give you, the fit gate plans against it, and until now
 *  nothing in the product could say what was behind it. A leftover
 *  llama-server holding 72 GiB reads here as 72 GiB of headroom gone with no
 *  deployment to explain it, and the only way to get it back was a shell.
 *
 *  A process we launched is deliberately NOT killable from this list. Killing
 *  a backend the router is still dispatching to would leave the deployment
 *  claiming READY while the process is gone, and the operator who clicked it
 *  would see 502s with nothing connecting them to their own click. Those rows
 *  point at the deployment instead, whose Stop drains first. */
export function ResidentProcesses({ nodeId }: { nodeId: string }) {
  const { backend, invalidate } = useBackend()
  const { data, loading } = useNodeProcesses(nodeId)
  const selection = useSelection()
  // Keyed by pid, so one row's kill does not grey out the others.
  const [busy, setBusy] = useState<Record<number, boolean>>({})
  const [error, setError] = useState<string | null>(null)
  const [note, setNote] = useState<string | null>(null)

  const kill = async (proc: GpuProcessDTO) => {
    if (
      !window.confirm(
        `Kill ${proc.name} (pid ${proc.pid})? It is holding ${gbytes(proc.gpu_memory, 1)} GiB. ` +
          `SIGTERM first, then SIGKILL if it has not exited in 10 seconds. ` +
          `Anything it is currently serving stops immediately.`,
      )
    )
      return
    setBusy((b) => ({ ...b, [proc.pid]: true }))
    setError(null)
    setNote(null)
    try {
      const result = await backend.killProcess(nodeId, proc.pid)
      // The server's own sentence: it is the only thing that knows whether
      // SIGTERM was enough and whether the driver actually released.
      setNote(result.detail)
      invalidate()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy((b) => ({ ...b, [proc.pid]: false }))
    }
  }

  const processes = data?.processes ?? []

  return (
    <>
      <div className="sub">resident on this GPU</div>
      {loading && data == null ? (
        <div className="unit">Reading…</div>
      ) : processes.length === 0 ? (
        <div className="unit">{data?.reason ?? 'Nothing is holding GPU memory.'}</div>
      ) : (
        processes.map((proc) => (
          <div key={proc.pid} className="row">
            <span className="mono" title={proc.command ?? undefined}>
              {proc.name}
            </span>
            <span style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
              <span className="unit">
                pid {proc.pid} · {gbytes(proc.gpu_memory, 1)} GiB ·{' '}
                {proc.served_name ?? 'not managed here'}
              </span>
              {proc.killable ? (
                <button onClick={() => void kill(proc)} disabled={busy[proc.pid]}>
                  {busy[proc.pid] ? 'Killing…' : 'Kill'}
                </button>
              ) : (
                <button
                  className="ghost"
                  title={proc.not_killable_reason ?? undefined}
                  onClick={() =>
                    proc.served_name &&
                    selection.openSheet({ kind: 'dep', id: proc.served_name })
                  }
                >
                  ↗
                </button>
              )}
            </span>
          </div>
        ))
      )}
      {note ? (
        <div style={{ marginTop: 8 }}>
          <Verbatim text={note} size="label" />
        </div>
      ) : null}
      {error ? (
        <p className="label" style={{ color: 'var(--fault)', fontWeight: 400, margin: '8px 0 0' }}>
          {error}
        </p>
      ) : null}
    </>
  )
}
