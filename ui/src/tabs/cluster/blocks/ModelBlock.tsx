import type { NodeProps } from '@xyflow/react'
import { BlockFrame } from '../../../components/BlockFrame'
import { OverrideGate } from '../../../components/OverrideGate'
import { Verbatim } from '../../../components/Verbatim'
import { RUNTIME_OPTIONS } from '../../../state/runtime'
import { claimFor } from '../../models/Verdict'
import { DEFAULT_DRAFT_TOKENS, MAX_DRAFT_TOKENS } from '../doc'
import { Socket, SocketLabel, socket, toneOf, useEditor } from './editor'
import type { BlockNode } from './Terminals'

function count(value: string): number | null {
  const n = Number(value)
  return value.trim() === '' || !Number.isFinite(n) || n <= 0 ? null : Math.floor(n)
}

/** What wiring `deploy to` means, in words. */
function placementSentence(nodeIds: readonly string[]): string {
  if (nodeIds.length === 0) return 'Not placed yet: wire "deploy to" into a machine.'
  if (nodeIds.length === 1) return `One copy on ${nodeIds[0]}.`
  return `One instance spanning ${nodeIds.join(' and ')}: tensor parallel ${nodeIds.length}.`
}

/** A model to launch. Wired to one machine it is one copy there; to two or
 *  more, one instance across them. Nothing launches until Save. */
export function ModelBlock({ data, selected }: NodeProps<BlockNode<'model'>>) {
  const editor = useEditor()
  const { model: m, mtp, sockets } = data.block
  const out = socket(sockets, 'deploy')
  const plan = editor.plans[m.id]
  const result = plan?.result ?? null
  const gates = result?.serve?.overrides ?? []
  const issues = editor.issuesFor(m.id)
  const listId = `models-${m.id}`
  return (
    <div className="wnode wmodel">
      <BlockFrame
        title="Draft model"
        icon="models"
        selected={selected}
        tone={toneOf(issues)}
        onRemove={() => editor.removeBlock(m.id)}
        removeLabel="Remove this draft model"
      >
        <div className="wfields nodrag">
          <label className="unit wfield-wide">
            model
            <input
              list={listId}
              value={m.model_id}
              placeholder="org/model"
              spellCheck={false}
              onChange={(e) => editor.setModel(m.id, { model_id: e.target.value.trim(), granted: {} })}
            />
            <datalist id={listId}>
              {editor.modelIds.map((id) => (
                <option key={id} value={id} />
              ))}
            </datalist>
          </label>
          <label className="unit">
            runtime
            <select value={m.runtime} onChange={(e) => editor.setModel(m.id, { runtime: e.target.value, granted: {} })}>
              {RUNTIME_OPTIONS.map((r) => (
                <option key={r.value} value={r.value}>
                  {r.label}
                </option>
              ))}
            </select>
          </label>
          <label className="unit">
            context
            <input
              className="mono"
              type="number"
              min={1}
              placeholder="auto"
              value={m.context ?? ''}
              onChange={(e) => editor.setModel(m.id, { context: count(e.target.value), granted: {} })}
            />
          </label>
          <label className="unit">
            concurrency
            <input
              className="mono"
              type="number"
              min={1}
              placeholder="auto"
              value={m.concurrency ?? ''}
              onChange={(e) => editor.setModel(m.id, { concurrency: count(e.target.value), granted: {} })}
            />
          </label>
        </div>

        {mtp?.present ? (
          <div className="wfields nodrag">
            <label className="unit">
              <input
                type="checkbox"
                checked={m.mtp}
                onChange={(e) => editor.setModel(m.id, { mtp: e.target.checked, granted: {} })}
              />{' '}
              MTP (speculative decoding)
            </label>
            {m.mtp ? (
              <label className="unit">
                draft tokens
                <input
                  className="mono"
                  type="number"
                  min={1}
                  max={MAX_DRAFT_TOKENS}
                  value={m.speculative_tokens}
                  onChange={(e) =>
                    editor.setModel(m.id, {
                      speculative_tokens: Math.min(MAX_DRAFT_TOKENS, Math.max(1, count(e.target.value) ?? DEFAULT_DRAFT_TOKENS)),
                      granted: {},
                    })
                  }
                />
              </label>
            ) : null}
          </div>
        ) : mtp ? (
          <span className="unit">No MTP module in this checkpoint</span>
        ) : null}

        <span className="unit">{placementSentence(m.node_ids)}</span>
        {plan?.checking ? <span className="unit">Planning…</span> : null}
        {plan?.error ? <Verbatim text={plan.error} size="unit" /> : null}
        {result?.fit ? <Verbatim text={result.fit.reason} size="unit" /> : null}
        {result?.serve && !result.serve.allowed && result.serve.reason !== result.fit?.reason ? (
          <Verbatim text={result.serve.reason} size="unit" />
        ) : null}
        {result?.resolver_warnings.map((w) => <Verbatim key={w} text={w} size="unit" />)}
        {gates.map((g) => (
          <div key={g.param} className="nodrag">
            <OverrideGate
              reason={g.reason}
              sentence={claimFor(g, result?.fit_live ?? null, result?.fit?.usable_per_node ?? null, result!.plan)}
              checked={m.granted[g.param] === true}
              onChange={(v) => editor.setModel(m.id, { granted: { ...m.granted, [g.param]: v } })}
            />
          </div>
        ))}

        <div className="wport wport-right">
          <SocketLabel spec={out} align="right" />
          <Socket spec={out} />
        </div>
      </BlockFrame>
    </div>
  )
}
