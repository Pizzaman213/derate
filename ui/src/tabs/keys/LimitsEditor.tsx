import { useState } from 'react'
import type { ServedModel } from '../../api/types'
import { Icon } from '../../components/Icon'
import {
  FIXED_LIMIT_TYPES,
  MODEL_LIMIT_SUFFIXES,
  limitValue,
  modelLimitType,
  type ModelLimitSuffix,
} from './rows'

const MODEL = '__model'

/** A key's configured limits: the ones already set, each removable, and one
 *  line to add another -- a fixed type, or `model:<served name>:<suffix>`
 *  built from what `/v1/models` serves. */
export function LimitsEditor({
  value,
  onChange,
  models,
  idPrefix,
}: {
  value: Record<string, number>
  onChange: (next: Record<string, number>) => void
  models: ServedModel[]
  idPrefix: string
}) {
  const [kind, setKind] = useState<string>(FIXED_LIMIT_TYPES[0])
  const [model, setModel] = useState('')
  const [suffix, setSuffix] = useState<ModelLimitSuffix>('tokens_cycle')
  const [amount, setAmount] = useState('')
  const [err, setErr] = useState<string | null>(null)

  const add = () => {
    const n = Number(amount)
    if (amount.trim() === '' || !Number.isFinite(n) || n < 0) {
      setErr('Enter a number of zero or more.')
      return
    }
    if (kind === MODEL && !model) {
      setErr('Pick a model for a per-model limit.')
      return
    }
    const type = kind === MODEL ? modelLimitType(model, suffix) : kind
    setErr(null)
    setAmount('')
    onChange({ ...value, [type]: n })
  }

  const remove = (type: string) => {
    const next = { ...value }
    delete next[type]
    onChange(next)
  }

  const entries = Object.entries(value)
  const errId = `${idPrefix}-limit-err`

  return (
    <div>
      {entries.length === 0 ? (
        <div className="unit">No limits of its own.</div>
      ) : (
        <table>
          <thead>
            <tr>
              <th scope="col">Limit</th>
              <th scope="col" style={{ textAlign: 'right' }}>
                Value
              </th>
              <th scope="col">
                <span className="unit">remove</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {entries.map(([type, v]) => (
              <tr key={type}>
                <td className="mono">{type}</td>
                <td className="num">{limitValue(type, v)}</td>
                <td>
                  <button type="button" className="ghost" aria-label={`Remove ${type}`} onClick={() => remove(type)}>
                    <Icon name="close" size={12} decorative />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div
        style={{
          display: 'flex',
          gap: 'var(--space-8)',
          alignItems: 'flex-end',
          flexWrap: 'wrap',
          marginTop: 'var(--space-8)',
        }}
      >
        <div className="fld">
          <label htmlFor={`${idPrefix}-ltype`}>Limit type</label>
          <select id={`${idPrefix}-ltype`} value={kind} onChange={(e) => setKind(e.target.value)}>
            {FIXED_LIMIT_TYPES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
            <option value={MODEL}>per model…</option>
          </select>
        </div>
        {kind === MODEL ? (
          <>
            <div className="fld">
              <label htmlFor={`${idPrefix}-lmodel`}>Model</label>
              <select id={`${idPrefix}-lmodel`} value={model} onChange={(e) => setModel(e.target.value)}>
                <option value="">choose…</option>
                {models.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.id}
                  </option>
                ))}
              </select>
            </div>
            <div className="fld">
              <label htmlFor={`${idPrefix}-lsuffix`}>Measure</label>
              <select
                id={`${idPrefix}-lsuffix`}
                value={suffix}
                onChange={(e) => setSuffix(e.target.value as ModelLimitSuffix)}
              >
                {MODEL_LIMIT_SUFFIXES.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </div>
          </>
        ) : null}
        <div className="fld">
          <label htmlFor={`${idPrefix}-lvalue`}>Value</label>
          <input
            id={`${idPrefix}-lvalue`}
            className="mono"
            inputMode="decimal"
            value={amount}
            aria-invalid={err ? true : undefined}
            aria-describedby={err ? errId : undefined}
            onChange={(e) => setAmount(e.target.value)}
          />
        </div>
        <button type="button" onClick={add}>
          <Icon name="plus" size={12} decorative /> Add limit
        </button>
      </div>
      {err ? (
        <div id={errId} className="unit" style={{ color: 'var(--fault)' }}>
          {err}
        </div>
      ) : null}
    </div>
  )
}
