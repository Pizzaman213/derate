import type { CustomServe } from '../../state/customServes'
import { removeCustomServe, useCustomServes } from '../../state/customServes'

/** A custom launch command replayed from a previous launch, so a command
 *  written by hand -- a quantization choice, a memory tuning knob, anything
 *  this cluster's standard recipe does not cover -- is not retyped every
 *  time.
 *
 *  Empty until the first launch that used one: see state/customServes.ts.
 *  Clicking an entry only seeds the Verdict card's custom-command field on
 *  the model it names; nothing here launches anything by itself. */
export function CustomServesCard({
  onReuse,
}: {
  onReuse: (serve: CustomServe) => void
}) {
  const serves = useCustomServes()
  if (!serves.length) return null

  return (
    <div style={{ margin: '0 0 10px' }}>
      <div className="sub">custom serves</div>
      <div className="chips" role="group" aria-label="Previously used custom launch commands">
        {serves.map((s) => (
          <span
            key={`${s.modelId} ${s.runtime} ${s.command}`}
            style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}
          >
            <button
              type="button"
              className="mono"
              title={`Reuse on ${s.modelId} (${s.runtime}): ${s.command}`}
              onClick={() => onReuse(s)}
              style={{
                maxWidth: 320,
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
            >
              {s.modelId} · {s.command}
            </button>
            <button
              type="button"
              className="ghost"
              aria-label={`Forget ${s.modelId}: ${s.command}`}
              title="Forget this"
              onClick={() => removeCustomServe(s)}
              style={{ padding: '4px 6px' }}
            >
              &#10005;
            </button>
          </span>
        ))}
      </div>
    </div>
  )
}
