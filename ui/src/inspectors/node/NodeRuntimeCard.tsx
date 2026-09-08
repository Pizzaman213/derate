import { useCallback, useEffect, useState } from 'react'
import type { NodeRuntime, PullAccepted } from '../../api/types'
import { Verbatim } from '../../components/Verbatim'
import { useBackend } from '../../state/backend'
import { ApiError } from '../../api/client'
import { gbytes } from '../../format'

/** A model runtime already listening on this node, and the one click that
 *  turns it into a route target.
 *
 *  The gap this closes is a sentence the Models tab was already showing: a
 *  machine with no GPU cannot carry a rank, but it can run a small model and
 *  be routed to -- "run Ollama on it, add it under Settings -> Providers, and
 *  it becomes a target here". That is correct and it is three steps, two of
 *  which are facts the coordinator already holds: the node's address, and the
 *  port the runtime listens on. Only installing it needs a human on that
 *  machine. So the other two happen here.
 *
 *  Deliberately silent when there is nothing. Most machines are not running a
 *  runtime and a GPU node has no reason to, so a card saying "no runtime
 *  found" on every node sheet would be noise on all of them to be useful on
 *  one. It renders only when there is something to say.
 *
 *  Nothing is adopted automatically. A provider is a routing target, and one
 *  that appeared without anybody choosing it is a request going somewhere
 *  nobody meant -- the same position the roster takes about a discovered node,
 *  where discovery proposes and a human accepts. */
/** The gateway's own error code, when the failure carried one. `ApiError.body`
 *  keeps the untouched envelope for exactly this. */
function errorCode(e: unknown): string | null {
  if (!(e instanceof ApiError)) return null
  try {
    const parsed = JSON.parse(e.body) as { error?: { code?: unknown } }
    return typeof parsed.error?.code === 'string' ? parsed.error.code : null
  } catch {
    return null
  }
}

export function NodeRuntimeCard({ nodeId }: { nodeId: string }) {
  const { backend, invalidate } = useBackend()
  const [state, setState] = useState<NodeRuntime | null>(null)
  const [busy, setBusy] = useState(false)
  // Keyed by model name, so loading one does not grey out the others.
  const [busyModel, setBusyModel] = useState<string | null>(null)
  const [want, setWant] = useState('')
  const [pulling, setPulling] = useState(false)
  const [pulled, setPulled] = useState<PullAccepted | null>(null)
  // Set only when the refusal was specifically about memory, because that is
  // the one this card can offer to do something about.
  const [overMemory, setOverMemory] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      setState(await backend.nodeRuntime(nodeId))
    } catch {
      // A probe that could not run is not worth a red box on a node sheet:
      // this card is an offer, and the absence of an offer is a fine outcome.
      setState(null)
    }
  }, [backend, nodeId])

  // Re-runs when the sheet switches nodes. `load` swallows its own failure,
  // so there is nothing here to catch.
  useEffect(() => {
    void load()
  }, [load])

  const adopt = async () => {
    setBusy(true)
    setError(null)
    try {
      await backend.adoptNodeRuntime(nodeId)
      // The provider list and every routing surface that reads it are stale
      // the moment this succeeds.
      invalidate()
      await load()
    } catch (e) {
      // The server's own sentence. It is the only thing that knows whether the
      // runtime stopped answering between the probe and the click.
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const setResident = async (model: string, resident: boolean) => {
    setBusyModel(model)
    setError(null)
    try {
      await backend.setRuntimeModel(nodeId, model, resident)
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusyModel(null)
    }
  }

  const pull = async () => {
    const model = want.trim()
    if (!model || !state?.provider_id) return
    setPulling(true)
    setError(null)
    setPulled(null)
    try {
      // The provider pull path, deliberately, rather than a second one of my
      // own: it is the thing that weighs the download against the machine's
      // measured free memory, and a pull that skipped that gate would be the
      // one filling an SD card nobody is watching.
      setPulled(await backend.pullToProvider(state.provider_id, { model }))
      setWant('')
      // The transfer is asynchronous, so the list will not show it yet. What
      // is worth reporting now is that it was accepted and judged.
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setOverMemory(errorCode(e) === 'pull_over_memory')
    } finally {
      setPulling(false)
    }
  }

  /** Unload everything resident, then try the same pull again.
   *
   *  The refusal names three ways out and this is the second of them -- "free
   *  memory on connor-pi" -- which the card is uniquely placed to act on,
   *  because it already knows what is holding it and already has the control
   *  to release it. The alternative is an operator reading a sentence about
   *  free memory with no way to act on it from the screen that said it.
   *
   *  Deliberately does NOT promise the retry will succeed. What is freed is
   *  known; whether it is enough is the gate's call, and it answers with its
   *  own numbers when the pull is re-attempted. Claiming here that it will fit
   *  would be doing the gate's arithmetic in a place that cannot see the
   *  budget fraction.
   */
  const freeAndRetry = async () => {
    const loaded = (state?.detected?.models ?? []).filter((m) => m.resident)
    setPulling(true)
    setError(null)
    try {
      for (const m of loaded) {
        await backend.setRuntimeModel(nodeId, m.name, false)
      }
      await load()
      setOverMemory(false)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setPulling(false)
      return
    }
    setPulling(false)
    await pull()
  }

  const detected = state?.detected
  if (!detected) return null
  const modelList = detected.models ?? []
  const residentModels = modelList.filter((m) => m.resident)
  const freeable = residentModels.reduce((n, m) => n + (m.resident_bytes ?? 0), 0)

  const models =
    detected.model_count === null
      ? 'it did not say how many models it holds'
      : detected.model_count === 1
        ? '1 model pulled'
        : `${detected.model_count} models pulled`

  return (
    <section className="node-runtime">
      <h4>Runtime on this machine</h4>
      <p className="node-runtime__found">
        <strong>{detected.kind}</strong> is answering at{' '}
        <code>{detected.base_url}</code> — {models}.
      </p>
      {state?.provider_id ? (
        <p className="node-runtime__done">
          Already a provider as <code>{state.provider_id}</code>. Its models
          start switched off: open Settings &rarr; Providers and enable the
          ones you want reachable, and they appear as served models here.
        </p>
      ) : (
        <>
          <p className="node-runtime__offer">
            This machine cannot carry a rank, but it can serve a small model
            over the network. Adding it as a provider makes it a target for the
            gateway; nothing is launched on the cluster. Its models arrive
            switched off, so this exposes nothing until you enable them.
          </p>
          <button type="button" onClick={adopt} disabled={busy}>
            {busy ? 'Adding…' : 'Add as a provider'}
          </button>
        </>
      )}
      {modelList.length ? (
        <ul className="node-runtime__models">
          {modelList.map((m) => (
            <li key={m.name}>
              <code>{m.name}</code>{' '}
              {m.size !== null ? (
                <span className="sub">{gbytes(m.size, 2)} GiB on disk</span>
              ) : null}
              {/* Loaded and downloaded are different states, and on a machine
                  this size the difference is what decides whether anything
                  else runs. `null` is neither -- the runtime would not say. */}
              {m.resident === null ? (
                <span className="sub"> · residency unknown</span>
              ) : m.resident ? (
                <span className="sub">
                  {' '}
                  · loaded
                  {m.resident_bytes
                    ? `, holding ${gbytes(m.resident_bytes, 2)} GiB`
                    : ''}
                </span>
              ) : (
                <span className="sub"> · not loaded</span>
              )}
              {detected.controllable && m.resident !== null ? (
                <button
                  type="button"
                  onClick={() => setResident(m.name, !m.resident)}
                  disabled={busyModel === m.name}
                >
                  {busyModel === m.name
                    ? m.resident
                      ? 'Unloading…'
                      : 'Loading…'
                    : m.resident
                      ? 'Unload'
                      : 'Load'}
                </button>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
      {state?.provider_id ? (
        <div className="node-runtime__pull">
          <label htmlFor={`pull-${nodeId}`}>Pull another model onto it</label>
          <input
            id={`pull-${nodeId}`}
            value={want}
            placeholder="qwen2.5:0.5b or hf.co/<repo>:<quant>"
            onChange={(e) => setWant(e.target.value)}
            disabled={pulling}
          />
          <button type="button" onClick={pull} disabled={pulling || !want.trim()}>
            {pulling ? 'Pulling…' : 'Pull'}
          </button>
          {/* Names the runtime understands, which is a wider set than the
              Models tab offers: that screen keeps one namespace across it and
              addresses everything as hf.co/<repo>:<quant>, so a name from the
              runtime's own library cannot be said there at all. */}
          <p className="sub">
            Any name this runtime accepts. The download is weighed against the
            machine&rsquo;s free memory before it starts.
          </p>
          {pulled ? (
            <p className="sub">
              Accepted {pulled.model} — {gbytes(pulled.download_bytes, 2)} GiB
              to fetch
              {pulled.gated
                ? `, against ${gbytes(pulled.free_bytes, 2)} GiB free on ${pulled.checked_against}`
                : ', unjudged: nothing in the roster claims that address'}
              . It appears above when it lands.
            </p>
          ) : null}
        </div>
      ) : null}
      {error ? <Verbatim text={error} /> : null}
      {overMemory && residentModels.length ? (
        <div className="node-runtime__free">
          <p className="sub">
            {residentModels.length === 1
              ? `${residentModels[0]!.name} is loaded`
              : `${residentModels.length} models are loaded`}
            {freeable
              ? `, holding ${gbytes(freeable, 2)} GiB that unloading would return`
              : ''}
            .
          </p>
          <button type="button" onClick={freeAndRetry} disabled={pulling}>
            {pulling
              ? 'Freeing…'
              : residentModels.length === 1
                ? `Unload ${residentModels[0]!.name} and retry`
                : 'Unload them and retry'}
          </button>
        </div>
      ) : null}
    </section>
  )
}
