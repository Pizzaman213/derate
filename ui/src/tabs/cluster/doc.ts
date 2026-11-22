// The wiring editor's document: what the operator has drawn, as data.
//
// `fromServer` reads the server's state into a Doc, the edit functions
// (`connect`, `disconnect`, `addLimit`, ...) return a new Doc, `diff` turns
// the difference between two Docs into the exact `POST /api/wiring` ops,
// `launches` into the `POST /api/deployments` bodies, and `blocks`/`wires`
// into what the canvas draws. Pure: no React, no @xyflow, no DOM -- so
// doc.check.mjs can drive it with tests/fixtures/wiring_rules.json.

import type {
  Cluster,
  DeploymentDTO,
  DeploymentState,
  LaunchRequest,
  MtpCapability,
  PlanRequest,
  PlanResponse,
  Provider,
  ProviderCatalogueModel,
  ProviderPatch,
  RouteLimits,
  RouteTarget,
  RoutingConfig,
  RoutingPolicy,
  Topology,
  TopologyEdge,
  WiringOp,
} from '../../api/types'
import { DEFAULT_CONCURRENCY, DEFAULT_CONTEXT } from '../../state/routes'
import { deploymentSignal, runners } from '../models/rows'
import {
  LB_POLICY,
  rowIn,
  rowOut,
  socketsOf,
  wireId,
  type BlockKind,
  type RowDesc,
  type SocketRef,
  type SocketSpec,
  type SocketType,
} from './sockets'
import { nodeSoftware } from './software'

// ── Inputs and the document ──────────────────────────────────────────────────

export interface ServerSnapshot {
  topology: Topology
  cluster: Cluster
  routing: RoutingConfig[]
  providers: Provider[]
  /** provider_id -> its whole catalogue, enabled or not. */
  catalogues: Record<string, ProviderCatalogueModel[]>
  /** model_id -> its MTP capability, `null` while unknown. */
  mtp: Record<string, MtpCapability | null>
  /** served_name -> spec_acceptance_rate over the last 15 minutes. */
  spec: Record<string, number | null>
}

export interface RouteEntry {
  policy: RoutingPolicy | 'auto'
  limits: RouteLimits
  /** The operator's split, target_id -> 0..100; `{}` for the measured
   *  weights. Only under `weighted_capacity`. */
  shares: Record<string, number>
}

export interface ProviderPin {
  /** The backend host tag OpenRouter is forced to for this model. */
  tag: string
  /** Whether it may fall back to other hosts when that one is down. */
  fallback: boolean
}

export interface ProviderEntry {
  enabled: boolean
  /** upstream_id -> served_name, for every enabled model. */
  models: Record<string, string>
  /** upstream_id -> the host it is pinned to. Absent = the upstream's own
   *  routing. */
  pins: Record<string, ProviderPin>
}

export interface LimitBlock {
  id: string
  rpm?: number
  tpm?: number
  /** Served names this limit caps, sorted. */
  plugged: string[]
}

export interface ModelBlock {
  id: string
  model_id: string
  /** `null` = let the planner choose. */
  context: number | null
  concurrency: number | null
  runtime: string
  /** Machines wired to `deploy to`, sorted. Two or more = one instance
   *  spanning them, tensor parallel. */
  node_ids: string[]
  mtp: boolean
  speculative_tokens: number
  /** Override params the operator granted after reading the sentence. */
  granted: Record<string, boolean>
}

/** A running deployment the operator wants back without some of its flags.
 *  Save stops it, then launches the same shape with these args. */
export interface Relaunch {
  extra_args: string[]
}

export interface Doc {
  routes: Record<string, RouteEntry>
  providers: Record<string, ProviderEntry>
  limits: LimitBlock[]
  models: ModelBlock[]
  /** deployment_id -> the flags it should be relaunched with. */
  relaunches: Record<string, Relaunch>
  /** What Save takes away: deployments to stop, and served names whose
   *  routing override and limits to clear. Both sorted. */
  removals: { deployments: string[]; routes: string[] }
}

export interface PlanState {
  /** The JSON of the request this state answers; a different key is stale. */
  key: string
  checking: boolean
  result: PlanResponse | null
  error: string | null
}

export const DEFAULT_DRAFT_TOKENS = 2
export const MAX_DRAFT_TOKENS = 8
export const TP_THRESHOLD_GBPS = 40

// ── Small shared readers ─────────────────────────────────────────────────────

/** `meta-llama/Llama-3.3-70B-Instruct-FP8` -> `Llama-3.3-70B-Instruct · FP8`.
 *  A row is titled by its model, never by its deployment id. */
export function modelTitle(modelId: string): string {
  const name = modelId.slice(modelId.lastIndexOf('/') + 1)
  const quant = /[-_.](fp8|nvfp4|mxfp4|fp4|awq|gptq|int8|int4|w4a16|w8a8|bf16|fp16|gguf)$/i.exec(name)
  return quant ? `${name.slice(0, quant.index)} · ${quant[1]!.toUpperCase()}` : name
}

function limitsOf(l: { rpm?: number; tpm?: number }): RouteLimits {
  const out: RouteLimits = {}
  if (l.rpm !== undefined) out.rpm = l.rpm
  if (l.tpm !== undefined) out.tpm = l.tpm
  return out
}

/** The limits a served name ends up with: the block plugged into it, else none. */
export function limitsFor(doc: Doc, served: string): RouteLimits {
  const block = doc.limits.find((l) => l.plugged.includes(served))
  return block ? limitsOf(block) : {}
}

/** Whether this gateway reports per-name limits at all. One that predates
 *  them omits the field, and the editor hides the rate limit block. */
export function limitsSupported(s: ServerSnapshot): boolean {
  return s.routing.every((r) => r.limits !== undefined)
}

function same(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b)
}

function sorted(xs: Iterable<string>): string[] {
  return [...xs].sort()
}

/** The policy a name actually runs on: an override, or what auto picked. */
export function effectivePolicy(doc: Doc, s: ServerSnapshot, served: string): RoutingPolicy | null {
  const entry = doc.routes[served]
  if (entry && entry.policy !== 'auto') return entry.policy
  return s.routing.find((r) => r.served_name === served)?.policy ?? null
}

// ── fromServer ───────────────────────────────────────────────────────────────

export function fromServer(s: ServerSnapshot): Doc {
  const routes: Record<string, RouteEntry> = {}
  for (const r of s.routing) {
    routes[r.served_name] = {
      policy: r.auto_selected ? 'auto' : r.policy,
      limits: { ...(r.limits ?? {}) },
      shares: { ...(r.shares ?? {}) },
    }
  }
  const providers: Record<string, ProviderEntry> = {}
  for (const p of s.providers) {
    const pins: Record<string, ProviderPin> = {}
    for (const m of p.models) if (m.backend_pin) pins[m.upstream_id] = { tag: m.backend_pin, fallback: m.backend_fallback === true }
    providers[p.provider_id] = {
      enabled: p.enabled,
      models: Object.fromEntries(p.models.map((m) => [m.upstream_id, m.served_name])),
      pins,
    }
  }
  // One block per distinct set of limits, plugged into every name carrying it.
  const groups = new Map<string, LimitBlock>()
  for (const name of sorted(Object.keys(routes))) {
    const l = limitsOf(routes[name]!.limits)
    if (Object.keys(l).length === 0) continue
    const id = `limit:server:${l.rpm ?? '-'}:${l.tpm ?? '-'}`
    const block = groups.get(id) ?? { id, ...l, plugged: [] }
    block.plugged.push(name)
    groups.set(id, block)
  }
  return { routes, providers, limits: [...groups.values()], models: [], relaunches: {}, removals: { deployments: [], routes: [] } }
}

// ── Edits ────────────────────────────────────────────────────────────────────

/** What a socket id is on a block, read from the id scheme alone -- the edit
 *  functions need no snapshot. */
function isOutput(ref: SocketRef): boolean {
  const kind = ref.block.slice(0, ref.block.indexOf(':'))
  if (ref.block === 'input') return true
  if (ref.socket.startsWith('out:')) return true
  if (kind === 'split') return ref.socket === 'local' || ref.socket === 'cloud'
  if (kind === 'merge') return ref.socket === 'out'
  if (kind === 'limit') return ref.socket === 'out'
  if (kind === 'model') return ref.socket === 'deploy'
  return false
}

function after(block: string, prefix: string): string {
  return block.slice(prefix.length)
}

/** Make the wire `from` -> `to` (either direction). Assumes `canConnect`
 *  already said yes; a wire with no editable meaning changes nothing. */
export function connect(doc: Doc, from: SocketRef, to: SocketRef): Doc {
  const [o, i] = isOutput(from) ? [from, to] : [to, from]
  if (o.block.startsWith('split:') && o.socket === 'cloud' && i.block.startsWith('provider:')) {
    // A provider row has one cloud input: wiring it to another split moves it.
    const providerId = after(i.block, 'provider:')
    const upstream = after(i.socket, `in:pm:${providerId}:`)
    return addProviderModel(doc, providerId, upstream, after(o.block, 'split:'))
  }
  if (o.block.startsWith('limit:') && i.block.startsWith('split:') && i.socket === 'limit') {
    // A split takes one limit: plugging another in unplugs the first.
    const served = after(i.block, 'split:')
    return {
      ...doc,
      limits: doc.limits.map((l) =>
        l.id === o.block
          ? { ...l, plugged: sorted(new Set([...l.plugged, served])) }
          : { ...l, plugged: l.plugged.filter((n) => n !== served) },
      ),
    }
  }
  if (o.block.startsWith('model:') && i.block.startsWith('machine:') && i.socket === 'deploy') {
    const node = after(i.block, 'machine:')
    return setModel(doc, o.block, (m) => ({ node_ids: sorted(new Set([...m.node_ids, node])) }))
  }
  return doc
}

/** Cut a wire by id. Cutting a cloud wire removes the provider row with it. */
export function disconnect(doc: Doc, id: string): Doc {
  const [ob = '', os = '', ib = '', is = ''] = id.split('|')
  if (ob.startsWith('split:') && os === 'cloud' && ib.startsWith('provider:')) {
    const providerId = after(ib, 'provider:')
    return removeProviderModel(doc, providerId, after(is, `in:pm:${providerId}:`))
  }
  if (ob.startsWith('limit:') && ib.startsWith('split:')) {
    const served = after(ib, 'split:')
    return {
      ...doc,
      limits: doc.limits.map((l) => (l.id === ob ? { ...l, plugged: l.plugged.filter((n) => n !== served) } : l)),
    }
  }
  if (ob.startsWith('model:') && ib.startsWith('machine:')) {
    const node = after(ib, 'machine:')
    return setModel(doc, ob, (m) => ({ node_ids: m.node_ids.filter((n) => n !== node) }))
  }
  return doc
}

export function addProviderModel(doc: Doc, providerId: string, upstream: string, served: string): Doc {
  const entry = doc.providers[providerId] ?? { enabled: true, models: {}, pins: {} }
  return {
    ...doc,
    providers: { ...doc.providers, [providerId]: { ...entry, models: { ...entry.models, [upstream]: served } } },
  }
}

export function removeProviderModel(doc: Doc, providerId: string, upstream: string): Doc {
  const entry = doc.providers[providerId]
  if (!entry || !(upstream in entry.models)) return doc
  const models = { ...entry.models }
  delete models[upstream]
  return { ...doc, providers: { ...doc.providers, [providerId]: { ...entry, models } } }
}

export function setProviderEnabled(doc: Doc, providerId: string, enabled: boolean): Doc {
  const entry = doc.providers[providerId]
  if (!entry) return doc
  return { ...doc, providers: { ...doc.providers, [providerId]: { ...entry, enabled } } }
}

export function addLimit(doc: Doc, id: string, values: { rpm?: number; tpm?: number } = {}): Doc {
  return { ...doc, limits: [...doc.limits, { id, ...limitsOf(values), plugged: [] }] }
}

/** `undefined` clears that key. */
export function setLimit(doc: Doc, id: string, patch: { rpm?: number; tpm?: number }): Doc {
  return {
    ...doc,
    limits: doc.limits.map((l) => {
      if (l.id !== id) return l
      const next: LimitBlock = { id: l.id, plugged: l.plugged, ...limitsOf({ ...l, ...patch }) }
      for (const k of ['rpm', 'tpm'] as const) if (k in patch && patch[k] === undefined) delete next[k]
      return next
    }),
  }
}

export function removeLimit(doc: Doc, id: string): Doc {
  return { ...doc, limits: doc.limits.filter((l) => l.id !== id) }
}

export function addModel(doc: Doc, id: string, init: Partial<Omit<ModelBlock, 'id'>> = {}): Doc {
  const block: ModelBlock = {
    id,
    model_id: '',
    context: null,
    concurrency: null,
    runtime: 'vllm',
    node_ids: [],
    mtp: false,
    speculative_tokens: DEFAULT_DRAFT_TOKENS,
    granted: {},
    ...init,
  }
  return { ...doc, models: [...doc.models, block] }
}

export function setModel(doc: Doc, id: string, patch: Partial<ModelBlock> | ((m: ModelBlock) => Partial<ModelBlock>)): Doc {
  return {
    ...doc,
    models: doc.models.map((m) => (m.id === id ? { ...m, ...(typeof patch === 'function' ? patch(m) : patch) } : m)),
  }
}

export function removeModel(doc: Doc, id: string): Doc {
  return { ...doc, models: doc.models.filter((m) => m.id !== id) }
}

/** Remove a deletable block (a limit or a draft model) by id. */
export function removeBlock(doc: Doc, id: string): Doc {
  if (id.startsWith('limit:')) return removeLimit(doc, id)
  if (id.startsWith('model:')) return removeModel(doc, id)
  return doc
}

/** Pin one provider model to a backend host, or `null` for the upstream's
 *  own routing. Only on providers whose kind aggregates hosts; the dry run
 *  checks the tag against the hosts it actually lists. */
export function setProviderPin(doc: Doc, providerId: string, upstream: string, pin: ProviderPin | null): Doc {
  const entry = doc.providers[providerId]
  if (!entry) return doc
  const pins = { ...entry.pins }
  if (pin) pins[upstream] = { tag: pin.tag, fallback: pin.fallback }
  else delete pins[upstream]
  return { ...doc, providers: { ...doc.providers, [providerId]: { ...entry, pins } } }
}

/** The routing policy a served name runs on; `auto` hands it back to the
 *  router's own choice. */
export function setRoutePolicy(doc: Doc, served: string, policy: RoutingPolicy | 'auto'): Doc {
  const entry = doc.routes[served] ?? { policy: 'auto', limits: {}, shares: {} }
  // A split only means something under the policy that reads it.
  const shares = policy === SHARES_POLICY ? (entry.shares ?? {}) : {}
  return { ...doc, routes: { ...doc.routes, [served]: { ...entry, policy, shares } } }
}

/** The policy whose weights an operator's split replaces. */
export const SHARES_POLICY: RoutingPolicy = 'weighted_capacity'

/** Set a served name's traffic split by hand (`null` hands it back to the
 *  measured weights). A split pins the policy to `weighted_capacity`. */
export function setRouteShares(doc: Doc, served: string, shares: Record<string, number> | null): Doc {
  const entry = doc.routes[served] ?? { policy: 'auto', limits: {}, shares: {} }
  const next = shares ? Object.fromEntries(sorted(Object.keys(shares)).map((k) => [k, shares[k]!])) : {}
  return {
    ...doc,
    routes: { ...doc.routes, [served]: { ...entry, policy: shares ? SHARES_POLICY : entry.policy, shares: next } },
  }
}

/** Move one target's share to `value` (0..100) and spread what is left over
 *  the others in proportion to what they had (evenly when they had nothing),
 *  in whole numbers that always sum to 100 -- so two sliders move together. */
export function rebalance(current: Record<string, number>, id: string, value: number): Record<string, number> {
  const v = Math.max(0, Math.min(100, Math.round(value)))
  const others = sorted(Object.keys(current)).filter((k) => k !== id)
  const out: Record<string, number> = { [id]: v }
  if (others.length === 0) return { [id]: 100 }
  const left = 100 - v
  const had = others.reduce((n, k) => n + Math.max(0, current[k] ?? 0), 0)
  let given = 0
  others.forEach((k, i) => {
    const share =
      i === others.length - 1
        ? left - given
        : Math.round(had > 0 ? (left * Math.max(0, current[k] ?? 0)) / had : left / others.length)
    out[k] = share
    given += share
  })
  return Object.fromEntries(sorted(Object.keys(out)).map((k) => [k, out[k]!]))
}

/** A deployment's launch flags as the operator reads them: each flag with the
 *  values after it (`--max-num-seqs 8` is one), `--flag=value` as itself. A
 *  token that only looks like a flag because it is negative (`-1`) is a value. */
export function flagGroups(args: readonly string[]): string[][] {
  const out: string[][] = []
  for (const t of args) {
    const head = t.startsWith('-') && !/^-\d/.test(t)
    if (head || out.length === 0) out.push([t])
    else out[out.length - 1]!.push(t)
  }
  return out
}

/** Drop one flag -- the group `flag` heads, values and all -- from the args a
 *  deployment runs with now (`currentArgs`: its own, or the pending
 *  relaunch's). Save stops it and launches it again without. */
export function dropFlag(doc: Doc, deploymentId: string, currentArgs: readonly string[], flag: string): Doc {
  const groups = flagGroups(currentArgs)
  const k = groups.findIndex((g) => g[0] === flag)
  if (k < 0) return doc
  const extra_args = groups.filter((_, j) => j !== k).flat()
  return { ...doc, relaunches: { ...doc.relaunches, [deploymentId]: { extra_args } } }
}

/** What removing a served name's load balancer takes with it. A deployment
 *  already stopping is on its way out and is not stopped twice. */
export function removalOf(
  doc: Doc,
  s: ServerSnapshot,
  served: string,
): { deployments: DeploymentDTO[]; cloud: Array<{ provider: string; upstream: string }> } {
  return {
    deployments: runners(s.cluster.deployments)
      .filter((d) => d.served_name === served && d.state !== 'stopping')
      .sort((a, b) => a.deployment_id.localeCompare(b.deployment_id)),
    cloud: Object.entries(doc.providers).flatMap(([provider, entry]) =>
      sorted(Object.keys(entry.models))
        .filter((u) => entry.models[u] === served)
        .map((upstream) => ({ provider, upstream })),
    ),
  }
}

export interface BalancerTarget {
  /** The routing target id: a deployment id, or `<provider>:<upstream>`. */
  id: string
  label: string
  kind: 'local' | 'remote'
  /** The router's live view of it, when it routes to it already. */
  live: RouteTarget | null
  /** Wired in the draft but not routed yet, or routed but cut in the draft. */
  pending: 'add' | 'remove' | null
}

/** Whole numbers summing to exactly 100, by largest remainder. */
function wholePercents(raw: Record<string, number>): Record<string, number> {
  const ids = sorted(Object.keys(raw))
  const total = ids.reduce((n, k) => n + Math.max(0, raw[k]!), 0)
  if (ids.length === 0) return {}
  const exact = ids.map((k) => (total > 0 ? (100 * Math.max(0, raw[k]!)) / total : 100 / ids.length))
  const floor = exact.map(Math.floor)
  let left = 100 - floor.reduce((a, b) => a + b, 0)
  const order = exact.map((x, i) => [x - floor[i]!, i] as const).sort((a, b) => b[0] - a[0] || a[1] - b[1])
  for (const [, i] of order) {
    if (left <= 0) break
    floor[i]! += 1
    left -= 1
  }
  return Object.fromEntries(ids.map((k, i) => [k, floor[i]!]))
}

/** The split a balancer's sliders show: the operator's own shares when it
 *  has set them, else the router's measured weights (even, when nothing is
 *  measured yet) -- over the targets that stay after Save, in whole numbers
 *  summing to 100. */
export function targetSplit(doc: Doc, served: string, targets: readonly BalancerTarget[]): { manual: boolean; split: Record<string, number> } {
  const shares = doc.routes[served]?.shares ?? {}
  const manual = Object.keys(shares).length > 0
  const stay = targets.filter((t) => t.pending !== 'remove')
  const raw = Object.fromEntries(stay.map((t) => [t.id, manual ? (shares[t.id] ?? 0) : (t.live?.weight ?? 0)]))
  return { manual, split: wholePercents(raw) }
}

/** Everything wired to a served name's load balancer -- its local
 *  deployments and the cloud rows the draft aliases to it -- with the
 *  router's live figures where it already routes there, and any target the
 *  router still has that the draft has cut. */
export function balancerTargets(doc: Doc, s: ServerSnapshot, served: string): BalancerTarget[] {
  const routed = s.routing.find((r) => r.served_name === served)?.targets ?? []
  const live = new Map(routed.map((t) => [t.target_id, t]))
  const stopping = new Set(doc.removals.deployments)
  const out: BalancerTarget[] = []
  for (const d of runners(s.cluster.deployments).filter((x) => x.served_name === served)) {
    const t = live.get(d.deployment_id) ?? null
    out.push({
      id: d.deployment_id,
      label: modelTitle(d.model_id),
      kind: 'local',
      live: t,
      pending: stopping.has(d.deployment_id) ? 'remove' : t ? null : 'add',
    })
  }
  for (const [provider, entry] of Object.entries(doc.providers)) {
    for (const u of sorted(Object.keys(entry.models))) {
      if (entry.models[u] !== served) continue
      const id = `${provider}:${u}`
      out.push({ id, label: id, kind: 'remote', live: live.get(id) ?? null, pending: live.has(id) ? null : 'add' })
    }
  }
  const seen = new Set(out.map((t) => t.id))
  for (const t of routed) {
    if (seen.has(t.target_id)) continue
    out.push({ id: t.target_id, label: t.target_id, kind: t.kind === 'local' ? 'local' : 'remote', live: t, pending: 'remove' })
  }
  return out
}

/** What the balancer dialog says, beside the removal confirm. */
export const BALANCER_SENTENCES = {
  /** Under the policy, when the name runs on the triangle's own rule. */
  triangleRule: "failover: local while any local deployment is admitting, else cloud — the triangle's rule.",
  /** A limit set while the policy is auto: what `diff` saves it as. */
  limitPins: `A limit pins the policy: it is saved as ${LB_POLICY}. Pick a policy to keep both.`,
  draft: 'Not served yet: its policy and limits can be set once Save has served it.',
  manualSplit: 'Split set by hand: saved as weighted_capacity, which sends each target its share.',
  measuredSplit: 'Drag a share to split traffic by hand (saved as weighted_capacity). Until then the policy above decides.',
} as const

/** The confirm a removal asks, naming everything it stops and switches off. */
export function removalSentence(doc: Doc, s: ServerSnapshot, served: string): string {
  const { deployments, cloud } = removalOf(doc, s, served)
  const stops = deployments.length
    ? `${deployments.map((d) => `${modelTitle(d.model_id)} on ${sorted(d.node_ids).join(', ')}`).join('; ')} (×${deployments.length})`
    : 'nothing'
  const off = cloud.length ? `${cloud.map((c) => `${c.provider}:${c.upstream}`).join(', ')} (×${cloud.length})` : 'nothing'
  return `Remove ${served}?\n\nStops: ${stops}\nSwitches off: ${off}\nClears: its routing override and limits\n\nNothing happens until you Save.`
}

/** Take a served name away in one go: stop every deployment serving it,
 *  switch off every cloud model aliased to it, and clear its routing
 *  override and limits. Nothing happens until Save. */
export function removeServedName(doc: Doc, s: ServerSnapshot, served: string): Doc {
  const { deployments, cloud } = removalOf(doc, s, served)
  let next = doc
  for (const c of cloud) next = removeProviderModel(next, c.provider, c.upstream)
  const ids = deployments.map((d) => d.deployment_id)
  const relaunches = { ...next.relaunches }
  for (const id of ids) delete relaunches[id]
  const routes = { ...next.routes }
  const clears = served in routes ? [served] : []
  delete routes[served]
  // A limit that capped only this name goes with it; one shared with other
  // names stays plugged into them.
  const limits = next.limits.flatMap((l) => {
    if (!l.plugged.includes(served)) return [l]
    const plugged = l.plugged.filter((n) => n !== served)
    return plugged.length > 0 ? [{ ...l, plugged }] : []
  })
  return {
    ...next,
    routes,
    limits,
    relaunches,
    removals: {
      deployments: sorted(new Set([...next.removals.deployments, ...ids])),
      routes: sorted(new Set([...next.removals.routes, ...clears])),
    },
  }
}

// ── diff and launches ────────────────────────────────────────────────────────

/** Every alias the provider holds: the catalogue's view (which carries
 *  disabled models too), overlaid with the doc's enabled ones. An alias
 *  patch replaces the whole map, so it must never drop one it cannot see. */
function aliasesOf(entry: ProviderEntry, catalogue: readonly ProviderCatalogueModel[] | undefined): Record<string, string> {
  const map: Record<string, string> = {}
  for (const m of catalogue ?? []) if (m.served_name !== m.upstream_id) map[m.upstream_id] = m.served_name
  for (const [u, served] of Object.entries(entry.models)) {
    if (served !== u) map[u] = served
    else delete map[u]
  }
  return Object.fromEntries(sorted(Object.keys(map)).map((u) => [u, map[u]!]))
}

/** The pin changes between two entries, as the two per-key maps a patch
 *  merges. A cleared pin sends `""` and `false`. */
function pinPatch(a: ProviderEntry, b: ProviderEntry): Pick<ProviderPatch, 'backend_pins' | 'backend_fallbacks'> {
  const pins: Record<string, string> = {}
  const fallbacks: Record<string, boolean> = {}
  for (const u of sorted(new Set([...Object.keys(a.pins ?? {}), ...Object.keys(b.pins ?? {})]))) {
    const before = a.pins?.[u]
    const after_ = b.pins?.[u]
    if (same(before, after_)) continue
    if (!after_) {
      pins[u] = ''
      fallbacks[u] = false
      continue
    }
    if (before?.tag !== after_.tag) pins[u] = after_.tag
    if ((before?.fallback ?? false) !== after_.fallback) fallbacks[u] = after_.fallback
  }
  return {
    ...(Object.keys(pins).length > 0 ? { backend_pins: pins } : {}),
    ...(Object.keys(fallbacks).length > 0 ? { backend_fallbacks: fallbacks } : {}),
  }
}

/** The ops that take the server from `base` to `next`, in the order they
 *  must be applied: provider patches, then route overrides set, then route
 *  overrides cleared. Never launches or stops. */
export function diff(base: Doc, next: Doc, s: ServerSnapshot): WiringOp[] {
  const ops: WiringOp[] = []
  for (const p of s.providers) {
    const a = base.providers[p.provider_id]
    const b = next.providers[p.provider_id]
    if (!a || !b) continue
    const patch: ProviderPatch = {}
    if (a.enabled !== b.enabled) patch.enabled = b.enabled
    if (!same(sorted(Object.keys(a.models)), sorted(Object.keys(b.models)))) {
      patch.enabled_models = sorted(Object.keys(b.models))
    }
    const catalogue = s.catalogues[p.provider_id]
    const aliasesBefore = aliasesOf(a, catalogue)
    const aliasesAfter = aliasesOf(b, catalogue)
    if (!same(aliasesBefore, aliasesAfter)) patch.aliases = aliasesAfter
    Object.assign(patch, pinPatch(a, b))
    if (Object.keys(patch).length > 0) ops.push({ op: 'provider.patch', provider_id: p.provider_id, patch })
  }
  const removed = new Set(next.removals.routes)
  const clears: string[] = []
  for (const r of [...s.routing].sort((x, y) => x.served_name.localeCompare(y.served_name))) {
    const name = r.served_name
    if (removed.has(name)) continue
    const limitsBefore = limitsFor(base, name)
    const limits = limitsFor(next, name)
    const policyBefore = base.routes[name]?.policy ?? 'auto'
    const policy = next.routes[name]?.policy ?? 'auto'
    const limitsChanged = !same(limitsBefore, limits)
    const sharesBefore = base.routes[name]?.shares ?? {}
    const shares = next.routes[name]?.shares ?? {}
    const sharesChanged = !same(sharesBefore, shares)
    if (!limitsChanged && !sharesChanged && policy === policyBefore) continue
    if (policy === 'auto' && policyBefore !== 'auto' && Object.keys(limits).length === 0) {
      clears.push(name)
      continue
    }
    // A limit needs a pinned policy: under auto it is saved as the
    // triangle's own, which the balancer dialog says.
    ops.push({
      op: 'route.set',
      served_name: name,
      policy: policy === 'auto' ? LB_POLICY : policy,
      limits,
      ...(sharesChanged ? { shares } : {}),
    })
  }
  for (const name of sorted(new Set([...next.removals.routes, ...clears]))) ops.push({ op: 'route.clear', served_name: name })
  return ops
}

/** The plan request a draft model block asks for. Two or more machines is
 *  one instance across them, so the tensor-parallel degree is pinned to the
 *  machine count: `node_ids` alone only pins placement. */
export function planRequest(m: ModelBlock, nodeIds: readonly string[] = m.node_ids): PlanRequest {
  return {
    model_id: m.model_id,
    target: 'throughput',
    ...(m.context ? { context: m.context } : {}),
    ...(m.concurrency ? { concurrency: m.concurrency } : {}),
    ...(nodeIds.length > 0 ? { node_ids: [...nodeIds] } : {}),
    ...(nodeIds.length > 1 ? { parallelism: { tensor_parallel: nodeIds.length } } : {}),
    ...(m.mtp ? { speculative_tokens: m.speculative_tokens } : {}),
  }
}

/** The single-machine plan beside a spanning one, for the link warning. */
export const singlePlanKey = (id: string): string => `${id}#single`

/** The deployments Save stops: the ones a removal takes away, then the ones
 *  being relaunched without a flag. */
export function stops(doc: Doc): string[] {
  const removed = new Set(doc.removals.deployments)
  return [...doc.removals.deployments, ...sorted(Object.keys(doc.relaunches)).filter((id) => !removed.has(id))]
}

export interface RelaunchPlan {
  deployment_id: string
  model_id: string
  node_ids: string[]
  /** The flag groups the relaunch leaves out, as the operator saw them. */
  without: string[]
  request: LaunchRequest
}

/** Each pending relaunch as the launch that brings the deployment back: its
 *  own shape, parallelism and options, with the flags it keeps. The request
 *  says `throughput` because a deployment does not carry its launch target.
 *  Joining tokens with a space is lossless: the server's allowlist
 *  (`deploy/recipes.py::_EXTRA_ARG_SAFE`) admits no whitespace or quote in
 *  one. */
export function relaunchesOf(doc: Doc, s: ServerSnapshot): RelaunchPlan[] {
  const out: RelaunchPlan[] = []
  for (const id of sorted(Object.keys(doc.relaunches))) {
    const d = runners(s.cluster.deployments).find((x) => x.deployment_id === id)
    if (!d) continue
    const keep = doc.relaunches[id]!.extra_args
    const kept = new Set(flagGroups(keep).map((g) => g.join(' ')))
    const parallelism = Object.fromEntries(
      (['tensor_parallel', 'pipeline_parallel', 'expert_parallel', 'data_parallel'] as const)
        .map((k) => [k, d.plan?.[k] ?? 1] as const)
        .filter(([, v]) => v > 1),
    )
    out.push({
      deployment_id: id,
      model_id: d.model_id,
      node_ids: [...d.node_ids],
      without: flagGroups(d.extra_args ?? []).map((g) => g.join(' ')).filter((g) => !kept.has(g)),
      request: {
        model_id: d.model_id,
        context: d.context_length,
        concurrency: d.max_concurrent_seqs,
        target: 'throughput',
        runtime: d.runtime,
        node_ids: [...d.node_ids],
        ...(Object.keys(parallelism).length > 0 ? { parallelism } : {}),
        speculative_tokens: d.speculative_tokens ?? 0,
        ...(keep.length > 0 ? { extra_args: keep.join(' ') } : {}),
        ...(d.custom_command?.length ? { custom_command: d.custom_command.join(' ') } : {}),
        ...(d.trust_remote_code ? { trust_remote_code: true } : {}),
      },
    })
  }
  return out
}

/** What Save launches: every wired draft model, then (given the snapshot)
 *  every pending relaunch. */
export function launches(doc: Doc, plans: Record<string, PlanState>, s?: ServerSnapshot): LaunchRequest[] {
  const out: LaunchRequest[] = []
  for (const m of doc.models) {
    if (!m.model_id || m.node_ids.length === 0) continue
    const result = plans[m.id]?.result ?? null
    const gates = result?.serve?.overrides ?? []
    out.push({
      model_id: m.model_id,
      context: result?.context ?? m.context ?? DEFAULT_CONTEXT,
      concurrency: result?.concurrency ?? m.concurrency ?? DEFAULT_CONCURRENCY,
      target: 'throughput',
      runtime: m.runtime,
      node_ids: [...m.node_ids],
      ...(m.node_ids.length > 1 ? { parallelism: { tensor_parallel: m.node_ids.length } } : {}),
      speculative_tokens: m.mtp ? m.speculative_tokens : 0,
      ...Object.fromEntries(gates.filter((g) => m.granted[g.param] === true).map((g) => [g.param, true])),
    })
  }
  if (s) for (const r of relaunchesOf(doc, s)) out.push(r.request)
  return out
}

// ── What the canvas draws ────────────────────────────────────────────────────

export type Signal = 'live' | 'warn' | 'fault' | 'idle'

export interface MachineRow {
  id: string
  deployment_id: string
  model_id: string
  /** Planned or launching rows show their launch step instead of a rate. */
  state: DeploymentState
  title: string
  served: string
  signal: Signal
  /** `TP n` on a deployment spanning machines -- or `PP n` when the planner
   *  split it by pipeline instead: the chip says what actually runs. */
  span: string | null
  /** On a machine other than the deployment's lowest node_id. */
  rank: { k: number; n: number } | null
  /** `MTP ×N · P% accepted`, or null when launched without it. */
  mtp: string | null
  /** The flags it runs with now, or will be relaunched with. */
  extra_args: string[]
  /** Its launch flags as chips: each flag with its values; `removed` while
   *  a relaunch without it is pending. */
  flags: Array<{ flag: string; text: string; removed: boolean }>
  /** Launched with a custom command: one chip, not removable here. */
  custom_command: boolean
  /** What Save will do to it. */
  pending: 'relaunch' | 'stop' | null
}

export interface ProviderRow {
  id: string
  upstream_id: string
  served: string
  title: string
  sub: string
  /** `<provider_id>:<upstream_id>`, the metrics frame's remote target id. */
  target_id: string
  /** The backend host it is pinned to, in the draft. */
  pin: ProviderPin | null
}

interface BlockBase {
  id: string
  kind: BlockKind
  title: string
  sockets: SocketSpec[]
}

export type Block =
  | (BlockBase & { kind: 'input' })
  | (BlockBase & { kind: 'output' })
  | (BlockBase & {
      kind: 'split'
      served: string
      draft: boolean
      /** Shown when the name runs on something other than the triangle's rule. */
      policyChip: string | null
      ports: { local: string; cloud: string }
      /** The draft's policy for this name, `auto` when the router picks. */
      policy: RoutingPolicy | 'auto'
      limits: RouteLimits
      /** What the router routes this name to now, from `GET /api/routing`. */
      targets: RouteTarget[]
      /** A served name the server knows can be removed; a draft one cannot. */
      removable: boolean
      /** Its removal is pending: drawn until Save takes it away. */
      removing: boolean
    })
  | (BlockBase & { kind: 'merge'; served: string })
  | (BlockBase & {
      kind: 'machine'
      node_id: string
      signal: Signal
      memory: { total: number | null; used: number | null }
      rows: MachineRow[]
    })
  | (BlockBase & {
      kind: 'provider'
      provider_id: string
      provider_kind: string
      enabled: boolean
      priority: number
      rows: ProviderRow[]
      /** How many of its rows are pinned to a backend host. */
      pinned: number
    })
  | (BlockBase & { kind: 'limit'; limit: LimitBlock })
  | (BlockBase & { kind: 'model'; model: ModelBlock; mtp: MtpCapability | null })

/** Every served name the canvas draws a load balancer for: what the router
 *  serves, plus any a cloud wire or a launching deployment brings with it. */
export function servedNames(doc: Doc, s: ServerSnapshot): { served: string[]; draft: Set<string> } {
  const served = s.routing.map((r) => r.served_name)
  const known = new Set(served)
  const draft = new Set<string>()
  const extra = [
    ...Object.values(doc.providers).flatMap((p) => Object.values(p.models)),
    ...runners(s.cluster.deployments).map((d) => d.served_name),
  ]
  for (const name of sorted(extra)) {
    if (known.has(name)) continue
    known.add(name)
    draft.add(name)
    served.push(name)
  }
  return { served, draft }
}

/** Admitting: what the router itself says, so the canvas never disagrees with
 *  where a request will actually go. */
export function admitting(s: ServerSnapshot, d: DeploymentDTO): boolean {
  const t = s.routing
    .find((r) => r.served_name === d.served_name)
    ?.targets.find((x) => x.target_id === d.deployment_id)
  return t ? t.healthy && t.admitting : false
}

function rowSignal(s: ServerSnapshot, d: DeploymentDTO): Signal {
  const base = deploymentSignal(d.state)
  return base === 'live' && !admitting(s, d) ? 'warn' : base
}

function price(v: number | null): string {
  return v == null ? '—' : `$${v.toFixed(2)}`
}

function nodeTitle(n: { label?: string | null; node_id: string }): string {
  return n.label?.trim() || n.node_id
}

/** A deployment's flags for its row: what it runs with, or will be
 *  relaunched with, and each original flag marked when it is being dropped. */
function flagsOf(doc: Doc, d: DeploymentDTO): Pick<MachineRow, 'extra_args' | 'flags' | 'custom_command'> {
  const own = d.extra_args ?? []
  const pending = doc.relaunches[d.deployment_id]?.extra_args
  const kept = new Set(flagGroups(pending ?? own).map((g) => g.join(' ')))
  return {
    extra_args: pending ?? own,
    flags: flagGroups(own).map((g) => ({ flag: g[0]!, text: g.join(' '), removed: !kept.has(g.join(' ')) })),
    custom_command: (d.custom_command?.length ?? 0) > 0,
  }
}

export function blocks(doc: Doc, s: ServerSnapshot): Block[] {
  const out: Block[] = []
  const deployments = runners(s.cluster.deployments)
  const { served, draft } = servedNames(doc, s)
  const supported = limitsSupported(s)
  const stopping = new Set(doc.removals.deployments)

  out.push({ id: 'input', kind: 'input', title: 'API Input', sockets: socketsOf('input', 'input', 'API Input') })

  for (const l of supported ? doc.limits : []) {
    out.push({ id: l.id, kind: 'limit', title: 'Rate limit', limit: l, sockets: socketsOf('limit', l.id, 'Rate limit') })
  }

  for (const name of served) {
    const id = `split:${name}`
    const title = `${name} load balancer`
    const policy = effectivePolicy(doc, s, name)
    const ranked = policy === LB_POLICY || policy === null
    const hasLocal = deployments.some((d) => d.served_name === name)
    const override = doc.routes[name]?.policy
    const ports = !ranked
      ? { local: 'local', cloud: 'cloud' }
      : hasLocal
        ? { local: '1 · local', cloud: '2 · fallback' }
        : { local: 'local', cloud: '1 · cloud' }
    const sockets = socketsOf('split', id, title).filter((sk) => supported || sk.socket !== 'limit')
    for (const sk of sockets) if (sk.socket === 'local' || sk.socket === 'cloud') sk.label = ports[sk.socket]
    const locals = deployments.filter((d) => d.served_name === name)
    out.push({
      id,
      kind: 'split',
      title,
      served: name,
      draft: draft.has(name),
      policyChip: ranked ? null : `policy: ${policy} (${override === 'auto' || override === undefined ? 'auto' : 'sidebar'})`,
      ports,
      sockets,
      policy: override ?? 'auto',
      limits: limitsFor(doc, name),
      targets: s.routing.find((r) => r.served_name === name)?.targets ?? [],
      removable: !draft.has(name),
      removing:
        doc.removals.routes.includes(name) || (locals.length > 0 && locals.every((d) => stopping.has(d.deployment_id))),
    })
  }

  for (const m of doc.models) {
    out.push({
      id: m.id,
      kind: 'model',
      title: 'Draft model',
      model: m,
      mtp: s.mtp[m.model_id] ?? null,
      sockets: socketsOf('model', m.id, 'Draft model'),
    })
  }

  for (const n of s.topology.nodes) {
    const id = `machine:${n.node_id}`
    const title = nodeTitle(n)
    const rows: MachineRow[] = deployments
      .filter((d) => d.node_ids.includes(n.node_id))
      .sort((a, b) => a.deployment_id.localeCompare(b.deployment_id))
      .map((d) => {
        const span = sorted(d.node_ids)
        const k = span.indexOf(n.node_id)
        const tokens = d.speculative_tokens ?? 0
        const rate = s.spec[d.served_name]
        return {
          id: `dep:${d.deployment_id}`,
          deployment_id: d.deployment_id,
          model_id: d.model_id,
          state: d.state,
          title: modelTitle(d.model_id),
          served: d.served_name,
          signal: rowSignal(s, d),
          span:
            span.length < 2
              ? null
              : (d.plan?.tensor_parallel ?? 1) > 1 || (d.plan?.pipeline_parallel ?? 1) <= 1
                ? `TP ${d.plan?.tensor_parallel ?? span.length}`
                : `PP ${d.plan!.pipeline_parallel}`,
          rank: k > 0 ? { k: k + 1, n: span.length } : null,
          mtp: tokens > 0 ? `MTP ×${tokens}${rate != null ? ` · ${Math.round(rate * 100)}% accepted` : ''}` : null,
          ...flagsOf(doc, d),
          pending: stopping.has(d.deployment_id) ? 'stop' : d.deployment_id in doc.relaunches ? 'relaunch' : null,
        }
      })
    const descs: RowDesc[] = rows.map((r) => ({ id: r.id, title: r.title, served: r.served, rank: r.rank ?? undefined }))
    const total = n.total_memory ?? null
    out.push({
      id,
      kind: 'machine',
      title,
      node_id: n.node_id,
      signal: n.state === 'healthy' ? 'live' : n.state === 'degraded' ? 'warn' : 'fault',
      memory: { total, used: total != null && Number.isFinite(n.memory_used_pct) ? (total * n.memory_used_pct) / 100 : null },
      rows,
      sockets: socketsOf('machine', id, title, descs),
    })
  }

  for (const p of s.providers) {
    const entry = doc.providers[p.provider_id]
    if (!entry) continue
    const id = `provider:${p.provider_id}`
    const title = p.display_name || p.provider_id
    const catalogue = s.catalogues[p.provider_id] ?? []
    const rows: ProviderRow[] = sorted(Object.keys(entry.models)).map((u) => {
      const servedName = entry.models[u]!
      // The provider's own row first: a pinned model carries its host's price.
      const m = p.models.find((c) => c.upstream_id === u) ?? catalogue.find((c) => c.upstream_id === u)
      const pin = entry.pins?.[u] ?? null
      const cost = `${price(m?.input_cost_per_mtok ?? null)} / ${price(m?.output_cost_per_mtok ?? null)}`
      return {
        id: `pm:${p.provider_id}:${u}`,
        upstream_id: u,
        served: servedName,
        title: u,
        sub:
          p.kind === 'openrouter'
            ? `fallback for ${servedName} · ${pin ? pin.tag : 'auto host'} · ${cost}`
            : `fallback for ${servedName} · ${cost}`,
        target_id: `${p.provider_id}:${u}`,
        pin,
      }
    })
    out.push({
      id,
      kind: 'provider',
      title,
      provider_id: p.provider_id,
      provider_kind: p.kind,
      enabled: entry.enabled,
      priority: p.priority,
      rows,
      pinned: rows.filter((r) => r.pin).length,
      sockets: socketsOf(
        'provider',
        id,
        title,
        rows.map((r) => ({ id: r.id, title: r.title, served: r.served })),
      ),
    })
  }

  for (const name of served) {
    const id = `merge:${name}`
    out.push({ id, kind: 'merge', title: `${name} merge`, served: name, sockets: socketsOf('merge', id, `${name} merge`) })
  }
  out.push({ id: 'output', kind: 'output', title: 'API Output', sockets: socketsOf('output', 'output', 'API Output') })
  return out
}

export type WireKind = 'entry' | 'local' | 'cloud' | 'local-back' | 'cloud-back' | 'exit' | 'limit' | 'deploy' | 'link'

export interface Wire {
  id: string
  from: SocketRef
  to: SocketRef
  type: SocketType
  kind: WireKind
  editable: boolean
  served?: string
  /** Live state painted on the wire: red when broken, amber worth a look. */
  tone: 'fault' | 'warn' | null
  label: string | null
  /** The routing target a request wire feeds, for particles. */
  target_id?: string
  /** Link wires only. */
  link?: { a: string; b: string; gbps: number | null; measured: boolean; edge: TopologyEdge | null }
}

function wire(
  from: SocketRef,
  to: SocketRef,
  type: SocketType,
  kind: WireKind,
  editable: boolean,
  extra: Partial<Wire> = {},
): Wire {
  return { id: wireId(from, to), from, to, type, kind, editable, tone: null, label: null, ...extra }
}

export function wires(doc: Doc, s: ServerSnapshot): Wire[] {
  const out: Wire[] = []
  const deployments = runners(s.cluster.deployments)
  const { served } = servedNames(doc, s)
  const cloudFor = new Map<string, Array<{ provider: string; upstream: string }>>()
  for (const [pid, entry] of Object.entries(doc.providers)) {
    if (!s.providers.some((p) => p.provider_id === pid)) continue
    for (const [u, name] of Object.entries(entry.models)) {
      const list = cloudFor.get(name) ?? []
      list.push({ provider: pid, upstream: u })
      cloudFor.set(name, list)
    }
  }

  for (const name of served) {
    const split = `split:${name}`
    const merge = `merge:${name}`
    out.push(wire({ block: 'input', socket: 'out' }, { block: split, socket: 'in' }, 'requests', 'entry', false, { served: name }))
    const locals = deployments.filter((d) => d.served_name === name)
    const clouds = cloudFor.get(name) ?? []
    const anyAdmitting = locals.some((d) => admitting(s, d))
    for (const d of locals) {
      const head = sorted(d.node_ids)[0]
      if (!head) continue
      const machine = `machine:${head}`
      const row = `dep:${d.deployment_id}`
      const down = d.state === 'ready' || d.state === 'degraded' ? !admitting(s, d) : false
      out.push(
        wire({ block: split, socket: 'local' }, { block: machine, socket: rowIn(row) }, 'requests', 'local', false, {
          served: name,
          target_id: d.deployment_id,
          ...(down && clouds.length > 0 ? { tone: 'fault' as const, label: 'not admitting' } : {}),
        }),
      )
      out.push(wire({ block: machine, socket: rowOut(row) }, { block: merge, socket: 'local' }, 'responses', 'local-back', false, { served: name, target_id: d.deployment_id }))
    }
    const fallbackActive = locals.length > 0 && !anyAdmitting && locals.some((d) => d.state === 'ready' || d.state === 'degraded')
    for (const c of clouds) {
      const block = `provider:${c.provider}`
      const row = `pm:${c.provider}:${c.upstream}`
      const target = `${c.provider}:${c.upstream}`
      out.push(
        wire({ block: split, socket: 'cloud' }, { block, socket: rowIn(row) }, 'requests', 'cloud', true, {
          served: name,
          target_id: target,
          ...(fallbackActive ? { tone: 'warn' as const, label: 'fallback active' } : {}),
        }),
      )
      out.push(wire({ block, socket: rowOut(row) }, { block: merge, socket: 'cloud' }, 'responses', 'cloud-back', false, { served: name, target_id: target }))
    }
    out.push(wire({ block: merge, socket: 'out' }, { block: 'output', socket: 'in' }, 'responses', 'exit', false, { served: name }))
  }

  if (limitsSupported(s)) {
    for (const l of doc.limits) {
      for (const name of l.plugged) {
        out.push(wire({ block: l.id, socket: 'out' }, { block: `split:${name}`, socket: 'limit' }, 'limit', 'limit', true, { served: name }))
      }
    }
  }

  const present = new Set(s.topology.nodes.map((n) => n.node_id))
  for (const m of doc.models) {
    for (const node of m.node_ids) {
      if (!present.has(node)) continue
      out.push(wire({ block: m.id, socket: 'deploy' }, { block: `machine:${node}`, socket: 'deploy' }, 'deploy', 'deploy', true))
    }
  }

  // Machine links: drawn when measured, when a deployment spans the pair, or
  // when there are few enough machines that every pair still reads.
  const nodes = s.topology.nodes.map((n) => n.node_id)
  const spanned = new Set<string>()
  for (const d of deployments) {
    const ids = d.node_ids.filter((n) => present.has(n))
    for (let k = 0; k + 1 < ids.length; k++) spanned.add([ids[k]!, ids[k + 1]!].sort().join('~'))
  }
  for (let x = 0; x < nodes.length; x++) {
    for (let y = x + 1; y < nodes.length; y++) {
      const a = nodes[x]!
      const b = nodes[y]!
      const edge =
        s.topology.edges.find((e) => (e.src === a && e.dst === b) || (e.src === b && e.dst === a)) ?? null
      const measured = edge?.measured === true && edge.all_reduce_gbps != null
      if (!(measured || spanned.has([a, b].sort().join('~')) || nodes.length <= 4)) continue
      const gbps = measured ? (edge!.all_reduce_gbps ?? null) : null
      out.push(
        wire({ block: `machine:${a}`, socket: 'link-bottom' }, { block: `machine:${b}`, socket: 'link-top' }, 'link', 'link', false, {
          label: measured ? `${gbps!.toFixed(1)} of ${TP_THRESHOLD_GBPS} GB/s` : 'never measured',
          link: { a, b, gbps, measured, edge },
        }),
      )
    }
  }
  return out
}

// ── validate ─────────────────────────────────────────────────────────────────

export interface Issue {
  id: string
  level: 'error' | 'warn'
  message: string
  /** The block to focus. */
  block?: string
  wire?: string
  /** A live condition the operator should see but that does not block Save. */
  live?: boolean
  /** A rebase conflict on this entity key (`route:<name>` / `provider:<id>`). */
  conflict?: string
}

export function validate(
  doc: Doc,
  s: ServerSnapshot,
  plans: Record<string, PlanState>,
  conflicts: readonly string[] = [],
): Issue[] {
  const issues: Issue[] = []
  for (const m of doc.models) {
    if (m.node_ids.length === 0) {
      issues.push({ id: `${m.id}:unwired`, level: 'error', message: 'Wire "deploy to" into at least one machine.', block: m.id })
      continue
    }
    const plan = plans[m.id]
    if (plan?.error) issues.push({ id: `${m.id}:plan`, level: 'error', message: plan.error, block: m.id })
    const r = plan?.result
    if (r?.fit && r.fit.verdict === 'wont_fit') {
      issues.push({ id: `${m.id}:fit`, level: 'error', message: r.fit.reason, block: m.id })
    } else if (r?.serve && !r.serve.allowed) {
      const gates = r.serve.overrides ?? []
      const granted = gates.length > 0 && gates.every((g) => m.granted[g.param] === true)
      if (!granted) issues.push({ id: `${m.id}:serve`, level: 'error', message: r.serve.reason, block: m.id })
    }
    // A spanning instance over a slow link, when one machine would do.
    const single = plans[singlePlanKey(m.id)]?.result
    if (m.node_ids.length > 1 && single?.fit && single.fit.verdict !== 'wont_fit') {
      for (let k = 0; k + 1 < m.node_ids.length; k++) {
        const a = m.node_ids[k]!
        const b = m.node_ids[k + 1]!
        const e = s.topology.edges.find((x) => (x.src === a && x.dst === b) || (x.src === b && x.dst === a))
        if (e?.measured && e.all_reduce_gbps != null && e.all_reduce_gbps < TP_THRESHOLD_GBPS) {
          issues.push({
            id: `${m.id}:link:${a}~${b}`,
            level: 'warn',
            message: `The ${a} ↔ ${b} link measures ${e.all_reduce_gbps.toFixed(1)} GB/s, under the ${TP_THRESHOLD_GBPS} GB/s tensor-parallel threshold. ${modelTitle(m.model_id)} fits on one machine, so one copy per machine will be faster.`,
            block: m.id,
          })
        }
      }
    }
  }
  if (limitsSupported(s)) {
    for (const l of doc.limits) {
      for (const k of ['rpm', 'tpm'] as const) {
        const v = l[k]
        if (v !== undefined && !(Number.isInteger(v) && v >= 1)) {
          issues.push({ id: `${l.id}:${k}`, level: 'error', message: `${k} must be a whole number of 1 or more.`, block: l.id })
        }
      }
      if (l.plugged.length === 0) {
        issues.push({
          id: `${l.id}:unplugged`,
          level: 'warn',
          message: 'This rate limit is not plugged into a load balancer and will not be saved.',
          block: l.id,
        })
      }
    }
  }
  // Live: a local model that is not admitting with nowhere else to go.
  const deployments = runners(s.cluster.deployments)
  const names = new Map(s.topology.nodes.map((n) => [n.node_id, nodeTitle(n)]))
  const cloudNames = new Set(Object.values(doc.providers).flatMap((p) => Object.values(p.models)))
  for (const name of new Set(deployments.map((d) => d.served_name))) {
    const locals = deployments.filter((d) => d.served_name === name)
    if (cloudNames.has(name) || locals.some((d) => admitting(s, d))) continue
    for (const d of locals) {
      if (d.state !== 'ready' && d.state !== 'degraded') continue
      const head = sorted(d.node_ids)[0] ?? ''
      issues.push({
        id: `live:${d.deployment_id}`,
        level: 'error',
        live: true,
        message: `${modelTitle(d.model_id)} on ${names.get(head) ?? head} is not admitting and ${name} has no cloud fallback: requests are failing with 503.`,
        block: `machine:${head}`,
      })
    }
  }
  for (const id of sorted(Object.keys(doc.relaunches))) {
    if (deployments.some((d) => d.deployment_id === id)) continue
    const d = s.cluster.deployments.find((x) => x.deployment_id === id)
    const head = d ? (sorted(d.node_ids)[0] ?? '') : ''
    issues.push({
      id: `relaunch:${id}`,
      level: 'warn',
      message: `${d ? modelTitle(d.model_id) : id} on ${names.get(head) ?? (head || 'its machine')} is gone; the flag change has nothing to relaunch.`,
      ...(head ? { block: `machine:${head}` } : {}),
    })
  }
  for (const key of conflicts) {
    const name = key.slice(key.indexOf(':') + 1)
    issues.push({
      id: `conflict:${key}`,
      level: 'warn',
      message: `${name} changed on the server while you were editing.`,
      block: key.startsWith('provider:') ? key : `split:${name}`,
      conflict: key,
    })
  }
  // Live, and a warning only: a node on another build, or one missing a
  // feature its agent should have. Rolling upgrades stay allowed, so this
  // never blocks Save -- it tints the machine and names the cure.
  for (const n of s.cluster.nodes) {
    const id = n.profile.node_id
    const sw = nodeSoftware(names.get(id) ?? id, n, s.cluster.summary.coordinator_build, s.cluster.summary.agent_features ?? [])
    for (const [k, message] of [sw.skew, ...sw.gaps].entries()) {
      if (message) issues.push({ id: `software:${id}:${k}`, level: 'warn', live: true, message, block: `machine:${id}` })
    }
  }
  return issues
}

// ── rebase ───────────────────────────────────────────────────────────────────

/** Fold a new server state into a draft. Per entity: untouched in the draft
 *  -> take the server; unchanged on the server -> keep the draft; changed on
 *  both -> keep the draft and report the key. A draft model drops out once a
 *  running deployment has its model_id on the same machines. */
export function rebase(
  base: Doc,
  doc: Doc,
  next: Doc,
  /** The cluster's deployments; omitted, the draft's relaunches and stops
   *  are kept as they are. */
  deployments?: readonly DeploymentDTO[],
): { doc: Doc; conflicts: string[] } {
  const conflicts: string[] = []
  const merge = <T,>(kind: string, a: Record<string, T>, d: Record<string, T>, n: Record<string, T>): Record<string, T> => {
    const out: Record<string, T> = {}
    for (const k of new Set([...Object.keys(a), ...Object.keys(d), ...Object.keys(n)])) {
      const [bv, dv, nv] = [a[k], d[k], n[k]]
      let pick: T | undefined
      // Untouched in the draft, or the server caught up with it (a Save that
      // landed): take the server's.
      if (same(dv, bv) || same(dv, nv)) pick = nv
      else if (same(nv, bv)) pick = dv
      else {
        pick = dv
        conflicts.push(`${kind}:${k}`)
      }
      if (pick !== undefined) out[k] = pick
    }
    return out
  }
  const routes = merge('route', base.routes, doc.routes, next.routes)
  const providers = merge('provider', base.providers, doc.providers, next.providers)

  let limits = doc.limits
  const names = sorted(new Set([...base.limits, ...doc.limits, ...next.limits].flatMap((l) => l.plugged)))
  const caughtUp = names.every((n) => same(limitsFor(doc, n), limitsFor(next, n)))
  if (same(doc.limits, base.limits) || (caughtUp && doc.limits.every((l) => l.plugged.length > 0))) limits = next.limits
  else if (!same(next.limits, base.limits)) {
    for (const name of names) {
      const [b, d, n] = [limitsFor(base, name), limitsFor(doc, name), limitsFor(next, name)]
      if (!same(b, n) && !same(b, d) && !same(d, n)) conflicts.push(`route:${name}`)
    }
  }
  const running = runners(deployments ?? [])
  const models = doc.models.filter(
    (m) =>
      !running.some(
        (d) => d.model_id === m.model_id && same(sorted(d.node_ids), sorted(m.node_ids)),
      ),
  )
  // Relaunches and removals are the draft's own. One whose deployment is no
  // longer running has landed (the stop went through); one whose name the
  // server no longer routes has nothing left to clear.
  const live = (id: string) => deployments === undefined || running.some((d) => d.deployment_id === id)
  const relaunches = Object.fromEntries(Object.entries(doc.relaunches).filter(([id]) => live(id)))
  const removals = {
    deployments: doc.removals.deployments.filter(live),
    routes: doc.removals.routes.filter((name) => name in next.routes),
  }
  return { doc: { routes, providers, limits, models, relaunches, removals }, conflicts: sorted(new Set(conflicts)) }
}

/** "Take server" on a conflict: the server's value for that one entity. */
export function takeServer(doc: Doc, next: Doc, key: string): Doc {
  const name = key.slice(key.indexOf(':') + 1)
  if (key.startsWith('provider:')) {
    const entry = next.providers[name]
    const providers = { ...doc.providers }
    if (entry) providers[name] = entry
    else delete providers[name]
    return { ...doc, providers }
  }
  const routes = { ...doc.routes }
  if (next.routes[name]) routes[name] = next.routes[name]!
  const server = next.limits.find((l) => l.plugged.includes(name))
  let limits = doc.limits.map((l) => ({ ...l, plugged: l.plugged.filter((n) => n !== name) }))
  if (server) {
    const existing = limits.find((l) => l.id === server.id)
    limits = existing
      ? limits.map((l) => (l.id === server.id ? { ...l, plugged: sorted([...l.plugged, name]) } : l))
      : [...limits, { ...server, plugged: [name] }]
  }
  return { ...doc, routes, limits, removals: { ...doc.removals, routes: doc.removals.routes.filter((n) => n !== name) } }
}

// ── Positions ────────────────────────────────────────────────────────────────

export interface XY {
  x: number
  y: number
}

const COLUMN: Record<BlockKind, number> = {
  input: 0,
  limit: 0,
  split: 1,
  model: 1,
  machine: 2,
  provider: 2,
  merge: 3,
  output: 4,
}
/** Column x: wide enough for each column's widest block. Port labels sit
 *  inside the triangles, so nothing hangs into the next column. */
const COLUMN_X = [0, 320, 680, 1080, 1380]
export const GAP = 40
/** Two blocks closer than this count as touching. */
const MARGIN = 8

export interface Size {
  w: number
  h: number
}

/** A height to stack by before the canvas has measured anything. */
function estimateHeight(b: Block): number {
  switch (b.kind) {
    case 'input':
    case 'output':
      return 70
    case 'split':
    case 'merge':
      return 150
    case 'limit':
      return 130
    case 'model':
      return 280
    case 'machine':
      return 110 + 64 * b.rows.length
    case 'provider':
      return 90 + 44 * b.rows.length
  }
}

/** Widths from the `.wnode` rules in derate.css, before measurement. */
function estimateWidth(b: Block): number {
  switch (b.kind) {
    case 'split':
    case 'merge':
      return 200
    case 'machine':
    case 'provider':
      return 304
    case 'model':
      return 288
    default:
      return 256
  }
}

/** A block's size: what the canvas measured, else the estimate. */
export function sizeOf(b: Block, sizes: Record<string, Size> = {}): Size {
  return sizes[b.id] ?? { w: estimateWidth(b), h: estimateHeight(b) }
}

function touches(a: XY & Size, b: XY & Size): boolean {
  return a.x < b.x + b.w + MARGIN && b.x < a.x + a.w + MARGIN && a.y < b.y + b.h + MARGIN && b.y < a.y + a.h + MARGIN
}

/** Move every block not in `fixed` down until it touches nothing: in column
 *  order, then top to bottom, each one drops by exactly the overlap plus
 *  `GAP` and is tested again. A fixed block (one the operator placed) is
 *  tested against but never moved -- a dealt block gets out of its way,
 *  never the reverse. Deterministic. */
export function resolveOverlaps(
  positions: Record<string, XY>,
  sizes: Record<string, Size>,
  fixed: ReadonlySet<string> = new Set(),
): Record<string, XY> {
  const out: Record<string, XY> = { ...positions }
  const rect = (id: string) => ({ ...out[id]!, ...(sizes[id] ?? { w: 0, h: 0 }) })
  const placed: Array<XY & Size> = Object.keys(out)
    .filter((id) => fixed.has(id))
    .map(rect)
  const movable = Object.keys(out)
    .filter((id) => !fixed.has(id))
    .sort((a, b) => out[a]!.x - out[b]!.x || out[a]!.y - out[b]!.y || a.localeCompare(b))
  for (const id of movable) {
    const r = rect(id)
    for (let hit = placed.find((p) => touches(r, p)); hit; hit = placed.find((p) => touches(r, p))) {
      r.y += hit.y + hit.h - r.y + GAP
    }
    out[id] = { x: r.x, y: r.y }
    placed.push(r)
  }
  return out
}

/** Where each block sits: the stored position when there is one, else a
 *  deterministic column layout -- splits stacked in routing order, each
 *  merge level with its split, machines then providers -- stacked by each
 *  block's measured size when the canvas has one, then moved clear of
 *  anything it would overlap. */
export function dealPositions(bs: readonly Block[], stored: Record<string, XY>, sizes: Record<string, Size> = {}): Record<string, XY> {
  const out: Record<string, XY> = {}
  const size: Record<string, Size> = Object.fromEntries(bs.map((b) => [b.id, sizeOf(b, sizes)]))
  const y = [0, 0, 0, 0, 0]
  const splitY = new Map<string, number>()
  const place = (b: Block, col: number, at?: number) => {
    const top = at ?? y[col]!
    out[b.id] = { x: COLUMN_X[col]!, y: top }
    y[col] = Math.max(y[col]!, top + size[b.id]!.h + GAP)
  }
  for (const b of bs) {
    const col = COLUMN[b.kind]
    if (b.kind === 'merge') continue
    if (b.kind === 'output' || b.kind === 'input') continue
    if (b.kind === 'split') {
      splitY.set(b.served, y[col]!)
    }
    place(b, col)
  }
  for (const b of bs) if (b.kind === 'merge') place(b, COLUMN.merge, splitY.get(b.served) ?? y[COLUMN.merge])
  const mid = Math.max(0, (y[COLUMN.split]! - GAP) / 2 - 35)
  for (const b of bs) {
    if (b.kind === 'input') {
      // Input heads column 0; limits stack under it.
      out[b.id] = { x: COLUMN_X[0]!, y: mid }
      let below = mid + size[b.id]!.h + GAP
      for (const l of bs) {
        if (l.kind !== 'limit') continue
        out[l.id] = { x: COLUMN_X[0]!, y: below }
        below += size[l.id]!.h + GAP
      }
    }
    if (b.kind === 'output') out[b.id] = { x: COLUMN_X[4]!, y: mid }
  }
  const fixed = new Set<string>()
  for (const [id, xy] of Object.entries(stored)) {
    if (!(id in out)) continue
    out[id] = xy
    fixed.add(id)
  }
  return resolveOverlaps(out, size, fixed)
}
