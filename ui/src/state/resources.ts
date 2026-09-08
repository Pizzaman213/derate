import type { ProviderCatalogueModel } from '../api/types'
import { useKeyedResource, useResource } from './backend'

// Poll intervals. Structure changes slowly; discovery is the one thing a person
// is actively waiting on, so it is checked most often. Live numbers do not come
// from here at all — they come from the stream.

export const useCluster = () => useResource((b) => b.cluster(), 5000)
export const useTopology = () => useResource((b) => b.topology(), 5000)
export const useCandidates = () => useResource((b) => b.candidates(), 3000)
export const useRouting = () => useResource((b) => b.routing(), 5000)
export const useProviders = () => useResource((b) => b.providers(), 15000)
// A static table compiled into the server, not cluster state. Polled at the
// laziest interval the hook offers rather than fetched once, because that is
// the only shape useResource has -- the server marks it cacheable and the
// browser will not re-ask on most of these ticks.
export const useProviderKinds = () => useResource((b) => b.providerKinds(), 300000)

/** How one served name's recent requests were CALLED: streamed, or waited for
 *  whole. Only the cluster graph reads it, and only to colour the return leg.
 *
 *  Deliberately not live, because it cannot be: the 1 Hz metrics frame carries
 *  no streaming flag and `/api/routing` does not split `outstanding` by one,
 *  so the request archive is the only place that records it at all. See
 *  `streamTone` in tabs/cluster/particles.ts for what that costs and why the
 *  animation keeps the two clocks apart.
 *
 *  30s: the mix moves over minutes and the query walks the archive. A 15m
 *  window comes back raw, so the rows carry `streaming`; a coordinator that
 *  rolled them into buckets has no such column, the ratio is null, and the
 *  blocks paint the no-reading grey rather than a guess. Telemetry switched
 *  off is an error, which lands in the same place. */
export const useRequestMix = (servedName: string | null) =>
  useKeyedResource(
    servedName ?? '',
    (b) =>
      servedName
        ? b.historyRequests({ servedName, from: '-15m', limit: 400 })
        : Promise.resolve(null),
    30000,
  )

/** Every provider's WHOLE catalogue, switched-on flag included.
 *
 *  `useProviders` is not this. `GET /api/providers` carries only the models a
 *  provider actually serves -- the allowlist has already filtered it server
 *  side -- which is right for every screen that asks what this cluster serves,
 *  and useless for the one screen that has to offer what it could serve
 *  instead. `GET /api/providers/{id}/models` is the single unfiltered provider
 *  surface, and it is unfiltered precisely because it is the one the allowlist
 *  is chosen from.
 *
 *  Lazy, at the same interval the Settings panel uses: a catalogue changes
 *  when an upstream publishes something or somebody pulls, not on a timer.
 *  `invalidate()` covers the case that matters within a session -- switching a
 *  model on has to be visible immediately, and a `revision` bump refires this.
 *
 *  One provider that cannot answer degrades to no rows from that provider,
 *  named in `failed`, rather than taking the other providers' catalogues down
 *  with it. Same shape as the gateway's own `_safe` around a routing source.
 */
export const useProviderCatalogues = (providerIds: string[]) => {
  const ids = [...providerIds].sort()
  return useKeyedResource(
    ids.join(','),
    async (b) => {
      const settled = await Promise.all(
        ids.map((id) =>
          b
            .providerModels(id)
            .then((rows) => ({ id, rows, error: null as string | null }))
            .catch((e: unknown) => ({
              id,
              rows: [] as ProviderCatalogueModel[],
              error: e instanceof Error ? e.message : String(e),
            })),
        ),
      )
      const byProvider: Record<string, ProviderCatalogueModel[]> = {}
      const failed: { provider_id: string; message: string }[] = []
      for (const entry of settled) {
        byProvider[entry.id] = entry.rows
        if (entry.error !== null) failed.push({ provider_id: entry.id, message: entry.error })
      }
      return { byProvider, failed }
    },
    300000,
  )
}
// What is already in secrets.json, for the add-provider reference field to
// offer. Names only. Polled lazily and re-fetched on invalidate(), which is
// what makes a reference minted by the previous add show up in the next one.
export const useProviderSecretRefs = () => useResource((b) => b.providerSecretRefs(), 300000)
export const useSettings = () => useResource((b) => b.getSettings(), 5000)
// Only ever a row or two, and only while somebody is mid-install. Polled
// rather than held in the card's own state so a second browser -- or a
// refresh -- still sees the token that is currently live.
export const useEnrollments = () => useResource((b) => b.enrollments(), 10000)
// The Chat tab's model list. `/v1/models` changes only when a deployment
// crosses into or out of READY, or a provider refreshes its catalogue, so it
// is polled at the lazy end -- but it is polled, because a model finishing its
// launch while you are looking at the list is the case worth catching.
export const useModels = () => useResource((b) => b.models(), 10000)

// What the Serve button gates on, so it is polled fast: a two-second-old
// allocatable figure is a different decision from a five-second-old one.
export const useMemoryReport = () => useResource((b) => b.memory(), 2000)

// A download's bar has to move, so this matches the memory poll rather than
// the five-second cadence around it. Affordable at that rate for a reason the
// storage hook below spells out in reverse: this endpoint reads a
// process-local dict and the in-memory deployment list, with no fan-out to the
// node agents and no syscalls. `useResource` does not deduplicate, so this is
// mounted in exactly one place -- the sidebar's Activity section.
export const useActivity = () => useResource((b) => b.activity(), 2000)
// What is resident on one node's GPU. Only mounted while a node sheet is open,
// and every poll costs an nvidia-smi call on that node, so it matches the
// telemetry cadence rather than the 2s memory poll. `nodeId` empty means no
// node is open; the hook still has to be called unconditionally, so it fetches
// an empty list instead of being skipped.
export const useNodeProcesses = (nodeId: string) =>
  useKeyedResource(
    // Keyed, not plain: opening a second node while the sheet stays open used
    // to leave the FIRST node's process list on screen under the second node's
    // heading until the next 5s tick, and nothing about it looked stale.
    nodeId,
    (b) =>
      nodeId
        ? b.nodeProcesses(nodeId)
        : Promise.resolve({ node_id: '', processes: [], available: true, reason: null }),
    5000,
  )

// Disk, cluster-wide. Slow on purpose, for two reasons: one call fans out to
// every node agent and walks a directory on each, and the number it returns
// moves over hours, not seconds. There is no stream to fall back on -- disk is
// not sampled anywhere -- so this hook is the only source and it still does not
// need to be fast.
// `enabled` exists because the dashboard's model picker wants the downloaded
// list, and one call here fans out to every node and walks a directory on
// each. Mounting it unconditionally would make every dashboard visitor trigger
// a cluster-wide disk walk every 30s to populate a menu they may never open.
// Same shape as `useNodeProcesses` below: resolve an empty payload rather than
// skip the hook, since hooks cannot be called conditionally.
export const useStorage = (enabled = true) =>
  useResource((b) => (enabled ? b.storage() : Promise.resolve(null)), 30000)

// The capacity walk resolves models against the hub and is memoised server
// side; polling it hard would buy nothing and cost the hub.
//
// Keyed on the numbers it was asked for. `useResource` holds its read function
// in a ref and keys only on the coordinator, so a caller that changes context
// or concurrency would otherwise keep the previous answer on screen for up to
// a full interval -- with the new numbers in the caption above it. Keyed, the
// stale answer is dropped the moment the question changes, exactly as the
// keyed form already does for a node id. Callers that pass constants are
// unaffected.
export const useCapacity = (
  context: number | null,
  concurrency: number | null,
  nodeIds?: string[] | null,
) =>
  useKeyedResource(
    // The whole question, so an answer to a different one is never left on
    // screen under this caption. Null reads as "" and is a key in its own
    // right: "the coordinator picks" is not the same request as 8192.
    `${context ?? ''}/${concurrency ?? ''}/${(nodeIds ?? []).join(',')}`,
    (b) => b.capacity(context, concurrency, nodeIds),
    20000,
  )

// Static for the life of the process: it is the contract's own table, and it
// answers with no ports wired. Fetched once, effectively.
export const useQuantTable = () => useResource((b) => b.quantTable(), 3_600_000)
// The curated shortlist changes only when someone edits fit/catalog.py.
export const useCatalog = () => useResource((b) => b.catalog(), 3_600_000)

/** One list, one request.
 *
 *  Replaces the catalog, cluster, storage and provider-catalogue reads this
 *  screen used to fold together itself. 10s, between `useCluster`'s 5s and
 *  `useProviders`' 15s: the fastest-moving thing in it is a deployment
 *  crossing into READY, and it is strictly cheaper than what it replaces --
 *  the server holds the merge in SQLite and refreshes each source on its own
 *  cadence, so a poll here is one SELECT rather than four fetches and a
 *  cluster-wide disk walk. */
export const useModelRegistry = () => useResource((b) => b.modelRegistry(), 10000)

// Model detail and the variant ladder are deliberately NOT here. `useResource`
// refires on every `revision` bump, so an open model would re-run its hub calls
// after every launch, admit and settings change. They are user-driven and live
// in the tab's own state, debounced.
