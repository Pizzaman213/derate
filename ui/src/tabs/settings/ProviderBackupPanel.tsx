// Which local model a provider stands behind.
//
// The mechanism was complete on the server and unreachable from the screen: a
// provider model whose served name matches a deployment's is merged into that
// name's routing entry (gateway/targets.py), and a name served both locally
// and remotely gets local_first chosen for it automatically, which hands the
// provider traffic only once every local replica stops admitting. All of that
// hinges on the two names being equal, and the only way to make them equal was
// PATCH /api/providers/<id> {"aliases": ...} by hand.
//
// So this panel is one question: which name should this provider's model
// answer to here. Pointing it at a deployment's name makes it that
// deployment's backup; pointing it at anything else is a rename for clients,
// and the row says which of the two it just did rather than leaving the
// operator to infer it from whether traffic ever arrives.

import { useMemo, useState } from 'react'

import { useBackend, useKeyedResource } from '../../state/backend'
import { useTopology } from '../../state/resources'

/** Slow, like the models panel: the catalogue changes when the upstream
 *  publishes something, and `invalidate()` covers every edit made here. */
const REFRESH_MS = 300000

interface Alias {
  upstream: string
  served: string
}

export function ProviderBackupPanel({ providerId }: { providerId: string }) {
  const catalogue = useKeyedResource(
    providerId,
    (b) => b.providerModels(providerId),
    REFRESH_MS,
  )
  const topology = useTopology()
  const { backend, invalidate } = useBackend()
  const [upstream, setUpstream] = useState('')
  const [served, setServed] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const models = catalogue.data

  // The whole alias map, read off the CATALOGUE rather than off
  // /api/providers. That endpoint carries only the models the allowlist
  // admits, and PATCH replaces the map wholesale -- so composing the next map
  // from a filtered list would silently drop the alias of every model somebody
  // had switched off, and switching one back on would restore it under the
  // wrong name.
  const aliases: Alias[] = useMemo(
    () =>
      (models ?? [])
        .filter((m) => m.served_name !== m.upstream_id)
        .map((m) => ({ upstream: m.upstream_id, served: m.served_name }))
        .sort((a, b) => a.served.localeCompare(b.served)),
    [models],
  )

  // What this cluster runs. Aliasing onto one of these is the backup case, and
  // it is the only list worth offering: any other name is a rename, which the
  // field still accepts by typing.
  const localNames = useMemo(
    () =>
      [...new Set((topology.data?.deployments ?? []).map((d) => d.served_name))].sort(),
    [topology.data],
  )

  const write = async (next: Alias[], what: string) => {
    setBusy(what)
    setError(null)
    try {
      // The complete map, because that is what the coordinator stores: a patch
      // carrying one pair would drop every other alias this provider has.
      await backend.patchProvider(providerId, {
        aliases: Object.fromEntries(next.map((a) => [a.upstream, a.served])),
      })
      invalidate()
    } catch (e) {
      // Verbatim. The server screens these strings and names what it refused.
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  const add = async () => {
    const u = upstream.trim()
    const s = served.trim()
    if (!u || !s) return
    await write([...aliases.filter((a) => a.upstream !== u), { upstream: u, served: s }], '__add__')
    setUpstream('')
    setServed('')
  }

  const drop = (a: Alias) =>
    write(aliases.filter((x) => x.upstream !== a.upstream), a.upstream)

  return (
    <div>
      <div className="unit" style={{ marginBottom: 10 }}>
        A model answering to a name this cluster already serves becomes that name's backup: one
        entry on <span className="mono">/v1/models</span>, two targets behind it. Routing picks{' '}
        <span className="mono">local_first</span> for a name served both ways on its own, and sends
        the provider traffic only once every local replica stops admitting — then returns to the
        cluster the moment one frees. A name nothing here serves is a rename, not a backup.
      </div>

      {catalogue.loading ? (
        <div className="unit">Reading the catalogue…</div>
      ) : catalogue.error ? (
        <div className="label" style={{ color: 'var(--fault)', whiteSpace: 'pre-wrap' }}>
          {catalogue.error.message}
        </div>
      ) : null}

      {aliases.length > 0 ? (
        <div style={{ overflowX: 'auto', marginBottom: 10 }}>
          <table>
            <thead>
              <tr>
                <th>Upstream model</th>
                <th>Answers to</th>
                <th>What that does</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {aliases.map((a) => (
                <tr key={a.upstream}>
                  <td className="mono">{a.upstream}</td>
                  <td className="mono">{a.served}</td>
                  <td className="unit">
                    {localNames.includes(a.served)
                      ? `backs up ${a.served}`
                      : 'renamed for clients — nothing here serves that name'}
                  </td>
                  <td style={{ textAlign: 'right' }}>
                    <button
                      style={{ padding: '3px 9px' }}
                      onClick={() => void drop(a)}
                      disabled={busy === a.upstream}
                    >
                      {busy === a.upstream ? 'Clearing…' : 'Clear'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="unit" style={{ marginBottom: 10 }}>
          Nothing here answers to another name yet.
        </div>
      )}

      <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end', flexWrap: 'wrap' }}>
        <div className="fld" style={{ flex: 1, minWidth: 220 }}>
          <label htmlFor={`bk-up-${providerId}`}>Upstream model</label>
          <input
            id={`bk-up-${providerId}`}
            list={`bk-up-list-${providerId}`}
            value={upstream}
            onChange={(e) => setUpstream(e.target.value)}
            placeholder="meta-llama/llama-3.1-8b-instruct"
            spellCheck={false}
          />
          {/* A datalist rather than a select: several hundred options is a
              list you type into, not one you scroll. */}
          <datalist id={`bk-up-list-${providerId}`}>
            {(models ?? []).map((m) => (
              <option key={m.upstream_id} value={m.upstream_id} />
            ))}
          </datalist>
        </div>
        <div className="fld" style={{ flex: 1, minWidth: 200 }}>
          <label htmlFor={`bk-served-${providerId}`}>Answers to</label>
          <input
            id={`bk-served-${providerId}`}
            list={`bk-served-list-${providerId}`}
            value={served}
            onChange={(e) => setServed(e.target.value)}
            placeholder={localNames[0] ?? 'the name clients ask for'}
            spellCheck={false}
          />
          <datalist id={`bk-served-list-${providerId}`}>
            {localNames.map((n) => (
              <option key={n} value={n} />
            ))}
          </datalist>
          <div className="unit" style={{ marginTop: 4 }}>
            {served.trim() && localNames.includes(served.trim())
              ? `Backs up ${served.trim()}, which this cluster is serving.`
              : localNames.length
                ? `Suggestions are what this cluster serves: ${localNames.join(', ')}.`
                : 'Nothing is deployed here yet, so any name typed is a rename.'}
          </div>
        </div>
        <button onClick={() => void add()} disabled={busy === '__add__' || !upstream.trim() || !served.trim()}>
          {busy === '__add__' ? 'Saving…' : 'Point it there'}
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
