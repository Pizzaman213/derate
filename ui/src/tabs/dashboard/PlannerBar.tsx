import { useEffect, useRef, useState } from 'react'
import type { PlanResponse } from '../../api/types'
import { useBackend } from '../../state/backend'
import { useCatalog } from '../../state/resources'
import { Verdict } from './Verdict'

const CUSTOM = '__custom__'

/** The fields row above the sub-tabs, always visible regardless of which one
 *  is active. Transplanted from the old standalone PlanView: the 450ms
 *  debounce, the sequence ref that drops a stale response, and the plain
 *  `POST /api/plan` dry run. What did NOT come across, on purpose, is
 *  everything mockups-next/js/planner.js computed client-side -- BPP tables,
 *  hand-rolled shapes, the USABLE constant, the launch-command mirror. The fit
 *  gate is the only thing allowed to say whether a model fits; duplicating its
 *  arithmetic here would be a second answer that can disagree with the real
 *  one. There is also no manual "Plan" button and no "Place on" field: replanning
 *  on every keystroke makes the button pointless, and placement is the
 *  planner's call, not a field a person fills in. */
export function PlannerBar() {
  const { backend, invalidate } = useBackend()
  // `GET /api/catalog`, not a copy of it. The same list used to live in
  // `ui/src/api/catalog.ts` as well, byte for byte, which is exactly the drift
  // moving it server-side was meant to end -- the Models tab read the wire while
  // this read the copy, so the two pickers could disagree about what exists.
  const catalog = useCatalog()
  const models = catalog.data ?? []

  const [curatedId, setCuratedId] = useState('')
  const [isCustom, setIsCustom] = useState(false)
  const [custom, setCustom] = useState('')
  // 8192/1 until the catalogue lands and its first entry says otherwise. It is
  // also what `GET /api/capacity` and the client's own fallback use, so a
  // verdict here and a verdict on the Models tab are answering the same
  // question.
  const [context, setContext] = useState(8192)
  const [concurrency, setConcurrency] = useState(1)
  const [target, setTarget] = useState<'throughput' | 'latency'>('throughput')
  const [runtime, setRuntime] = useState<'vllm' | 'sglang'>('vllm')

  const modelId = isCustom ? custom.trim() : curatedId

  const [result, setResult] = useState<PlanResponse | null>(null)
  const [checking, setChecking] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [launching, setLaunching] = useState(false)
  // An override is granted against one measurement. It is cleared on every
  // replan below, so it can never outlive the number that justified it.
  const [override, setOverride] = useState(false)

  const seq = useRef(0)
  const adopted = useRef(false)

  // The catalogue is fetched, so the first selection cannot be made at mount.
  // Adopt it once, and only while nothing has been chosen: doing it on every
  // catalogue poll would yank the field out from under whoever is using it.
  useEffect(() => {
    if (adopted.current || isCustom || curatedId) return
    const firstModel = models[0]
    if (!firstModel) return
    adopted.current = true
    setCuratedId(firstModel.model_id)
    setContext(firstModel.default_context)
    setConcurrency(firstModel.default_concurrency)
  }, [models, curatedId, isCustom])

  // Re-plan on any change, debounced. The dry run is cheap and starts
  // nothing, so making someone press a button to see the consequence of a
  // number they just typed is friction for its own sake.
  useEffect(() => {
    if (!modelId) {
      // Bump the sequence even on this early return: an in-flight request
      // from a model id the field no longer holds (e.g. the custom-HF-id
      // text was cleared while a plan was in flight) must not be allowed to
      // land and repopulate the verdict for a model that isn't there.
      ++seq.current
      setResult(null)
      setError(null)
      return
    }
    const mine = ++seq.current
    setChecking(true)
    setOverride(false)
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

  const launch = async () => {
    if (!result) return
    setLaunching(true)
    try {
      await backend.launch({
        model_id: modelId,
        context,
        concurrency,
        target,
        runtime,
        // Sent only when someone has read the sentence naming the measured
        // figure and ticked the box.
        ...(override ? { allow_over_live_memory: true } : {}),
      })
      invalidate()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLaunching(false)
    }
  }

  return (
    <div>
      <div className="bararea">
        <div className="fld" style={{ flex: 1, minWidth: 200 }}>
          <label htmlFor="pb-model">Model</label>
          <select
            id="pb-model"
            value={isCustom ? CUSTOM : curatedId}
            onChange={(e) => {
              const v = e.target.value
              if (v === CUSTOM) {
                setIsCustom(true)
                return
              }
              setIsCustom(false)
              setCuratedId(v)
              const m = models.find((x) => x.model_id === v)
              if (m) {
                setContext(m.default_context)
                setConcurrency(m.default_concurrency)
              }
            }}
          >
            {/* Empty until the catalogue lands. A hardcoded placeholder here
                would be a fifth copy of the list. */}
            {models.map((m) => (
              <option key={m.model_id} value={m.model_id}>
                {m.label}
              </option>
            ))}
            <option value={CUSTOM}>Custom HuggingFace ID…</option>
          </select>
        </div>

        {isCustom ? (
          <div className="fld" style={{ flex: 1, minWidth: 180 }}>
            <label htmlFor="pb-custom">HuggingFace ID</label>
            <input
              id="pb-custom"
              className="mono"
              value={custom}
              placeholder="org/model"
              spellCheck={false}
              onChange={(e) => setCustom(e.target.value)}
            />
          </div>
        ) : null}

        <div className="fld" style={{ width: 92 }}>
          <label htmlFor="pb-ctx">Context</label>
          <input
            id="pb-ctx"
            className="mono"
            type="number"
            min={1}
            value={context}
            onChange={(e) => {
              const n = Number(e.target.value)
              if (Number.isFinite(n) && n > 0) setContext(Math.round(n))
            }}
          />
        </div>

        <div className="fld" style={{ width: 70 }}>
          <label htmlFor="pb-seqs">Seqs</label>
          <input
            id="pb-seqs"
            className="mono"
            type="number"
            min={1}
            value={concurrency}
            onChange={(e) => {
              const n = Number(e.target.value)
              if (Number.isFinite(n) && n > 0) setConcurrency(Math.round(n))
            }}
          />
        </div>

        <div className="fld">
          <label htmlFor="pb-target">Optimise for</label>
          <select id="pb-target" value={target} onChange={(e) => setTarget(e.target.value as 'throughput' | 'latency')}>
            <option value="throughput">throughput</option>
            <option value="latency">latency</option>
          </select>
        </div>

        <div className="fld">
          <label htmlFor="pb-runtime">Runtime</label>
          <select id="pb-runtime" value={runtime} onChange={(e) => setRuntime(e.target.value as 'vllm' | 'sglang')}>
            <option value="vllm">vllm</option>
            <option value="sglang">sglang</option>
          </select>
        </div>
      </div>

      {result ? (
        <Verdict
          result={result}
          checking={checking}
          error={error}
          context={context}
          onUseMaxContext={setContext}
          onLaunch={() => void launch()}
          launching={launching}
          override={override}
          onOverride={setOverride}
        />
      ) : !modelId ? (
        <p className="unit" style={{ margin: '13px 0 0' }}>
          {catalog.loading && !models.length
            ? 'Loading the model list…'
            : 'Enter a HuggingFace ID to plan a model.'}
        </p>
      ) : error ? (
        <p className="label" style={{ color: 'var(--fault)', fontWeight: 400, margin: '13px 0 0' }}>{error}</p>
      ) : (
        <p className="unit" style={{ margin: '13px 0 0' }}>Checking…</p>
      )}
    </div>
  )
}
