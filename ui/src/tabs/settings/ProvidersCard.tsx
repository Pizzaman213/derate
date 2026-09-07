import { useState } from 'react'
import type { ProviderKind } from '../../api/types'
import { useProviders } from '../../state/resources'
import { useBackend } from '../../state/backend'
import { relativeTime } from '../../format'

// Ported from mockups-next/js/settings.js `settings()`'s `provTable` block.
// One row per provider, same as the mockup -- a provider's own accounting
// (requests_today, spend_today_usd) is provider-wide, not per-model, so
// there is no honest single $/Mtok for a provider serving many models at
// different prices; that column is dashed unless there is exactly one model
// to be unambiguous about.
const KINDS: ProviderKind[] = [
  'openrouter',
  'openai',
  'anthropic',
  'together',
  'groq',
  'ollama',
  'custom',
]

export function ProvidersCard() {
  const providers = useProviders()
  const { backend, invalidate } = useBackend()
  const [kind, setKind] = useState<ProviderKind>('openrouter')
  const [baseUrl, setBaseUrl] = useState('')
  const [apiKeyRef, setApiKeyRef] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const list = providers.data ?? []

  const remove = async (id: string) => {
    setBusy(id)
    setError(null)
    try {
      await backend.removeProvider(id)
      invalidate()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  const add = async () => {
    setBusy('__add__')
    setError(null)
    try {
      await backend.addProvider({
        kind,
        base_url: baseUrl.trim() || undefined,
        api_key_ref: apiKeyRef.trim() || undefined,
      })
      setBaseUrl('')
      setApiKeyRef('')
      invalidate()
    } catch (e) {
      // The server's own rejection -- e.g. a pasted key instead of a
      // reference -- rendered verbatim, never paraphrased.
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="card2">
      <h3>Providers</h3>
      <div className="unit" style={{ marginBottom: 10 }}>
        Keys are stored as references and resolved at request time. They are never displayed,
        logged, or included in exports.
      </div>
      <div style={{ overflowX: 'auto' }}>
        <table>
          <thead>
            <tr>
              <th>Provider</th>
              <th>Key reference</th>
              <th style={{ textAlign: 'right' }}>Models</th>
              <th style={{ textAlign: 'right' }}>$/Mtok</th>
              <th style={{ textAlign: 'right' }}>Today</th>
              <th>State</th>
              <th>Refreshed</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {list.map((p) => {
              const state = p.admission_block ? (
                <span style={{ color: 'var(--warn)' }}>{p.admission_block}</span>
              ) : p.healthy ? (
                'admitting'
              ) : (
                <span style={{ color: 'var(--fault)' }}>unhealthy</span>
              )

              const today =
                p.spend_today_usd == null
                  ? '—'
                  : p.daily_budget_usd != null
                    ? `$${p.spend_today_usd.toFixed(2)} of $${p.daily_budget_usd.toFixed(2)}`
                    : `$${p.spend_today_usd.toFixed(2)}`

              const onlyModel = p.models.length === 1 ? p.models[0] : null
              const price =
                onlyModel && onlyModel.output_cost_per_mtok != null
                  ? `$${onlyModel.output_cost_per_mtok.toFixed(3)}`
                  : '—'

              return (
                <tr key={p.provider_id}>
                  <td className="mono">{p.display_name}</td>
                  <td className="mono unit">{p.api_key_ref || '—'}</td>
                  <td className="num">{p.model_count ?? p.models.length}</td>
                  <td className="num">{price}</td>
                  <td className="num">{today}</td>
                  <td className="unit">{state}</td>
                  <td className="unit">{p.last_refreshed ? relativeTime(p.last_refreshed) : '—'}</td>
                  <td style={{ textAlign: 'right' }}>
                    <button
                      style={{ padding: '3px 9px' }}
                      onClick={() => void remove(p.provider_id)}
                      disabled={busy === p.provider_id}
                    >
                      {busy === p.provider_id ? 'Removing…' : 'Remove'}
                    </button>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <div
        className="unit"
        style={{ marginTop: 12, paddingTop: 10, borderTop: '1px solid var(--rule)' }}
      >
        The key reference is the name of an environment variable, not a key. It is resolved at
        request time and never displayed, logged, or exported — there is no reveal control and
        adding one would be the bug.
      </div>

      <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end', marginTop: 12, flexWrap: 'wrap' }}>
        <div className="fld">
          <label htmlFor="pkind">Provider</label>
          <select id="pkind" value={kind} onChange={(e) => setKind(e.target.value as ProviderKind)}>
            {KINDS.map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </select>
        </div>
        <div className="fld" style={{ flex: 1, minWidth: 180 }}>
          <label htmlFor="pbase">Base URL</label>
          <input
            id="pbase"
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            placeholder="leave blank for the default"
            spellCheck={false}
          />
        </div>
        <div className="fld" style={{ flex: 1, minWidth: 180 }}>
          {/* Labelled "Key reference", not "API key" -- the mockup's label
              and "sk-..." placeholder (derate.html) steer toward pasting a
              real key into a field that is POSTed as api_key_ref, the name
              of an environment variable. That is precisely the confusion
              the footnote below exists to correct; the field itself should
              not invite it. */}
          <label htmlFor="pkey">Key reference</label>
          <input
            id="pkey"
            type="password"
            autoComplete="off"
            value={apiKeyRef}
            onChange={(e) => setApiKeyRef(e.target.value)}
            placeholder="OPENROUTER_API_KEY"
          />
        </div>
        <button onClick={() => void add()} disabled={busy === '__add__'}>
          {busy === '__add__' ? 'Adding…' : 'Add provider'}
        </button>
      </div>

      {error ? (
        <div className="label" style={{ color: 'var(--fault)', marginTop: 8, whiteSpace: 'pre-wrap' }}>
          {error}
        </div>
      ) : null}
    </div>
  )
}
