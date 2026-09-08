import { useEffect, useMemo, useRef, useState } from 'react'
import type { Cluster, PlanResponse, Provider } from '../../api/types'
import type { Runtime } from '../../state/runtime'
import { RUNTIME_OPTIONS, servesOnCluster, shardsAcrossNodes } from '../../state/runtime'
import { useBackend } from '../../state/backend'
import { usePlacement } from '../../state/placement'
import { useRouter } from '../../state/router'
import { useMemoryReport, useTopology } from '../../state/resources'
import { recordCustomServe } from '../../state/customServes'
import { DEFAULT_CONCURRENCY, DEFAULT_CONTEXT } from '../../state/routes'
import { Verdict } from './Verdict'
import { DegreeFields } from './DegreeFields'
import { Disclosure } from '../../components/Panel'
import { buildBoard } from './board'
import { NodeBoard } from './NodeBoard'
import { ProviderPicker } from './ProviderPicker'
import type { CacheIndex } from './rows'

/** Where this model would run, and what the fit gate says about it there.
 *
 *  The model screen used to have no answer to "where": it fetched
 *  `/api/models/detail` and `/api/models/variants` and served whatever the
 *  planner had picked, with every control that could change it on a bar at the
 *  top of the dashboard. This is the dry run on the model's own URL, and since
 *  2026-09-07 it is the only one -- that bar is gone, because two plan surfaces
 *  answering the same question is one more than the answer needs, and this is
 *  the one that carries the question in the URL. The machine board stands where
 *  the bar had a checkbox popover: choosing between machines needs the figures
 *  a popover hides.
 *
 *  Nothing here computes a fit. The verdict, the breakdown, the refusal and
 *  the permissions all come off the wire; a second copy of that arithmetic in
 *  the browser is a second answer, and the one that disagrees with the gate is
 *  the one that costs somebody an out-of-memory kill.
 */
export function ServePanel({
  modelId,
  context,
  concurrency,
  target,
  onTarget,
  runtime,
  onRuntime,
  pullTargets,
  providerId,
  onProviderId,
  cache,
  cluster,
  initialCustomCommand,
}: {
  modelId: string
  /** Already debounced by the tab, so this and the quantization ladder below
   *  are always answering the same question at the same moment.
   *
   *  Null is the default path and means the coordinator chooses -- so it is
   *  spread out of the plan body rather than sent, exactly like `node_ids`.
   *  An explicit null there would be a third request, distinct from both
   *  "8192" and "you pick". */
  context: number | null
  concurrency: number | null
  target: 'throughput' | 'latency'
  onTarget: (t: 'throughput' | 'latency') => void
  runtime: Runtime
  onRuntime: (r: Runtime) => void
  /** Providers a pull can land on, derived once by the tab from the server's
   *  kind table. Empty is a normal state, not an error. */
  pullTargets: Provider[]
  providerId: string
  onProviderId: (id: string) => void
  /** Polled once by ModelInspector and shared with the quantization ladder.
   *  `useResource` does not deduplicate, so a second `useStorage` here would
   *  fan a second cluster-wide disk walk out every 30s for the same answer. */
  cache: CacheIndex
  cluster: Cluster | null
  /** Seeds the Verdict card's custom-command field from a custom serve
   *  picked on the list beside this pane (state/customServes.ts). Read once,
   *  at mount -- the pane is keyed on the model id, so picking a different
   *  custom serve always remounts rather than overwriting whatever is
   *  mid-edit here. */
  initialCustomCommand?: string
}) {
  const { backend, invalidate } = useBackend()
  const { route, navigate } = useRouter()
  const { nodeIds, degrees, setNodeIds, setDegrees } = usePlacement()

  // Whether this runtime splits a model at all, which is a different question
  // from whether it launches here: `tts` launches on the cluster and still has
  // no degrees.
  const shards = shardsAcrossNodes(runtime)
  // The one question the rest of this component asks about the runtime: does
  // Serve go through the launcher, or onto somebody else's box.
  const onCluster = servesOnCluster(runtime)

  const [result, setResult] = useState<PlanResponse | null>(null)
  const [checking, setChecking] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [launching, setLaunching] = useState(false)
  // Every override granted against this measurement, keyed by the launch field
  // it unlocks. Cleared wholesale on every replan below, so none can outlive
  // the number that justified it.
  const [granted, setGranted] = useState<Record<string, boolean>>({})
  // Raw CLI text that replaces the generated command, for a model the
  // standard recipe doesn't cover. Local state, not URL-linked like
  // context/concurrency: it has no sizing implication and no "what the plan
  // was judged at" to echo back on reload.
  const [customCommandText, setCustomCommandText] = useState(() => initialCustomCommand ?? '')

  const topology = useTopology()
  const memory = useMemoryReport()

  // What the advanced fields show. `route.context` is the override, or null
  // for "the coordinator chose"; in that case the boxes show what it actually
  // chose, taken off the plan result, and fall back to the placeholder only
  // until the first answer lands. A box that showed 8192 while the verdict
  // beside it was taken at 40960 would be the same lie the caption above the
  // model list was rewritten to stop telling.
  //
  // Typed straight into the address bar, replacing rather than pushing: this
  // is typing, and Back must leave the model rather than walk back through
  // 1, 16, 163, 1638, 16384. The tab debounces what it hands back down.
  const liveContext = route.context ?? result?.context ?? DEFAULT_CONTEXT
  const liveConcurrency = route.concurrency ?? result?.concurrency ?? DEFAULT_CONCURRENCY

  // Open when there is something to see: an override in the URL is somebody
  // else's decision arriving on a shared link, and hiding it behind a closed
  // disclosure would show numbers on screen that no visible control explains.
  const [advanced, setAdvanced] = useState(
    () => route.context !== null || route.concurrency !== null,
  )
  // Whether the custom-command checkbox in the Verdict card is ticked.
  // Separate from `customCommandText` itself so unchecking it disables the
  // tokens without discarding whatever was typed -- re-checking restores
  // them rather than making somebody retype. Starts ticked when a custom
  // serve seeded a value, since that field arrived non-empty on purpose.
  const [customCommand, setCustomCommand] = useState(() => !!initialCustomCommand)

  const seq = useRef(0)

  useEffect(() => {
    const mine = ++seq.current
    // Nothing to plan. `POST /api/plan` answers "does this fit on a machine in
    // this cluster", and under a provider runtime no machine here carries it
    // -- the weights land on a box derate does not own and does not size. A
    // verdict from that gate would be about the wrong memory pool, and it
    // would arrive looking exactly like one that meant something.
    //
    // This amends the note that used to sit at the bottom of this effect:
    // runtime was absent because "it changes what launches, not what fits".
    // That held while every runtime launched here. It now also changes
    // whether there is anything on this screen to fit, so it is in the
    // dependency list and it short-circuits.
    if (!servesOnCluster(runtime)) {
      setChecking(false)
      setResult(null)
      setError(null)
      setGranted({})
      return
    }
    setChecking(true)
    setGranted({})
    const id = window.setTimeout(() => {
      backend
        .plan({
          model_id: modelId,
          target,
          // Spread, never sent as null: absence is the contract for "the
          // planner picks", and an explicit null is a different request.
          // Context and concurrency join them for the same reason -- omitted,
          // the coordinator derives a context from what fits and reports what
          // it chose; sent as 8192, it answers a narrower question nobody
          // asked.
          ...(context ? { context } : {}),
          ...(concurrency ? { concurrency } : {}),
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
  }, [backend, modelId, context, concurrency, target, runtime, nodeIds, degrees])

  // A ticked machine can leave the cluster. Drop it rather than planning
  // against a name nothing answers to. The `every` short-circuit is not an
  // optimisation: without it this writes a fresh array on every 5s cluster
  // poll, which refires the effect above, forever.
  const nodes = useMemo(() => cluster?.nodes ?? [], [cluster])
  useEffect(() => {
    if (nodeIds == null) return
    const live = new Set(nodes.map((n) => n.profile.node_id))
    if (nodeIds.every((id) => live.has(id))) return
    const kept = nodeIds.filter((id) => live.has(id))
    setNodeIds(kept.length ? kept : null)
  }, [nodes, nodeIds, setNodeIds])

  const board = useMemo(
    () =>
      buildBoard({
        nodes,
        memory: memory.data?.nodes ?? [],
        edges: topology.data?.edges ?? [],
        deployments: cluster?.deployments ?? [],
        cache,
        repoId: modelId,
        plannerChose: result?.plan.node_ids ?? null,
        placement: result?.placement ?? null,
        chosen: nodeIds,
      }),
    [nodes, memory.data, topology.data, cluster, cache, modelId, result, nodeIds],
  )

  // The permissions this launch needs, from the backend. `overrides` is the
  // complete list when present; the older single-gate pair is the fallback for
  // a gateway that predates it, and no gates at all is the normal case.
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
      const dep = await backend.launch({
        model_id: modelId,
        // The numbers the verdict above was actually taken at, which on the
        // default path the coordinator chose rather than being told. Launching
        // at anything else would start a deployment the gate never checked --
        // the one thing this panel exists to prevent.
        context: result.context ?? liveContext,
        concurrency: result.concurrency ?? liveConcurrency,
        target,
        runtime,
        ...(nodeIds ? { node_ids: nodeIds } : {}),
        ...(degrees ? { parallelism: degrees } : {}),
        ...(customCommand && customCommandText.trim()
          ? { custom_command: customCommandText.trim() }
          : {}),
        // One key per gate the backend itself published, sent only where
        // somebody has read that gate's sentence and ticked it. Driven off the
        // published list rather than a fixed set of names, and filtered against
        // the current result, so a key granted against a previous plan cannot
        // ride along with this one.
        ...Object.fromEntries(
          gates.filter((g) => granted[g.param] === true).map((g) => [g.param, true]),
        ),
      })
      invalidate()
      // Worth replaying only once the backend has actually accepted it -- a
      // launch it rejected (an unsafe token, a runtime that cannot shard)
      // taught nothing -- and only if the box was actually ticked, exactly
      // the condition that decided whether it was sent above.
      const trimmedCommand = customCommandText.trim()
      if (customCommand && trimmedCommand) {
        recordCustomServe({ modelId, runtime, target, command: trimmedCommand })
      }
      // Take you to the thing you just started. A launch that leaves you on
      // the picker makes you go and find your own deployment, and the row here
      // only flips to "Running here" a poll later.
      //
      // `served_name`, NOT `deployment_id`: `?dep=` is matched against the
      // served name (state/selection.tsx), and an id here does not fail --
      // it falls through to defaultDep() and silently selects somebody else's
      // deployment, which is worse than not navigating at all.
      if (dep?.served_name) navigate({ dest: 'cluster', dep: dep.served_name })
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLaunching(false)
    }
  }

  return (
    <div>
      <div className="bararea">
        <div className="fld">
          <label htmlFor="sp-target">Optimise for</label>
          <select
            id="sp-target"
            value={target}
            onChange={(e) => onTarget(e.target.value as 'throughput' | 'latency')}
          >
            <option value="throughput">throughput</option>
            <option value="latency">latency</option>
          </select>
        </div>
        <div className="fld">
          <label htmlFor="sp-runtime">Runtime</label>
          <select
            id="sp-runtime"
            value={runtime}
            onChange={(e) => onRuntime(e.target.value as Runtime)}
          >
            {RUNTIME_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </div>
        {/* Degrees describe how one model is split across machines this
            cluster owns. A provider runtime serves from a single box that
            derate does not orchestrate, so there is nothing to split -- and
            neither is there under `tts`, which is one process holding one
            checkpoint. Hidden rather than shown and then refused: the launch
            would 400 with `runtime_cannot_shard`, and offering a control
            whose only outcome is that refusal is worse than not offering it. */}
        {shards ? (
          <DegreeFields
            idPrefix="sp"
            degrees={degrees}
            onChange={setDegrees}
            effective={result?.plan ?? null}
            recommended={result?.recommended_plan ?? null}
            overruled={result?.degrees?.source === 'operator'}
          />
        ) : null}
      </div>

      {/* Context and sequences, behind a disclosure and nowhere else.

          They used to be two plain fields here AND two more above the model
          list, before a model had been chosen. Both asked somebody to name a
          window in the units of a thing they had not picked yet, and the
          answer they gave was worse than the one the arithmetic gives: the
          coordinator solves for the largest context that actually fits at the
          best quantization that holds it, clamps to the model's own window,
          and reports what it chose. There is nothing to type on the default
          path, so there is no field on the default path.

          The pair stays reachable because "does it fit at 128k" is a real
          question somebody occasionally has, and the fields still write to the
          same two query parameters, so an answer is still linkable. What
          changed is that leaving them alone is now a request in its own right
          rather than a silent 8192. */}
      {onCluster ? (
        <div style={{ marginTop: 'var(--s-3)' }}>
          <Disclosure
            summary={
              route.context === null && route.concurrency === null
                ? 'Advanced — context and sequences are chosen for you'
                : 'Advanced — you have overridden context or sequences'
            }
            open={advanced}
            onToggle={() => setAdvanced((v) => !v)}
          >
            <div className="bararea">
              <div className="fld" style={{ width: 96 }}>
                <label htmlFor="sp-ctx">Context</label>
                <input
                  id="sp-ctx"
                  className="mono"
                  type="number"
                  min={1}
                  value={liveContext}
                  onChange={(e) => {
                    const n = Number(e.target.value)
                    if (Number.isFinite(n) && n > 0) {
                      navigate({ context: Math.round(n) }, { replace: true })
                    }
                  }}
                />
              </div>
              <div className="fld" style={{ width: 72 }}>
                <label htmlFor="sp-seq">Seqs</label>
                <input
                  id="sp-seq"
                  className="mono"
                  type="number"
                  min={1}
                  value={liveConcurrency}
                  onChange={(e) => {
                    const n = Number(e.target.value)
                    if (Number.isFinite(n) && n > 0) {
                      navigate({ concurrency: Math.round(n) }, { replace: true })
                    }
                  }}
                />
              </div>
              {/* The way back. Without it an override is a one-way door: once
                  a number is in the URL there is no value you can type that
                  means "you choose", and clearing the box types 0. */}
              {route.context !== null || route.concurrency !== null ? (
                <div className="fld">
                  <label htmlFor="sp-auto">&nbsp;</label>
                  <button
                    id="sp-auto"
                    onClick={() =>
                      navigate({ context: null, concurrency: null }, { replace: true })
                    }
                  >
                    Choose for me
                  </button>
                </div>
              ) : null}
            </div>
            <p className="unit" style={{ margin: '6px 0 0' }}>
              {route.context === null && route.concurrency === null
                ? result?.context
                  ? `Judged at ${result.context.toLocaleString()} context and ` +
                    `${result.concurrency ?? 1} ` +
                    `${(result.concurrency ?? 1) === 1 ? 'sequence' : 'sequences'} — ` +
                    'the largest window that fits here, capped at this model’s own.'
                  : 'The coordinator picks the largest window that fits, capped at ' +
                    'this model’s own.'
                : 'Every verdict on this screen is taken at these numbers.'}
            </p>
          </Disclosure>
        </div>
      ) : null}

      {/* `?on=` is left alone across the switch rather than cleared. It is not
          in this runtime's request, but it is in the URL, and discarding a
          machine selection because somebody looked at another runtime would
          lose it on a reload with nothing having been launched. */}
      {onCluster ? (
        <NodeBoard board={board} onChange={setNodeIds} />
      ) : (
        <>
          <ProviderPicker
            targets={pullTargets}
            chosen={providerId}
            onChoose={onProviderId}
            nodes={cluster?.nodes}
          />
          <p className="unit" style={{ marginTop: 'var(--s-3)' }}>
            This runtime does not launch on the cluster. It tells the provider to
            fetch a GGUF onto itself, and the gateway routes to it once it lands —
            so the quantizations below are the download, and the machine's own free
            memory is what the pull is judged against. A box that has not joined the
            cluster cannot be measured, and the pull proceeds unjudged rather than
            being refused; the reply says which of the two happened.
          </p>
        </>
      )}

      {!onCluster ? null : result ? (
        <div style={{ marginTop: 'var(--s-3)' }}>
          <Verdict
            result={result}
            checking={checking}
            error={error}
            // What the verdict was taken at, which on the default path the
            // coordinator chose. `Verdict` compares it against
            // `max_context_that_fits` to decide whether to offer the adjust
            // button, and comparing against a null override would offer to
            // "use" a context the plan is already at.
            context={result.context ?? liveContext}
            onUseMaxContext={(c) => navigate({ context: c }, { replace: true })}
            onLaunch={() => void launch()}
            launching={launching}
            gates={gates}
            granted={granted}
            onGrant={(param: string, value: boolean) =>
              setGranted((g) => ({ ...g, [param]: value }))
            }
            customCommandText={customCommandText}
            onCustomCommandTextChange={setCustomCommandText}
            customCommand={customCommand}
            onCustomCommandChange={setCustomCommand}
          />
        </div>
      ) : error ? (
        <p
          className="label"
          style={{
            color: 'var(--fault)',
            fontWeight: 400,
            whiteSpace: 'pre-wrap',
            margin: 'var(--s-3) 0 0',
          }}
        >
          {error}
        </p>
      ) : (
        <p className="unit" style={{ margin: 'var(--s-3) 0 0' }}>
          Checking where this fits…
        </p>
      )}
    </div>
  )
}
