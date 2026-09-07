import { useState } from 'react'
import type { SettingSource } from '../../api/types'
import { useSettings } from '../../state/resources'
import { useBackend } from '../../state/backend'

// Ported from mockups-next/derate.html's "Containment" card (moved here from
// the old Spend tab in the approved plan -- this is the only place these
// three settings are actually mutable). Split out of ScopeCards when Settings
// grew sub-tabs: this is the only mutable thing in that file, and it belongs
// under Policy while the static roadmap cards belong under About. One
// component cannot render into two sub-tabs.
//
// The 501 sentence below is not a client paraphrase: it is copied from
// gateway/ui_api.py's own literal string, so it reads identically whichever
// way the cap gets refused.
//
// The card keeps the mockup's heading and lede verbatim ("Containment" /
// "Stop traffic leaving your hardware, and cap what it can cost when it
// does.") -- true regardless of enforceability, since local-only alone
// already does the first half. Only the mockup's trailing sentence, "Cloud
// targets stop admitting once the cap is hit.", is dropped: it is false
// whenever daily_spend_cap_enforceable is false, and the per-field
// CAP_UNENFORCEABLE_SENTENCE already covers that case where the cap input
// actually lives.
const CAP_UNENFORCEABLE_SENTENCE =
  'A daily spend cap cannot be enforced: no provider port reports spend, so there is nothing to measure the cap against.'

const SOURCE_LABEL: Record<SettingSource, string> = {
  env: 'set by env',
  file: 'file',
  default: 'default',
}

export function ContainmentCard() {
  const settings = useSettings()
  const { backend, invalidate } = useBackend()
  const data = settings.data

  const [capInput, setCapInput] = useState<string | null>(null)
  const [rateInput, setRateInput] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const capValue = capInput ?? (data?.daily_spend_cap_usd != null ? String(data.daily_spend_cap_usd) : '')
  const rateValue = rateInput ?? (data ? String(data.electricity_rate_usd_per_kwh) : '')

  const toggleLocalOnly = async () => {
    if (!data) return
    setError(null)
    try {
      await backend.patchSettings({ local_only: !data.local_only })
      invalidate()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const commitCap = async () => {
    // Untouched means capInput is still null (the value shown is only the
    // server's own, via the capValue fallback below) -- committing anyway
    // would PATCH the field's current value right back at the server, which
    // moves it into the settings file and flips its source hint from
    // "set by env"/"default" to "file" purely because focus passed through
    // the input.
    if (!data || capInput === null) return
    const v = capValue.trim()
    // Blank means "no cap" (null); a typed number, including zero, is a real
    // instruction and must reach the server as that number, not collapse to
    // the same null a cleared field sends.
    const parsed = v === '' ? null : Number(v)
    if (parsed != null && !Number.isFinite(parsed)) {
      setCapInput(null)
      return
    }
    setError(null)
    try {
      await backend.patchSettings({ daily_spend_cap_usd: parsed })
      setCapInput(null)
      invalidate()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const commitRate = async () => {
    if (!data || rateInput === null) return
    const parsed = Number(rateValue.trim())
    if (!Number.isFinite(parsed)) {
      setRateInput(null)
      return
    }
    setError(null)
    try {
      await backend.patchSettings({ electricity_rate_usd_per_kwh: parsed })
      setRateInput(null)
      invalidate()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <div className="card2">
      <h3>Containment</h3>
      <div className="unit">
        Stop traffic leaving your hardware, and cap what it can cost when it does.
      </div>
      <label className="sw">
        <input
          type="checkbox"
          checked={data?.local_only ?? false}
          disabled={!data}
          onChange={() => void toggleLocalOnly()}
        />
        <span>Local only — never route to cloud providers</span>
      </label>
      {data ? (
        <div className="unit" style={{ marginLeft: 24, marginTop: -3 }}>
          {SOURCE_LABEL[data.sources.local_only]}
        </div>
      ) : null}

      <div style={{ display: 'flex', gap: 10, alignItems: 'flex-start', marginTop: 10, flexWrap: 'wrap' }}>
        <div className="fld" style={{ width: 150 }}>
          <label htmlFor="cap">Daily cap</label>
          <input
            id="cap"
            className="mono"
            value={capValue}
            disabled={!data || !data.daily_spend_cap_enforceable}
            onChange={(e) => setCapInput(e.target.value)}
            onBlur={() => void commitCap()}
            placeholder="none"
          />
          <span className="unit">
            {!data
              ? null
              : !data.daily_spend_cap_enforceable
                ? CAP_UNENFORCEABLE_SENTENCE
                : SOURCE_LABEL[data.sources.daily_spend_cap_usd]}
          </span>
        </div>
        <div className="fld" style={{ width: 150 }}>
          <label htmlFor="rate">Electricity $/kWh</label>
          <input
            id="rate"
            className="mono"
            value={rateValue}
            disabled={!data}
            onChange={(e) => setRateInput(e.target.value)}
            onBlur={() => void commitRate()}
          />
          <span className="unit">
            {data ? SOURCE_LABEL[data.sources.electricity_rate_usd_per_kwh] : null}
          </span>
        </div>
      </div>

      {error ? (
        <div className="label" style={{ color: 'var(--fault)', marginTop: 8, whiteSpace: 'pre-wrap' }}>
          {error}
        </div>
      ) : null}
    </div>
  )
}
