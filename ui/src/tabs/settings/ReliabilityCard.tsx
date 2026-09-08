import type { SettingSource } from '../../api/types'
import { useState } from 'react'
import { useSettings } from '../../state/resources'
import { useBackend } from '../../state/backend'

// A reliability concern, not a cost/locality one -- split from ContainmentCard
// rather than folded in, so each card's heading names one policy family.

const SOURCE_LABEL: Record<SettingSource, string> = {
  env: 'set by env',
  file: 'file',
  default: 'default',
}

export function ReliabilityCard() {
  const settings = useSettings()
  const { backend, invalidate } = useBackend()
  const data = settings.data
  const [error, setError] = useState<string | null>(null)

  const toggleAutoRestart = async () => {
    if (!data) return
    setError(null)
    try {
      await backend.patchSettings({
        auto_restart_crashed_deployments: !data.auto_restart_crashed_deployments,
      })
      invalidate()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <div className="card2">
      <h3>Reliability</h3>
      <div className="unit">
        Bring a deployment back on its own after it crashes.
      </div>
      <label className="sw">
        <input
          type="checkbox"
          checked={data?.auto_restart_crashed_deployments ?? false}
          disabled={!data}
          onChange={() => void toggleAutoRestart()}
        />
        <span>Auto-restart crashed deployments</span>
      </label>
      {data ? (
        <div className="unit" style={{ marginLeft: 24, marginTop: -3 }}>
          {SOURCE_LABEL[data.sources.auto_restart_crashed_deployments]}
        </div>
      ) : null}
      {error ? (
        <div className="label" style={{ color: 'var(--fault)', marginTop: 8, whiteSpace: 'pre-wrap' }}>
          {error}
        </div>
      ) : null}
    </div>
  )
}
