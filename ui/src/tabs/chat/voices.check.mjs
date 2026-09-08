// requires: coordinator -- the voices refusals are facts about the live
// target index, and are not checkable from source.
//
// Verifier for the voice/format controls the Chat composer draws for a speech
// model (chat/VoiceFields.tsx, chat/useVoices.ts).
//
//   cd ui && node src/tabs/chat/voices.check.mjs
//   DERATE_CHECK_ORIGIN=http://localhost:18088 node src/tabs/chat/voices.check.mjs
//
// Two classes of bug live here, and `tsc` sees neither.
//
//   1. `SPEECH_FORMATS` is a copy of a Python dict. The format list in
//      `api/types.ts` and `FORMATS` in `control_plane/runtimes/tts.py` are the
//      same fact in two languages, and nothing in either build compares them.
//      An option the server refuses is a dropdown entry that always 400s.
//
//   2. The voices route's refusals are the interesting half. Asking a text
//      model for its voices, or asking for nobody's, must come back naming
//      the mechanism -- that is the whole argument for the route having its
//      own refusals rather than proxying somebody else's 404.
//
// This used to live in tabs/speech/speech.check.mjs, alongside assertions
// about the now-deleted `/speech` screen's own picker split; those are
// covered by chat/rows.check.mjs, which already holds every row (speech
// included) to its own endpoint heading.

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { load, report } from '../../check/harness.mjs'

const T = await load(import.meta.url, '../../api/types.ts')

const { check, note, done } = report()

const ORIGIN = process.env.DERATE_CHECK_ORIGIN ?? 'http://localhost:8088'
const get = async (p) => {
  const res = await fetch(`${ORIGIN}${p}`)
  return { status: res.status, body: await res.json().catch(() => null) }
}

// ── 1. The format list against the server that has to accept it ──────────────

const ttsSource = readFileSync(
  fileURLToPath(new URL('../../../../control_plane/runtimes/tts.py', import.meta.url)),
  'utf8',
)
// The `FORMATS: dict[str, AudioFormat] = { ... }` block, keys only.
const block = ttsSource.match(/^FORMATS: dict\[str, AudioFormat\] = \{$([\s\S]*?)^\}$/m)
check(Boolean(block), 'the tts runtime still declares a FORMATS table to compare against')
if (block) {
  const serverFormats = [...block[1].matchAll(/^\s{4}"([a-z0-9]+)":/gm)].map((m) => m[1])
  const ui = [...T.SPEECH_FORMATS].sort().join(',')
  const server = [...serverFormats].sort().join(',')
  check(ui === server, `SPEECH_FORMATS matches the runtime's own (ui: ${ui}; server: ${server})`)
  check(!T.SPEECH_FORMATS.includes('aac'), 'aac is not offered -- libsndfile cannot write it')
}

// ── 2. The voices route, and the two refusals that are its argument ──────────

const noModel = await get('/v1/audio/voices')
check(
  noModel.status === 400 && noModel.body?.error?.code === 'missing_model',
  `voices with no ?model= is a 400 that says which parameter (got ${noModel.status} ` +
    `${noModel.body?.error?.code ?? '-'})`,
)

const models = (await get('/v1/models')).body?.data ?? []
const aTextModel = models.find((m) => (m.modality ?? 'text') === 'text')
if (aTextModel) {
  const wrong = await get(`/v1/audio/voices?model=${encodeURIComponent(aTextModel.id)}`)
  check(
    wrong.status === 400 && wrong.body?.error?.code === 'wrong_modality',
    `a text model has no voices, refused as wrong_modality (got ${wrong.status} ` +
      `${wrong.body?.error?.code ?? '-'})`,
  )
  check(
    wrong.body?.error?.correct_endpoint === '/v1/chat/completions',
    'and the refusal names the endpoint that would have worked',
  )
} else {
  note('no text model is served, so the wrong_modality refusal was not exercised')
}

const nobody = await get('/v1/audio/voices?model=definitely-not-a-model')
check(
  nobody.status === 404 && nobody.body?.error?.code === 'model_not_found',
  `an unknown name is a 404 listing what does exist (got ${nobody.status} ` +
    `${nobody.body?.error?.code ?? '-'})`,
)

// The happy path, when there is one. Empty is a legitimate answer -- the voice
// directory is a mount somebody has to populate -- so the assertion is about
// the shape, not about there being voices in it.
const declared = models.filter((m) => m.modality === 'speech').map((m) => m.id)
if (declared.length === 0) note('no speech model is serving, so the happy path below is vacuous')
for (const id of declared) {
  const lib = await get(`/v1/audio/voices?model=${encodeURIComponent(id)}`)
  check(
    lib.status === 200 && Array.isArray(lib.body?.data),
    `${id} answers its voice list (got ${lib.status})`,
  )
  if (lib.status === 200) {
    note(
      `${id}: ${lib.body.data.length} voice(s)` +
        (lib.body.skipped?.length ? `, ${lib.body.skipped.length} skipped` : ''),
    )
  }
}

done()
