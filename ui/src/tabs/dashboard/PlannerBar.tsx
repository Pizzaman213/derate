import { useEffect, useMemo, useRef, useState } from 'react'
import type { ParallelismRequest, PlanResponse } from '../../api/types'
import { useBackend } from '../../state/backend'
import { useCatalog, useCluster, useStorage } from '../../state/resources'
import { DegreeFields } from './DegreeFields'
import { MachinePicker } from './MachinePicker'
import { modelGroups } from './modelOptions'
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
 *  one. There is still no manual "Plan" button: replanning on every keystroke
 *  makes it pointless.
 *
 *  Placement and the degrees ARE fields, as of 2026-09-07. This reverses what
 *  this comment used to say ("placement is the planner's call, not a field a
 *  person fills in") and restores what `agents/E-planner.md` specified all
 *  along: "`alternatives` returns every legal plan ranked, so the UI can offer
 *  an override. The user can always override; you are a recommendation with
 *  reasoning, not a lock." The planner still ranks, still writes the reason,
 *  and its own pick comes back in `recommended_plan` whenever it is overruled.
 *
 *  What is NOT overridable is the arithmetic. An overruled shape is sent back
 *  through `POST /api/plan`, and the verdict, the breakdown and the Serve
 *  button all describe the shape that will actually launch. Sending no machines
 *  and no degrees is byte for byte the request this bar sent before the fields
 *  existed. */
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
  // Every override granted against this measurement, keyed by the launch field
  // it unlocks. Cleared wholesale on every replan below, so none can outlive
  // the number that justified it. A record rather than a boolean because a
  // launch can need more than one permission at once.
  const [granted, setGranted] = useState<Record<string, boolean>>({})

  // null means "the planner picks", and sends no field at all. Only an explicit
  // toggle materialises a value -- which is what makes "a request with neither
  // is exactly the old request" true by construction rather than by care.
  //
  // These MUST stay plain state. Deriving either through a `useMemo` with
  // unstable inputs, defaulting one inline to `[]`, or rebuilding it on each
  // 5s cluster poll gives it a new identity every render, and the debounced
  // effect below -- which has both in its dependency array -- then refires
  // forever. Sixty seconds of an untouched dashboard must produce exactly one
  // POST /api/plan.
  const [nodeIds, setNodeIds] = useState<string[] | null>(null)
  const [degrees, setDegrees] = useState<ParallelismRequest | null>(null)

  const cluster = useCluster()
  const nodes = useMemo(() => cluster.data?.nodes ?? [], [cluster.data])

  // The cache walk fans out to every node and walks a directory on each. Gated
  // behind first contact with the model field so a dashboard nobody is
  // planning on never triggers one. Same shape as `useNodeProcesses`, which
  // resolves an empty payload rather than being skipped -- hooks cannot be
  // called conditionally.
  const [cacheWanted, setCacheWanted] = useState(false)
  const storage = useStorage(cacheWanted)

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
    setGranted({})
    const id = window.setTimeout(() => {
      backend
        .plan({
          model_id: modelId,
          context,
          concurrency,
          target,
          // Spread, never sent as null: absence is the contract for "the
          // planner picks", and an explicit null would be a different request.
          ...(nodeIds ? { node_ids: nodeIds } : {}),
          ...(degrees ? { parallelism: degrees } : {}),
        })
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
    // `runtime` is deliberately absent: it changes what launches, not what
    // fits, so replanning on it would be a round trip for an unchanged answer.
    // `nodeIds` and `degrees` both change the sizing, so both belong here --
    // see the identity warning where they are declared.
  }, [backend, modelId, context, concurrency, target, nodeIds, degrees])

  // A ticked machine can leave the cluster. Drop it rather than planning
  // against a name nothing answers to -- the same fallback `selection.tsx`
  // applies to a deployment that stops being served.
  //
  // The `every` short-circuit is not an optimisation. Without it this hands
  // `setNodeIds` a freshly built array on every 5s cluster poll, which refires
  // the debounced effect above, forever.
  useEffect(() => {
    if (nodeIds == null) return
    const live = new Set(nodes.map((n) => n.profile.node_id))
    if (nodeIds.every((id) => live.has(id))) return
    const kept = nodeIds.filter((id) => live.has(id))
    setNodeIds(kept.length ? kept : null)
  }, [nodes, nodeIds])

  // The permissions this launch needs, from the backend. `overrides` is the
  // complete list when present; the older single-gate pair is the fallback for
  // a gateway that predates it, and no gates at all is the normal case.
  const groups = useMemo(
    () =>
      modelGroups({
        curated: models,
        deployments: cluster.data?.deployments ?? [],
        storage: storage.data?.nodes ?? null,
        selected: isCustom ? null : curatedId || null,
      }),
    [models, cluster.data, storage.data, isCustom, curatedId],
  )

  const serve = result?.serve
  const gates = useMemo(
    () =>
      serve?.overrides ??
      (serve?.override_required
        ? [
            {
              param: serve.override_param ?? 'allow_over_live_memory',
              reason: serve.reason,
            },
          ]
        : []),
    [serve],
  )

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
        ...(nodeIds ? { node_ids: nodeIds } : {}),
        ...(degrees ? { parallelism: degrees } : {}),
        // One key per gate the backend itself published, sent only where
        // someone has read that gate's sentence and ticked it. Driven off the
        // published list rather than a fixed set of names, so a gateway that
        // adds a third gate tomorrow works with no change here -- and filtered
        // against the current result, so a key granted against a previous plan
        // can never ride along with this one.
        ...Object.fromEntries(
          gates
            .filter((g) => granted[g.param] === true)
            .map((g) => [g.param, true]),
        ),
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
              // A serving model carries its real configured numbers, not a
              // guess; a cached one carries none, and changes nothing.
              const m = groups.flatMap((g) => g.options).find((x) => x.model_id === v)
              if (m?.default_context) setContext(m.default_context)
              if (m?.default_concurrency) setConcurrency(m.default_concurrency)
            }}
            onFocus={() => setCacheWanted(true)}
            onPointerDown={() => setCacheWanted(true)}
          >
            {/* Empty until the catalogue lands. A hardcoded placeholder here
                would be a fifth copy of the list. */}
            {groups.map((g) => (
              <optgroup key={g.label} label={g.label}>
                {g.options.map((m) => (
                  <option
                    key={`${g.label}:${m.model_id}`}
                    value={m.model_id}
                    disabled={m.disabled}
                  >
                    {m.detail ? `${m.label} — ${m.detail}` : m.label}
                  </option>
                ))}
              </optgroup>
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

        <MachinePicker
          nodes={nodes}
          plannerChose={result?.plan.node_ids ?? null}
          placement={result?.placement ?? null}
          chosen={nodeIds}
          onChange={setNodeIds}
        />

        <DegreeFields
          degrees={degrees}
          onChange={setDegrees}
          effective={result?.plan ?? null}
          recommended={result?.recommended_plan ?? null}
          overruled={result?.degrees?.source === 'operator'}
        />

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
          gates={gates}
          granted={granted}
          onGrant={(param: string, value: boolean) =>
            setGranted((g) => ({ ...g, [param]: value }))
          }
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
