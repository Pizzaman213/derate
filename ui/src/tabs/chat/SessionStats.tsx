import { Readout } from '../../components/Readout'
import { sessionFigures } from './stats'
import type { Turn } from './turn'

/** The whole transcript's figures, above it. Absent until an answer has
 *  finished: a strip of dashes over an empty transcript reads as six failed
 *  measurements rather than as nothing having been sent. */
export function SessionStats({ turns, contextLength }: { turns: Turn[]; contextLength: number | null }) {
  const s = sessionFigures(turns)
  if (s.turns === 0) return null
  const contextPct =
    s.contextUsed !== null && contextLength !== null && contextLength > 0
      ? (s.contextUsed / contextLength) * 100
      : null
  return (
    <div className="chatstats quad">
      <Tile value={s.turns} decimals={0} width={3} label="turns" />
      <Tile value={s.promptTokens} decimals={0} width={6} label="in tok" />
      <Tile value={s.completionTokens} decimals={0} width={6} label={s.estimated ? 'out tok (est.)' : 'out tok'} />
      {s.reasoningTokens !== null ? (
        <Tile value={s.reasoningTokens} decimals={0} width={6} label="thinking tok" />
      ) : null}
      <Tile value={s.meanTtftMs} decimals={0} width={5} label="ttft ms, mean" />
      <Tile value={s.tokPerS} decimals={1} width={5} label="tok/s" />
      {contextPct !== null ? (
        <Tile
          value={contextPct}
          decimals={1}
          width={5}
          label="context %"
          title={`${s.contextUsed} of ${contextLength} tokens in the last exchange`}
        />
      ) : null}
    </div>
  )
}

function Tile({
  value,
  decimals,
  width,
  label,
  title,
}: {
  value: number | null
  decimals: number
  width: number
  label: string
  title?: string
}) {
  return (
    <div>
      <Readout value={value} decimals={decimals} width={width} size="readout" align="left" title={title} />
      <div className="unit" style={{ marginTop: 'var(--space-4)' }}>
        {label}
      </div>
    </div>
  )
}
