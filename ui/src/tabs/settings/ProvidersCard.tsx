import { Fragment, useState } from 'react'
import type { Provider, ProviderKind } from '../../api/types'
import { useProviderKinds, useProviderSecretRefs, useProviders } from '../../state/resources'
import { useBackend } from '../../state/backend'
import { relativeTime } from '../../format'
import {
  keyPlaceholder,
  keyStateNote,
  keyStateTone,
  mintedRef,
  predictedProviderId,
  type KeyMode,
} from './keyfield'
import { KeyField } from './KeyField'
import { ProviderBackupPanel } from './ProviderBackupPanel'

// Ported from mockups-next/js/settings.js `settings()`'s `provTable` block.
// One row per provider, same as the mockup -- a provider's own accounting
// (requests_today, spend_today_usd) is provider-wide, not per-model, so
// there is no honest single $/Mtok for a provider serving many models at
// different prices; that column is dashed unless there is exactly one model
// to be unambiguous about.
// The kind list is the server's, fetched from /api/providers/kinds rather than
// restated here. A local copy went stale in both directions: it offered
// Anthropic, which this build cannot talk to and rejects at POST time, and it
// could not say that Ollama needs no key or that its default base_url resolves
// on the coordinator -- so "leave blank for the default" pointed at the wrong
// machine and failed as a bare connection timeout.

/** True for an address that resolves on whatever machine dials it.
 *
 *  Not a security check -- it decides whether to warn that "localhost" means
 *  the coordinator rather than the box the operator is picturing. */
function isLocalUrl(url: string): boolean {
  return /^https?:\/\/(localhost|127\.0\.0\.1|\[::1\]|0\.0\.0\.0)(:|\/|$)/i.test(url.trim())
}

export function ProvidersCard() {
  const providers = useProviders()
  const { backend, invalidate } = useBackend()
  const kinds = useProviderKinds()
  const secretRefs = useProviderSecretRefs()
  const [kind, setKind] = useState<ProviderKind>('openrouter')
  const [baseUrl, setBaseUrl] = useState('')
  // Two ways to give a credential, and the field for one is not the field for
  // the other. Paste is the default because it needs nothing set up first: the
  // coordinator writes the key to secrets.json and keeps only the reference.
  // Naming a reference stays for operators who already manage keys in the
  // environment -- which used to be the only path, and had no way to say so.
  const [keyMode, setKeyMode] = useState<KeyMode>('key')
  const [apiKey, setApiKey] = useState('')
  const [apiKeyRef, setApiKeyRef] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  // Which row is having its key set, and that editor's own fields. One row at
  // a time: two open editors are two password boxes on one screen with nothing
  // but a row position to tell them apart. The fields are cleared on open, so
  // a key typed for one provider cannot be saved onto the next.
  // Which row has its model list open. Separate from `keyRow`: they are two
  // different questions about the same provider and both can be open at once
  // without either meaning anything different.
  // Which row has its backup editor open. A third independent question about
  // the same provider -- what it serves, what credential it uses, and which of
  // our names it answers to -- so a third piece of row state rather than one
  // mode enum three things fight over.
  const [backupRow, setBackupRow] = useState<string | null>(null)
  const [keyRow, setKeyRow] = useState<string | null>(null)
  const [rowMode, setRowMode] = useState<KeyMode>('key')
  const [rowKey, setRowKey] = useState('')
  const [rowRef, setRowRef] = useState('')
  const [rowError, setRowError] = useState<string | null>(null)

  const list = providers.data ?? []
  const specs = kinds.data ?? []
  const spec = specs.find((k) => k.kind === kind)
  const offered = specs.filter((k) => !k.unsupported_reason)
  const knownRefs = secretRefs.data ?? []
  const needsKey = !spec || spec.requires_key
  // Where a pasted key will land. The id is predicted the way the coordinator
  // mints it, so the card can name the reference rather than describe it.
  const destination = mintedRef(predictedProviderId(kind, list.map((p) => p.provider_id)))

  // Changing the kind carries its default into the field rather than leaving a
  // blank the server fills in silently. The operator can see the address that
  // is about to be dialled, and edit it -- which for a LAN box like Ollama on
  // another machine is the whole job.
  const chooseKind = (next: ProviderKind) => {
    setKind(next)
    const target = specs.find((k) => k.kind === next)
    setBaseUrl(target?.base_url ?? '')
    if (target && !target.requires_key) {
      setApiKey('')
      setApiKeyRef('')
    }
  }

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

  // Opening always clears. Carrying a half-typed key from one row into the
  // next is the one way this control could send a credential to a provider
  // nobody chose to send it to.
  const openKeyRow = (id: string) => {
    setKeyRow(id)
    setRowMode('key')
    setRowKey('')
    setRowRef('')
    setRowError(null)
  }

  const closeKeyRow = () => {
    setKeyRow(null)
    setRowKey('')
    setRowRef('')
    setRowError(null)
  }

  // Setting a key on a provider that already exists. The same two modes as the
  // add form and the same wire fields -- PATCH takes `api_key` and stores it
  // under a reference exactly as POST does -- so the only thing this could not
  // do before was be reached, which meant a wrong or expired key was fixed by
  // removing the provider and adding it back.
  const setKey = async (p: Provider) => {
    setBusy(`key:${p.provider_id}`)
    setRowError(null)
    try {
      await backend.patchProvider(
        p.provider_id,
        rowMode === 'key'
          ? { api_key: rowKey.trim() }
          : { api_key_ref: rowRef.trim() },
      )
      closeKeyRow()
      invalidate()
    } catch (e) {
      // Verbatim, and it is the useful half of this control: the coordinator
      // refuses a key whose reference the environment already answers to, and
      // names the variable.
      setRowError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  const add = async () => {
    setBusy('__add__')
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
        A key you paste is written to secrets.json at 0600 and only its reference is kept on the
        provider. Keys are resolved at request time and are never displayed, logged, or included
        in exports. Each row says whether its reference resolves and which of the environment and
        secrets.json answered — the whole of what can be shown about a key — and Set key gives one
        to a provider that already exists, or replaces the one it has.
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

              const editing = keyRow === p.provider_id
              const showingBackup = backupRow === p.provider_id
              const served = p.model_count ?? p.models.length
              const rotatesTo = mintedRef(p.provider_id)

              return (
                <Fragment key={p.provider_id}>
                  <tr>
                    <td className="mono">{p.display_name}</td>
                    {/* The reference, and whether it resolves. Both are names --
                        a variable and a place -- and between them they are the
                        whole of what may be said about a key here. */}
                    <td className="unit">
                      <div className="mono">{p.api_key_ref || '—'}</div>
                      <div
                        style={{
                          color:
                            keyStateTone(p.key_state) === 'ok'
                              ? 'var(--live)'
                              : keyStateTone(p.key_state) === 'warn'
                                ? 'var(--warn)'
                                : 'var(--ink-muted)',
                        }}
                      >
                        {keyStateNote(p.key_state, p.key_source)}
                      </div>
                    </td>
                    {/* Served, out of published -- a figure, and no longer a
                        way in. This cell used to open a flat list of every
                        model the provider publishes, each with a checkbox. At
                        OpenRouter's several hundred that list was a bulk edit
                        with no model in front of it: no context window, no
                        price, no verdict, nothing but a name and a tick. The
                        decision it was asking for is a decision about ONE
                        model, so it is made on that model's own page, next to
                        the facts that answer it, by pressing Serve.

                        The count stays because it is the fact this row is for:
                        the gap between served and published is the allowlist,
                        and seeing it is what sends somebody to the Models tab.

                        The reverse is deliberately not offered here either.
                        Switching a model off is the same one-model decision as
                        switching it on, and a bulk control that could only
                        take things away would be the old list with half its
                        verbs. */}
                    <td className="num">
                      {served}
                      {p.catalogue_count != null ? ` of ${p.catalogue_count}` : ''}
                    </td>
                    <td className="num">{price}</td>
                    <td className="num">{today}</td>
                    <td className="unit">{state}</td>
                    <td className="unit">{p.last_refreshed ? relativeTime(p.last_refreshed) : '—'}</td>
                    <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                      <button
                        style={{ padding: '3px 9px', marginRight: 6 }}
                        aria-expanded={showingBackup}
                        aria-controls={`pbackuprow-${p.provider_id}`}
                        onClick={() => setBackupRow(showingBackup ? null : p.provider_id)}
                      >
                        Backs up
                      </button>
                      <button
                        style={{ padding: '3px 9px', marginRight: 6 }}
                        aria-expanded={editing}
                        aria-controls={`pkeyrow-${p.provider_id}`}
                        onClick={() => (editing ? closeKeyRow() : openKeyRow(p.provider_id))}
                      >
                        {editing ? 'Cancel' : p.key_state === 'set' ? 'Replace key' : 'Set key'}
                      </button>
                      <button
                        style={{ padding: '3px 9px' }}
                        onClick={() => void remove(p.provider_id)}
                        disabled={busy === p.provider_id}
                      >
                        {busy === p.provider_id ? 'Removing…' : 'Remove'}
                      </button>
                    </td>
                  </tr>
                  {editing ? (
                    <tr id={`pkeyrow-${p.provider_id}`}>
                      <td colSpan={8} style={{ background: 'var(--panel-recessed)' }}>
                        <div
                          style={{
                            display: 'flex',
                            gap: 8,
                            alignItems: 'flex-end',
                            flexWrap: 'wrap',
                          }}
                        >
                          <div className="fld" style={{ flex: 1, minWidth: 260 }}>
                            <KeyField
                              idPrefix={`pk-${p.provider_id}`}
                              label={`API key for ${p.display_name}`}
                              mode={rowMode}
                              onMode={setRowMode}
                              apiKey={rowKey}
                              onApiKey={setRowKey}
                              apiKeyRef={rowRef}
                              onApiKeyRef={setRowRef}
                              knownRefs={knownRefs}
                              // Where a pasted key lands for a provider that
                              // already exists: always the minted name, because
                              // that is what the coordinator does with it --
                              // keeping its own reference and never overwriting
                              // one the operator brought.
                              destination={rotatesTo}
                              displaced={
                                p.api_key_ref && p.api_key_ref !== rotatesTo ? p.api_key_ref : null
                              }
                            />
                          </div>
                          <button
                            onClick={() => void setKey(p)}
                            disabled={
                              busy === `key:${p.provider_id}` ||
                              !(rowMode === 'key' ? rowKey.trim() : rowRef.trim())
                            }
                          >
                            {busy === `key:${p.provider_id}` ? 'Saving…' : 'Save key'}
                          </button>
                        </div>
                        {rowError ? (
                          <div
                            className="label"
                            style={{
                              color: 'var(--fault)',
                              marginTop: 8,
                              whiteSpace: 'pre-wrap',
                            }}
                          >
                            {rowError}
                          </div>
                        ) : null}
                      </td>
                    </tr>
                  ) : null}
                  {showingBackup ? (
                    <tr id={`pbackuprow-${p.provider_id}`}>
                      <td colSpan={8} style={{ background: 'var(--panel-recessed)' }}>
                        <ProviderBackupPanel providerId={p.provider_id} />
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
              )
            })}
          </tbody>
        </table>
      </div>
      <div
        className="unit"
        style={{ marginTop: 12, paddingTop: 10, borderTop: '1px solid var(--rule)' }}
      >
        {spec && !spec.requires_key
          ? `${spec.display_name} takes no key — it is a server you run, reached over the network. Only the address matters.`
          : keyMode === 'key'
            ? `Paste the key and it is stored on the coordinator, in secrets.json at 0600, under a reference. Only the reference is written to the provider record. Nothing sends the key back — there is no reveal control and adding one would be the bug.`
            : `A reference is the name of an environment variable or of a secrets.json key, never the key itself. It is resolved at request time and the value never leaves the coordinator.`}
      </div>

      <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end', marginTop: 12, flexWrap: 'wrap' }}>
        <div className="fld">
          <label htmlFor="pkind">Provider</label>
          <select
            id="pkind"
            value={kind}
            onChange={(e) => chooseKind(e.target.value as ProviderKind)}
          >
            {offered.map((k) => (
              <option key={k.kind} value={k.kind}>
                {k.display_name}
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
            placeholder={spec?.base_url || 'https://…/v1'}
            spellCheck={false}
          />
          {spec && isLocalUrl(baseUrl || spec.base_url) ? (
            <div className="unit" style={{ marginTop: 4 }}>
              This address is dialled from the coordinator, so localhost means the
              coordinator itself. For a server on another machine, use its address.
            </div>
          ) : null}
        </div>
        {/* One field with two modes rather than one field with a warning. The
            single masked input labelled "Key reference" was the whole bug: it
            looked exactly like somewhere to paste a key, took the name of an
            environment variable, and said so only in prose beneath the table.
            Naming the mode makes the two inputs different things on screen,
            which is what they always were on the wire. */}
        <div
          className="fld"
          style={{ flex: 1, minWidth: 220, display: needsKey ? undefined : 'none' }}
        >
          <KeyField
            idPrefix="padd"
            // The kind's own name and key shape, not "API key" and "sk-…".
            // The field is the freeze point this card exists to remove, and
            // an operator arriving with an OpenRouter key in the clipboard
            // should be able to see that it is the field for it without
            // reading the prose underneath. `spec` is the server's kind
            // record, so the label follows the dropdown for free.
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
