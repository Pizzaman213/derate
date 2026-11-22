/** The launch body, built in one place and read back in one place.
 *
 *  `buildLaunchRequest` is what `useModelSession` sends to
 *  `POST /api/deployments`; `knobsFromRequest` is its inverse, for a saved
 *  profile or a deployment's `relaunch_request`. They live together, and out
 *  of React, so `launchRequest.check.mjs` can prove one undoes the other:
 *  a knob one of them forgets is a setting a profile silently drops.
 *
 *  The two rules the builder keeps, unchanged from when it was inline:
 *  - spread, never null: an absent key is the request ("the planner picks",
 *    "the coordinator's default"), and an explicit null is a different one;
 *  - a custom command replaces every plan-derived flag, so nothing it would
 *    replace is sent beside it -- the coordinator refuses the pair.
 */

import type {
  EngineOptions,
  LaunchRequest,
  Lifecycle,
  ParallelismRequest,
  QuantVariant,
  SpeculativeRequest,
} from '../../api/types'
import type { Route, SpecRef } from '../../state/routes'
import { degreesFromRoute } from '../../state/routes'
import { launchId } from './ollamaTarget'

/** One flag row in the Advanced tab: a key and an optional value. */
export interface FlagRow {
  k: string
  v: string
}

/** Every setting that is NOT in the URL. The URL owns context, concurrency,
 *  machines, degrees and speculation; these are the rest. */
export interface SessionKnobs {
  /** What is launched: the base id or a variant's `launchId`. */
  modelId: string
  target: string
  runtime: string
  customCommand: boolean
  customCommandText: string
  flags: FlagRow[]
  servedName: string
  enforceEager: boolean
  /** Already parsed; null is the runtime's own sizing. */
  cudagraphCaptureSizes: number[] | null
  /** '' is the coordinator's configured default. */
  kvDtype: string
  /** '' is the checkpoint's own packing. */
  forcedQuant: string
  /** MTP draft tokens, 0 when off. */
  speculativeTokens: number
  /** Whether the launch opts in to remote code. The caller has already
   *  ANDed this with the resolver's `remote_code`. */
  trustRemoteCode: boolean
  /** Typed engine knobs WITHOUT `env`, which `envRows` holds. */
  engineOptions: EngineOptions
  lifecycle: Partial<Lifecycle> | null
  envRows: FlagRow[]
}

/** The URL-owned half, as the request wants it. */
export interface RouteFields {
  context: number
  concurrency: number
  nodeIds: string[] | null
  degrees: ParallelismRequest | null
  speculative: SpeculativeRequest | null
}

/** The whole launch: knobs, URL fields, and the overrides somebody ticked. */
export interface LaunchInputs extends SessionKnobs, RouteFields {
  /** Gate params granted against the current plan, each sent as `true`. */
  grants?: string[]
}

/** True when the custom command is on AND has text -- the condition every
 *  plan-derived key is suppressed under. */
export function usingCustomCommand(k: Pick<SessionKnobs, 'customCommand' | 'customCommandText'>): boolean {
  return k.customCommand && k.customCommandText.trim().length > 0
}

/** One flag per row, value after its key; blank keys dropped. */
export function flagsToArgs(rows: FlagRow[]): string {
  return rows
    .filter((f) => f.k.trim())
    .map((f) => (f.v.trim() ? `${f.k.trim()} ${f.v.trim()}` : f.k.trim()))
    .join(' ')
}

/** The inverse: a token starting with `-` opens a row, the next non-flag
 *  token is its value. */
export function argsToFlags(text: string): FlagRow[] {
  const rows: FlagRow[] = []
  for (const tok of text.trim().split(/\s+/).filter(Boolean)) {
    const last = rows[rows.length - 1]
    if (tok.startsWith('-') || !last || last.v) rows.push({ k: tok, v: '' })
    else last.v = tok
  }
  return rows
}

/** `engine_options` as sent: the typed knobs plus `env` from the rows with a
 *  name, or null when nothing is set. */
export function engineOptionsOf(k: Pick<SessionKnobs, 'engineOptions' | 'envRows'>): EngineOptions | null {
  const env = Object.fromEntries(
    k.envRows.filter((r) => r.k.trim()).map((r) => [r.k.trim(), r.v.trim()]),
  )
  const out: EngineOptions = { ...k.engineOptions }
  delete out.env
  if (Object.keys(env).length) out.env = env
  return Object.keys(out).length ? out : null
}

export function buildLaunchRequest(s: LaunchInputs): LaunchRequest {
  const custom = usingCustomCommand(s)
  const extraArgs = custom ? '' : flagsToArgs(s.flags)
  const engine = custom ? null : engineOptionsOf(s)
  return {
    model_id: s.modelId,
    context: s.context,
    concurrency: s.concurrency,
    target: s.target,
    runtime: s.runtime,
    ...(s.nodeIds ? { node_ids: s.nodeIds } : {}),
    ...(s.degrees ? { parallelism: s.degrees } : {}),
    ...(custom ? { custom_command: s.customCommandText.trim() } : {}),
    ...(extraArgs ? { extra_args: extraArgs } : {}),
    ...(s.servedName.trim() ? { served_name: s.servedName.trim() } : {}),
    ...(!custom && s.enforceEager ? { enforce_eager: true } : {}),
    ...(!custom && !s.enforceEager && s.cudagraphCaptureSizes
      ? { cudagraph_capture_sizes: s.cudagraphCaptureSizes }
      : {}),
    // The gate sized the cache at this width and the engine has to be told it.
    ...(!custom && s.kvDtype ? { kv_dtype: s.kvDtype } : {}),
    // The scheme the verdict priced the weights at. Same grounds.
    ...(!custom && s.forcedQuant ? { dtype: s.forcedQuant } : {}),
    // A custom command has no speculative flag to carry; the pair is refused.
    ...(s.speculative && !custom
      ? {
          speculative: {
            method: s.speculative.method,
            num_speculative_tokens: s.speculative.num_speculative_tokens,
            ...(s.speculative.model ? { model: s.speculative.model } : {}),
          },
        }
      : {}),
    ...(!custom && s.speculativeTokens > 0 ? { speculative_tokens: s.speculativeTokens } : {}),
    ...(s.trustRemoteCode ? { trust_remote_code: true } : {}),
    ...(engine ? { engine_options: engine } : {}),
    ...(s.lifecycle ? { lifecycle: s.lifecycle } : {}),
    // Only gates somebody has read and ticked against THIS plan.
    ...Object.fromEntries((s.grants ?? []).map((g) => [g, true])),
  }
}

/** The URL-owned fields out of a route, by the same rules the session reads
 *  them. `fallback` supplies context/concurrency when the route has none. */
export function routeFields(
  route: Pick<Route, 'context' | 'concurrency' | 'on' | 'tp' | 'pp' | 'ep' | 'spec'>,
  fallback: { context: number; concurrency: number },
): RouteFields {
  const spec = route.spec
  return {
    context: route.context ?? fallback.context,
    concurrency: route.concurrency ?? fallback.concurrency,
    nodeIds: route.on,
    degrees: degreesFromRoute(route.tp, route.pp, route.ep),
    speculative:
      spec && spec.tokens && spec.model !== ''
        ? {
            method: spec.method,
            num_speculative_tokens: spec.tokens,
            ...(spec.model ? { model: spec.model } : {}),
          }
        : null,
  }
}

/** A saved request back into knobs and URL fields. `variant` is the ladder
 *  row whose `launchId` is the request's `model_id`, or null when none is. */
export function knobsFromRequest(
  req: LaunchRequest,
  ladder: { variants: QuantVariant[] } | null,
): {
  knobs: SessionKnobs
  route: Pick<Route, 'context' | 'concurrency' | 'on' | 'tp' | 'pp' | 'ep' | 'spec'>
  variant: QuantVariant | null
} {
  const { env, ...typed } = req.engine_options ?? {}
  const p = req.parallelism
  const spec: SpecRef | null = req.speculative
    ? {
        method: req.speculative.method,
        tokens: req.speculative.num_speculative_tokens,
        ...(req.speculative.model ? { model: req.speculative.model } : {}),
      }
    : null
  return {
    knobs: {
      modelId: req.model_id,
      target: req.target,
      runtime: req.runtime,
      customCommand: Boolean(req.custom_command),
      customCommandText: req.custom_command ?? '',
      flags: req.extra_args ? argsToFlags(req.extra_args) : [],
      servedName: req.served_name ?? '',
      enforceEager: req.enforce_eager === true,
      cudagraphCaptureSizes: req.cudagraph_capture_sizes ?? null,
      kvDtype: req.kv_dtype ?? '',
      forcedQuant: req.dtype ?? '',
      speculativeTokens: req.speculative_tokens ?? 0,
      trustRemoteCode: req.trust_remote_code === true,
      engineOptions: typed,
      lifecycle: req.lifecycle ?? null,
      envRows: Object.entries(env ?? {}).map(([k, v]) => ({ k, v })),
    },
    route: {
      context: req.context,
      concurrency: req.concurrency,
      on: req.node_ids && req.node_ids.length ? [...new Set(req.node_ids)].sort() : null,
      tp: p ? (p.tensor_parallel ?? 1) : null,
      pp: p ? (p.pipeline_parallel ?? 1) : null,
      ep: p ? (p.expert_parallel ?? 1) : null,
      spec,
    },
    variant: ladder?.variants.find((v) => launchId(v) === req.model_id) ?? null,
  }
}
