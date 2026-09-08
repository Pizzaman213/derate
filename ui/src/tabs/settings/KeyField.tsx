import { keyFieldWarning, type KeyMode } from './keyfield'

export interface KeyFieldProps {
  /** Unique per instance. Two of these are on screen at once -- the add form
   *  and whichever row is having its key set -- and a shared radio `name`
   *  would make them one group, so choosing "Paste a key" in the row would
   *  silently switch the form above it. */
  idPrefix: string
  mode: KeyMode
  onMode: (mode: KeyMode) => void
  apiKey: string
  onApiKey: (value: string) => void
  apiKeyRef: string
  onApiKeyRef: (value: string) => void
  /** Names already in secrets.json, offered rather than typed blind. */
  knownRefs: readonly string[]
  /** The secrets.json name a pasted key will be stored under. Predicted the
   *  way the coordinator mints it, so the field names the reference it is
   *  about to create instead of describing one in the abstract. */
  destination: string
  /** The reference this provider carries now, when a pasted key would move it
   *  off that name. Null while adding, and null when the two are the same. */
  displaced?: string | null
  label?: string
  /** What a key for this kind looks like. From `keyPlaceholder`, so the hint
   *  and the screen that reads what is pasted under it come from one place. */
  placeholder?: string
}

/** The two ways to give a provider a credential, as one field.
 *
 *  Shared by the add-provider form and the per-row "Set key" editor, because
 *  they are the same decision made at two moments and the mistake they catch
 *  is the same one: a key typed into the field that takes a name, or a name
 *  typed into the field that takes a key. The rules live in keyfield.ts, which
 *  is a port of the server's own screen and verified against it.
 *
 *  What this component will never have: a control that shows the current key.
 *  Nothing sends one back, there is no endpoint for one to call, and adding
 *  either would be the bug. */
export function KeyField(props: KeyFieldProps) {
  const {
    idPrefix,
    mode,
    onMode,
    apiKey,
    onApiKey,
    apiKeyRef,
    onApiKeyRef,
    knownRefs,
    destination,
    displaced,
    label = 'API key',
    placeholder = 'sk-…',
  } = props

  const keyId = `${idPrefix}-key`
  const refId = `${idPrefix}-ref`
  const listId = `${idPrefix}-refs`
  const warning = keyFieldWarning(mode, mode === 'key' ? apiKey : apiKeyRef)

  return (
    <>
      <label htmlFor={mode === 'key' ? keyId : refId}>{label}</label>
      <div style={{ display: 'flex', gap: 12, marginBottom: 2 }}>
        {(
          [
            ['key', 'Paste a key'],
            ['ref', 'Name a reference'],
          ] as const
        ).map(([option, text]) => (
          <label
            key={option}
            style={{ display: 'flex', alignItems: 'center', gap: 4, cursor: 'pointer' }}
          >
            <input
              type="radio"
              name={`${idPrefix}-mode`}
              style={{ padding: 0, margin: 0 }}
              checked={mode === option}
              onChange={() => onMode(option)}
            />
            {text}
          </label>
        ))}
      </div>
      {mode === 'key' ? (
        <input
          id={keyId}
          type="password"
          autoComplete="off"
          value={apiKey}
          onChange={(e) => onApiKey(e.target.value)}
          placeholder={placeholder}
          spellCheck={false}
        />
      ) : (
        <>
          {/* Not type="password": a reference is a name, and masking it is
              what invited a key into the field in the first place. */}
          <input
            id={refId}
            type="text"
            autoComplete="off"
            list={listId}
            value={apiKeyRef}
            onChange={(e) => onApiKeyRef(e.target.value)}
            placeholder="OPENROUTER_API_KEY"
            spellCheck={false}
          />
          <datalist id={listId}>
            {knownRefs.map((r) => (
              <option key={r} value={r} />
            ))}
          </datalist>
        </>
      )}
      {warning ? (
        <div className="unit" style={{ marginTop: 4, color: 'var(--warn)' }}>
          {warning}
        </div>
      ) : null}
      {mode === 'key' && apiKey.trim() && !warning ? (
        <div className="unit" style={{ marginTop: 4 }}>
          Stored as <span className="mono">{destination}</span> in secrets.json.
          {displaced ? (
            <>
              {' '}
              This provider moves onto that reference, and{' '}
              <span className="mono">{displaced}</span> stops being what authenticates it.
            </>
          ) : null}
        </div>
      ) : null}
    </>
  )
}
