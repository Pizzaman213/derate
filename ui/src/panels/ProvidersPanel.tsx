import type { Provider } from '../api/types'
import { Lamp } from '../components/Lamp'
import { Readout } from '../components/Readout'

/** Configured upstreams: health, how many models they contribute, and what they
 *  have cost today.
 *
 *  The key is rendered as three asterisks and there is no control to reveal it.
 *  This is a tool people screenshot; a key on screen is unrecoverable. What is
 *  shown instead is the environment variable the coordinator reads it from,
 *  which is the thing you would actually need to know. */
export function ProvidersPanel({ providers }: { providers: Provider[] }) {
  if (providers.length === 0) {
    return (
      <p className="label muted" style={{ fontWeight: 400, margin: 0 }}>
        No upstream providers configured.
      </p>
    )
  }

  return (
    <div style={{ display: 'grid', gap: 'var(--s1)' }}>
      {providers.map((p) => {
        const off = !p.enabled
        return (
          <div key={p.provider_id} style={{ display: 'grid', gap: 4, opacity: off ? 0.62 : 1 }}>
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                gap: 8,
              }}
            >
              <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <Lamp
                  signal={off ? 'idle' : p.healthy ? 'live' : 'fault'}
                  label={
                    off
                      ? `${p.display_name} disabled`
                      : p.healthy
                        ? `${p.display_name} healthy`
                        : `${p.display_name} unreachable`
                  }
                />
                <span className="label" style={{ fontWeight: 400 }}>
                  {p.display_name}
                </span>
              </span>
              {off ? (
                <span className="unit">disabled</span>
              ) : (
                <Readout
                  value={p.spend_today_usd ?? null}
                  decimals={2}
                  width={5}
                  unit="USD today"
                />
              )}
            </div>

            <div className="unit" style={{ paddingLeft: 16 }}>
              {p.models.length} {p.models.length === 1 ? 'model' : 'models'}
              {p.api_key_ref ? ` · ${p.api_key_ref} ***` : ' · no key'}
            </div>

            {p.last_error ? (
              <div
                className="label"
                style={{ fontWeight: 400, color: 'var(--fault)', paddingLeft: 16 }}
              >
                {p.last_error}
              </div>
            ) : null}
          </div>
        )
      })}
    </div>
  )
}
