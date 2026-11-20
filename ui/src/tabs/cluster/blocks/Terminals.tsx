import type { Node, NodeProps } from '@xyflow/react'
import { BlockFrame } from '../../../components/BlockFrame'
import type { Block } from '../doc'
import { Socket, SocketLabel, socket, toneOf, useEditor } from './editor'

export type BlockNode<K extends Block['kind']> = Node<{ block: Extract<Block, { kind: K }> }, K>

/** API Input: where every request enters, one fixed wire to each balancer. */
export function InputBlock({ data, selected }: NodeProps<BlockNode<'input'>>) {
  const out = socket(data.block.sockets, 'out')
  return (
    <div className="wnode">
      <BlockFrame title="API Input" sub="/v1" icon="arrow-right" selected={selected}>
        <div className="wport wport-right">
          <SocketLabel spec={out} align="right" />
          <Socket spec={out} />
        </div>
      </BlockFrame>
    </div>
  )
}

/** API Output: where every answer leaves, through its balancer's merge. */
export function OutputBlock({ data, selected }: NodeProps<BlockNode<'output'>>) {
  const input = socket(data.block.sockets, 'in')
  return (
    <div className="wnode">
      <BlockFrame title="API Output" sub="/v1" icon="arrow-right" selected={selected}>
        <div className="wport">
          <Socket spec={input} />
          <SocketLabel spec={input} />
        </div>
      </BlockFrame>
    </div>
  )
}

function whole(value: string): number | undefined {
  return value.trim() === '' ? undefined : Number(value)
}

/** A rate limit. Plugged into one or more balancers' `limit` sockets, it caps
 *  each of those served names at the same rpm/tpm. */
export function LimitBlock({ data, selected }: NodeProps<BlockNode<'limit'>>) {
  const editor = useEditor()
  const { limit, sockets } = data.block
  const out = socket(sockets, 'out')
  const issues = editor.issuesFor(limit.id)
  return (
    <div className="wnode">
      <BlockFrame
        title="Rate limit"
        icon="gauge"
        selected={selected}
        tone={toneOf(issues)}
        onRemove={() => editor.removeBlock(limit.id)}
        removeLabel="Remove this rate limit"
      >
        <div className="wfields">
          <label className="unit">
            rpm
            <input
              className="nodrag mono"
              type="number"
              min={1}
              step={1}
              value={limit.rpm ?? ''}
              onChange={(e) => editor.setLimit(limit.id, { rpm: whole(e.target.value) })}
            />
          </label>
          <label className="unit">
            tpm
            <input
              className="nodrag mono"
              type="number"
              min={1}
              step={1}
              value={limit.tpm ?? ''}
              onChange={(e) => editor.setLimit(limit.id, { tpm: whole(e.target.value) })}
            />
          </label>
        </div>
        <span className="unit">{limit.plugged.length ? `caps ${limit.plugged.join(', ')}` : 'caps nothing yet'}</span>
        <div className="wport wport-right">
          <SocketLabel spec={out} align="right" />
          <Socket spec={out} />
        </div>
      </BlockFrame>
    </div>
  )
}
