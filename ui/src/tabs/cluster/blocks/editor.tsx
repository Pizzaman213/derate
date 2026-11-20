// What every block on the canvas reaches for: the socket lookup, the edit
// actions, the plans, and the one Socket component every handle is drawn by.

import { createContext, useContext, type CSSProperties } from 'react'
import { Handle, Position, useConnection } from '@xyflow/react'
import type { Issue, ModelBlock, PlanState, ProviderPin } from '../doc'
import { SOCKET_TYPES, dragClass, fixedSentence, type SocketSpec, type Wired } from '../sockets'

export interface EditorApi {
  specOf: (block: string, socket: string) => SocketSpec | undefined
  wired: Wired
  /** Writes to the canvas's aria-live region. */
  announce: (message: string) => void
  /** Issues pinned to a block: validation, dry-run refusals, live faults. */
  issuesFor: (block: string) => Issue[]
  plans: Record<string, PlanState>
  /** Model ids to offer in a draft model's picker. */
  modelIds: string[]
  setLimit: (id: string, patch: { rpm?: number; tpm?: number }) => void
  setModel: (id: string, patch: Partial<ModelBlock>) => void
  removeBlock: (id: string) => void
  setProviderEnabled: (providerId: string, enabled: boolean) => void
  addProviderModel: (providerId: string, upstream: string, served: string) => void
  removeProviderModel: (providerId: string, upstream: string) => void
  /** Catalogue rows not yet on a provider, for its Add model popover. */
  catalogueFor: (providerId: string) => { upstream_id: string; served_name: string }[]
  openDeployment: (served: string) => void
  /** Cut a wire by id: the × on a selected wire. */
  cut: (wireId: string) => void
  /** Drop one launch flag (with its values) from a running deployment; Save
   *  relaunches it without. `args` is what it runs with now. */
  dropFlag: (deploymentId: string, args: string[], flag: string) => void
  /** Pin a provider model to a backend host, or `null` for automatic. */
  setProviderPin: (providerId: string, upstream: string, pin: ProviderPin | null) => void
}

export const EditorContext = createContext<EditorApi | null>(null)

export function useEditor(): EditorApi {
  const api = useContext(EditorContext)
  if (!api) throw new Error('useEditor outside the wiring canvas')
  return api
}

const POSITION = {
  left: Position.Left,
  right: Position.Right,
  top: Position.Top,
  bottom: Position.Bottom,
} as const

/** One socket. Filled when it is yours to wire, hollow when the server fixes
 *  it; coloured by type. While a wire is being dragged it glows when it would
 *  take that wire and dims when it would not. */
export function Socket({ spec, style }: { spec: SocketSpec; style?: CSSProperties }) {
  const editor = useEditor()
  const from = useConnection((c) => (c.inProgress ? `${c.fromHandle.nodeId}\u0000${c.fromHandle.id ?? ''}` : null))
  let state = ''
  if (from) {
    const [block = '', socket = ''] = from.split('\u0000')
    const source = editor.specOf(block, socket)
    if (source) state = dragClass(source, spec, editor.wired)
  }
  // A link line runs from one machine's lower edge to the next one's upper
  // edge, and React Flow draws an edge only from a source handle.
  const type = spec.socket === 'link-bottom' || spec.dir === 'out' ? 'source' : 'target'
  const fixed = fixedSentence(spec)
  const name = `${spec.label} (${spec.type}${spec.editable ? '' : ', fixed'})`
  return (
    <Handle
      id={spec.socket}
      type={type}
      position={POSITION[spec.edge]}
      isConnectable={spec.editable}
      className={`sock ${spec.editable ? 'sock-edit' : 'sock-fixed'}${state ? ` sock-${state}` : ''}`}
      style={{ ['--sock' as string]: SOCKET_TYPES[spec.type].colour, ...style }}
      title={name}
      aria-label={name}
      onPointerDown={fixed ? () => editor.announce(fixed) : undefined}
    />
  )
}

/** A socket's visible label, in its type's colour. */
export function SocketLabel({ spec, align = 'left' }: { spec: SocketSpec; align?: 'left' | 'right' }) {
  return (
    <span className="socklabel" style={{ color: SOCKET_TYPES[spec.type].colour, textAlign: align }}>
      {spec.label}
    </span>
  )
}

/** The block's own socket by id. Every block knows its sockets already; this
 *  just reads them back without an `!` at each call site. */
export function socket(sockets: readonly SocketSpec[], id: string): SocketSpec {
  const s = sockets.find((x) => x.socket === id)
  if (!s) throw new Error(`no socket ${id}`)
  return s
}

/** A block's tone from its issues: red when anything is broken. */
export function toneOf(issues: readonly Issue[]): 'fault' | 'warn' | null {
  if (issues.some((i) => i.level === 'error')) return 'fault'
  if (issues.some((i) => i.level === 'warn')) return 'warn'
  return null
}
