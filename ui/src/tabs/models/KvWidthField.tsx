import { useId } from 'react'
import type { RuntimeInfo } from '../../api/types'
import { FALLBACK_KV_NAMES, kvRows, pickableKvRows } from './kvWidths'
import type { ModelSession } from './useModelSession'

/** The KV cache width picker. Unlike a launch-only flag this changes what the
 *  gate APPROVES: the verdict re-plans when it moves, so every number above it
 *  is already the number for this width. Rows come from `kvWidths.ts`. */
export function KvWidthField({ s, info }: { s: ModelSession; info: RuntimeInfo | null }) {
  const uid = useId()
  // A width this plan refuses is not shown at all; only the one already
  // picked stays, so it can be left.
  const offered = pickableKvRows(
    kvRows(info?.kv_dtypes ?? FALLBACK_KV_NAMES, s.kvWidths, info?.kv_dtype_bytes, s.kvDtype),
  )
  return (
    <>
      <div className="mdl-kvgrid" role="radiogroup" aria-label="KV cache precision" aria-describedby={`${uid}-kvnote`}>
        {offered.map((row) => {
          // The accessible name is the width and its multiplier; the server's
          // reason describes it rather than becoming a 300-character name.
          const id = `${uid}-kv-${row.value || 'own'}`
          return (
            <label key={row.value || 'default'} className="mdl-check label">
              <input
                type="radio"
                name={`${uid}-kv`}
                checked={s.kvDtype === row.value}
                aria-labelledby={`${id}-name ${id}-ratio`}
                aria-describedby={row.why ? `${id}-why` : undefined}
                // A dashed width is one the plan answers with a 400, which
                // nulls the verdict this picker is drawn in. The selected one
                // stays reachable so it can be left.
                disabled={row.disabled}
                onChange={() => s.setKvDtype(row.value)}
              />
              <span id={`${id}-name`}>{row.label}</span>
              <span id={`${id}-ratio`} className="mono unit mdl-kvgrid-ratio">
                {row.ratio != null ? (
                  <>
                    {`×${row.ratio.toFixed(2)}`}
                    <span className="sr-only"> times the 16-bit cache</span>
                  </>
                ) : row.dashed ? (
                  <>
                    <span aria-hidden="true">—</span>
                    <span className="sr-only">not available for this plan</span>
                  </>
                ) : (
                  ''
                )}
              </span>
              <span className="mono unit">{row.flag}</span>
              {/* The server's sentence, as written. A span, not `Verbatim`:
                  that renders a <p>, which a <label> may not contain. */}
              {row.why ? (
                <span id={`${id}-why`} className="unit mdl-node-note">
                  {row.why}
                </span>
              ) : null}
            </label>
          )
        })}
      </div>
      <p className="unit" id={`${uid}-kvnote`}>
        The model's own is what the engine stores when told nothing. The multiplier is this model's cache against
        16-bit, scales included; the verdict above is already recomputed for the width you pick, and the engine stores
        that same width.
      </p>
    </>
  )
}
