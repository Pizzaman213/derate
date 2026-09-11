# chat

The parts of the console for talking to what this cluster is actually running.
It filters nothing out of the picker: every name `/v1/models` reports gets a
row, and one field on that row -- `modality` -- decides both the heading a
person reads and the URL their message is posted to, so the two cannot
disagree. A model that can be switched on, listed and curled must never be
unselectable in the console shipped to talk to it.

Everything here is entered through `../ChatTab.tsx`, which owns the turns, the
abort controller and the send. This folder owns the picker, the composer, the
transcript, and four verifiers: `rows.check.mjs` and `voices.check.mjs` hold
the picker and the voice controls to a live gateway, and `tags.check.mjs` and
`meta.check.mjs` are hermetic -- they hold the transcript's colours to
`styles/tokens.css` and the line under every answer to what was measured.

## Layout

| File | Lines | What it owns |
|---|---|---|
| `ModelList.tsx` | 228 | `buildRows`, `sections`, `emptyNote` -- every served name, cut into one section per endpoint family |
| `Transcript.tsx` | 306 | the `Turn` type, the turn list, scroll anchoring, the per-turn action row, inline edit |
| `Composer.tsx` | 259 | the send box and `SendExtra` -- one value picks the controls drawn and the URL posted to; the image attach |
| `markdown.tsx` | 253 | `MessageBody` -- the markdown-lite renderer every turn's text (and reasoning) goes through |
| `RequestParams.tsx` | 107 | `RequestParamsFields` -- the closed-by-default system prompt / temperature / max tokens / stop disclosure |
| `tags.ts` | 114 | `tagsFor`, `tagStyle`, `tagInk`: which model a turn went to, drawn as a colour |
| `meta.ts` | 99 | `metaLine`, `decodeTps`: the line under an answer -- model, request id, ttft, elapsed, tokens, tok/s |
| `VoiceFields.tsx` | 104 | voice and container format for a speech model; `OWN_VOICE`, `resolveVoice` |
| `UploadFields.tsx` | 87 | file and language for `/v1/audio/transcriptions`; `DEFAULT_LANGUAGE` |
| `Clip.tsx` | 71 | one synthesised clip: a player, and what only the response headers know |
| `useVoices.ts` | 55 | `GET /v1/audio/voices` for one model, fetched rather than polled |
| `tags.check.mjs` | 296 | hermetic: no two models drawn the same, no colour that moves, no hue nobody can separate |
| `meta.check.mjs` | 148 | hermetic: the dash rule, the decode rate the gateway computes, and the turns that have no rate |
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
back; `images` and `reasoning` follow the same rule -- a chat turn with a
picture attached is still a chat turn, not a different kind of turn. `kind`
(`'chat' | 'speech' | 'transcription'`) is recorded per turn rather than read
off the picker's *current* modality, because the picker can move on before
Edit or Regenerate is clicked on an old turn and those only mean something for
a plain chat exchange. The line under every answer is `meta.ts`'s `metaLine`,
called with the turn's `meta` and `requestId`; it moved out of this file so a
verifier could import it, and the rules it keeps are in its own section below.
The in-flight caption follows `mode`: "generating audio",
"transcribing", or "waiting for the first token", because naming the wrong one
makes a working request look stalled. `followRef` anchors the scroll to the
tail unless the reader has scrolled up, the same pattern `DeploymentLog` uses.

Turn text renders through `markdown.tsx`'s `MessageBody`, not a raw
`white-space: pre-wrap` paragraph. A non-empty `reasoning` renders above it in
a closed-by-default `<details>` -- closed, because a model's thinking is
chrome around the answer, not the answer, and the composer already treats
"advanced" the same way. Each finished turn carries a small action row: Copy
on an assistant turn (reuses `components/clipboard.ts`, not a full
`Copyable`), Edit and Delete on a user turn, Regenerate and Delete on an
assistant one -- Edit and Regenerate hidden whenever `turn.kind !== 'chat'` or
the turn carries audio. All of it is disabled, not hidden, while any turn is
streaming, so a resend can never race the request already in flight. Editing
is local `useState` in this file (`editingKey`/`draft`), not lifted into
`Turn` -- only one turn is ever mid-edit.

## `Composer.tsx`

The send box, and `SendExtra` -- a discriminated union of `{kind: 'chat',
images}`, `{kind: 'speech', voice, format}` and `{kind: 'transcription', file,
language}`. Three endpoints take disjoint inputs and `ChatTab` switches on
exactly this value, so one thing decides which controls are drawn *and* which
URL the send goes to; the composer cannot offer a voice for a request with no
voice field. `null` used to stand for a chat completion; it does not any more,
because a plain chat send needed a place to carry its attached images too, and
a `kind: 'chat'` case beside the other two was less surprising than a fourth
shape. `ready()` is one function returning the payload or `null`, so the
disabled state and the payload cannot disagree about whether this composer can
send -- they were two conditions, and the file case made that a bug waiting to
happen. On the transcription path the file *is* the request: an empty message
box must not block it, so the textarea is replaced rather than stacked above
the chooser, and the turn is labelled with the file's name. On the chat path
an image with no caption is the same case for the same reason -- the picture
is the request, so the empty-text check is `!body && images.length === 0`, not
`!body`. `voice` and `format` are held across a switch to a text model and
back, and only ever read when `speaking`. Enter sends, Shift+Enter breaks the
line, and the caption under the field says so.

**Images are read to a data URL, not an object URL.** `Clip.tsx` revokes an
object URL because it is a browser-local resource; a data URL is a string, has
no revoke step, and is already the exact shape `image_url.url` takes on the
wire (OpenAI's own vision request accepts a data URL directly) -- so the same
value serves as both the request payload and the `<img src>` preview, rather
than a request copy and a display copy that could drift. Held at 25 MiB per
file, the same figure `gateway/settings.py` documents as what a request body
here accepts, checked client-side before the read rather than after.

**No model on this cluster advertises vision support**, and the attach control
is not gated on a capability flag that does not exist -- `Modality` has no
`vision` value and nothing in `ProviderModel` covers it either. The caption
under the control says so in as many words. Sending anyway and letting a
model that cannot read it refuse -- through the ordinary `Verbatim` error path
every other refusal already takes -- was judged more honest than hiding a
control some remote provider model genuinely does support, for want of a fact
this system has no way to know.

`RequestParamsFields` (system prompt, temperature, max tokens, stop
sequences) mounts here too, both closed by default and both gated out
whenever `speaking || transcribing` -- neither has a `messages` array for a
system prompt to join or a text-completion sampling parameter to accept.

## `markdown.tsx`

`MessageBody`, and a markdown-LITE parser behind it -- not CommonMark, the
subset model output actually uses: paragraphs, `**bold**`/`*italic*`,
`` `inline code` ``, fenced code (a monospace block, a language label, a copy
button), lists, blockquotes, links. `parseBlocks` is line-oriented and
greedy per block type; `renderInline` builds React elements directly off a
single alternation regex, never an HTML string, which is what makes this safe
against a model's own output rather than a hardening pass bolted on after --
there is no `dangerouslySetInnerHTML` anywhere in the file. A link (or a bare
URL) only becomes a real `<a>` when its scheme is `http:`, `https:` or
`mailto:`; anything else, `javascript:` included, simply fails to match the
pattern and prints as the literal text the model sent, the same fallback an
unclosed `**` or a malformed fence already gets. No token-level syntax
highlighting -- a language label and a monospace block, deliberately, to avoid
a highlighter dependency in a UI whose entire runtime dependency list is seven
packages, none of them a text-processing library.

## `RequestParams.tsx`

`ChatParams` and `RequestParamsFields` -- system prompt, temperature, max
tokens, stop sequences, none of which the composer sent before. Every field is
held as a string, the natural shape for a controlled input mid-edit, and only
parsed (`parseTemperature`/`parseMaxTokens`/`parseStop`) at send time in
`ChatTab`; a blank or unparseable field becomes `undefined`, never `0` or
`NaN`, so `chatStream` omits the key from the request body entirely rather
than sending a value nobody chose. This is almost pure frontend plumbing --
`control_plane/gateway/openai_api.py` rewrites only `model` before forwarding
a chat completion's body, so `temperature`/`max_tokens`/`stop` already reached
the runtime untouched whenever a caller sent them; the composer was the only
thing that never did.

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

## `meta.ts`

`metaLine` is the line that makes this a control-plane surface rather than a
chat window: what served, the request id, ttft, elapsed, tokens, tok/s. It
takes `ChatTurnMeta | null` and the request id rather than a `Turn`, which is
what keeps it out of `Transcript.tsx` -- the verifiers bundle with esbuild's
`platform: 'neutral'` and cannot import a module that pulls in React, and every
rule here is a string `tsc` cannot see.

When `ttftMs`, `elapsedMs` and `completionTokens` are all null it prints the
model and the id alone -- both audio endpoints land there, and `ttft — ms · —
s · — tok` would read as three failed measurements rather than as a request
that never had them. The test is on the figures, not on the modality, so it
survives a fourth endpoint.

`decodeTps` is the rate, and it is `completionTokens / (elapsed - ttft)`:
tokens over the window that followed the first one. That is not a choice made
here. It is the arithmetic `gateway/stats.py::TargetStats.complete` folds into
`decode_tps` from `usage.tokens / usage.decode_s`, which the deployment
inspector prints as `tok/s per stream` -- one request, two readouts, and a
second definition written in the browser is how they would come to disagree
about it. The whole-request reading (`40 tok / 2.6 s = 15`) is the wrong number
for the same reason ttft is its own figure three fields to the left: it falls
when a queue is long rather than when decode is slow.

Three things about the rate that are not obvious:

- **A turn with no decode window prints no rate at all**, rather than a dash.
  The dash is for a figure this request has and we failed to measure; every
  input to the rate is printed to its left, so `— tok/s` there would read as a
  measurement that failed rather than as arithmetic that was not done.
- **`MIN_DECODE_MS` is what "no decode window" means.** An upstream that ships
  a whole completion in one frame has `elapsed == ttft`, and a real token count
  over a window of microseconds prints five figures of tok/s for a request
  nobody decoded quickly.
- **One decimal under 10 tok/s, none above.** `fmt`'s fixed-width rule is for a
  column of readouts that must not jump as a value crosses a boundary; this is
  prose, and the widths on this line already vary. What the decimal buys is
  that a genuinely slow decode reads `0.4 tok/s` rather than rounding into the
  `0` this UI reserves for a measured zero.

The count's provenance carries into the rate: when `tokensEstimated` says the
tokens were counted delta frames, the rate is that estimate over a measured
window, and the `(est.)` printed beside the count governs the figure next to
it. The gateway's own `decode_tps` is arrived at the same way -- it counts SSE
markers -- so the two agree about their provenance as well as their arithmetic.

## `meta.check.mjs`

Hermetic -- no `requires:` line. Four sections, one per class of bug: a figure
that was never measured printed as a number, a rate that disagrees with the
rest of the product, a rate divided by a window that measures nothing, and a
row that should not exist at all. The sharpest assertion is the whole line for
one real turn, pinned verbatim:

```
Qwen3-4B-AWQ · r-076dd2c900f1 · ttft 1985 ms · 2.6 s · 40 (est.) tok · 65 tok/s
```

40 tokens over the 615 ms that followed the first one is 65 tok/s; the
whole-request reading of that same turn is 15, so the pin is what tells the two
definitions apart if anyone rewrites the arithmetic.

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

`../ChatTab.tsx` (429 lines) is the only importer:

```tsx
import { ModelList, buildRows, emptyNote } from './chat/ModelList'
import { Transcript, type Turn } from './chat/Transcript'
import { Composer, type ImageAttachment, type SendExtra } from './chat/Composer'
import { DEFAULT_CHAT_PARAMS, parseMaxTokens, parseStop, parseTemperature } from './chat/RequestParams'
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

`send()` takes an optional `history: Turn[]` argument, defaulting to the live
`turns` state. `editAndResend` and `regenerate` are the reason it exists:
both truncate the transcript first and need the outgoing `messages` built from
that truncated list, not from state a synchronous `setTurns` call has not
applied yet. `deleteTurn` leans on an invariant `nextKey` documents where it is
declared: a user turn's key is always even and its reply's is that key plus
one, minted together and never reused, so either half's Delete button can find
its pair from the key alone.

Outside the tab, this folder reaches `api/types.ts` for `Modality`,
`ServedModel`, `DeploymentDTO`, `TargetKind`, `ChatRole`, `ChatMessage`,
`ChatContentPart`, `SpeechFormat`, `SPEECH_FORMATS`, `SpeechResult`,
`VoiceLibrary`, `ChatTurnMeta` and `ENDPOINT_FOR_MODALITY` (which mirrors
`control_plane/contracts/modality.py`), `components/Verbatim` for anything the
server wrote -- `Verbatim` for a sentence, `VerbatimList` for
`library.skipped` -- `components/clipboard.ts` for the copy buttons, `format`
for `fmt`, which is what puts an em dash rather than a zero in every figure
`metaLine` prints, `state/backend` for the client, and `styles/tokens.css` for
`--tag-1`..`--tag-4` and `--ink-muted`. `styles/derate.css` owns `.chatlist`,
`.chatgroup`, `.chatgrouphead`, `.chatlog`, `.turn`, `.composer`, `.speechrow`,
`.clip`, `.cliprow`, `.mdbody`, `.codeblock`(`head`), `.reasoning`,
`.turnimages`, `.turnedit`, `.turnactions`(`.turnaction`), `.reqparams`(`grid`)
and `.attachrow`/`.filepick`/`.attachchips`/`.attachchip`.

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
  reworded here or downstream. This is also what an image attached to a model
  that cannot read one looks like -- there is no capability check upstream of
  the send, only the runtime's own refusal, rendered the same way.
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
