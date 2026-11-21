import { useState } from 'react'
import type { Provider, ProviderKind, ProviderKindSpec } from '../api/types'
import { useBackend } from '../state/backend'
import { Select, type SelectOption } from './Select'
import {
  keyPlaceholder,
  mintedRef,
  predictedProviderId,
  type KeyMode,
} from '../tabs/settings/keyfield'
import { KeyField } from '../tabs/settings/KeyField'
import { ProviderSignIn } from '../tabs/settings/ProviderSignIn'

/** True for an address that resolves on whatever machine dials it.
 *
 *  Not a security check -- it decides whether to warn that "localhost" means
 *  the coordinator rather than the box the operator is picturing. */
function isLocalUrl(url: string): boolean {
  return /^https?:\/\/(localhost|127\.0\.0\.1|\[::1\]|0\.0\.0\.0)(:|\/|$)/i.test(url.trim())
}

export interface ProviderAddFormProps {
  /** The providers that exist, so a pasted key's reference can be named. */
  providers: Provider[]
  /** The server's kind list, from `/api/providers/kinds`. */
  kinds: ProviderKindSpec[]
  /** Secret references the coordinator already holds. */
  knownRefs: string[]
  /** Called after a provider is added or a sign-in completes. */
  onAdded?(): void
}

/** Adding a provider: a key-kind form, or the sign-in flow for a kind added
 *  by signing in. Shared by Settings' Providers card and the Models tab's
 *  Provider dialog, so the two cannot drift. The kind list is the server's;
 *  a local copy went stale in both directions. */
export function ProviderAddForm({ providers, kinds, knownRefs, onAdded }: ProviderAddFormProps) {
  const { backend, invalidate } = useBackend()
  const [kind, setKind] = useState<ProviderKind>('openrouter')
  const [baseUrl, setBaseUrl] = useState('')
  // Two ways to give a credential, and the field for one is not the field for
  // the other. Paste is the default because it needs nothing set up first: the
  // coordinator writes the key to secrets.json and keeps only the reference.
  const [keyMode, setKeyMode] = useState<KeyMode>('key')
  const [apiKey, setApiKey] = useState('')
  const [apiKeyRef, setApiKeyRef] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Bumped when a first sign-in is acknowledged, so the sign-in starts over
  // from its first step for the next one.
  const [signInRun, setSignInRun] = useState(0)

  const spec = kinds.find((k) => k.kind === kind)
  const offered = kinds.filter((k) => !k.unsupported_reason)
  const needsKey = !spec || spec.requires_key
  // A kind added by signing in: no address to edit, no key, no Add button.
  const signingIn = spec?.sign_in ?? null
  // Where Cancel on the sign-in lands: the first kind that takes a key.
  const firstKeyKind = offered.find((k) => !k.sign_in)?.kind ?? 'openrouter'
  // Where a pasted key will land. The id is predicted the way the coordinator
  // mints it, so the form can name the reference rather than describe it.
  const destination = mintedRef(predictedProviderId(kind, providers.map((p) => p.provider_id)))

  // Changing the kind carries its default into the field rather than leaving a
  // blank the server fills in silently.
  const chooseKind = (next: ProviderKind) => {
    setKind(next)
    const target = kinds.find((k) => k.kind === next)
    setBaseUrl(target?.base_url ?? '')
    if (target && (!target.requires_key || target.sign_in)) {
      setApiKey('')
      setApiKeyRef('')
    }
  }

  const add = async () => {
    setBusy(true)
    setError(null)
    try {
      // One or the other, never both: sending both would ask the coordinator
      // to store this key under a name the operator meant to reuse as-is.
      await backend.addProvider({
        kind,
        base_url: baseUrl.trim() || undefined,
        api_key: keyMode === 'key' ? apiKey.trim() || undefined : undefined,
        api_key_ref: keyMode === 'ref' ? apiKeyRef.trim() || undefined : undefined,
      })
      setBaseUrl('')
      setApiKey('')
      setApiKeyRef('')
      invalidate()
      onAdded?.()
    } catch (e) {
      // The server's own rejection, rendered verbatim, never paraphrased.
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <div
        className="unit"
        style={{ marginTop: 'var(--space-12)', paddingTop: 'var(--space-10)', borderTop: '1px solid var(--rule)' }}
      >
        {signingIn
          ? `Signing in stores ${signingIn === 'claude' ? 'Claude' : 'ChatGPT'}'s tokens on the coordinator, in oauth.json at 0600, where they are refreshed. They are never displayed, logged, or included in exports. Requests routed to this provider use your ${signingIn === 'claude' ? 'Claude subscription' : 'ChatGPT plan'} allowance, not a key.`
          : spec && !spec.requires_key
          ? `${spec.display_name} takes no key — it is a server you run, reached over the network. Only the address matters.`
          : keyMode === 'key'
            ? `Paste the key and it is stored on the coordinator, in secrets.json at 0600, under a reference. Only the reference is written to the provider record. Nothing sends the key back — there is no reveal control and adding one would be the bug.`
            : `A reference is the name of an environment variable or of a secrets.json key, never the key itself. It is resolved at request time and the value never leaves the coordinator.`}
      </div>

      <div style={{ display: 'flex', gap: 'var(--space-8)', alignItems: 'flex-end', marginTop: 'var(--space-12)', flexWrap: 'wrap' }}>
        <div className="fld">
          <label htmlFor="pkind">Provider</label>
          <Select
            id="pkind"
            value={kind}
            options={offered.map((k): SelectOption<ProviderKind> => ({ value: k.kind, label: k.display_name }))}
            onChange={chooseKind}
          />
        </div>
        <div className="fld" style={{ flex: 1, minWidth: 180, display: signingIn ? 'none' : undefined }}>
          <label htmlFor="pbase">Base URL</label>
          <input
            id="pbase"
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            placeholder={spec?.base_url || 'https://…/v1'}
            spellCheck={false}
          />
          {spec && isLocalUrl(baseUrl || spec.base_url) ? (
            <div className="unit" style={{ marginTop: 'var(--space-4)' }}>
              This address is dialled from the coordinator, so localhost means the
              coordinator itself. For a server on another machine, use its address.
            </div>
          ) : null}
        </div>
        {/* One field with two modes rather than one field with a warning:
            naming the mode makes the two inputs different things on screen,
            which is what they always were on the wire. */}
        <div
          className="fld"
          style={{ flex: 1, minWidth: 220, display: needsKey && !signingIn ? undefined : 'none' }}
        >
          <KeyField
            idPrefix="padd"
            // The kind's own name and key shape, not "API key" and "sk-…".
            label={spec ? `${spec.display_name} key` : 'API key'}
            placeholder={keyPlaceholder(kind)}
            mode={keyMode}
            onMode={setKeyMode}
            apiKey={apiKey}
            onApiKey={setApiKey}
            apiKeyRef={apiKeyRef}
            onApiKeyRef={setApiKeyRef}
            knownRefs={knownRefs}
            destination={destination}
          />
        </div>
        {signingIn ? null : (
          <button type="button" onClick={() => void add()} disabled={busy}>
            {busy ? 'Adding…' : 'Add provider'}
          </button>
        )}
      </div>

      {signingIn ? (
        <div style={{ marginTop: 'var(--space-12)' }}>
          <ProviderSignIn
            key={`${signingIn}:${signInRun}`}
            flow={signingIn}
            onDone={() => {
              invalidate()
              setSignInRun((n) => n + 1)
              onAdded?.()
            }}
            onCancel={() => chooseKind(firstKeyKind)}
          />
        </div>
      ) : null}

      {error ? (
        <div className="label" style={{ color: 'var(--fault)', marginTop: 'var(--space-8)', whiteSpace: 'pre-wrap' }}>
          {error}
        </div>
      ) : null}
    </>
  )
}
