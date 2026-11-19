import { useCallback, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent } from 'react'
import {
  Background,
  BackgroundVariant,
  Panel,
  ReactFlow,
  applyNodeChanges,
  useConnection,
  type Connection,
  type Edge,
  type EdgeChange,
  type FinalConnectionState,
  type HandleType,
  type Node,
  type NodeChange,
  type OnBeforeDelete,
} from '@xyflow/react'
import type { RoutingConfig } from '../../api/types'
import { useSelection } from '../../state/selection'
import type { Block, Size, Wire, XY } from './doc'
import { EditorContext, type EditorApi } from './blocks/editor'
import { MergeBlock, SplitBlock } from './blocks/Balancer'
import { MachineBlock, ProviderBlock } from './blocks/Containers'
import { ModelBlock } from './blocks/ModelBlock'
import { InputBlock, LimitBlock, OutputBlock } from './blocks/Terminals'
import { PathRegistry, WireEdge, type WireEdgeType } from './edges'
import { ParticleLayer } from './ParticleLayer'
import type { PathLike } from './particles'
import { SENTENCES, canConnect, dropOnBlock, type SocketRef } from './sockets'

const NODE_TYPES = {
  // Not `input`/`output`: those are React Flow's own built-in node types,
  // and its stylesheet pads and borders them.
  'api-input': InputBlock,
  'api-output': OutputBlock,
  split: SplitBlock,
  merge: MergeBlock,
  machine: MachineBlock,
  provider: ProviderBlock,
  limit: LimitBlock,
  model: ModelBlock,
}
const EDGE_TYPES = { wire: WireEdge }
const DELETE_KEYS = ['Backspace', 'Delete']

type BlockNodeType = Node<{ block: Block }>

interface NodeView {
  position: XY
  selected?: boolean
  measured?: BlockNodeType['measured']
  dragging?: boolean
}

interface Props {
  blocks: Block[]
  wires: Wire[]
  positions: Record<string, XY>
  editor: EditorApi
  routing: RoutingConfig[]
  onMoved: (id: string, xy: XY) => void
  onConnect: (from: SocketRef, to: SocketRef) => void
  onCut: (wireId: string) => void
  onRemove: (blockId: string) => void
  /** Sizes React Flow measured, per block, as they arrive. */
  onMeasured: (sizes: Record<string, Size>) => void
  /** Double-click on a load balancer: its dialog. */
  onOpenBalancer: (served: string) => void
  /** Delete on a selected load balancer: confirm, then take it away. */
  onRemoveServed: (served: string) => void
}

/** The hint at the top of the canvas while a wire is being dragged. */
function DragHint({ editor }: { editor: EditorApi }) {
  const from = useConnection((c) => (c.inProgress ? `${c.fromHandle.nodeId}\u0000${c.fromHandle.id ?? ''}` : null))
  if (!from) return null
  const [block = '', socket = ''] = from.split('\u0000')
  const spec = editor.specOf(block, socket)
  if (!spec) return null
  return (
    <Panel position="top-center">
      <span className="whint">{SENTENCES.dragHint(spec.type, spec.blockTitle)}</span>
    </Panel>
  )
}

function pointer(event: MouseEvent | TouchEvent): { x: number; y: number } | null {
  if ('changedTouches' in event) {
    const t = event.changedTouches[0]
    return t ? { x: t.clientX, y: t.clientY } : null
  }
  return { x: event.clientX, y: event.clientY }
}

/** The wiring canvas. Blocks and wires come from doc.ts; every edit goes back
 *  out through the callbacks, so this holds nothing but React Flow's own
 *  view state (positions while dragging, what is selected). */
export function WiringCanvas({
  blocks,
  wires,
  positions,
  editor,
  routing,
  onMoved,
  onConnect,
  onCut,
  onRemove,
  onMeasured,
  onOpenBalancer,
  onRemoveServed,
}: Props) {
  const { selectNode, selectDep, selectLink, openSheet } = useSelection()
  const registry = useRef<Record<string, PathLike>>({}).current
  // React Flow's own view state per block -- where it is while being dragged,
  // what it measured, whether it is selected. The nodes themselves are derived
  // from the document in the same render, never copied in an effect: a node
  // one render behind its block resets a controlled input mid-word.
  const [view, setView] = useState<ReadonlyMap<string, NodeView>>(new Map())
  const [selectedEdges, setSelectedEdges] = useState<ReadonlySet<string>>(new Set())

  const nodes = useMemo<BlockNodeType[]>(
    () =>
      blocks.map((b) => {
        const v = view.get(b.id)
        return {
          id: b.id,
          type: b.kind === 'input' || b.kind === 'output' ? `api-${b.kind}` : b.kind,
          position: v?.dragging ? v.position : (positions[b.id] ?? v?.position ?? { x: 0, y: 0 }),
          data: { block: b },
          // A load balancer the server knows is deletable too: Delete asks
          // first, then takes it away (onBeforeDelete below).
          deletable: b.kind === 'limit' || b.kind === 'model' || (b.kind === 'split' && b.removable),
          selected: v?.selected,
          measured: v?.measured,
          dragging: v?.dragging,
        }
      }),
    [blocks, positions, view],
  )
  const nodesRef = useRef(nodes)
  nodesRef.current = nodes

  const edges = useMemo<WireEdgeType[]>(
    () =>
      wires.map((w) => ({
        id: w.id,
        type: 'wire',
        source: w.from.block,
        sourceHandle: w.from.socket,
        target: w.to.block,
        targetHandle: w.to.socket,
        data: { wire: w },
        deletable: w.editable,
        reconnectable: w.editable,
        selectable: w.editable || w.link != null,
        focusable: w.editable || w.link != null,
        selected: selectedEdges.has(w.id),
        ariaLabel: w.link
          ? `Link ${w.link.a} to ${w.link.b}, ${w.label ?? ''}`
          : `${w.type} wire from ${w.from.block} to ${w.to.block}`,
      })),
    [wires, selectedEdges],
  )

  const onNodesChange = useCallback(
    (changes: NodeChange<BlockNodeType>[]) => {
      const measured: Record<string, Size> = {}
      for (const c of changes) {
        if (c.type === 'dimensions' && c.dimensions) measured[c.id] = { w: c.dimensions.width, h: c.dimensions.height }
      }
      if (Object.keys(measured).length > 0) onMeasured(measured)
      setView((prev) => {
        const next = applyNodeChanges(
          changes.filter((c) => c.type !== 'remove'),
          nodesRef.current,
        )
        const out = new Map(prev)
        for (const n of next) {
          out.set(n.id, { position: n.position, selected: n.selected, measured: n.measured, dragging: n.dragging })
        }
        return out
      })
    },
    [onMeasured],
  )

  const onEdgesChange = useCallback((changes: EdgeChange[]) => {
    setSelectedEdges((prev) => {
      const next = new Set(prev)
      for (const c of changes) if (c.type === 'select') (c.selected ? next.add(c.id) : next.delete(c.id))
      return next
    })
  }, [])

  const isValidConnection = useCallback(
    (c: Connection | WireEdgeType) => {
      const a = editor.specOf(c.source, c.sourceHandle ?? '')
      const b = editor.specOf(c.target, c.targetHandle ?? '')
      return a != null && b != null && canConnect(a, b, editor.wired) === null
    },
    [editor],
  )

  const connect = useCallback(
    (c: Connection) =>
      onConnect({ block: c.source, socket: c.sourceHandle ?? '' }, { block: c.target, socket: c.targetHandle ?? '' }),
    [onConnect],
  )

  /** The block under a pointer that let go, if any. */
  const blockAt = useCallback(
    (event: MouseEvent | TouchEvent): Block | null => {
      const at = pointer(event)
      const el = at ? document.elementFromPoint(at.x, at.y)?.closest('.react-flow__node') : null
      const id = el?.getAttribute('data-id')
      return id ? (blocks.find((b) => b.id === id) ?? null) : null
    },
    [blocks],
  )

  // A drop that did not connect says why: on a socket, that socket's refusal;
  // on a block's body, the first socket there that takes it, else the refusal
  // for the nearest one.
  const onConnectEnd = useCallback(
    (event: MouseEvent | TouchEvent, state: FinalConnectionState) => {
      if (state.isValid || !state.fromHandle) return
      const from = editor.specOf(state.fromHandle.nodeId, state.fromHandle.id ?? '')
      if (!from) return
      if (state.toHandle) {
        const to = editor.specOf(state.toHandle.nodeId, state.toHandle.id ?? '')
        const refusal = to ? canConnect(from, to, editor.wired) : null
        if (refusal) editor.announce(refusal)
        return
      }
      const block = blockAt(event)
      if (!block || block.id === from.block) return
      const result = dropOnBlock(from, block.sockets, block.title, editor.wired)
      if ('socket' in result) onConnect(from, result.socket)
      else editor.announce(result.refusal)
    },
    [blockAt, editor, onConnect],
  )

  // Dragging an existing wire's end: onto a socket that takes it moves the
  // wire (cut, then connect -- React Flow already ran isValidConnection).
  const onReconnect = useCallback(
    (old: Edge, c: Connection) => {
      onCut(old.id)
      connect(c)
    },
    [onCut, connect],
  )

  // A wire end let go anywhere else: on a socket that refuses it, the
  // refusal and the wire stays; on a block's body, the first socket there
  // that takes it; on empty canvas, the wire is cut -- ComfyUI's convention.
  const onReconnectEnd = useCallback(
    (event: MouseEvent | TouchEvent, edge: Edge, _end: HandleType, state: FinalConnectionState) => {
      if (state.isValid || !state.fromHandle) return
      const from = editor.specOf(state.fromHandle.nodeId, state.fromHandle.id ?? '')
      if (!from) return
      if (state.toHandle) {
        const to = editor.specOf(state.toHandle.nodeId, state.toHandle.id ?? '')
        const refusal = to ? canConnect(from, to, editor.wired) : null
        if (refusal) editor.announce(refusal)
        return
      }
      const block = blockAt(event)
      if (block && block.id !== from.block) {
        const result = dropOnBlock(from, block.sockets, block.title, editor.wired)
        if ('socket' in result) {
          onCut(edge.id)
          onConnect(from, result.socket)
        } else editor.announce(result.refusal)
        return
      }
      if (block) return
      onCut(edge.id)
      editor.announce(SENTENCES.wireDropped(from.type))
    },
    [blockAt, editor, onConnect, onCut],
  )

  // Delete on a selected load balancer asks before it takes anything away;
  // the rest of the selection goes as usual.
  const onBeforeDelete = useCallback<OnBeforeDelete<BlockNodeType, WireEdgeType>>(
    async ({ nodes: ns, edges: es }) => {
      const splits = ns.flatMap((n) => (n.data.block.kind === 'split' ? [n.data.block] : []))
      if (splits.length === 0) return true
      for (const b of splits) onRemoveServed(b.served)
      const gone = new Set(splits.map((b) => b.id))
      return {
        nodes: ns.filter((n) => !gone.has(n.id)),
        edges: es.filter((e) => !gone.has(e.source) && !gone.has(e.target)),
      }
    },
    [onRemoveServed],
  )

  const onNodeClick = useCallback(
    (_: unknown, node: BlockNodeType) => {
      const b = node.data.block
      if (b.kind === 'machine') selectNode(b.node_id)
      if (b.kind === 'split' || b.kind === 'merge') selectDep(b.served)
    },
    [selectNode, selectDep],
  )

  // A double-click on a control inside a block belongs to that control.
  const onNodeDoubleClick = useCallback(
    (event: ReactMouseEvent, node: BlockNodeType) => {
      if ((event.target as Element).closest('button, input, select, textarea, label, a')) return
      const b = node.data.block
      if (b.kind === 'split' || b.kind === 'merge') onOpenBalancer(b.served)
      if (b.kind === 'machine') openSheet({ kind: 'node', id: b.node_id })
    },
    [onOpenBalancer, openSheet],
  )

  const onEdgeClick = useCallback(
    (_: unknown, edge: WireEdgeType) => {
      const link = edge.data?.wire.link
      if (link) selectLink(link.a, link.b)
    },
    [selectLink],
  )

  return (
    <EditorContext.Provider value={editor}>
      <PathRegistry.Provider value={registry}>
        {/* React Flow fills its parent with `height: 100%`, which needs a
            definite height: an absolutely pinned box has one, a flex item
            sized by min-height does not. */}
        <div className="graph-box">
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={NODE_TYPES}
            edgeTypes={EDGE_TYPES}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onNodeDragStop={(_, node) => onMoved(node.id, node.position)}
            onNodesDelete={(ns) => ns.forEach((n) => onRemove(n.id))}
            onEdgesDelete={(es) => es.forEach((e) => onCut(e.id))}
            onBeforeDelete={onBeforeDelete}
            onConnect={connect}
            onConnectEnd={onConnectEnd}
            isValidConnection={isValidConnection}
            edgesReconnectable
            reconnectRadius={14}
            onReconnect={onReconnect}
            onReconnectEnd={onReconnectEnd}
            onNodeClick={onNodeClick}
            onNodeDoubleClick={onNodeDoubleClick}
            onEdgeClick={onEdgeClick}
            zoomOnDoubleClick={false}
            deleteKeyCode={DELETE_KEYS}
            minZoom={0.25}
            maxZoom={2}
            fitView
            fitViewOptions={{ padding: 0.08 }}
            colorMode="light"
          >
            <Background variant={BackgroundVariant.Dots} gap={20} size={1} />
            <DragHint editor={editor} />
            <ParticleLayer wires={wires} routing={routing} />
          </ReactFlow>
        </div>
      </PathRegistry.Provider>
    </EditorContext.Provider>
  )
}
