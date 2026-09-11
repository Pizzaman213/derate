// The four fields the composer sends nothing for today, even though the
// gateway proxies all of them untouched (`control_plane/gateway/openai_api.py`
// rewrites only `model` before forwarding the body). This is therefore almost
// entirely frontend plumbing: the values live here as strings -- the natural
// shape for a controlled text/number input mid-edit -- and are only parsed
// into `temperature`/`max_tokens`/`stop` at send time, in `ChatTab`.

export interface ChatParams {
  system: string
  temperature: string
  maxTokens: string
  stop: string
}

export const DEFAULT_CHAT_PARAMS: ChatParams = {
  system: '',
  temperature: '',
  maxTokens: '',
  stop: '',
}

/** `undefined` for blank or unparseable, never `NaN` or `0` -- an unset field
 *  is "use the server's default", the same convention `client.ts` already
 *  uses to omit a key from the request body entirely. */
export function parseTemperature(value: string): number | undefined {
  const n = Number(value)
  return value.trim() === '' || Number.isNaN(n) ? undefined : n
}

export function parseMaxTokens(value: string): number | undefined {
  const n = Number(value)
  return value.trim() === '' || Number.isNaN(n) || n <= 0 ? undefined : Math.floor(n)
}

export function parseStop(value: string): string[] | undefined {
  const parts = value
    .split(',')
    .map((s) => s.trim())
    .filter((s) => s !== '')
  return parts.length > 0 ? parts : undefined
}

interface Props {
  value: ChatParams
  onChange: (next: ChatParams) => void
  disabled?: boolean
}

/** Closed by default -- these are the exception, not the common case, and a
 *  console that opened with four empty fields already showing would read as
 *  four things the operator is expected to fill in. */
export function RequestParamsFields({ value, onChange, disabled = false }: Props) {
  const set = (patch: Partial<ChatParams>) => onChange({ ...value, ...patch })

  return (
    <details className="reqparams">
      <summary className="unit">Advanced</summary>
      <div className="reqparamsgrid">
        <label>
          <span className="unit">System prompt</span>
          <textarea
            rows={2}
            value={value.system}
            disabled={disabled}
            onChange={(e) => set({ system: e.target.value })}
            placeholder="Sent as the first message"
          />
        </label>
        <label>
          <span className="unit">Temperature</span>
          <input
            type="number"
            min={0}
            max={2}
            step={0.1}
            value={value.temperature}
            disabled={disabled}
            onChange={(e) => set({ temperature: e.target.value })}
            placeholder="server default"
          />
        </label>
        <label>
          <span className="unit">Max tokens</span>
          <input
            type="number"
            min={1}
            step={1}
            value={value.maxTokens}
            disabled={disabled}
            onChange={(e) => set({ maxTokens: e.target.value })}
            placeholder="server default"
          />
        </label>
        <label>
          <span className="unit">Stop sequences</span>
          <input
            type="text"
            value={value.stop}
            disabled={disabled}
            onChange={(e) => set({ stop: e.target.value })}
            placeholder="comma-separated"
          />
        </label>
      </div>
    </details>
  )
}
