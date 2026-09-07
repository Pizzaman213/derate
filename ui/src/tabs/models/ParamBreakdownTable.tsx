import type { ModelDetail } from '../../api/types'

const ROWS: [string, string][] = [
  ['embedding', 'Embedding'],
  ['attention', 'Attention'],
  ['dense_mlp', 'Dense MLP'],
  ['routed_experts', 'Routed experts'],
  ['shared_experts', 'Shared experts'],
  ['router', 'Router'],
  ['norms', 'Norms'],
  ['vision', 'Vision tower'],
  ['lm_head', 'Output head'],
]

/** Where the parameters are, and the one place the two totals differ.
 *
 *  The MTP row is the reason this table exists rather than a single figure.
 *  `total` is what a runtime loads; `total_with_mtp` is what a weight index on
 *  the hub counts. Showing only the first leaves a reader to discover a gap
 *  between our number and the repository's own; showing both, with the
 *  resolver's sentence under them, explains it. */
export function ParamBreakdownTable({ detail }: { detail: ModelDetail }) {
  const b = detail.param_breakdown ?? {}
  const rows = ROWS.filter(([key]) => (b[key] ?? 0) > 0)
  const mtp = b['mtp'] ?? 0
  const total = b['total']
  const withMtp = b['total_with_mtp']

  if (!rows.length && !total) {
    return <p className="unit">No parameter breakdown was available.</p>
  }

  return (
    <div style={{ overflowX: 'auto' }}>
      <table>
        <tbody>
          {rows.map(([key, label]) => (
            <tr key={key}>
              <td>{label}</td>
              <td className="num">{billions(b[key])}</td>
            </tr>
          ))}
          {mtp > 0 ? (
            <tr>
              <td>
                Multi-token prediction{' '}
                <span className="unit">excluded from total</span>
              </td>
              <td className="num">{billions(mtp)}</td>
            </tr>
          ) : null}
          <tr>
            <td>
              <strong style={{ fontWeight: 500 }}>Loaded by a runtime</strong>
            </td>
            <td className="num">{billions(detail.shape.total_params)}</td>
          </tr>
          {mtp > 0 ? (
            <tr>
              <td>In the checkpoint</td>
              <td className="num">
                {billions(detail.capabilities.mtp.total_params_with_mtp ?? withMtp)}
              </td>
            </tr>
          ) : null}
        </tbody>
      </table>
      {mtp > 0 && detail.capabilities.mtp.note ? (
        <p className="unit" style={{ marginTop: 6, whiteSpace: 'pre-wrap' }}>
          {detail.capabilities.mtp.note}
        </p>
      ) : null}
    </div>
  )
}

function billions(n: number | null | undefined): string {
  // A missing count is an em dash. Never 0 -- a real zero is a claim that a
  // bucket is empty, which is a different statement from not knowing.
  if (n == null) return '—'
  return `${(n / 1e9).toFixed(2)}B`
}
