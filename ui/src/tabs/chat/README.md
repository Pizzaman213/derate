# chat

The parts of the console for talking to what this cluster is actually running.
It filters nothing out of the picker: every name `/v1/models` reports gets a
row, and one field on that row -- `modality` -- decides both the heading a
person reads and the URL their message is posted to, so the two cannot
disagree. A model that can be switched on, listed and curled must never be
unselectable in the console shipped to talk to it.

Everything here is entered through `../ChatTab.tsx`, which owns the turns, the
abort controller and the send. This folder owns the picker, the composer, the
transcript, and three verifiers: `rows.check.mjs` and `voices.check.mjs` hold
the picker and the voice controls to a live gateway, and `tags.check.mjs` is
hermetic -- it holds the transcript's colours to `styles/tokens.css`.

## Layout

| File | Lines | What it owns |
|---|---|---|
| `ModelList.tsx` | 228 | `buildRows`, `sections`, `emptyNote` -- every served name, cut into one section per endpoint family |
| `Transcript.tsx` | 205 | the `Turn` type, the turn list, the meta line, scroll anchoring |
| `Composer.tsx` | 172 | the send box and `SendExtra` -- one value picks the controls drawn and the URL posted to |
| `tags.ts` | 114 | `tagsFor`, `tagStyle`, `tagInk`: which model a turn went to, drawn as a colour |
| `VoiceFields.tsx` | 104 | voice and container format for a speech model; `OWN_VOICE`, `resolveVoice` |
| `UploadFields.tsx` | 87 | file and language for `/v1/audio/transcriptions`; `DEFAULT_LANGUAGE` |
| `Clip.tsx` | 71 | one synthesised clip: a player, and what only the response headers know |
| `useVoices.ts` | 55 | `GET /v1/audio/voices` for one model, fetched rather than polled |
| `tags.check.mjs` | 296 | hermetic: no two models drawn the same, no colour that moves, no hue nobody can separate |
| `rows.check.mjs` | 241 | requires a coordinator: `buildRows` against the live `/v1/models` |
| `voices.check.mjs` | 110 | requires a coordinator: `SPEECH_FORMATS` against the runtime's own table, and the voices route's refusals |

## `ModelList.tsx`

`buildRows(models)` turns `/v1/models` into `ModelRow[]` and keeps every one of
them. That is a reversal, and `buildRows`'s docstring records it three times
over -- the provider filter, then speech, then transcription. The list was
once the endpoint's local half, because a coordinator pointed at OpenRouter with no
allowlist offered several hundred rows of somebody else's hardware; the
allowlist ended that argument -- a provider serves nothing until somebody
presses Serve -- and the filter's real cost showed instead: a model switched
on, listed, answering a curl, and unselectable here with nothing on screen
saying why. `target_kinds` still decides, read server-side off the same index
the router dispatches from, but it now decides what a row *says* rather than
whether it exists. Locally-served names sort first, then alphabetically.
`sections()` cuts the rows on `SECTION_ORDER` -- text, embedding, speech,
transcription, the same order and reasoning as `MODALITY_ORDER` in
`tabs/cluster/layout.ts`. Headings name the route, not the category:
`POST /v1/audio/speech` is the thing a person can act on. `emptyNote` names
what is pending from `NOTABLE` (`launching`, `planned`, `stopping`, `failed`),
because "nothing is running" and "something is launching" read very
differently.

## `Transcript.tsx`

The turn list, and the `Turn` type both halves of an exchange are stored as.
`model` is recorded at send time on the question and the answer alike;
`meta.model` -- what actually served -- wins where both exist, which is
`destination()`. `audio` is a separate field rather than a tagged union,
because a turn's identity is the request and not the media type of what came
back. `metaLine` is the line that makes this a control-plane surface: what
served, the request id, ttft, elapsed, tokens. When `ttftMs`, `elapsedMs` and
`completionTokens` are all null it prints the model and the id alone -- both
audio endpoints land there, and `ttft — ms · — s · — tok` would read as three
failed measurements rather than as a request that never had them. The test is
on the figures, not on the modality, so it survives a fourth endpoint. The
in-flight caption follows `mode`: "generating audio", "transcribing", or
"waiting for the first token", because naming the wrong one makes a working
request look stalled. `followRef` anchors the scroll to the tail unless the
reader has scrolled up, the same pattern `DeploymentLog` uses.

## `Composer.tsx`

The send box, and `SendExtra` -- a discriminated union of `{kind: 'speech',
voice, format}` and `{kind: 'transcription', file, language}`, `null` for a
chat completion. Three endpoints take disjoint inputs and `ChatTab` switches on
exactly this value, so one thing decides which controls are drawn *and* which
URL the send goes to; the composer cannot offer a voice for a request with no
voice field. `ready()` is one function returning the payload or `null`, so the
disabled state and the payload cannot disagree about whether this composer can
send -- they were two conditions, and the file case made that a bug waiting to
happen. On the transcription path the file *is* the request: an empty message
box must not block it, so the textarea is replaced rather than stacked above
the chooser, and the turn is labelled with the file's name. `voice` and
`format` are held across a switch to a text model and back, and only ever read
when `speaking`. Enter sends, Shift+Enter breaks the line, and the caption
under the field says so.

## `tags.ts`

Which model a turn went to, drawn as a colour, because this tab lets the model
change between turns and until this module the only thing that said so was the
name over each *answer*. **Slots are assigned by order of first appearance,
never by hashing the name.** A hash is stable across transcripts, which sounds
like the better property: it can also hand two models in the *same* transcript
the same slot, and that transcript is the only place the colour is ever read.
First appearance is unique by construction and fixed the moment a turn is
drawn, since turns are appended and never reordered. `TAG_HUES` is 4 and
`TAG_SLOTS` is 8 -- each hue solid, then dashed, so eight identities survive a
greyscale screenshot. A ninth model gets no slot at all and draws plainly:
`tagStyle(undefined)` returns `undefined` and `tagInk(undefined)` returns
`var(--ink-muted)`. An absent mark says "read the name"; a repeated one says
"these are the same model". The tint is a 6% `color-mix` of the same token
rather than a second set of pale values: one token per identity is the whole
ramp, and 6% sits beside the 5% `--hover` already mixes, which keeps `--ink`
body text where it was measured.

## `VoiceFields.tsx`

Voice and container format, drawn in the composer whenever the picked model
answers in audio. `OWN_VOICE` is the empty string rather than a sentinel word,
because that is what an unset `<select>` carries and because the field it maps
to is *absent* from the request body, not set to something -- naming no voice
is a real request and the default one, since a cloned voice needs a reference
clip and that clip's exact transcript installed on the node. `resolveVoice`
drops a name the current library does not carry, so a voice that was installed
when the page loaded and is not there now falls back rather than being sent and
refused. The format list is `SPEECH_FORMATS` from `api/types.ts`, not a local
copy. `voicesError` goes through `Verbatim` and `library.skipped` through
`VerbatimList`, because the first is one sentence the server wrote and the
second is a list of them; `skipped` is the only thing that tells somebody why a
clip they installed is not in the list.

## `UploadFields.tsx`

File plus language for `POST /v1/audio/transcriptions`, and a sibling of
`VoiceFields` rather than a mode inside it -- one component drawing both would
be four controls of which two are always inert. `DEFAULT_LANGUAGE` is `'en'`
and is a visible control on purpose: with the field absent vLLM runs language
auto-detection, and `whisper-*.en` has no language tokens, so the model fails
an assertion inside `supported_languages` and returns a 500 from a request that
looks fine. Sending `en` invisibly would fix the English case and make a
multilingual checkpoint mysteriously monolingual; a field defaulting to `en`
fixes both for one control. `ACCEPT` filters the browser's picker and is not a
claim about what the runtime decodes -- the upstream's refusal is rendered
verbatim if it disagrees. The native file input is used unstyled: a custom
button over a hidden input costs the file name, the drag target and the
keyboard behaviour.

## `Clip.tsx`

One synthesised clip: an `<audio>` element and a row of what only the response
headers know -- content type, size, duration, sample rate, request id. **The
object URL is state, not a `useMemo`.** It is a resource rather than a
derivation and has to be revoked; a memo has nowhere to do that, and without
the revoke a session spent trying voices holds every clip it ever produced
until the tab closes. **`pcm` gets a readout instead of a player.** Raw samples
carry no container and no header, so nothing in the file says what rate or
width to play them at and every browser refuses -- an `<audio>` there is a
permanently broken control. The match is `contentType.startsWith('audio/pcm')`,
a prefix so a charset parameter or a provider's own spelling cannot slip into a
player that cannot decode it. Missing duration and rate print an em dash: a
provider sends neither header, and nobody reporting a figure is not the same as
reporting zero.

## `useVoices.ts`

`GET /v1/audio/voices` for one model, fetched on change rather than polled: a
voice library is a directory on the node the deployment runs on, nothing
changes it but somebody copying a file there, and the runtime only reads that
directory at startup. `enabled` is what stops it being asked for a text model
-- the gateway would answer `wrong_modality`, correctly, and the tab would then
show a refusal about a control it is not drawing. The effect keeps a
`cancelled` flag so a fast switch between two models cannot let the slower
answer overwrite the newer one, which here would offer one deployment's voices
for another's. A failure is returned, not thrown: naming no voice works
whatever this said, so the caller renders the sentence beside a form that still
functions.

## `tags.check.mjs`

Hermetic -- no `requires:` line, so `check.mjs` runs it on any checkout. Nothing
it asserts is visible to `tsc`: `tagsFor` returns a `Map` whether or not two
models share a slot, `tagStyle` returns a `CSSProperties` whether or not
`--tag-9` exists, and an undefined var renders as no colour at all. Its header
numbers four classes of bug under a sentence that still says three; the four are
the sections. Uniqueness is asserted on the whole style declaration, not the
slot number, because two slots that differ numerically and render identically
pass a slot-wise test and fail the reader. Stability is asserted by comparing
every prefix of a transcript against the whole. All three theme blocks are
parsed by name -- `:root`, the `:root:not([data-theme='light'])` inside the
`prefers-color-scheme` query, and `:root[data-theme='dark']` -- and each must
define all four hues, since a token defined in one and missed in another is a
screen that loses its colours on an OS setting; the `var()` strings the module
itself writes are then resolved against the light block's table. And the colour
rule types cannot see: `APART` is 40 degrees, between the palette somebody could
not read at 29 and the one that replaced it at 43, applied both between tags and
from `--live`, `--warn` and `--fault`. That rule and the contrast bar are both
measured in `light` and `dark, by explicit choice` only -- the media-query block
carries the same four hexes, so a third pass would measure the same numbers.
Contrast is against `--panel` at 4.5:1, the bar for text, because these are the
label as well as the rule.

## `rows.check.mjs`

`// requires: coordinator`. It esbuild-bundles `ModelList.tsx` and
`api/types.ts` and runs `buildRows` against the live `/v1/models`,
`/api/deployments` and `/api/topology` on `$DERATE_CHECK_ORIGIN` (`:8088` by
default). **The invariant here inverted.** It used to hold the picker to the
local half of the endpoint; it now asserts the opposite -- `names.size ===
models.length`, every switched-on provider model offered, every speech model
offered, every transcription model offered. `sections()` is checked row by row
against the modality of its heading, because that heading and `ChatTab`'s send
branch are two code paths reading one field and a speech model under the chat
heading is a message about to be refused, drawn as if it were fine. The
sharpest check is the second from last: every row must be a name `?dep=` can
hold, taken from `topology.deployments` *and* `topology.remotes`, exactly as
`selDep` validates. Deployments alone was the bug -- every provider row failed
it, and a click fell through to `defaultDep()` and silently selected the local
deployment instead. The file closes on `emptyNote`, which is the whole answer
when nothing serves and so has to say which of its two reasons applies.

## `voices.check.mjs`

`// requires: coordinator`. Two classes of bug, neither visible to `tsc`.
`SPEECH_FORMATS` in `api/types.ts` is a copy of a Python dict: the file reads
`FORMATS: dict[str, AudioFormat]` out of `control_plane/runtimes/tts.py` by
regex and compares the sorted key lists, because an option the server refuses
is a dropdown entry that always 400s. It also asserts `aac` is absent --
libsndfile cannot write it, and a client that asked for AAC and got MP3 under
`Content-Type: audio/aac` fails somewhere much further away than here. The
other half is the voices route's refusals, which are the argument for it having
its own rather than proxying somebody else's 404: no `?model=` is a 400
`missing_model`, a text model is a 400 `wrong_modality` whose body names
`/v1/chat/completions` as the endpoint that would have worked, and an unknown
name is a 404 `model_not_found`. It absorbed the useful half of the deleted
`tabs/speech/speech.check.mjs`.

## The seam with `ChatTab.tsx`

`../ChatTab.tsx` (305 lines) is the only importer:

```tsx
import { ModelList, buildRows, emptyNote } from './chat/ModelList'
import { Transcript, type Turn } from './chat/Transcript'
import { Composer, type SendExtra } from './chat/Composer'
import { useVoices } from './chat/useVoices'
```

It builds the rows from `useModels()`, the empty note from the cluster's
deployments, and then reads one field twice:

```tsx
const speaking = activeRow?.modality === 'speech'
const transcribing = activeRow?.modality === 'transcription'
const { library, error: voicesError } = useVoices(active, speaking)
```

`speaking` and `transcribing` decide which fields `Composer` draws; the
`SendExtra` that comes back decides which of `backend.transcribe`,
`backend.speech` and `backend.chatStream` is called. The header prints
`ENDPOINT_FOR_MODALITY[activeRow?.modality ?? 'text']`, so the URL on screen is
the URL the send takes.

Outside the tab, this folder reaches `api/types.ts` for `Modality`,
`ServedModel`, `DeploymentDTO`, `TargetKind`, `ChatRole`, `SpeechFormat`,
`SPEECH_FORMATS`, `SpeechResult`, `VoiceLibrary`, `ChatTurnMeta` and
`ENDPOINT_FOR_MODALITY` (which mirrors `control_plane/contracts/modality.py`),
`components/Verbatim` for anything the server wrote -- `Verbatim` for a
sentence, `VerbatimList` for `library.skipped` -- `format` for `fmt`, which is
what puts an em dash rather than a zero in every figure `metaLine` prints,
`state/backend` for the client, and `styles/tokens.css` for `--tag-1`..`--tag-4`
and `--ink-muted`. `styles/derate.css` owns `.chatlist`, `.chatgroup`,
`.chatgrouphead`, `.chatlog`, `.turn`, `.composer`, `.speechrow`, `.clip` and
`.cliprow`.

## Things that look like details and are not

**`modality` is one field read in two places, and that is the point.**
`ModelList` files the row under a heading naming an endpoint; `ChatTab`
branches the send on the same value. Two fields, or a heading inferred from the
name, is a picker that can promise `/v1/chat/completions` and post to
`/v1/audio/speech`. `rows.check.mjs` asserts the two agree over the live model
list.

**Nothing here is filtered, and the filter is what used to break it.** Speech
models were dropped on the argument that a TTS model is not a chat model --
true of the model, and the wrong thing to be true of. Transcription stayed out
one round longer on the argument that an upload is not a message, which was
about the *composer*, and was fixed by giving the composer a file chooser. If a
fifth modality is ever added, the honest failure is a section with no controls
under it, which is why `sections()` files by modality rather than by "is it
audio".

**The `--tag-*` ramp is ordered by use, not by spectrum.** `tokens.css` runs
blue, magenta, teal, violet. A first draft ran blue-teal-violet-magenta, which
is how a ramp is normally written and put the two closest hues on the two
commonest slots: a two-model transcript came out 29 degrees apart, correctly
coloured and unreadable at a glance, with every assertion passing. The
two-model case is now the 100-degree pair and the worst pair anywhere is 43.
Reordering those four lines re-pairs them.

**Colour here is identity, never a verdict.** This app colours nothing
decoratively, and the exception is taken on the grounds that the colour *is*
the state -- which model a turn went to was not on screen at all for a
question. The name is printed beside every mark for the same reason. The hues
are held clear of `--live`, `--warn` and `--fault` by value and by hue angle,
because a model coloured `--fault` is a model that looks broken. This is not
`tabs/models/owner.ts` reused: that function hashes (so it can collide), spans
the amber and green families, and is raw `hsl()` with no dark variant.

**An unmeasured figure is an em dash and never a zero.** It holds in
`metaLine`, in `Clip`'s duration and sample rate, and in `describe()`'s context
length. A figure that does not exist for this kind of request is not printed as
a row at all.

**No row carries a `state` or `servable` flag**, and `rows.check.mjs` asserts
it. Deployments that are not serving yet were once listed disabled and
captioned; that was reversed. Every row in this picker can answer, and a launch
in flight is reported by `emptyNote` and the dashboard.

## Failure behaviour

- **No rows.** `emptyNote` names up to three pending deployments and their
  states, or the plain sentence `No model is running on this cluster.` when
  nothing is pending.
- **`/v1/models` fails.** `ModelList` renders `error.message` in `--fault` with
  `pre-wrap`, above the list rather than instead of the screen.
- **The voices fetch fails.** `useVoices` returns the server's own sentence and
  `VoiceFields` renders it through `Verbatim` beside a working form -- a
  request naming no voice still succeeds.
- **A selected voice disappears from the library.** `resolveVoice` falls back
  to `OWN_VOICE` rather than sending a name that would be refused.
- **A ninth model in one transcript.** No slot, no rule, no tint, muted label.
  The name is still printed.
- **A turn with no destination.** Skipped by `tagsFor` rather than given a slot
  of its own, so the first real model is not left uncoloured.
- **A speech or transcription send is stopped.** There is no partial audio to
  keep, so `ChatTab` marks the turn `stopped` in its meta rather than failed.
- **The gateway refuses.** `ApiError` has already unwrapped the
  `{error:{message}}` envelope, so the turn's `error` is the sentence the
  gateway wrote and `Transcript` renders it through `Verbatim` -- never
  reworded here or downstream.
- **A verifier cannot reach a coordinator.** `check.mjs` reads the
  `// requires:` line and reports the verifier in its own column with the
  reason. `--strict` makes that a failure. A skip is never a pass.

## Deliberately not built

**A `/speech` screen.** It existed briefly as the only way in the product to
send `POST /v1/audio/speech`, on the argument that the chat picker filtered
audio models out. The filter was removed instead, and the screen was retired on
2026-09-08 once it had nothing `/chat` did not already do. `voices.check.mjs`
is what survived of its verifier.

**A hash from model name to colour.** Rejected in `tags.ts` and in
`tags.check.mjs`: stability across transcripts is not worth a collision inside
one, and inside one is the only place the colour is read.

**A wrapping palette.** A ninth model draws plainly. Repeating a hue would make
two different models identical, which is worse than uncoloured.

**A tagged union for `Turn`.** `audio` sits beside `content` because a turn's
identity is the request, not the media type of what came back -- which is also
why a transcription answer lands in `content` and every existing reader renders
it unchanged.

**A poll on the voice library.** The answer changes by hand, once, and the
runtime reads that directory only at startup.

**`aac` in the format list.** libsndfile cannot write it, so it is absent
rather than offered and refused; a client that asked for AAC and received MP3
under an AAC content type fails a long way from here.

**A styled file button.** The native input keeps the file name, the drag target
and the keyboard behaviour that a hidden input behind a custom button throws
away.
