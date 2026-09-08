# api

Where the UI talks to the coordinator. Eighty-six files under `ui/src/` import
from this folder, seventy-six of them types only: they call a `Backend` method
and get back a shape from `types.ts` without ever writing a route or knowing
that a coordinator base can be configured. Three files outside this folder do
spell a path. `inspectors/node/Terminal.tsx` and `tabs/cluster/providerLogo.ts`
compose one and hand it to `wsUrl`/`apiUrl`, so a configured base still
applies; `tabs/models/owner.ts:157` is the one that bypasses this folder
outright — a bare `fetch('/api/publishers/avatars?owners=…')` with no `apiUrl`
around it, which is why publisher marks are the one thing still asking the
page's own origin when the base points somewhere else. The client is **live
only**: `fixtures.ts` and `VITE_API_MODE` went out with the derate port
(`7626319`), because a second code path that runs only when the coordinator is
absent is a second thing to keep honest and it is never the shape that ships.

Everything here is a mirror of something on the server, and mirrors go stale
silently. `contracts.check.mjs` is the answer to that: it verifies `types.ts`
by running the Python.

## Layout

| File | Lines | What it owns |
|---|---|---|
| `types.ts` | 2285 | every wire shape, mirrored from the frozen Python contracts; `Modality`, `isAudio`, `ENDPOINT_FOR_MODALITY`, `SPEECH_FORMATS` |
| `client.ts` | 1113 | the `Backend` interface and `httpBackend`: every HTTP call, the chat stream, the metrics SSE stream, `ApiError`, `StreamState` |
| `origin.ts` | 240 | where this browser tab sends `/api` and `/v1` — `apiUrl`, `wsUrl`, `normalizeBase`, `describeBase`, `probeCoordinator`, `setCoordinatorBase` |
| `redact.ts` | 56 | `scrub()`: key-shaped material stripped from every response on the way in |
| `keyshape.ts` | 52 | `looksLikeSecret` / `looksLikeRefName`, a port of the server's `looks_like_secret` |
| `modelcache.ts` | 31 | `folderFor` and `repoIdIsUnambiguous`, mirroring `registry/modelcache.py` |
| `contracts.check.mjs` | 211 | the verifier that runs the Python and diffs it against `types.ts` |

## `types.ts`

121 interfaces and 18 type aliases, and nothing in it is invented: every field
is one the coordinator emits. It is also where three wire-adjacent facts live
because more than one screen needs them — `isAudio(m)`, the single question the
UI actually asks about a modality; `ENDPOINT_FOR_MODALITY`, mirroring the map in
`control_plane/contracts/modality.py`, read by the cluster floor's entry plates
and by the model pane; and `SPEECH_FORMATS`, the five `libsndfile` can write,
with `aac` deliberately absent rather than offered and refused.

The comments carry the corrections. `PlanResponse.fit` is `FitResult | null`
because `internal_api.py` returns `"fit": null` when the fit port is unwired and
this type used to claim otherwise, so every render dereferenced null and blanked
the box. `LaunchActivity` used to say a launch carried no percentage and never
would; `fraction` is now null unless something measured it, and only the
checkpoint loader counting its own shards ever does.

## `client.ts`

`Backend` is the interface — roughly sixty methods, each documented with the
route it calls — and `httpBackend` is the one implementation. `req<T>` is the
funnel: `apiUrl(path)`, a JSON content-type, `204` to `undefined`, a non-2xx to
`ApiError(status, body, url)` carrying the *resolved* URL because when a base is
set, which coordinator refused is half the message. Everything that comes back
as JSON goes through `scrub`.

Three methods cannot use it, each for a different reason, and each says so where
it is. `chatStream` awaits headers rather than `res.json()` — the whole value is
in not waiting for the end of the body, and `EventSource` is GET-only so it
cannot send one. `speech` awaits the body but it is a `Blob`, and must stay
clear of `scrub`, which walks a decoded object graph. `transcribe` sends
multipart and therefore sets **no** headers at all: the boundary lives inside
`content-type`, and writing that header by hand — to anything, including the
right media type — destroys it and the server sees a body with no parts.

`ApiError.message` unwraps the gateway's OpenAI-shaped `{error:{message}}`
envelope, so the sentence the server wrote is the one a `Verbatim` renders.
`subscribe` is the metrics `EventSource` with 1s/2s/4s backoff capped at 30s,
reporting `StreamState` so the UI can grey live values during a gap instead of
freezing or zeroing them; a malformed frame is dropped and the stream kept,
which is the same rule `chatStream` applies to a malformed SSE event.

## `origin.ts`

The base every request is prefixed with, held in the browser and nowhere else.
`''` means same origin, which is the default and the deployed shape — the
coordinator serves the built assets from its own origin, so same-origin *is* the
coordinator, and `apiUrl` passes the path through byte-for-byte in that case.
The base exists for the shapes where that is false: `npm run dev` on Vite's
port, a bundle opened from disk, or one coordinator while you want to look at
another.

**It is deliberately not part of `/api/settings`.** That endpoint is on the far
side of the very connection this configures, and a setting you must already be
connected to read cannot be the one that tells you how to connect. It is stored
under `derate.coordinator-base` in `localStorage`, read once at module load
inside a `try` — that call throws in a sandboxed iframe and under some
private-browsing modes, and a UI that will not start because it could not read
an optional preference is worse than one that forgets it.

`normalizeBase` trims, supplies `http://` for a bare `host:port` (this is a LAN
coordinator; guessing https fails on the handshake with an error that says
nothing about the missing scheme), keeps a path because a reverse proxy at
`/derate` is a real deployment, and returns `null` for a leading slash —
`http:///api` has an empty host and `new URL` accepts it happily. `wsUrl` is the
one function that has to resolve same origin to an absolute URL —
`new WebSocket('/api/...')` is not a thing — so it carries the page's own
scheme with it, which is `ws://` in the deployed shape: the session is not
encrypted, and anything on it, the shell key included, is in clear.
`describeBase` and `unreachable` read `window.location` too, both only to name
an origin inside a sentence an operator reads.

`probeCoordinator` hits `/healthz`, then appends `/api/cluster` as a footnote,
because "something answered" and "there is a cluster there" are different
answers and an operator pressing Test wants the second. A failed `fetch` rejects
with a bare `TypeError` and no detail — DNS failure, refused connection and a
blocked cross-origin response are genuinely indistinguishable from the browser —
so `unreachable()` names all three plus the one this UI can cause, with the fix
spelled out: `DERATE_ALLOWED_ORIGINS=<this page's origin>`.

## `redact.ts`

`scrub(value)` walks every decoded response and replaces anything under a
key-shaped name — `api_key`, `secret`, `token`, `authorization`, `auth`,
`password`, `bearer` — with `***`. The contract says a key never leaves the
coordinator; this backs that up on the way in, so a future backend regression
dies here instead of on screen.

**There is no inverse and no reveal control, by policy.** `api_key_ref` is an
environment variable *name* and is kept, screened with `looksLikeRefName` —
the same predicate the server applied on the way in. A stricter test here
catches nothing extra, because the value already passed that one; a
`/^[A-Z][A-Z0-9_]{0,63}$/` test rendered a correctly-configured `secrets.json`
key named `my-openrouter-key` as `***`.

One credential is nevertheless rendered and it is a decision, not an oversight:
the enrollment token, inside `Enrollment.command` as part of a composed shell
line rather than a field named `token`. It is minted for one install, spent on
first use, expires within the hour and is revocable from the same card. No
endpoint returns the permanent cluster token, which is the secret this filter
exists to keep off the screen.

## `keyshape.ts`

Two predicates. `looksLikeSecret` is nine conservative patterns — `sk-or-v1-`,
`sk-ant-`, `sk-proj-`, `sk-`, `gsk_`, `hf_`, `AIza`, `bearer <tail>`, an
`api_key=`/`access_token=` assignment — plus a shape rule: 32+ contiguous
`[A-Za-z0-9_-]` mixing case and digits. `looksLikeRefName` is its exact inverse,
with an empty string allowed and 128 characters as the ceiling.

It is a port of `control_plane/redaction.py::looks_like_secret`, which
`providers/secrets.py` re-exports and which every docstring on this side still
cites by that second name. It lives here rather than in `tabs/settings/`
because it has two callers — `redact.ts` and `tabs/settings/keyfield.ts`, which
re-exports it — and `api/` must not import from `tabs/`. Drift between the two
languages is invisible to `tsc`: the UI would either warn about input the
server accepts or stay quiet about input it refuses.
`tabs/settings/keyfield.check.mjs` asserts the agreement by running the Python.

## `modelcache.ts`

`folderFor(modelId)` is the HuggingFace cache folder encoder — `models--` plus
the id with `/` replaced by `--` — mirroring `registry/modelcache.py::folder_for`.
It moved out of `tabs/storage/ModelCacheCard.tsx` so there is one copy.

`repoIdIsUnambiguous` is the interesting half, and it records a test that does
*not* work: re-encoding the decoded repo id and comparing it to the folder. Both
sides split on the first separator, so `folder_for(repo_from_folder(f)) === f`
holds for every folder there is, ambiguous ones included — it is a tautology,
not a check. The real question is how many separators the folder has. One, or
none for an org-less id like `gpt2`, admits exactly one reading;
`models--a--b--c` could be `a/b--c` or `a--b/c`, and handing a guessed id to the
planner would be deciding on a value nothing is allowed to decide on.

## `contracts.check.mjs`

The verifier that catches what types cannot. `types.ts` is a copy, and `tsc`
cannot see the original: a union that still lists five device classes typechecks
perfectly against a Python enum that grew a sixth, and the new value arrives at
runtime as one no branch handles. So the expectations are not restated here —
they are computed by running `python3 -m control_plane.contracts.manifest` and
diffing.

Three things are checked. Every `export type X = 'a' | 'b'` union parsed out of
`types.ts` against the manifest enum of the same name, values and order (the
order is not load-bearing at runtime, but a reordering is a diff worth looking
at). `TERMINAL` in `tabs/models/rows.ts` against
`control_plane.deploy.fsm.TERMINAL`. And one cross-language port:
`planShortFromDegrees` in `ui/src/format.ts` against
`gateway/internal_api.py::_plan_label`, over 36 TP/PP/EP/DP combinations,
bundled with `esbuild` so node can import the TypeScript. A Python enum with no
union of its name is reported as a `note`, not a failure — not every contract
reaches the wire. On failure it says which side is the original: edit
`types.ts`, then run `npm run typecheck` to find the call sites that cared.

### `// requires: python`, and why a skip is its own column

The first line declares what the verifier needs, and `ui/check.mjs` reads it
there rather than from a list. `npm run check` **discovers** every `*.check.mjs`
under `src/` — a hard-coded list stops covering a verifier the moment somebody
adds or renames one, which is the same failure as not having the verifier. The
declared requirement is one of `python`, `coordinator`, `browser`,
`fixtures $NAME`, or absent, which means hermetic.

**An unmet requirement is a skip, and a skip is never a pass.** It goes in its
own column with the reason printed, never folded into the passes;
`npm run check -- --strict` turns each one into a failure. Two verifiers used to
`process.exit(0)` when they could not reach a coordinator, which made "checked
nothing" and "checked everything" the same exit code.

## The seam with the rest of the UI

`state/backend.tsx` is the only module that constructs anything from here. It
puts `httpBackend` on a context alongside a `revision` counter and the
coordinator `origin`, and every screen reaches it through `useBackend()`.

```tsx
const { backend } = useBackend()
const models = await backend.models()          // GET /v1/models
```

Three details of that provider are about this folder. `origin` comes from
`useSyncExternalStore(subscribeBase, coordinatorBase, coordinatorBase)`, so
changing the base re-renders the app. A **fresh object** holding the same
methods is memoised per base — `httpBackend` is a module singleton whose methods
read the base at call time, so nothing needs rebuilding, but every effect
downstream keys on the backend's identity and without a new one the metrics
`EventSource` would go on streaming from the coordinator you just navigated away
from. And `origin` is part of every resource's cache identity: two coordinators
answering the same path are not answering the same question.

The rest of the imports are narrow. `state/useMetrics.ts` takes `StreamState`;
`inspectors/node/NodeRuntimeCard.tsx`, `tabs/models/QuantLadder.tsx` and
`tabs/models/PullCard.tsx` take `ApiError` to branch on a status.
`inspectors/node/Terminal.tsx` takes `wsUrl`, `tabs/cluster/providerLogo.ts`
takes `apiUrl`, and `tabs/settings/CoordinatorCard.tsx` takes the four an
operator's Test-and-save needs — `describeBase`, `normalizeBase`,
`probeCoordinator`, `setCoordinatorBase`, plus the `ProbeResult` type — and
never `apiUrl`. `tabs/settings/keyfield.ts` takes `keyshape`,
`tabs/storage/ModelCacheCard.tsx` takes `folderFor`. Ten files in all reach
past `api/types`; the other 76 import types only.

`toNodeState` is exported for one reason: `tabs/models/rows.check.mjs` drives
`board.ts` with real `/api/cluster` payloads and has to hand it the same shape
the app does. A second copy of that mapping written inside the verifier would
agree with any bug that came from the same reading of the schema.

## Things that look like details and are not

**Null is a question, not "use the default".** `fitQuestion` omits `context`,
`concurrency` and `on` when they are null, and `query()` returns the bare path
when nothing survived. A missing `context=` asks the coordinator to choose one
per model out of what actually fits, clamped to the model's own window; a
missing `on=` means its own host. That is the default path for the whole models
screen and the reason no field anywhere asks for either number before a model is
chosen. Sending `context=8192` when nobody asked for 8192 answers a narrower
question and prints it as though somebody had asked it. `CapacityReport.context`
is `null` on that path, and `report.context ?? 8192` would put a figure next to a
row it was not computed for.

**`historyPath` drops empty strings, except the ones that are values.** Its
`meaningfulWhenEmpty` parameter holds exactly one key so far. Absent `exclude`
means "apply the server's own quiet-logger list"; present-but-empty means "apply
none of it, show me everything the archive still holds". Dropping it with the
other blanks turns the second answer silently into the first.

**A missing reading stays missing, everywhere.** `toNodeState` passes
`power_w`, `temp_c` and `util_pct` through as null rather than defaulting to 0,
because a live-looking zero for a node that has never reported telemetry is a
claim. `numberHeader` returns null rather than 0 for an absent or unparseable
header — a provider sends neither `X-Audio-Duration-Seconds` nor
`X-Audio-Sample-Rate`, and `0 Hz` on screen would be a statement about the audio.
`chatStream`'s `textFrames || null` says the same thing about a turn where
nothing was observed.

**`X-Request-Id` is read before anything can throw on the body.** A refusal
carries the id too, and it is the only handle on the row that recorded the
refusal — which is why `onOpen` fires at headers rather than at first token, and
is the only way a caller learns the id for a turn that goes on to fail.

**`quantTable()` fetches the byte figures instead of re-typing them.** A second
copy of that table is a second answer, and the one that disagrees with the fit
gate is the one that gets somebody an OOM.

**Aborts are checked by name, not by `instanceof DOMException`.** `isAbort`
tests `e.name === 'AbortError'`, which holds across every runtime this can run
in. Stop is an outcome, not an error: `chatStream` returns `meta(true)` and the
partial text stands.

## Failure behaviour

- **Non-2xx.** `ApiError` with the status, the raw body and the resolved URL.
  Its message is the gateway's own `{error:{message}}` sentence when the body
  parses that way — the 501 explaining why a daily spend cap cannot be enforced,
  the unknown-voice refusal that lists what *is* installed — and falls back to
  `${status} on ${path}: ${body}` when it does not.
- **A dropped metrics stream.** `onState({status:'stale', since, retryInMs,
  attempt})`, reconnecting at 1s, 2s, 4s… capped at 30s. A coordinator restart
  is picked up quickly; a coordinator that is gone is not hammered.
- **A malformed frame**, on either stream. Dropped, stream kept. A parse failure
  is not a disconnect.
- **An aborted request.** `chatStream` resolves with `stopped: true` rather than
  throwing. `probeCoordinator` reports `Nothing answered within 6 s.`
- **An unreachable coordinator under Test.** One sentence naming all three
  causes the browser refuses to distinguish, plus the CORS remedy when the base
  is cross-origin. Never a stack trace.
- **`localStorage` unavailable.** The base still applies to this tab; the next
  reload goes back to same origin. Nothing fails to start.
- **A field a stub or an older gateway does not send.** Optional on the type,
  and the fallback is decided in one place — `modality` is left absent on the
  wire so `isAudio` is the only thing that decides it means `text`.

## Deliberately not built

**A fixture backend.** `api/fixtures.ts` and `VITE_API_MODE` were deleted in
`7626319`. The coordinator is always there in the deployed shape, and
`state/backend.tsx` is live-only with no probe and no fallback, so every
resource stops asking "is it resolved yet" and asks only "did this poll
succeed".

**An inverse of `scrub`.** No unredact, no reveal control, no "show key" toggle.

**The coordinator base as a server setting.** It cannot be, and the reason is
stated above: `/api/settings` is on the far side of the connection it would
configure.

**`stream_options: {include_usage: true}`.** Not every upstream in
`ProviderKind` accepts it, and a request refused for an unknown field is worse
than a token count labelled as an estimate — so `chatStream` counts frames and
prefers a real `usage.completion_tokens` whenever one turns up.

**`speed` and `stream` on `/v1/audio/speech`.** The server refuses the first, a
resample moves the pitch, and it has never had the second.

**`probeCoordinator` as a `Backend` method.** It probes a *candidate* base
rather than the configured one, which is the entire point of testing before
saving.
