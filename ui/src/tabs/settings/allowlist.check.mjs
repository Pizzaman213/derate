// requires: coordinator -- an agreement between two endpoints and a routing table
// Checks the provider allowlist against the live coordinator on :8088.
//
// The sibling of `tabs/models/rows.check.mjs`, and for the same reason: there
// is no test runner in `ui/`, typecheck is the only other gate, and the thing
// worth checking here is not a type. It is an agreement between two endpoints
// and a routing table -- that the only provider models reaching a screen are
// the ones somebody switched on, and that they are the same ones reaching
// `/v1/models`.
//
// That is exactly the class of bug `tsc` cannot see. `Provider.models` and
// `ProviderCatalogueModel[]` are structurally near-identical, so serving the
// unfiltered list from the filtered endpoint typechecks perfectly and puts
// three hundred models back on screen.
//
//   cd ui && node src/tabs/settings/allowlist.check.mjs
//   DERATE_CHECK_ORIGIN=http://localhost:18088 node src/tabs/settings/allowlist.check.mjs
//
// Needs a coordinator: :8088 by default, or whatever DERATE_CHECK_ORIGIN names.
// Every assertion is about a fact the gateway reports.

const ORIGIN = process.env.DERATE_CHECK_ORIGIN ?? 'http://localhost:8088'
const get = async (p) => (await fetch(`${ORIGIN}${p}`)).json()

let fail = 0
const check = (ok, msg) => {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${msg}`)
  if (!ok) fail++
}

const providers = await get('/api/providers')
const served = new Set((await get('/v1/models')).data.map((m) => m.id))
// Remote routing targets, one per (provider, model) actually being routed to.
// `/v1/models` cannot answer this on its own: two providers may publish the
// same model, and one of them serving it puts the name in that list whether or
// not the other does. Attribution needs the target id, which is what this has.
const remotes = (await get('/api/topology')).remotes ?? []
const routedBy = new Map()
for (const r of remotes) {
  if (!routedBy.has(r.provider_id)) routedBy.set(r.provider_id, new Set())
  routedBy.get(r.provider_id).add(r.upstream_id)
}

// An empty coordinator is a RESULT, not a reason to skip. This used to
// `process.exit(0)` here, which gave "there was nothing to check" and "every
// provider checks out" the same exit code -- and a gate cannot report a
// failure it has made indistinguishable from success.
//
// Whether this file runs at all is the runner's decision now, made once
// against a probe, off the `// requires: coordinator` line at the top.
check(Array.isArray(providers), 'GET /api/providers answers a list')
if (Array.isArray(providers) && providers.length === 0) {
  check(
    remotes.length === 0,
    'no providers are configured, so nothing is routed to a remote either',
  )
}

for (const p of providers) {
  const id = p.provider_id
  const catalogue = await get(`/api/providers/${encodeURIComponent(id)}/models`)

  check(
    Array.isArray(catalogue),
    `${id}: /api/providers/{id}/models answers a list`,
  )
  check(
    catalogue.every((m) => typeof m.enabled === 'boolean'),
    `${id}: every catalogue row says whether it is enabled`,
  )

  const enabled = catalogue.filter((m) => m.enabled)
  const listed = p.models ?? []

  // The load-bearing one. Everything the UI renders for this provider comes
  // from `/api/providers`; if an un-enabled model is in there, it is on screen.
  const listedIds = new Set(listed.map((m) => m.upstream_id))
  const enabledIds = new Set(enabled.map((m) => m.upstream_id))
  check(
    listed.every((m) => enabledIds.has(m.upstream_id)),
    `${id}: /api/providers lists only enabled models (${listed.length} listed, ${enabled.length} enabled)`,
  )
  check(
    enabled.every((m) => listedIds.has(m.upstream_id)),
    `${id}: every enabled model is listed -- the two endpoints agree`,
  )

  // The counts the screen reads. `model_count` drives "N of M" and must be the
  // served figure, not the published one, or the allowlist reads as inert.
  check(
    p.model_count === listed.length,
    `${id}: model_count (${p.model_count}) is what is served (${listed.length})`,
  )
  check(
    p.catalogue_count === catalogue.length,
    `${id}: catalogue_count (${p.catalogue_count}) is what is published (${catalogue.length})`,
  )

  // And the other wire path: the router's own target index, which is built
  // from a different call than the listing above and is what actually decides
  // where a request goes. A model switched off must contribute no target.
  const routed = routedBy.get(id) ?? new Set()
  const leaked = [...routed].filter((u) => !enabledIds.has(u))
  check(
    leaked.length === 0,
    `${id}: routes to nothing disabled${leaked.length ? ` -- leaked ${leaked.slice(0, 3).join(', ')}` : ''}`,
  )
  check(
    enabled.every((m) => routed.has(m.upstream_id)),
    `${id}: every enabled model is a live routing target (${routed.size} routed)`,
  )
  // Every enabled model's name is answerable through the OpenAI surface.
  check(
    enabled.every((m) => served.has(m.served_name)),
    `${id}: every enabled model appears in /v1/models`,
  )

  // Whether anybody ever chose. Three states, and the Models tab's banner
  // prints a sentence off this one: telling somebody nothing was chosen when
  // they chose everything is false, and the counts alone cannot tell those
  // apart -- `model_count === catalogue_count` in both cases.
  check(
    p.models_chosen === true || p.models_chosen === false || p.models_chosen === null,
    `${id}: models_chosen reached the wire (${JSON.stringify(p.models_chosen)}) -- it has to be named in ui_detail._SPEND_KEYS or it never does`,
  )
  if (p.models_chosen === false) {
    // The grandfathered record. It has no allowlist at all, so it cannot have
    // a switched-off model -- and the banner says "serving all N", which is
    // only true while these two agree.
    check(
      enabled.length === catalogue.length && p.model_count === p.catalogue_count,
      `${id}: with nothing ever chosen it serves its whole catalogue (${enabled.length} of ${catalogue.length})`,
    )
  }
}

console.log(fail ? `\n${fail} check(s) failed` : '\nall passed')
process.exit(fail ? 1 : 0)
