# Send requests to the one endpoint

Every model the cluster serves — one you launched on your own machine and one
that lives on somebody else's API — answers on a single base URL in the OpenAI
shape. This page is derate from a client's side: what is on `/v1`, what a
refusal looks like, and how the gateway decides which machine answers. You need
a coordinator you can reach and at least one model serving.

## Every request carries a key

`/v1` answers only to an API key, sent as a bearer token:

```
Authorization: Bearer drt_<key_id>_<secret>
```

The header is the only place a key is read, the `/v1/realtime` websocket
included. There is no query-string form: a key in a URL lands in access logs.

**Minting one.** Setup's first step makes a key named `default`, with a
365-day expiry, and shows its secret once — see
[Your first visit](first-run.md). After that, open **Keys**, press create,
choose a name, an org and an expiry (1, 10, 20, 30, 50, 100 or 365 days), and
copy the secret from the dialog before pressing **I saved it**. Signed in, the
same thing is `POST /api/keys`:

```bash
curl -s -b cookies.txt http://<coordinator>:8080/api/keys \
  -H 'content-type: application/json' \
  -d '{"name": "laptop", "expiry_days": 30}'
```

The secret comes back as `secret_once` in that response and nowhere else, ever:
derate stores a hash of it. `POST /api/keys/{key_id}/rotate` issues a new
secret the same way and kills the old one on its next use. Lost a secret?
Rotate; there is nothing to look up.

**A missing or refused key is a `401`** with
`WWW-Authenticate: Bearer realm="derate"` and one of four codes:

| `code` | Means |
|---|---|
| `missing_api_key` | no `Authorization: Bearer` header |
| `invalid_api_key` | does not parse, unknown id, wrong secret, or the key or its org was deleted |
| `api_key_disabled` | an admin disabled it (re-enable on **Keys**) |
| `api_key_expired` | past `expires_at`; expiry is not editable, so create or rotate |

If the key store itself cannot be opened, every gated route answers `503`
`auth_unavailable` rather than letting anything through.

`/api` — the screens' own surface — takes an admin session cookie instead of a
key, and answers `401` `session_required` without one. A write carrying an
`Origin` header that disagrees with the coordinator's own address is still
refused on both surfaces; `curl` and the SDK send no `Origin`.

### `GET /v1/key`

What the calling key is and how much it has left:

- **`key`** — its id, name, org, status, expiry, limits and allowlist. Never
  the secret or its hash, and not the admin's notes.
- **`org`** — `org_id` and `name`.
- **`expires_at`** — epoch seconds, or `null` for a key that never expires.
- **`allowed_models`** — the effective allowlist: the key's own list, else
  the org's, else everything `/v1/models` exposes now.
- **`limit_states`** — `key` and `org`, one entry per configured limit:
  `limit_type`, `limit`, `effective_limit`, `used`, `remaining`, `resets_at`,
  `fraction`, `near` (≥ 80 %), `at`.
- **`cycle`** — this 30-day cycle's `start`, `resets_at`, and its `usage`
  (`null` when there is no telemetry archive).

### Limits

A key, and the org it belongs to, can each carry limits; a key's value may not
exceed its org's. Cycles are 30 days, anchored at the key's (or org's)
creation. The check runs before dispatch and stops at the first refusal:

**`403` `model_not_allowed`** — the model is outside the key's effective
allowlist.

**`400` `max_output_tokens_exceeded`** — `max_tokens` is above
`max_output_tokens_per_request` (the tighter of the key's and the org's); the message names the cap. A request with no
`max_tokens` at all is sent with the cap filled in instead.

**`413` `max_prompt_tokens_exceeded`** — the prompt estimate is above
`max_prompt_tokens_per_request`.

**`429` `limit_exceeded`** — a rate (`rpm`, `tpm`, `max_concurrent`) or a cycle
limit (`requests_cycle`, `tokens_total_cycle`, `budget_usd_cycle`, …) is used
up:

```json
{
  "error": {
    "message": "…",
    "type": "rate_limit_error",
    "code": "limit_exceeded",
    "limit_source": "key:requests_cycle",
    "used": 5,
    "limit": 5,
    "resets_at": 1765929600.0
  }
}
```

`limit_source` is `<scope>:<limit_type>` — `key:` or `org:`, then the limit's
name, per-model ones included (`org:model:gpt-4o:rpm`). The headers say the
same: `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and `X-RateLimit-Reset` in
epoch seconds — the window's end for `rpm`/`tpm`, now for `max_concurrent`, the
cycle's end for a cycle limit.

A key with `cloud_fallback_local_model` set does not get that 429 when a
provider-served request hits `cloud_tokens_cycle`, `cloud_usd_cycle` or
`budget_usd_cycle`: the request is answered by that local model instead.

## What is on `/v1`

```
GET  /v1/models
POST /v1/chat/completions
POST /v1/completions
POST /v1/embeddings
POST /v1/audio/speech
POST /v1/audio/transcriptions
GET  /v1/audio/voices
GET  /v1/key
GET  /v1/realtime               websocket
```

Same origin as the UI, same port. `http://<coordinator>:8080/v1` is the base
URL you give a client.

## 1. Ask what is being served

```bash
curl -s http://<coordinator>:8080/v1/models \
  -H "Authorization: Bearer $DERATE_API_KEY"
```

```json
{
  "object": "list",
  "data": [
    {
      "id": "Llama-3.1-8B-Instruct",
      "object": "model",
      "created": 1757289600,
      "owned_by": "derate",
      "context_length": 16384,
      "target_count": 2,
      "target_kinds": ["local", "remote"],
      "modality": "text"
    }
  ]
}
```

The first four fields are the standard OpenAI ones, so a client that has never
heard of derate works unmodified. `owned_by` is always `"derate"` — whoever
actually runs the weights, this is the endpoint you asked.

The last four are extra, and a client that does not know them ignores them:

- **`context_length`** — what this name will accept, in tokens.
- **`target_count`** and **`target_kinds`** — how many places can answer to this
  name, and whether they are `local` (a deployment on your hardware) or
  `remote` (a provider). Two targets under one name is what makes failover and
  spill-to-cloud possible; see [Routing](#routing-which-machine-answers) below.
- **`modality`** — which endpoint family accepts this name: `text`,
  `embedding`, `speech` or `transcription`.

**The `id` is the name you send as `model`, and it is not always the repository
id.** A deployment derate launched is served under the tail of the repository:
`meta-llama/Llama-3.1-8B-Instruct` is served as `Llama-3.1-8B-Instruct`. A
model on a provider answers to the id that provider publishes, so an OpenRouter
model keeps its vendor prefix — `meta-llama/llama-3.1-8b-instruct`. Whatever is
in `id` is what goes in the request.

A model that is still launching is not in this list. It is not missing, and the
error you get if you send to it anyway says which.

## 2. Send a request

```bash
curl http://<coordinator>:8080/v1/chat/completions \
  -H "Authorization: Bearer $DERATE_API_KEY" \
  -H 'content-type: application/json' \
  -d '{"model": "Llama-3.1-8B-Instruct",
       "messages": [{"role": "user", "content": "Name the measured link speed."}]}'
```

Add `"stream": true` for server-sent events. Streaming is honoured on
`/v1/chat/completions` and `/v1/completions`; the other routes answer in one
piece.

The same thing through the OpenAI SDK — `base_url` and `api_key` change:

```python
from openai import OpenAI

client = OpenAI(base_url="http://<coordinator>:8080/v1", api_key="drt_<key_id>_<secret>")

for model in client.models.list():
    print(model.id)

answer = client.chat.completions.create(
    model="Llama-3.1-8B-Instruct",
    messages=[{"role": "user", "content": "Name the measured link speed."}],
)
print(answer.choices[0].message.content)
```

`api_key` is the `drt_` key itself; the SDK sends it as the bearer token. An
SDK that reads `OPENAI_API_KEY` from the environment works with the key put
there.

**Every answer carries `X-Request-Id`**, shaped `r-4f2a9c8d1e77`. It is minted
before the first thing that can refuse, so a 404 has one as readily as a 200,
and every target a single request was offered to shares it. Quote it when
something answered oddly.

### Embeddings

```bash
curl http://<coordinator>:8080/v1/embeddings \
  -H "Authorization: Bearer $DERATE_API_KEY" \
  -H 'content-type: application/json' \
  -d '{"model": "Llama-3.1-8B-Instruct", "input": "the measured link"}'
```

Text and embedding are deliberately not treated as exclusive: one vLLM server
answers `/v1/chat/completions` and `/v1/embeddings` from the same weights, so
derate does not refuse a text model on the embeddings route. Audio is the axis
that is genuinely a different endpoint — see below.

### Audio

```bash
curl http://<coordinator>:8080/v1/audio/speech \
  -H "Authorization: Bearer $DERATE_API_KEY" \
  -H 'content-type: application/json' \
  -d '{"model": "audio8-tts", "input": "The link is measured, not assumed.",
       "response_format": "mp3"}' --output line.mp3
```

```bash
curl http://<coordinator>:8080/v1/audio/transcriptions \
  -H "Authorization: Bearer $DERATE_API_KEY" \
  -F model=whisper-1 \
  -F file=@meeting.m4a
```

The transcription route is the only one here that is not JSON. The upload is
forwarded byte for byte, boundary and all — nothing re-encodes a 25 MiB file in
order to change nothing about it.

`GET /v1/audio/voices?model=<served name>` lists the reference voices a
text-to-speech deployment has installed, and the ones it skipped. It is not an
OpenAI route; it exists because an unknown voice is refused by design and a
caller that cannot enumerate them has to guess at the one thing the server will
not let them guess at. It answers for deployments this cluster runs and not for
a provider:

```
'tts-1' is served by a provider, and a voice library is a property of a deployment this cluster runs: the reference clips live on the node, in the directory the tts runtime was pointed at. Deploy it here to choose a voice, or send /v1/audio/speech without one and the model speaks in its own.
```

### `/v1/realtime`

A websocket, opened with the model in the query string and the key in the
`Authorization` header:

```
ws://<coordinator>:8080/v1/realtime?model=gpt-4o-realtime-preview
```

A missing or refused key closes the handshake with `1008` and the 401 code as
the reason.

Frames are relayed both ways without being parsed, to a provider that already
speaks the realtime protocol. This is the one route with no failover in it: a
session cannot be re-offered to another target halfway through, because the
upstream holds conversation state derate never saw.

It is also the one route that will not use a model on your own hardware.
Faking a realtime session out of local speech-to-text, chat and text-to-speech
means implementing thirty-odd event types, server-side voice activity
detection, buffering and barge-in, and none of that exists here. So a local
model is refused during the handshake rather than accepted into a session that
would never produce audio:

```
'Llama-3.1-8B-Instruct' is served locally, and this build relays realtime sessions to a provider that implements them rather than composing one from local deployments
```

Refusals arrive as close code `1008` with that reason. `1011` means the
upstream itself could not be reached.

## Naming a model on the wrong endpoint

Send a speech model to the chat route and the answer names the route that would
have worked:

```json
{
  "error": {
    "message": "The model 'tts-1' is a speech model and cannot serve /v1/chat/completions, which requires a text model. Send it to /v1/audio/speech instead.",
    "type": "invalid_request_error",
    "param": null,
    "code": "wrong_modality",
    "model_modality": "speech",
    "endpoint_modality": "text",
    "correct_endpoint": "/v1/audio/speech"
  }
}
```

"No such model" would be a lie — the model exists and is serving — and a bare
refusal leaves you thinking you mistyped the name.

**This matters even if you never serve audio.** A provider's catalogue is
ingested wholesale. Add an OpenAI key and `tts-1` and `whisper-1` arrive as
ordinary models alongside the chat ones; add OpenRouter and several hundred
names arrive at once. Without a modality on each of them they would show up in
your client's model picker as though they were chat models, and the first thing
you would learn about the difference is a confusing 400 from somebody else's
backend. The `modality` field on `/v1/models` and this refusal are the same
fact, said before and after the request.

## What can go wrong here

Every one of these is derate's own answer. An error that came from a
backend — vLLM refusing a parameter, a provider's 401 — is passed through
untouched: status, headers and bytes unchanged, with nothing of derate's added
on top.

**`400` — `You must provide a 'model' parameter.`** No `model` key in the JSON
body, or no `model` field in the transcription form.

**`404` — `The model 'llama3' does not exist. Available models:
'Llama-3.1-8B-Instruct', 'gpt-4o-mini'.`** The list is the same one
`/v1/models` reports, so the fix is in the message. The body also carries
`available_models` as an array.

**`503` — `The model 'Llama-3.1-8B-Instruct' is not ready to serve. Current
state: launching.`** It exists; it cannot answer yet. Comes with
`Retry-After: 5` and `deployment_states`. The three states you can see here
are `planned`, `launching` and `stopping` — `degraded` is not one of them, a
degraded deployment is serving more slowly than it should and routing around it
entirely would take a working model out of rotation.

**`503` — `No target for 'Llama-3.1-8B-Instruct' is currently admitting
requests.`** Every target for this name is unhealthy, draining, over a
provider's daily budget, rate limited by an upstream, or under critical memory
pressure. `Retry-After: 5`. This is a refusal rather than an unbounded queue on
purpose.

**`502` — `No upstream for 'Llama-3.1-8B-Instruct' is reachable: <cause> after
trying 2 targets.`** No backend was reached at all, so there is no upstream
status to report and derate does not invent one. The count is how many targets
it offered the request to.

**`503` — `The gateway has no free upstream connection for
'Llama-3.1-8B-Instruct' (tried 1 target). Every connection is held by a request
already in flight. Retry shortly; the backends themselves are not
implicated.`** Deliberately not the 502 above. Nothing was learned about the
backend here — the request never got as far as dialling one — and answering
"no upstream is reachable" for a machine answering in under a millisecond sends
you to the wrong place.

**`413` — `The request body is larger than the 25 MiB limit.`** or `The upload
is larger than the 25 MiB limit.` The body is read as it streams and dropped at
the limit rather than buffered in full and then rejected.

**`400` — `Request body is not valid JSON: <reason>.`** and `Request body must
be a JSON object.`

**`400` — `This endpoint expects a multipart/form-data upload carrying 'file'
and 'model'.`** Sending JSON to `/v1/audio/transcriptions`.

## Routing: which machine answers

One name on `/v1/models` can have several targets behind it — two replicas of a
model on two machines, or a local deployment and a provider serving the same
name. `target_count` and `target_kinds` say how many and of what kind. Which
one answers is decided per request, by a policy chosen per served name.

**Nothing outranks admission.** A target that is not admitting — memory went
critical, it is draining, an upstream is rate limiting it — is filtered out
before any policy runs, round robin included. If nothing is admitting, the
answer is the 503 above rather than a queue.

There are seven policies. These are the descriptions the product shows beside
the picker:

| Policy | What it does |
|---|---|
| `least_outstanding` | Fewest in-flight wins. Accounts for a replica mid-prefill. |
| `round_robin` | Even rotation. Ignores that a replica may be mid-prefill. |
| `weighted_capacity` | Share proportional to measured throughput. |
| `cache_affinity` | Repeat prompt prefixes stick to the same local target. Remote targets are excluded — a remote runtime's cache cannot be reasoned about. |
| `failover` | Everything to the primary target; switches only when it stops admitting. |
| `local_first` | Remote targets take traffic only once every local slot stops admitting. |
| `cost_aware` | Cheapest admitting target. Local priced from measured draw. |

**You do not have to choose one.** The coordinator picks per served name and
says why, in one of three sentences:

```
served both locally and remotely; the cluster is preferred and the remote provider is the overflow valve
```

```
local replicas differ in measured capability by 34%, above the 25% threshold for an even split
```

```
targets are close enough in capability that sending each request to the least-busy one balances load well enough on its own
```

The first picks `local_first`, the second `weighted_capacity`, the third
`least_outstanding` — which is what a plain cluster of similar machines gets.
A name served both locally and remotely takes the first even when the second
would also apply: a mixed fleet is a spill decision before it is a balance
decision.

To see it and change it, select a deployment — click it in the Dashboard's
deployment strip, or on the cluster graph, which puts `?dep=<name>` in the URL
— and read the **Routing** section in the right-hand rail. It is a dropdown of
the seven, the auto reason underneath while nothing has overridden it, and one
bar per target. Under `local_first` it also says which way traffic is going
right now: `Serving locally`, or `Spilled to remote: every local target is
saturated`.

### The two timescales

This is worth knowing before you watch a node get hot.

**Weights are recomputed on a 60-second cadence.** They come from measured
capability, so a node that starts thermally throttling sheds its share on its
own, within a minute, with nobody touching anything.

**Outstanding counts and the admitting flag are re-read on every selection.**
So a critical memory event takes effect on the *next request*, not at the next
refresh. Memory pressure is derived from live node telemetry twice a second,
which is what makes "stops admitting within a second" true even when nothing
reports the event explicitly.

### When a target fails mid-request

A request that fails on one target is offered to another, up to 2 attempts,
inside a 30-second budget. If the first target dies after the client has been
committed to but before a single body byte has gone out, a second target may
finish the response — once, and only if its content type matches what was
already sent. A body of a different type underneath headers already on the wire
would be corruption, not recovery.

---

**Next:** [Put somebody else's API behind the same endpoint](providers.md) —
OpenRouter, OpenAI, an Ollama box on the LAN, where the key goes, and why a
provider you just added is serving nothing.
