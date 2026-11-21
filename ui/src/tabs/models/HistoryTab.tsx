import { useState } from 'react'
import type { DeploymentDTO, HistoryEvent } from '../../api/types'
import { Lamp } from '../../components/Lamp'
import { Verbatim } from '../../components/Verbatim'
import { useBackend } from '../../state/backend'
import { useRouter } from '../../state/router'
import { asRuntime } from '../../state/runtime'
import { launchId, variantKey } from './ollamaTarget'
import { deploymentSignal, TERMINAL } from './rows'
import type { ModelSession } from './useModelSession'

/** This model's deployments on this cluster, newest first, each with what it
 *  was launched with and what went wrong. Nothing is launched from here:
 *  "Roll back to this" only puts the settings back on the Serve tab, where the
 *  verdict is taken again before anything starts. */
export function HistoryTab({ s, onServeTab }: { s: ModelSession; onServeTab: () => void }) {
  const { navigate } = useRouter()

  if (!s.deployments.length) {
    return (
      <p className="unit">
        This model has not been served on this cluster recently. The coordinator keeps finished deployments for 6
        hours.
      </p>
    )
  }

  const again = (d: DeploymentDTO) => {
    // The server's own relaunch body is the whole launch, every knob included;
    // the hand-mapped fallback is for a record written before it existed.
    if (d.relaunch_request) {
      s.applyRequest(d.relaunch_request, null)
      onServeTab()
      return
    }
    s.setRuntime(asRuntime(d.runtime))
    const v = s.ladder?.variants.find((x) => launchId(x) === d.model_id)
    if (v) s.selectVariant(variantKey(v))
    s.setKvDtype(d.kv_dtype ?? '')
    s.setEnforceEager(!!d.enforce_eager)
    navigate({ context: d.context_length, concurrency: d.max_concurrent_seqs }, { replace: true })
    onServeTab()
  }

  return (
    <div className="mdl-history">
      {s.deployments.map((d) => (
        <HistoryRow key={d.deployment_id} d={d} onAgain={() => again(d)} />
      ))}
    </div>
  )
}

function HistoryRow({ d, onAgain }: { d: DeploymentDTO; onAgain: () => void }) {
  const { backend } = useBackend()
  const [events, setEvents] = useState<HistoryEvent[] | null>(null)
  const [eventsError, setEventsError] = useState<string | null>(null)
  const [open, setOpen] = useState(false)
  const signal = deploymentSignal(d.state)

  const toggle = () => {
    const next = !open
    setOpen(next)
    if (next && events == null) {
      backend
        .historyEvents({ deploymentId: d.deployment_id, limit: 50 })
        .then((r) => {
          setEvents(r.events)
          setEventsError(null)
        })
        .catch((e: unknown) => setEventsError(e instanceof Error ? e.message : String(e)))
    }
  }

  const sub = [
    d.runtime,
    d.node_ids.join(', '),
    `${d.context_length?.toLocaleString()} context`,
    `${d.max_concurrent_seqs} seqs`,
    d.kv_dtype ? `kv ${d.kv_dtype}` : null,
    d.quantization ? d.quantization : null,
    d.started_at != null ? `started ${new Date(d.started_at * 1000).toLocaleString()}` : null,
  ]
    .filter(Boolean)
    .join(' · ')

  return (
    <div className="mdl-hrow">
      <div className="mdl-hrow-head">
        <Lamp signal={signal} label={d.state} />
        <span className="label">{`${d.state} · ${d.served_name}`}</span>
        {!TERMINAL.has(d.state) ? <span className="mdl-pill">current</span> : null}
        <span className="mdl-hrow-actions">
          <button type="button" className="ghost" aria-expanded={open} onClick={toggle}>
            Events
          </button>
          {TERMINAL.has(d.state) ? (
            <button type="button" className="ghost" onClick={onAgain}>
              Roll back to this
            </button>
          ) : null}
        </span>
      </div>
      <span className="unit">{sub}</span>
      {d.last_error ? (
        <div className="mdl-hrow-error mono">
          <Verbatim text={d.last_error} size="unit" />
        </div>
      ) : null}
      {open ? (
        <div className="mdl-events">
          {eventsError ? (
            <Verbatim text={eventsError} size="unit" />
          ) : events == null ? (
            <p className="unit">Reading events…</p>
          ) : !events.length ? (
            <p className="unit">No events recorded for this deployment.</p>
          ) : (
            events.map((e, i) => {
              const said = (['reason', 'detail', 'message'] as const)
                .map((k) => e[k])
                .filter((v): v is string => typeof v === 'string' && v.length > 0)
              return (
                <div key={`${e.ts}-${i}`} className="mdl-event">
                  <span className="unit mono">{new Date(e.ts * 1000).toLocaleTimeString()}</span>
                  <span className="unit">{e.type}</span>
                  {said.map((t) => (
                    <Verbatim key={t} text={t} size="unit" />
                  ))}
                </div>
              )
            })
          )}
        </div>
      ) : null}
    </div>
  )
}
