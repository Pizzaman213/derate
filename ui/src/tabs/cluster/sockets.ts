// The wiring editor's socket model: the five socket types, the sockets every
// block carries, what may connect to what, and every sentence the editor says
// about a socket. One module on purpose -- the type table below is the single
// source of truth for what can connect, and everything else (doc.ts, the
// blocks, the edges, sockets.check.mjs) imports from here.
//
// Pure: no React, no @xyflow, no DOM. sockets.check.mjs bundles it for node.

import type { RoutingPolicy } from '../../api/types'

/** The policy the load-balancer triangle stands for: "local while any local
 *  deployment is admitting, else cloud". `failover` is exactly that --
 *  `gateway/policies.py::select_failover` takes the first healthy, admitting
 *  target in `_stable_order` (locals before remotes), so a remote is chosen
 *  only when no local admits. `local_first` also spills when every local is
 *  merely saturated, which is not the triangle's contract. */
export const LB_POLICY: RoutingPolicy = 'failover'

// ── Types ────────────────────────────────────────────────────────────────────

export type SocketType = 'requests' | 'responses' | 'limit' | 'deploy' | 'link'

export interface SocketTypeInfo {
  type: SocketType
  /** The pack token (`component.graph.*`) its sockets and wires are drawn in. */
  token: string
  /** `var(--graph-…)`, ready for a style attribute. */
  colour: string
  meaning: string
}

export const SOCKET_TYPES: Record<SocketType, SocketTypeInfo> = {
  requests: {
    type: 'requests',
    token: 'graph-req',
    colour: 'var(--graph-req)',
    meaning: 'traffic going toward a model',
  },
  responses: {
    type: 'responses',
    token: 'graph-resp',
    colour: 'var(--graph-resp)',
    meaning: 'answers coming back',
  },
  limit: {
    type: 'limit',
    token: 'graph-limit',
    colour: 'var(--graph-limit)',
    meaning: 'a rate limit applied to a served name',
  },
  deploy: {
    type: 'deploy',
    token: 'graph-deploy',
    colour: 'var(--graph-deploy)',
    meaning: 'where a draft model will be launched',
  },
  link: {
    type: 'link',
    token: 'graph-link',
    colour: 'var(--graph-link)',
    meaning: 'measured machine-to-machine link (display only)',
  },
}

export const SOCKET_TYPE_ORDER: readonly SocketType[] = ['requests', 'responses', 'limit', 'deploy', 'link']

/** One weight for a wire and for the blocks that travel it. A wire is drawn
 *  the way the derate mark is: a heavy stepped stroke laid down faint, with
 *  the solid stroke moving through it -- here, the particles. A request block
 *  is exactly as thick as its wire, so it reads as the wire filling in rather
 *  than as a dot riding on top of a hairline. */
export const WIRE_WEIGHT = 8

/** Wire strokes: fixed wires recede, the ones you own stand forward. Both are
 *  faint enough that a solid block of the same colour shows inside them; the
 *  mark's own echo stroke is 0.32. */
export const WIRE_STROKE = {
  fixed: { width: WIRE_WEIGHT, opacity: 0.18 },
  editable: { width: WIRE_WEIGHT, opacity: 0.34 },
  /** A selected wire you own: the one Delete or the × will cut. */
  selected: { width: WIRE_WEIGHT, opacity: 0.62 },
} as const

/** How far a wire runs straight out of its socket before its first corner.
 *  Wires are orthogonal steps with square corners (issue #5), never curves:
 *  the drawn wire and the drag preview both read this, so a wire does not
 *  change shape the moment it is dropped. */
export const WIRE_STEP_OFFSET = 20

export const FAULT_COLOUR = 'var(--graph-fault)'
export const WARN_COLOUR = 'var(--graph-warn)'

// ── Blocks and their sockets ─────────────────────────────────────────────────

export type BlockKind = 'input' | 'output' | 'split' | 'merge' | 'machine' | 'provider' | 'limit' | 'model'

export type Direction = 'in' | 'out'
export type SocketEdge = 'left' | 'right' | 'top' | 'bottom'

/** What a socket is, independent of which block instance it is on. The rules
 *  below key off this rather than off ids. */
export type SocketRole =
  | 'api-in' // API Input's requests output
  | 'api-out' // API Output's responses input
  | 'split-in'
  | 'split-limit'
  | 'split-local'
  | 'split-cloud'
  | 'merge-local'
  | 'merge-cloud'
  | 'merge-out'
  | 'machine-row-in'
  | 'machine-row-out'
  | 'machine-deploy'
  | 'machine-link'
  | 'provider-row-in'
  | 'provider-row-out'
  | 'limit-out'
  | 'model-deploy'

export interface SocketRef {
  block: string
  socket: string
}

export interface SocketSpec {
  block: string
  socket: string
  /** The block's own title, for sentences ("Nothing on <block> takes …"). */
  blockTitle: string
  type: SocketType
  dir: Direction
  role: SocketRole
  editable: boolean
  edge: SocketEdge
  /** The visible label. Rows inside containers carry it as title/aria only. */
  label: string
  /** For machine rows: the model's title and the name it serves. */
  model?: string
  served?: string
}

/** A row inside a machine or provider container. */
export interface RowDesc {
  /** `dep:<deployment_id>` or `pm:<provider_id>:<upstream_id>`. */
  id: string
  /** The model, never the deployment id: `Qwen3-Next-80B-A3B · FP8`. */
  title: string
  served: string
  /** A multi-node deployment's row on a machine other than its lowest
   *  node_id: its sockets are drawn hollow and take no wires. */
  rank?: { k: number; n: number }
}

export const rowIn = (rowId: string): string => `in:${rowId}`
export const rowOut = (rowId: string): string => `out:${rowId}`

/** Every socket a block carries, in drawing order.
 *
 *  Sides: every input's `edge` is `left` and every output's is `right`, so a
 *  wire always leaves a block on the right and enters the next on the left.
 *  Two exceptions, both deliberate: `machine-link` sits `top`/`bottom` (the
 *  measured line between stacked machines, display only), and `split-limit`
 *  is `bottom`, drawn on the triangle's lower slanted edge. The blocks draw
 *  exactly this; sockets.check.mjs holds the rule as data. */
export function socketsOf(kind: BlockKind, block: string, blockTitle: string, rows: readonly RowDesc[] = []): SocketSpec[] {
  const s = (
    socket: string,
    type: SocketType,
    dir: Direction,
    role: SocketRole,
    editable: boolean,
    edge: SocketEdge,
    label: string,
    extra: Partial<SocketSpec> = {},
  ): SocketSpec => ({ block, socket, blockTitle, type, dir, role, editable, edge, label, ...extra })
  switch (kind) {
    case 'input':
      return [s('out', 'requests', 'out', 'api-in', false, 'right', 'requests')]
    case 'output':
      return [s('in', 'responses', 'in', 'api-out', false, 'left', 'responses')]
    case 'split':
      return [
        s('in', 'requests', 'in', 'split-in', false, 'left', 'requests'),
        s('limit', 'limit', 'in', 'split-limit', true, 'bottom', 'limit'),
        s('local', 'requests', 'out', 'split-local', false, 'right', 'local'),
        s('cloud', 'requests', 'out', 'split-cloud', true, 'right', 'cloud'),
      ]
    case 'merge':
      return [
        s('local', 'responses', 'in', 'merge-local', false, 'left', 'local'),
        s('cloud', 'responses', 'in', 'merge-cloud', false, 'left', 'cloud'),
        s('out', 'responses', 'out', 'merge-out', false, 'right', 'responses'),
      ]
    case 'machine':
      return [
        s('link-top', 'link', 'in', 'machine-link', false, 'top', 'link'),
        ...rows.flatMap((r) => [
          s(rowIn(r.id), 'requests', 'in', 'machine-row-in', false, 'left', `${r.title}: requests`, {
            model: r.title,
            served: r.served,
          }),
          s(rowOut(r.id), 'responses', 'out', 'machine-row-out', false, 'right', `${r.title}: responses`, {
            model: r.title,
            served: r.served,
          }),
        ]),
        s('deploy', 'deploy', 'in', 'machine-deploy', true, 'left', 'deploy here'),
        s('link-bottom', 'link', 'in', 'machine-link', false, 'bottom', 'link'),
      ]
    case 'provider':
      return rows.flatMap((r) => [
        s(rowIn(r.id), 'requests', 'in', 'provider-row-in', true, 'left', `${r.title}: requests`, {
          model: r.title,
          served: r.served,
        }),
        s(rowOut(r.id), 'responses', 'out', 'provider-row-out', false, 'right', `${r.title}: responses`, {
          model: r.title,
          served: r.served,
        }),
      ])
    case 'limit':
      return [s('out', 'limit', 'out', 'limit-out', true, 'right', 'limit')]
    case 'model':
      return [s('deploy', 'deploy', 'out', 'model-deploy', true, 'right', 'deploy to')]
  }
}

// ── Connecting ───────────────────────────────────────────────────────────────

export const SENTENCES = {
  self: 'A block does not wire to itself.',
  bothOut: 'Both of those are outputs. Join an output (right side) to an input (left side).',
  bothIn: 'Both of those are inputs. Join an output (right side) to an input (left side).',
  typeMismatch: (from: SocketType, to: SocketType): string =>
    `${from.charAt(0).toUpperCase()}${from.slice(1)} sockets only join ${from} sockets; that one takes ${to}.`,
  responses:
    'Responses are routed for you: each model answers through the output of the load balancer that sent it the request.',
  link: 'Machines are joined by the network; the line between them is their measured link.',
  apiInput: 'Fixed: every load balancer is reachable from the API Input.',
  localToCloud: 'The local port feeds models on your machines; cloud models go on the cloud port.',
  localToMachine: (model: string, served: string): string =>
    `${model} serves '${served}' and always sits on that load balancer's local port. Relaunch it under another name to move it.`,
  cloudToLocal: 'The cloud port feeds cloud models; your own models sit on the local port.',
  already: 'Already wired.',
  dragHint: (type: SocketType, block: string): string =>
    `Dragging ${type} from ${block}: drop on a glowing ${type} socket.`,
  nothingTakes: (block: string, type: SocketType): string => `Nothing on ${block} takes ${type}.`,
  wireDropped: (type: SocketType): string => `Wire dropped: ${type} wire cut. Undo with Discard.`,
} as const

/** Shown when a fixed socket is pressed: a fixed socket starts no drag. */
export const FIXED_SENTENCES = {
  responses:
    'Fixed. Responses are routed for you: each model answers through the output of the load balancer that sent it the request.',
  link: 'Fixed: the measured link between two machines. Click the line for the figure.',
  local:
    'Fixed: the local port feeds the model on your machine that serves this name. Relaunch the model under another name to move it.',
  entry: 'Fixed: every load balancer is reachable from the API Input.',
} as const

export function fixedSentence(s: SocketSpec): string | null {
  if (s.editable) return null
  if (s.type === 'responses') return FIXED_SENTENCES.responses
  if (s.type === 'link') return FIXED_SENTENCES.link
  if (s.role === 'split-local' || s.role === 'machine-row-in') return FIXED_SENTENCES.local
  if (s.role === 'api-in' || s.role === 'split-in') return FIXED_SENTENCES.entry
  return null
}

/** `(output, input)` from either drag direction. */
export function normalise(a: SocketSpec, b: SocketSpec): { o: SocketSpec; i: SocketSpec } {
  return a.dir === 'in' && b.dir === 'out' ? { o: b, i: a } : { o: a, i: b }
}

/** Whether a wire between these two exists already. */
export type Wired = (o: SocketRef, i: SocketRef) => boolean

/** `null` when the wire may be made, else the sentence that says why not.
 *  `from` is where the drag started, `to` where it was dropped. The rules are
 *  checked in this order and nowhere else. */
export function canConnect(from: SocketSpec, to: SocketSpec, wired: Wired = () => false): string | null {
  const { o, i } = normalise(from, to)
  if (o.block === i.block) return SENTENCES.self
  if (from.dir === to.dir) return from.dir === 'out' ? SENTENCES.bothOut : SENTENCES.bothIn
  if (o.type !== i.type) return SENTENCES.typeMismatch(from.type, to.type)
  if (o.type === 'responses') return SENTENCES.responses
  if (o.type === 'link') return SENTENCES.link
  if (o.type === 'requests') {
    if (o.role === 'api-in') return SENTENCES.apiInput
    if (o.role === 'split-local') {
      if (i.role === 'provider-row-in') return SENTENCES.localToCloud
      if (i.role === 'machine-row-in') return SENTENCES.localToMachine(i.model ?? i.label, i.served ?? '')
    }
    if (o.role === 'split-cloud' && i.role !== 'provider-row-in') return SENTENCES.cloudToLocal
  }
  if (wired(o, i)) return SENTENCES.already
  return null
}

export type DragClass = 'source' | 'valid' | 'dim'

/** What a socket looks like while `from` is being dragged: the socket the drag
 *  started on, one it would accept (grows and glows), or anything else
 *  (dims to 0.18). */
export function dragClass(from: SocketSpec, candidate: SocketSpec, wired: Wired = () => false): DragClass {
  if (candidate.block === from.block && candidate.socket === from.socket) return 'source'
  return canConnect(from, candidate, wired) === null ? 'valid' : 'dim'
}

/** A drop on a block's body: the first of its sockets that accepts the wire,
 *  else the sentence to show. */
export function dropOnBlock(
  from: SocketSpec,
  blockSockets: readonly SocketSpec[],
  blockTitle: string,
  wired: Wired = () => false,
): { socket: SocketSpec } | { refusal: string } {
  for (const s of blockSockets) if (canConnect(from, s, wired) === null) return { socket: s }
  const opposite = blockSockets.find((s) => s.type === from.type && s.dir !== from.dir)
  if (opposite) return { refusal: canConnect(from, opposite, wired) ?? SENTENCES.already }
  return { refusal: SENTENCES.nothingTakes(blockTitle, from.type) }
}

/** The id of the wire from one socket to another. Opaque outside this
 *  module; `|` never appears in a block id's own scheme separators. */
export function wireId(o: SocketRef, i: SocketRef): string {
  return `${o.block}|${o.socket}|${i.block}|${i.socket}`
}

export function isEditableWire(o: SocketSpec, i: SocketSpec): boolean {
  return o.editable && i.editable
}

// ── The legend ───────────────────────────────────────────────────────────────

export const LEGEND = {
  shapes: 'filled = yours to wire · hollow = fixed by the server',
  colours: 'amber = worth a look · red = broken',
} as const
