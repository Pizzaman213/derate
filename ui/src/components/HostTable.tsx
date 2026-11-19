// OpenRouter's backend hosts for one model, as a pickable table.
//
// Shared by the Models tab's `BackendPicker` and the cluster editor's
// provider rows. Pure: it draws the rows it is handed and reports the pick;
// fetching `GET /api/providers/{id}/backends` and writing the pin are the
// caller's. `pinned` is the tag currently in force (null = OpenRouter picks).

import { useId } from 'react'

import type { ProviderBackendOption } from '../api/types'
import { Verbatim } from './Verbatim'

export type HostTableProps = {
  rows: ProviderBackendOption[]
  /** The pinned host's tag, or null when OpenRouter picks. */
  pinned: string | null
  /** Whether OpenRouter may fall back to other hosts when the pinned one is down. */
  fallback: boolean
  onPin: (tag: string | null) => void
  onFallback: (v: boolean) => void
  loading?: boolean
  /** The server's sentence, shown verbatim. */
  error?: string | null
  disabled?: boolean
  className?: string
}

/** Dollars per million tokens, the Models tab's `$x.xx` form. */
export const perMtok = (v: number | null) => (v == null ? '—' : `$${v.toFixed(2)}`)

export function HostTable({
  rows,
  pinned,
  fallback,
  onPin,
  onFallback,
  loading = false,
  error = null,
  disabled = false,
  className,
}: HostTableProps) {
  const name = useId()
  const priced = rows.filter((r) => r.output_cost_per_mtok != null)
  const cheapest = priced.length
    ? priced.reduce((a, b) => ((b.output_cost_per_mtok as number) < (a.output_cost_per_mtok as number) ? b : a)).tag
    : null

  return (
    <div className={className} style={{ display: 'grid', gap: 'var(--space-6)' }}>
      {loading ? <div className="unit">Reading OpenRouter's hosts…</div> : null}
      {error ? <Verbatim text={error} size="unit" /> : null}
      <div style={{ overflowX: 'auto' }}>
        <table>
          <thead>
            <tr>
              <th>
                <span className="sr-only">Use</span>
              </th>
              <th>Host</th>
              <th className="num">In $/Mtok</th>
              <th className="num">Out $/Mtok</th>
              <th className="num">Context</th>
              <th>Quantization</th>
            </tr>
          </thead>
          <tbody>
            <tr data-host="">
              <td>
                <input
                  type="radio"
                  name={name}
                  checked={pinned == null}
                  disabled={disabled}
                  onChange={() => onPin(null)}
                  aria-label="Use Automatic (OpenRouter's default)"
                />
              </td>
              <td colSpan={5}>Automatic (OpenRouter's default)</td>
            </tr>
            {rows.map((r) => (
              <tr key={r.tag} data-host={r.tag}>
                <td>
                  <input
                    type="radio"
                    name={name}
                    checked={pinned === r.tag}
                    disabled={disabled}
                    onChange={() => onPin(r.tag)}
                    aria-label={`Use ${r.provider_name}`}
                  />
                </td>
                <td>
                  {r.provider_name} <span className="unit mono">{r.tag}</span>
                  {r.tag === cheapest ? (
                    <>
                      {' '}
                      <span className="pill">cheapest</span>
                    </>
                  ) : null}
                </td>
                <td className="num">{perMtok(r.input_cost_per_mtok)}</td>
                <td className="num">{perMtok(r.output_cost_per_mtok)}</td>
                <td className="num">{r.context_length != null ? r.context_length.toLocaleString() : '—'}</td>
                <td className="unit">{r.quantization ?? '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!loading && !error && rows.length === 0 ? (
        <div className="unit">OpenRouter lists no hosts for this model yet.</div>
      ) : null}
      <label className="unit" style={{ display: 'flex', gap: 'var(--space-6)', alignItems: 'center' }}>
        <input
          type="checkbox"
          checked={fallback}
          disabled={disabled || pinned == null}
          onChange={(e) => onFallback(e.target.checked)}
        />
        Fall back to other hosts when this one is down
      </label>
    </div>
  )
}
