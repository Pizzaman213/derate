import type {
  DeploymentDTO,
  MemoryReport,
  NodeStateDTO,
  PlacementBlock,
  TopologyEdge,
} from '../../api/types'
import { TERMINAL, type CacheIndex } from './rows'
import { canCarryRank, memoryPool, type Runtime } from '../../state/runtime'

// The machine board's arithmetic, with no DOM and no React in it, for the same
// reason `tabs/cluster/layout.ts` is split from `ClusterGraph.tsx`: it is the
// part that can be wrong in a way types cannot catch, so it is the part
// `rows.check.mjs` bundles and asserts against the live cluster.
//
// Nothing here scores a machine or ranks a set of them. Nothing server-side
// does either -- `Planner._nodes_for` takes the alphabetical prefix of the
// strongest homogeneous group, and `alternatives` returns that same prefix for
// every entry -- so a ranking invented in the browser would be a second answer
// with no arithmetic behind it. This joins facts the gateway already reports
// and lets the fit gate say what they mean.

/** One machine, as the board draws it. Every field is read off a payload or
 *  joined by exact key; none is derived from a name or a size. */
export interface BoardRow {
  nodeId: string
  /** What to call it. The label a human set, else the hostname, else the id. */
  name: string
  /** Set only when the name above is not the node id, so the row can print the
   *  id underneath instead of losing it. */
  subtitle: string | null
  gpu: string
  deviceClass: string
  /** false when the registry will not vouch for this machine. */
  eligible: boolean
  /** The registry's own sentence. Rendered verbatim, never re-worded. */
  ineligibleReason: string | null
  /** Whether this machine can be ticked at all. Two ways it cannot: a machine
   *  with no addressable GPU memory is refused by the gateway with a 400, and
   *  a machine already running this model is refused with a 409 -- a node runs
   *  one copy of a model. Either way, offering the tick would be offering a
   *  refusal. */
  selectable: boolean
  /** Why not, when `selectable` is false. */
  unselectableReason: string | null
  /** Already running this very model, so the launch would be refused. Distinct
   *  from `occupants` being non-empty: sharing a machine with a DIFFERENT
   *  model is normal, and whether both fit is the fit gate's answer, not
   *  this rule's. */
  runningThisModel: boolean
  ticked: boolean
  /** In the plan that came back, whoever chose it. */
  inPlan: boolean
  /** Named by the operator but carrying no rank. */
  unused: boolean
  /** Live allocatable bytes, or null when nothing has read this machine. Null
   *  is not zero: an unpolled node must never draw as a full one. */
  allocatable: number | null
  /** The static ceiling the fit gate falls back to when there is no reading. */
  ceiling: number | null
  /** The live reading is older than the poll that should have refreshed it. */
  stale: boolean
  /** Are the weights already here. */
  cached: boolean
  cachedBytes: number | null
  /** What is already running on this machine, by served name. Deployments
   *  that are over are not on it: `/api/deployments` lists FAILED and STOPPED
   *  records too, and history occupies no GPU. */
  occupants: string[]
  /** Worst measured all-reduce from here to the other ticked machines, or null
   *  when any of those pairs was never probed. Only meaningful while more than
   *  one machine is ticked. */
  linkGbps: number | null
  /** True when this machine has a ticked peer that nothing has ever measured
   *  against it. */
  linkUnmeasured: boolean
}

export interface Board {
  rows: BoardRow[]
  /** The machines currently in force, whoever chose them. */
  effective: string[]
  /** Whether the operator owns the selection, as opposed to mirroring the
   *  planner's. */
  owned: boolean
  /** Set when more than one machine is ticked and some pair among them was
   *  never measured. `LinkService.worst_all_reduce` answers for the whole set
   *  or not at all, so one unprobed pair makes the planner treat every link as
   *  unknown and fall back to a conservative pipeline. That is invisible
   *  otherwise, and it changes the plan. */
  unmeasuredPairs: [string, string][]
  /** How many machines in the cluster can carry a rank at all -- the memory
   *  test only. A machine withheld because it already runs this model is
   *  still counted: it is capable, and calling it incapable would put a
   *  wrong number under a screen that is about this launch. */
  selectableCount: number
}

export interface BoardInput {
  nodes: NodeStateDTO[]
  /** `/api/memory`, every machine. Keyed by node id below. */
  memory: MemoryReport[]
  /** `/api/topology` edges: every pair, measured or not. */
  edges: TopologyEdge[]
  deployments: DeploymentDTO[]
  cache: CacheIndex
  /** The repository whose weights the cached column is asking about. */
  repoId: string
  /** What the last plan occupied, so an untouched board mirrors the planner's
   *  answer instead of showing nothing before the first plan lands. */
  plannerChose: string[] | null
  placement: PlacementBlock | null
  /** The operator's selection, or null for "the planner picks". */
  chosen: string[] | null
  /** Which runtime the ticks are being offered for.
   *
   *  Required, not optional with a default: the whole reason it is here is
   *  that the answer differs per runtime, and a default would let a caller
   *  silently get the GPU answer for a CPU launch -- which is exactly the
   *  disagreement between board and launcher this field exists to prevent. */
  runtime: Runtime
}

/** The one definition of "measured" this board is allowed to use, matching
 *  `tabs/cluster/layout.ts`: the wire's own flag AND a figure to show for it.
 *  An edge claiming `measured: true` with no `all_reduce_gbps` has nothing to
 *  draw and must read exactly like one that was never probed. */
export function linkMeasured(
  edge: Pick<TopologyEdge, 'measured' | 'all_reduce_gbps'> | undefined,
): boolean {
  return edge?.measured === true && edge.all_reduce_gbps != null
}

function edgeKey(a: string, b: string): string {
  return a < b ? `${a} ${b}` : `${b} ${a}`
}

/** Why this machine cannot carry a rank of this runtime, or null.
 *
 *  The client's half of `deploy/flags.py::placement_refusal`, and it has to
 *  stay the client's half of exactly that: the tick is withheld here so the
 *  operator is not offered a selection the launch would refuse, which only
 *  works while both sides answer the same question.
 *
 *  It was one sentence until there was a runtime that did not need a GPU.
 *  `addressable_memory <= 0` was then both "has no GPU" and "cannot serve",
 *  and a single string could say so. Now a machine with no GPU is the ONLY
 *  kind `llamacpp` will run on, and a machine with one is the only kind the
 *  other three will -- so there are two refusals pointing in opposite
 *  directions, and each has to name the remedy that is actually available.
 *
 *  The second one is an ACCOUNTING refusal and has to read like one. Saying
 *  llama.cpp "would not use" a GPU is a consequence dressed as a reason, and
 *  a preference is not grounds for a refusal here: it would run on a Spark's
 *  CPU perfectly well, just slowly. What derate cannot do is budget it there
 *  -- `registry.allocatable_bytes` reports host memory only for a CPU device
 *  class and a GPU figure for every other, so the gate would size a host-RAM
 *  launch against memory the server never touches.
 *
 *  The server refuses a third case this cannot see: a CPU node whose free
 *  memory nothing has measured. That needs a live telemetry read, and the
 *  board would rather offer a tick the launch explains than withhold one on a
 *  reading it does not have. */
function rankRefusal(runtime: Runtime, addressable: number): string | null {
  if (memoryPool(runtime) === 'host') {
    return addressable > 0
      ? 'llamacpp serves from host RAM, and derate only measures host RAM on a machine with no GPU — here the fit gate would size the launch against GPU memory the server never touches. Pick a machine with no GPU, or serve this on vllm or sglang.'
      : null
  }
  return addressable > 0
    ? null
    : 'This machine reports no addressable GPU memory, so it cannot carry a rank of this runtime. Pick llamacpp to serve from its RAM instead.'
}

/** The client's half of the deployment manager's one-copy-per-node rule. Said
 *  here so the tick is withheld rather than offered and then refused; the
 *  server's own sentence, which names the deployment to stop, is what the
 *  409 renders if a launch reaches it anyway. */
const RUNNING_IT = (servedName: string) =>
  `This machine is already running this model as ${servedName}. A node runs one copy of a model: a second copy shares the same GPU and the same unified memory.`

export function buildBoard(input: BoardInput): Board {
  const { nodes, memory, edges, deployments, cache, repoId } = input

  const mem = new Map(memory.map((m) => [m.node_id, m]))
  const edgeAt = new Map(edges.map((e) => [edgeKey(e.src, e.dst), e]))

  const occupants = new Map<string, string[]>()
  // Machines already running THIS repository, by the name they serve it as.
  // The gateway refuses a second copy on one of them (409 already_deployed),
  // so the board must not offer the tick.
  const runningThisModel = new Map<string, string>()
  for (const d of deployments) {
    if (TERMINAL.has(d.state)) continue
    for (const id of d.node_ids ?? []) {
      const list = occupants.get(id)
      if (list) list.push(d.served_name)
      else occupants.set(id, [d.served_name])
      if (d.model_id === repoId && !runningThisModel.has(id)) {
        runningThisModel.set(id, d.served_name)
      }
    }
  }

  const cachedOn = new Set(cache.nodes(repoId))
  const cachedBytes = cache.bytes(repoId)

  const inPlan = new Set(input.plannerChose ?? [])
  const unused = new Set(input.placement?.unused_node_ids ?? [])

  // A machine this runtime cannot be placed on is not a placement the gateway
  // will accept: `_select_nodes` refuses it with 400 `node_has_no_memory`.
  // Offering the tick would be offering a refusal, so it is withheld here and
  // the reason is said out loud on the row.
  //
  // Keyed on the runtime as well as the machine since there are two kinds of
  // serving node. The same Pi is capable under `llamacpp` and incapable under
  // `vllm`, and the same Spark is the reverse.
  const rankCapable = nodes
    .filter((n) => canCarryRank(input.runtime, n.profile))
    .map((n) => n.profile.node_id)
  // The second refusal, and the reason this is not the same list: a machine
  // already running this model CAN carry a rank -- it is carrying one now --
  // so it stays in `selectableCount`, which counts the cluster's capable
  // machines rather than this launch's available ones. It just cannot carry
  // this rank.
  const selectableSet = new Set(
    rankCapable.filter((id) => !runningThisModel.has(id)),
  )

  // Before the first touch the ticks mirror the planner, so the board is not
  // blank while a plan is in flight and does not claim a selection nobody
  // made. Filtered against what can actually be ticked, so a plan naming a
  // machine the board will not offer cannot desynchronise the two.
  const chosen = input.chosen?.filter((id) => selectableSet.has(id)) ?? null
  const effective = (chosen ?? input.plannerChose ?? []).filter((id) =>
    selectableSet.has(id),
  )
  const tickedSet = new Set(effective)
  const owned = chosen != null

  // Every unprobed pair among the ticked machines. Computed over the set the
  // plan will actually cross, not the whole cluster: an unmeasured link to a
  // machine nobody selected says nothing about this deployment.
  const unmeasuredPairs: [string, string][] = []
  for (let i = 0; i < effective.length; i++) {
    for (let j = i + 1; j < effective.length; j++) {
      const a = effective[i]!
      const b = effective[j]!
      if (!linkMeasured(edgeAt.get(edgeKey(a, b)))) unmeasuredPairs.push([a, b])
    }
  }

  const rows: BoardRow[] = nodes.map((n) => {
    const id = n.profile.node_id
    const name = n.label || n.profile.hostname || id
    const m = mem.get(id)
    const selectable = selectableSet.has(id)
    const ticked = tickedSet.has(id)

    // Worst measured all-reduce from here to every other ticked machine. Null
    // the moment one of those pairs is unmeasured, because the planner's own
    // figure is all-or-nothing over the set and a "worst" computed from the
    // measured subset would read better than the truth.
    let linkGbps: number | null = null
    let linkUnmeasured = false
    if (ticked && effective.length > 1) {
      for (const peer of effective) {
        if (peer === id) continue
        const e = edgeAt.get(edgeKey(id, peer))
        if (!linkMeasured(e)) {
          linkUnmeasured = true
          linkGbps = null
          break
        }
        const g = e!.all_reduce_gbps!
        linkGbps = linkGbps === null ? g : Math.min(linkGbps, g)
      }
    }

    const busyWith = runningThisModel.get(id)

    return {
      nodeId: id,
      name,
      subtitle: name === id ? null : id,
      gpu: n.profile.gpu_name,
      deviceClass: n.profile.device_class,
      eligible: n.eligible !== false,
      ineligibleReason: n.ineligible_reason ?? null,
      selectable,
      unselectableReason: selectable
        ? null
        : busyWith !== undefined
          ? RUNNING_IT(busyWith)
          : rankRefusal(input.runtime, n.profile.addressable_memory),
      runningThisModel: busyWith !== undefined,
      ticked,
      inPlan: inPlan.has(id),
      unused: unused.has(id),
      // `?? null` and not `?? 0`: a machine nothing has polled has no reading,
      // and a zero would draw as one with nothing left.
      allocatable: m?.allocatable ?? null,
      ceiling: m?.static_ceiling ?? null,
      stale: m?.stale === true,
      cached: cachedOn.has(id),
      cachedBytes: cachedOn.has(id) ? cachedBytes : null,
      occupants: occupants.get(id) ?? [],
      linkGbps,
      linkUnmeasured,
    }
  })

  return {
    rows,
    effective,
    owned,
    unmeasuredPairs,
    selectableCount: rankCapable.length,
  }
}

/** Apply one tick. Sorted before handback so two tick orders produce one
 *  request body, one answer and one server-side memo key, which is the rule
 *  `routes.ts` follows too when it reads `?on=`. The one place order
 *  matters, which host is the pipeline head, is the planner's to decide from
 *  the set. */
export function toggle(board: Board, nodeId: string, on: boolean): string[] {
  const next = on
    ? [...board.effective, nodeId]
    : board.effective.filter((id) => id !== nodeId)
  return [...new Set(next)].sort()
}
