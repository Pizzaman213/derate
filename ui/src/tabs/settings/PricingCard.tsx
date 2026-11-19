import { useState, type FormEvent } from 'react'
import type { LocalPrice } from '../../api/types'
import { Verbatim } from '../../components/Verbatim'
import { useBackend, useResource } from '../../state/backend'
import { useModels } from '../../state/resources'
import { isPriced, parseRate, pricingRows, rateText, type PricingRow } from './pricing'

/** What a local model costs, for dollar limits and the per-key cost column.
 *
 *  A local request is estimated only when its served name has BOTH rates
 *  (`PricingStore.cost`); otherwise it is unpriced, which is not free: it
 *  counts toward no dollar limit and is counted on its own. An empty field is
 *  therefore null, never 0 -- a 0 would claim the request cost nothing.
 *
 *  Rows are every model a local target serves now plus every model that
 *  already has a price, so a price outlives the deployment it was set for and
 *  can still be cleared. */
export function PricingCard() {
  const models = useModels()
  const prices = useResource<LocalPrice[]>((b) => b.pricing(), 30000)
  const rows = pricingRows(models.data, prices.data)
  const ready = models.data != null && prices.data != null
  const error = prices.error ?? models.error

  return (
    <div className="card2">
      <h3>Local model pricing</h3>
      <div className="unit">
        Dollars per million tokens for models served on your own hardware. Leave a rate empty for
        unpriced: an unpriced request counts toward no dollar limit. A model is priced only when
        both rates are set.
      </div>
      {error ? <Verbatim text={error.message} size="unit" /> : null}
      {!ready ? (
        error ? null : (
          <div className="unit" aria-busy="true" aria-live="polite">
            Reading local models and prices…
          </div>
        )
      ) : rows.length === 0 ? (
        <div className="unit">No model is served locally yet — deploy one from Models.</div>
      ) : (
        <div style={{ overflowX: 'auto', marginTop: 'var(--space-10)' }}>
          <table>
            <thead>
              <tr>
                <th scope="col">Model</th>
                <th scope="col" style={{ textAlign: 'right' }}>
                  $ / 1M input tokens
                </th>
                <th scope="col" style={{ textAlign: 'right' }}>
                  $ / 1M output tokens
                </th>
                <th scope="col">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <PriceRow key={r.model} row={r} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function PriceRow({ row }: { row: PricingRow }) {
  const { backend, invalidate } = useBackend()
  const [inText, setInText] = useState(rateText(row.price?.input_usd_per_mtok))
  const [outText, setOutText] = useState(rateText(row.price?.output_usd_per_mtok))
  const [fieldError, setFieldError] = useState<string | null>(null)
  const [serverError, setServerError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const errId = `price-err-${row.model}`
  const savedIn = rateText(row.price?.input_usd_per_mtok)
  const savedOut = rateText(row.price?.output_usd_per_mtok)
  const dirty = inText.trim() !== savedIn || outText.trim() !== savedOut

  const run = async (op: () => Promise<unknown>) => {
    setBusy(true)
    setServerError(null)
    try {
      await op()
      invalidate()
    } catch (e) {
      setServerError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const save = (e: FormEvent) => {
    e.preventDefault()
    const i = parseRate(inText)
    const o = parseRate(outText)
    if ('error' in i || 'error' in o) {
      setFieldError('error' in i ? i.error : 'error' in o ? o.error : null)
      return
    }
    setFieldError(null)
    void run(async () => {
      await backend.setPricing(row.model, i.value, o.value)
      // What was stored, spelled the way the row will read it back ("1.50" -> "1.5").
      setInText(rateText(i.value))
      setOutText(rateText(o.value))
    })
  }

  const clear = () => {
    setFieldError(null)
    void run(async () => {
      await backend.clearPricing(row.model)
      setInText('')
      setOutText('')
    })
  }

  return (
    <tr>
      <td>
        <span className="mono">{row.model}</span>
        <div className="unit">
          {isPriced(row.price) ? 'priced' : 'unpriced'}
          {row.served ? '' : ' · not served locally now'}
        </div>
        {fieldError ? (
          <div id={errId} className="unit" style={{ color: 'var(--fault)' }}>
            {fieldError}
          </div>
        ) : null}
        {serverError ? <Verbatim text={serverError} size="unit" /> : null}
      </td>
      <td style={{ textAlign: 'right' }}>
        <input
          form={`price-${row.model}`}
          className="mono"
          inputMode="decimal"
          value={inText}
          placeholder="unpriced"
          aria-label={`${row.model}: dollars per million input tokens`}
          aria-invalid={fieldError ? true : undefined}
          aria-describedby={fieldError ? errId : undefined}
          onChange={(e) => setInText(e.target.value)}
        />
      </td>
      <td style={{ textAlign: 'right' }}>
        <input
          form={`price-${row.model}`}
          className="mono"
          inputMode="decimal"
          value={outText}
          placeholder="unpriced"
          aria-label={`${row.model}: dollars per million output tokens`}
          aria-invalid={fieldError ? true : undefined}
          aria-describedby={fieldError ? errId : undefined}
          onChange={(e) => setOutText(e.target.value)}
        />
      </td>
      <td style={{ whiteSpace: 'nowrap', textAlign: 'right' }}>
        {/* A form cannot wrap table cells; the inputs join it by id. */}
        <form id={`price-${row.model}`} onSubmit={save} style={{ display: 'inline' }}>
          <button type="submit" disabled={busy || !dirty}>
            {busy ? 'Saving…' : 'Save'}
          </button>
        </form>{' '}
        <button type="button" className="ghost" disabled={busy || row.price == null} onClick={clear}>
          Clear
        </button>
      </td>
    </tr>
  )
}
