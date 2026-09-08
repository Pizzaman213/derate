import { useEffect, useRef, useState } from 'react'
import type { DeploymentLogs } from '../api/types'
import { useBackend } from '../state/backend'

/** The backend's own log, in its own column of the deployment sheet.
 *
 *  The one thing the deployment sheet could not answer was "what is it
 *  actually doing" — and while a launch is in flight that is the only
 *  question anybody has. The lines are the launcher's output and then the
 *  container's, which is one story to whoever is reading it.
 *
 *  It used to be a `<details>` at the bottom of a single column, which put the
 *  answer below everything else on the sheet and behind a click. It is now a
 *  pane beside the rest, in `.logbox`'s frame — the same frame the node page
 *  gives its events and log — and the column it sits in is sticky, so it holds
 *  its place while the left column scrolls.
 *
 *  **The cost of the answer decides how it is asked for.** `GET
 *  /api/deployments/{id}/logs` reports its own `source`: `buffer` is the
 *  coordinator repeating lines it is already streaming, which is free and
 *  polled here; `read` is one bounded `sparkrun logs` against a deployment
 *  nothing is following any more — that command tails *and follows*, so it
 *  has to be cut off rather than waited out, and putting it on a timer would
 *  mean a subprocess every two seconds for as long as a sheet is open. It is
 *  fetched once and refreshed by hand.
 *
 *  That cost is why `autoOpen` exists rather than the pane simply always
 *  reading. A deployment the coordinator is still following costs nothing to
 *  show and is shown; a stopped one that nothing is following is a command on
 *  a machine, and stays one click away with the reason said out loud.
 */
export function DeploymentLog({
  deploymentId,
  autoOpen = true,
}: {
  deploymentId: string
  autoOpen?: boolean
}) {
  const { backend } = useBackend()
  const [open, setOpen] = useState(autoOpen)
  const [logs, setLogs] = useState<DeploymentLogs | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)
  const [nonce, setNonce] = useState(0)
  const boxRef = useRef<HTMLPreElement>(null)
  // Whether the reader is at the bottom, sampled before the next paint. A log
  // that scrolls itself is right until somebody scrolls up to read something,
  // at which point following the tail drags them away from it mid-sentence.
  const followRef = useRef(true)

  // A deployment that becomes worth reading while the sheet is open — a stop
  // that failed, a state that moved — opens the pane. Never the reverse: a log
  // somebody asked for does not close itself under them.
  useEffect(() => {
    if (autoOpen) setOpen(true)
  }, [autoOpen])

  useEffect(() => {
    if (!open) return
    let live = true
    let timer = 0

    const load = async () => {
      setPending(true)
      try {
        const answer = await backend.deploymentLogs(deploymentId)
        if (!live) return
        setLogs(answer)
        setError(null)
        // Only the free source is polled. See the note above.
        if (answer.source === 'buffer') timer = window.setTimeout(load, 2000)
      } catch (e) {
        if (!live) return
        setError(e instanceof Error ? e.message : String(e))
      } finally {
        if (live) setPending(false)
      }
    }
    void load()
    return () => {
      live = false
      window.clearTimeout(timer)
    }
  }, [backend, deploymentId, open, nonce])

  // Follow the tail, unless the reader has scrolled off it.
  useEffect(() => {
    const box = boxRef.current
    if (!box || !followRef.current) return
    box.scrollTop = box.scrollHeight
  }, [logs])

  const lines = logs?.lines ?? []

  return (
    <div className="logbox">
      <h4>
        <span>
          backend log
          {logs ? <span className="unit"> · {lines.length} lines</span> : null}
        </span>
        {!open ? (
          <button onClick={() => setOpen(true)}>Read it</button>
        ) : logs?.source === 'read' ? (
          <button onClick={() => setNonce((n) => n + 1)} disabled={pending}>
            {pending ? 'Reading…' : 'Refresh'}
          </button>
        ) : null}
      </h4>

      {lines.length > 0 && !error ? (
        <pre
          ref={boxRef}
          className="mono body"
          onScroll={(e) => {
            const el = e.currentTarget
            followRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24
          }}
        >
          {lines.join('\n')}
        </pre>
      ) : (
        <div className="body">
          {error ? (
            <p className="label" style={{ color: 'var(--fault)', fontWeight: 400, margin: 0 }}>
              {error}
            </p>
          ) : !open ? (
            <div className="unit">
              Nothing is following this deployment any more, so reading its log runs one command
              on the machine. Ask for it and it is read once.
            </div>
          ) : !logs ? (
            <div className="unit">Reading…</div>
          ) : (
            <div className="unit">
              {logs.source === 'unavailable'
                ? // Not "the backend printed nothing", which is a different fact.
                  'This control plane cannot read a backend log.'
                : 'Nothing has been read from this deployment yet.'}
            </div>
          )}
        </div>
      )}

      {open && lines.length > 0 ? (
        <div className="foot unit">
          {logs?.source === 'buffer'
            ? 'Live, as the coordinator reads it.'
            : 'Read once. This deployment is no longer being followed.'}
        </div>
      ) : null}
    </div>
  )
}
