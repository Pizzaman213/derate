// requires: coordinator -- the local half of /v1/models is what it folds
// Checks the chat picker's row set against the live coordinator on :8088.
//
// The sibling of `tabs/settings/allowlist.check.mjs`, and for the same reason.
// A green `tsc` proves nothing about this module: `buildRows` takes a
// `ServedModel[]` and returns rows, and `/v1/models` has exactly that shape
// whichever rows come back.
//
// The invariant here INVERTED. This file used to hold the picker to the local
// half of /v1/models, because an unfiltered provider catalogue was several
// hundred rows of somebody else's hardware. The allowlist ended that -- a
// provider serves nothing until somebody presses Serve -- and the filter's
// real cost showed up instead: a model switched on, listed by /v1/models,
// answering a curl, and unselectable in the console shipped to talk to it.
// So the checks below now demand the opposite, and the sharpest of them is
// the `?dep=` one at the bottom: a row the selection layer will not hold is
// a click that silently selects a DIFFERENT model, which is how this was
// nearly shipped.
//
// It esbuild-bundles the module and imports the bundle, because the repo's
// sources use extensionless specifiers that node's ESM resolver rejects.
//
//   cd ui && node src/tabs/chat/rows.check.mjs
//   DERATE_CHECK_ORIGIN=http://localhost:18088 node src/tabs/chat/rows.check.mjs
//
// Needs a coordinator: :8088 by default, or whatever DERATE_CHECK_ORIGIN names.
// Every assertion is about a fact the gateway reports.
import { build } from 'esbuild'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

const dir = mkdtempSync(join(tmpdir(), 'chatrows-'))
const bundle = async (rel, name) => {
  const out = join(dir, `${name}.mjs`)
  await build({
    entryPoints: [fileURLToPath(new URL(rel, import.meta.url))],
    bundle: true, format: 'esm', outfile: out, jsx: 'automatic', logLevel: 'silent',
  })
  return import(out)
}
const M = await bundle('./ModelList.tsx', 'modellist')
// `ENDPOINT_FOR_MODALITY` is imported rather than re-typed: a second copy of
// the table here would agree with any bug that came from the same reading of
// it. The modality strings are compared literally on purpose -- they are wire
// values from /v1/models, and a helper in between could only hide a rename.
const T = await bundle('../../api/types.ts', 'types')

const ORIGIN = process.env.DERATE_CHECK_ORIGIN ?? 'http://localhost:8088'
const get = async (p) => (await fetch(`${ORIGIN}${p}`)).json()

let fail = 0
const check = (ok, msg) => {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${msg}`)
  if (!ok) fail++
}

// Every failure here is a set difference, and the interesting ones are set
// differences of however many models a provider serves. Printed whole they
// bury the other checks; printed as a count they do not say which. A few names
// and a count is enough to recognise what went missing.
const few = (list) => {
  const a = [...list]
  if (a.length === 0) return 'none'
  return a.slice(0, 5).join(', ') + (a.length > 5 ? `, and ${a.length - 5} more` : '')
}

// Fetched back to back: the target index is rebuilt on a 1 s TTL, so the two
// answers are a moment apart and a deployment can legitimately cross between
// them. Anything below that compares the two is stated as a set relation, and
// a genuine skew shows up as one name, not as a whole side going missing.
const models = (await get('/v1/models')).data
const deployments = await get('/api/deployments')

const rows = M.buildRows(models)
const names = new Set(rows.map((r) => r.name))

// Serving now, per gateway/targets.py ROUTABLE_STATES and deploy/fsm.SERVING.
const SERVING = new Set(['ready', 'degraded'])
const serving = deployments.filter((d) => SERVING.has(d.state))
const notServing = deployments.filter((d) => !SERVING.has(d.state))

console.log(
  `${ORIGIN}: ${models.length} served name(s), ${deployments.length} deployment(s) ` +
    `(${serving.length} serving) -> ${rows.length} chat row(s)`,
)

const served = new Set(models.map((m) => m.id))
check(
  rows.every((r) => served.has(r.name) && r.kinds.length > 0),
  `every row is a name /v1/models carries, with a target behind it${
    rows.length ? '' : ' (vacuous: no row)'
  }`,
)

// Local first, then alphabetical. The one thing somebody came to this screen
// for is the model running on their own hardware, and a provider's names must
// not push it down the list.
const firstRemote = rows.findIndex((r) => !r.kinds.includes('local'))
const lastLocal = rows.map((r) => r.kinds.includes('local')).lastIndexOf(true)
check(
  firstRemote === -1 || lastLocal === -1 || lastLocal < firstRemote,
  `locally-served rows sort above provider rows (last local ${lastLocal}, ` +
    `first remote ${firstRemote})`,
)

// The load-bearing one, and the reason this file exists. `serving` is empty on
// an idle coordinator, which makes the deployment relations below pass for the
// wrong reason; this one still has every switched-on provider model to be
// wrong about. It used to assert that none of them was offered.
const remoteOnly = models
  .filter((m) => !m.target_kinds.includes('local'))
  .map((m) => m.id)
const dropped = remoteOnly.filter((id) => !names.has(id))
check(
  dropped.length === 0,
  `every switched-on provider model is offered (${remoteOnly.length} remote-only ` +
    `in /v1/models, ${dropped.length} missing: ${few(dropped)})`,
)

// Nothing is filtered by modality any more. This assertion USED to be "no
// audio model is offered", and the reversal is the point: a speech model is a
// row, because ChatTab posts it to /v1/audio/speech and it answers. A whisper
// deployment went the same way one round later, once the composer grew a file
// picker to match /v1/audio/transcriptions -- see the transcription check
// below, which is the same claim for that modality.
const speech = models.filter((m) => m.modality === 'speech').map((m) => m.id)
const missedSpeech = speech.filter((id) => !names.has(id))
check(
  missedSpeech.length === 0,
  `every speech model is offered (${speech.length} in /v1/models, ` +
    `${missedSpeech.length} missing: ${few(missedSpeech)})`,
)

const transcription = models.filter((m) => m.modality === 'transcription').map((m) => m.id)
const missedTranscription = transcription.filter((id) => !names.has(id))
check(
  missedTranscription.length === 0,
  `every transcription model is offered (${transcription.length} in /v1/models, ` +
    `${missedTranscription.length} missing: ${few(missedTranscription)})`,
)
// Nothing is filtered any more, so the filter cannot be wrong -- but a row
// going missing for some other reason still can be, and that is now the only
// way this list can be short.
check(
  names.size === models.length,
  `every served name is a row (${models.length} in /v1/models, ${names.size} rows)`,
)

// The section a row is filed under is the endpoint its message will be posted
// to. Those are two different code paths -- ModelList's heading and ChatTab's
// branch -- reading one field, and this is what holds them to it: a speech
// model under the chat heading is a message about to be refused, drawn as if
// it were fine.
for (const g of M.sections(rows)) {
  check(
    g.rows.every((r) => r.modality === g.modality),
    `every row under POST ${T.ENDPOINT_FOR_MODALITY[g.modality]} answers there ` +
      `(${g.rows.length} row(s))`,
  )
}

check(
  rows.every((r) => r.name !== null && r.state === undefined && r.servable === undefined),
  'no row carries a servable/state flag -- nothing here is listed unselectable',
)

const servingNames = new Set(serving.map((d) => d.served_name))
// Scoped to the LOCAL rows. A provider row is backed by no deployment at all
// -- that is what makes it a provider row -- so the old unscoped form would
// now fail on every one of them.
const localNames = rows.filter((r) => r.kinds.includes('local')).map((r) => r.name)
const unserved = localNames.filter((n) => !servingNames.has(n))
check(
  unserved.length === 0,
  `every local row names a ready/degraded deployment (${unserved.length} do not: ${few(unserved)})`,
)

const wanted = serving.map((d) => d.served_name)
const missing = wanted.filter((n) => !names.has(n))
check(
  missing.length === 0,
  `every serving deployment has a row, whatever it answers on (${missing.length} ` +
    `missing: ${few(missing)})`,
)

// A failed deployment's name can legitimately still be a row: if a provider
// serves the same name, the gateway forwards it and the row is that remote
// target, not the dead deployment. So the offer has to be justified by a
// serving deployment OR by a remote target -- never by neither.
const remoteNames = new Set(remoteOnly)
const zombies = notServing
  .filter((d) => names.has(d.served_name))
  .filter((d) => !servingNames.has(d.served_name) && !remoteNames.has(d.served_name))
  .map((d) => `${d.served_name} (${d.state})`)
check(
  zombies.length === 0,
  `no launching/failed/stopped deployment is offered unless a provider carries ` +
    `the name (${notServing.length} not serving, ${zombies.length} bad: ${few(zombies)})`,
)

// The picker writes `?dep=`, and `selDep` (state/selection.tsx) only honours a
// name it can find in `/api/topology`. If the two ever diverge, clicking a row
// sets a `?dep=` the selection layer rejects and the choice snaps back to the
// default with nothing on screen to explain it -- so the row set has to be a
// subset of the domain `selDep` validates against.
const topo = await get('/api/topology')
// Both halves, exactly as `selDep` validates them. Deployments alone was the
// bug: every provider row failed this test, and a click on one fell through to
// defaultDep() and selected the local deployment instead -- silently, which is
// the failure mode this check was written to catch in the first place.
const selectable = new Set([
  ...(topo.deployments ?? []).map((d) => d.served_name),
  ...(topo.remotes ?? []).map((r) => r.served_name),
])
const unholdable = [...names].filter((n) => !selectable.has(n))
check(
  unholdable.length === 0,
  `every row is a name ?dep= can hold (${unholdable.length} it cannot: ${few(unholdable)})`,
)

// The empty panel is the whole answer when nothing serves, so it has to say
// which of the two reasons applies rather than going blank.
const note = M.emptyNote(deployments)
const NOTABLE = ['launching', 'planned', 'stopping', 'failed']
const pending = deployments.filter((d) => NOTABLE.includes(d.state))
if (pending.length > 0) {
  check(
    pending.slice(0, 3).every((d) => note.includes(d.served_name) || note.includes('more')),
    `the empty note names what is pending: ${JSON.stringify(note)}`,
  )
} else {
  check(
    note === 'No model is running on this cluster.',
    `with nothing pending the empty note is the plain sentence: ${JSON.stringify(note)}`,
  )
}

console.log(fail ? `\n${fail} check(s) failed` : '\nall passed')
process.exit(fail ? 1 : 0)
