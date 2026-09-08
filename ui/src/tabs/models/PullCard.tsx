import { useState } from 'react'
import type { ProviderKindSpec } from '../../api/types'
import { useCluster, useProviders } from '../../state/resources'
import { pullableProviders } from './pullTargets'
import { useBackend } from '../../state/backend'
import { ApiError } from '../../api/client'
import { Verbatim } from '../../components/Verbatim'
import { gbytes } from '../../format'

/** Put a model on a server derate does not launch.
 *
 *  The Serve button next to a quantization launches vLLM or SGLang on a node
 *  in the cluster. A box with no GPU cannot run either, so it joins as a
 *  provider instead -- somebody else's hardware, reached over the network --
 *  and the way to get a model onto it is to tell it to fetch one.
 *
 *  This card names the model the way that server names it -- `qwen2.5:0.5b`,
 *  `gemma3:270m` -- and that is now what it is for. The quantization ladder is
 *  the primary route: picking the ollama runtime there sends the row you
 *  chose, as `hf.co/<repo>:<tag>`, so the thing on screen and the thing served
 *  are one object.
 *
 *  That reverses half of what this comment used to say. It argued the pull was
 *  deliberately a second namespace because "the ladder's sizes, variants and
 *  fit verdicts describe weights this cluster would hold, and none of them
 *  survive the trip". The fit verdicts genuinely do not -- which is why the
 *  ladder suppresses every one of them under that runtime rather than reusing
 *  them. The sizes and variants do: `file_bytes` is a measured download, and
 *  it is the number the pull gate weighs. What is left here is the escape
 *  hatch for Ollama's own names, which no ladder row can express.
 */
export function PullCard({
  providerKinds,
}: {
  /** Polled once by the tab. `useResource` does not deduplicate, so mounting
   *  `useProviderKinds` here as well would be a second interval for one
   *  static table -- and two answers to "which providers can be pulled onto". */
  providerKinds?: ProviderKindSpec[] | null
}) {
  const providers = useProviders()
  const cluster = useCluster()
  const { backend, invalidate } = useBackend()
  const [providerId, setProviderId] = useState('')
  const [model, setModel] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [refusal, setRefusal] = useState<string | null>(null)
  const [override, setOverride] = useState(false)
  const [done, setDone] = useState<string | null>(null)

  const targets = pullableProviders(providers.data, providerKinds)

  // With no provider configured this used to render nothing at all, on the
  // reasoning that a control which cannot act is worse than an absent one.
  // That was wrong in the way that matters: a machine with no GPU is told, on
  // the board directly above this, that it cannot carry a rank -- and the one
  // path that does work was invisible, so the screen read as "this machine is
  // useless" with nothing to disagree with it. An empty state that names the
  // machines this is for, and where to enable it, is not a dead control.
  const first = targets[0]
  if (!first) {
    const gpuless = (cluster.data?.nodes ?? []).filter(
      (n) => n.profile.gpu_count === 0,
    )
    return (
      <div className="card2">
        <h3>Run on a provider</h3>
        <div className="unit">
          {gpuless.length
            ? `${gpuless
                .map((n) => n.profile.node_id)
                .join(', ')} ${gpuless.length === 1 ? 'has' : 'have'} no GPU, so ${
                gpuless.length === 1 ? 'it cannot' : 'they cannot'
              } carry a rank — but a machine like that can still serve a small model over
               the network, and this cluster can route to it. Run a server on it, add it
               under Settings → Providers, and it becomes a target here.`
            : 'A machine that cannot carry a rank can still serve a small model over the network. Add it under Settings → Providers and it becomes a target here.'}
        </div>
      </div>
    )
  }

  const chosen = providerId || first.provider_id

  const pull = async (allowOverMemory = false) => {
    setBusy(true)
    setError(null)
    setDone(null)
    if (!allowOverMemory) setRefusal(null)
    try {
      const reply = await backend.pullToProvider(chosen, {
        model: model.trim(),
        ...(allowOverMemory ? { allow_over_memory: true } : {}),
      })
      setDone(
        reply.download_bytes
          ? // Points at the rail, where the bar is. This used to say only "it
            // appears here when the download finishes", which was the whole
            // problem: a 70B pull was minutes of silence after one sentence.
            //
            // The restart clause stays. The transfer runs as a background task
            // in the coordinator, so restarting it drops the download and
            // leaves the far end holding a partial file. Cheap to say, and the
            // alternative is somebody watching a long pull vanish unexplained.
            `Pulling ${reply.model} — ${gbytes(reply.download_bytes, 2)} GiB. Progress is in the sidebar under Activity, and it appears here when the download finishes. Restarting the coordinator cancels it.`
          : `${reply.model} is already on that machine.`,
      )
      setRefusal(null)
      setOverride(false)
      setModel('')
      invalidate()
    } catch (e) {
      // 409 is the memory gate, and an unchanged retry succeeds once space
      // frees. It gets the override path; a 400 is a different answer and
      // correctly does not.
      if (e instanceof ApiError && e.status === 409) {
        setRefusal(e.message)
        setOverride(false)
      } else {
        setError(e instanceof Error ? e.message : String(e))
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="card2">
      <h3>Run on a provider</h3>
      <div className="unit" style={{ marginBottom: 10 }}>
        A machine with no GPU cannot run vLLM or SGLang, so it is reached as a provider
        rather than launched onto. Name the model the way that server names it — to send
        a quantization from a HuggingFace repository instead, open the model and pick the
        ollama runtime.
      </div>

      <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end', flexWrap: 'wrap' }}>
        <div className="fld">
          <label htmlFor="pullprov">Provider</label>
          <select
            id="pullprov"
            value={chosen}
            onChange={(e) => setProviderId(e.target.value)}
          >
            {targets.map((p) => (
              <option key={p.provider_id} value={p.provider_id}>
                {p.display_name}
              </option>
            ))}
          </select>
        </div>
        <div className="fld" style={{ flex: 1, minWidth: 200 }}>
          <label htmlFor="pullmodel">Model</label>
          <input
            id="pullmodel"
            value={model}
            onChange={(e) => setModel(e.target.value)}
            placeholder="qwen2.5:0.5b"
            spellCheck={false}
          />
        </div>
        <button onClick={() => void pull()} disabled={busy || !model.trim()}>
          {busy ? 'Pulling…' : 'Pull and serve'}
        </button>
      </div>

      {refusal ? (
        <div style={{ marginTop: 10 }}>
          {/* The server's sentence, which names both figures and the way past
              it. Rewriting it here would be a second answer. */}
          <Verbatim text={refusal} size="label" />
          <label style={{ display: 'flex', gap: 6, alignItems: 'center', marginTop: 6 }}>
            <input
              type="checkbox"
              checked={override}
              onChange={(e) => setOverride(e.target.checked)}
            />
            <span className="unit">Pull it anyway</span>
          </label>
          <button
            style={{ marginTop: 6 }}
            disabled={!override || busy}
            onClick={() => void pull(true)}
          >
            Pull over the limit
          </button>
        </div>
      ) : null}

      {done ? (
        <div className="label" style={{ marginTop: 8 }}>
          {done}
        </div>
      ) : null}
      {error ? (
        <div
          className="label"
          style={{ color: 'var(--fault)', marginTop: 8, whiteSpace: 'pre-wrap' }}
        >
          {error}
        </div>
      ) : null}
    </div>
  )
}
