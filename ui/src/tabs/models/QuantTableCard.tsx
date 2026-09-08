import { useState } from 'react'
import { useQuantTable } from '../../state/resources'

/** The quantization table, as the gateway reports it.
 *
 *  Here because it is the reference a person needs while reading a ladder --
 *  what "4.90 bpw" means, which schemes need which silicon, and which runtime
 *  will admit to loading them. Fetched rather than held in TypeScript: a second
 *  copy of these figures is a second answer, and the one that disagrees with
 *  the fit gate is the one that costs somebody a failed load. */
export function QuantTableCard() {
  const table = useQuantTable()
  const [open, setOpen] = useState(false)
  // Read off the payload, never typed here. There were two columns and three
  // runtimes the day `tts` landed, and a hardcoded pair does not render as a
  // missing column -- it renders as a complete table that quietly omits one
  // of the answers the caption promises. Order is the gateway's own.
  const runtimes = Object.keys(table.data?.schemes[0]?.runtimes ?? {})

  return (
    <div className="card2">
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
        <h3 style={{ flex: 1 }}>Quantization reference</h3>
        <button className="ghost" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
          {open ? 'Hide' : 'Show'} <span className="mono">{open ? '–' : '+'}</span>
        </button>
      </div>
      <div className="unit">
        Real bytes per parameter, block scales and zero points included — not the
        nominal bit width. Q4_K_M costs 4.90 bits per weight once its super-block
        scales are counted, not 4.00.
      </div>

      {!open ? null : table.error ? (
        <p
          className="label"
          style={{ fontWeight: 400, color: 'var(--fault)', whiteSpace: 'pre-wrap' }}
        >
          {table.error.message}
        </p>
      ) : !table.data ? (
        <p className="unit" style={{ marginTop: 8 }}>
          {table.loading ? 'Loading…' : 'The table could not be read.'}
        </p>
      ) : (
        <div style={{ overflowX: 'auto', marginTop: 10 }}>
          <table>
            <thead>
              <tr>
                <th>Scheme</th>
                <th style={{ textAlign: 'right' }}>bpw</th>
                <th style={{ textAlign: 'right' }}>bytes/param</th>
                <th>Family</th>
                <th>Silicon</th>
                {runtimes.map((name) => (
                  <th key={name}>{name}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {table.data.schemes.map((s) => (
                <tr key={s.key}>
                  <td className="mono">{s.key}</td>
                  <td className="num">{s.bits_per_weight.toFixed(2)}</td>
                  <td className="num">{s.bytes_per_param.toFixed(4)}</td>
                  <td className="unit">{s.family}</td>
                  <td className="unit" title={s.note}>
                    {s.native_compute_capability == null
                      ? 'any'
                      : `sm_${s.native_compute_capability}${s.emulated_below_native ? ' (emulated below)' : ''}`}
                  </td>
                  {runtimes.map((name) => (
                    <td key={name} className="unit">
                      {s.runtimes[name] ?? '—'}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
