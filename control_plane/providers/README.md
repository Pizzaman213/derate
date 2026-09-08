# Providers (Agent I)

Remote OpenAI-compatible upstreams as first-class route targets, alongside local
deployments. OpenRouter, OpenAI, Together, Groq, an Ollama box on the LAN, or
anything with a base URL and a bearer token.

The point is not to be a proxy service. The point is that the cluster is the
default and a paid API is the overflow valve.

## The seam with Agent G

```python
from control_plane.providers import ProviderService, build_stub_service

providers = build_stub_service()          # day 0, no real key needed
providers = ProviderService()             # real, reads /data

# /v1/models -- allowlisted; a model nobody switched on is not a route target
for provider_id, model in providers.models():
    ...                                    # model.served_name is the client-facing id

# routing
targets = providers.route_targets()                    # kind=REMOTE, admitting set
by_name = providers.route_targets_by_served_name()     # merge with local targets

# serving a request
provider_id, upstream_id = ProviderService.split_target_id(target.target_id)
async for chunk in providers.forward(provider_id, upstream_id, body, stream=True):
    yield chunk

# when you want the upstream's status code and content type as well
async with providers.open_upstream(provider_id, upstream_id, body, True) as up:
    return StreamingResponse(up.body, status_code=up.status_code,
                             media_type=up.media_type)
```

`forward` raises before the first byte, never after:

| Exception | Gateway should return |
|---|---|
| `UpstreamError` | `.status_code` and `.to_openai_error()` verbatim |
| `ProviderNotAdmittingError` | 503, with `.retry_after_s` when set |
| `MissingKeyError` | 503; the provider is disabled and `.api_key_ref` names the fix |
| `UnknownProviderError` | 404 |
| `AdapterUnsupportedError` | 501; the message says to use OpenRouter instead |

`route_targets()` leaves `strength` and `weight` at zero. Strength is a
normalized score over local hardware and we have not measured a remote; Agent G
owns both fields. `cost_per_mtok` is a blend of published input and output
pricing weighted toward output, and is `None` when the provider publishes
nothing, so `COST_AWARE` skips the target rather than treating it as free.

## A provider serves what it was asked to, not what it publishes

OpenRouter publishes several hundred models. Ingesting that wholesale made
every one of them a servable name and a row in the Models tab the moment a key
was added, which is a wall nobody chose and a freeze point on the screen.

`ProviderRuntime.enabled_models` is the allowlist, and `runtime.serves(id)` is
the only thing that reads it. A new provider starts with nothing switched on.

Three states, and the third is the one that matters:

| value | means |
|---|---|
| `frozenset()` | serves nothing. What a newly added provider gets. |
| `frozenset({...})` | serves exactly these. |
| `None` | record predates the allowlist; serves its whole catalogue. |

`None` is not folded into "everything I can currently see" at load, because the
persisted model list is empty on any record written before its first successful
refresh -- and pinning such a provider to serving nothing, permanently, for
having been saved at the wrong moment is a worse failure than the tri-state.

`servable()` is the filtered view and the seam that makes this real: the
gateway builds its target index from it and `GET /api/providers` renders it, so
switching a model off removes it from `/v1/models`, the chat picker, the models
list, routing and the cluster graph together rather than one at a time.
`list()` still returns the whole catalogue -- something has to, or nothing
could offer the operator a model to switch on.

A requested id is checked against what the provider actually publishes.
That is the guard, and a stronger one than the key screens the free-text fields
get: an id has to have come from the upstream, and no API key ever will have.
It is also the only guard that works here -- `_screen_free_text`'s entropy
fallback is what refused all 96 variants of Mistral-Small-24B-Instruct-2501,
and a model id is exactly the shape it gets wrong.

## The seam with Agent H

`public_list()`, `public_dict(id)` and `public_models(id)` return the JSON the
gateway's own `serialize.py` draws on for `GET /api/providers`: health,
admission, spend, any budget, and two counts -- `model_count` for what is
served and `catalogue_count` for what is published, because one number cannot
express "2 of 312". `kinds_public()` gives the add-form defaults so adding
OpenRouter is a name and either a key or a key reference.

`catalogue(id)` backs `GET /api/providers/{id}/models` and is the **one
provider surface the allowlist does not filter**, because it is the one the
allowlist is chosen from: every model, each with an `enabled` flag.

`api_key` is always `"***"`. There is no reveal control and nothing for one to
call. `api_key_ref` is shown, because it is a name, and showing it is how
someone fixes a provider whose reference does not resolve.

## Keys

`api_key_ref` is the **name** of an environment variable or of a key in
`/data/secrets.json` at mode 0600. Environment wins. Values are resolved at
request time and nowhere else.

A key can also be *given*, as `api_key` on an add or update spec. It is a
request field and nothing more: `_register` reads it, hands it to the redactor
before any screen below can raise, and — after every other screen has passed,
so a refused spec leaves no secret behind — writes it through
`SecretStore.put`. What lands on the record is the name. With no
`api_key_ref` beside it the name is minted as `DERATE_<PROVIDER_ID>_API_KEY`,
which is also what marks the entry as ours to delete when the provider is
removed; an operator's own name is never deleted. Because the environment is
read first, writing to a name an environment variable already holds with a
different value is refused — otherwise the stored key would be shadowed at
resolution and the provider would authenticate as somebody else.

Three layers keep them out of everything else:

1. Nothing but the reference is ever stored. `providers.json` has no key field.
2. `Redactor` scrubs every resolved value, and anything matching a known key
   shape, out of error messages, log records and response headers. Upstreams do
   quote credentials back at you in 401 bodies.
3. `assert_no_key_material` runs over every serialized response and every disk
   write, and raises rather than emitting.

`tests/unit/test_providers.py::test_no_key_material_in_any_output` asserts this
across every response shape the package emits. If that test is missing, the
feature is not done.

## Deliberately not built

**Native Anthropic.** The Messages API differs in request shape, system-prompt
placement and streaming events. Adding an Anthropic provider works for
discovery and health, and `forward` refuses with an `AdapterUnsupportedError`
naming OpenRouter as the route. That is a day of work OpenRouter already solves.

**Invented pricing.** Only OpenRouter and Together publish costs in their model
lists. Everyone else leaves `input_cost_per_mtok` and `output_cost_per_mtok`
as `None`, and `public_dict` reports `unpriced_requests_today` so a zero spend
is not misread as free.

## Failure behaviour

- **Missing key reference**: provider is disabled with a `last_error` naming the
  reference. Startup is never blocked. It re-enables itself when the reference
  resolves again.
- **429**: `Retry-After` when present, honoured up to a 300s DoS guard
  (`RETRY_AFTER_MAX_S`); otherwise exponential from 1s to a 60s cap
  (`BACKOFF_MAX_S`). `admitting` goes false for the window; `healthy` stays
  true, because rate limited is busy, not broken.
- **5xx**: one jittered retry, then unhealthy, with an exponential
  `failure_backoff_until` from 1s to the same 60s cap.
- **Unreachable** (connect/read/DNS): unhealthy, same exponential backoff.
- **401/403**: unhealthy immediately, no retry, `last_error` names the reference.
  The one unhealthy state that never expires -- `auth_rejected` latches it,
  because retrying a key the upstream refused burns quota and can get an account
  locked, and waiting does not turn a wrong key into a right one.
- **Failed model refresh**: keeps the cached list and sets `last_error`. Stays
  healthy and keeps serving, because a model list is not an inference path.
- **Cold start**: reads the cached model list from disk. No network needed.

Unhealthy is a **timed** state, not a latch. Routing selects on `healthy`
(`gateway/targets.py`), so leaving it false forever made the block prevent its
own cure: an unhealthy provider was never offered a request, and a successful
request was the only thing that could clear it. The only other way out was the
model-list refresh, `PROVIDER_REFRESH_S` -- six hours -- away, and on 2026-09-08
one transient `ReadTimeout` took an Ollama box off the air for a working day
while it answered pings in 11 ms.

So once the backoff expires `_sync` calls `half_open`, which stops *asserting*
the provider is down and lets one request settle it. `last_error` deliberately
stays readable while the probe is out: this is the admission that we no longer
know, not a claim of recovery. A success clears everything; another failure
backs off further. The stampede in that window is bounded by the gateway's own
circuit breaker, which covers remote targets exactly as it covers local ones.

`failure_backoff_until` is kept apart from the 429 `backoff_until` on purpose:
`rate_limited()` reads the latter, so folding the two together would have an
unreachable box reporting itself as throttled by an upstream it never reached.
`retry_in()` returns the later of the two, which is the one honest answer to
"how long".

## Layout

Everything above argues the design. This table and the sections under it are the
per-file map: what each of the fourteen modules owns, and the failure that
shaped it.

| File | Lines | What it owns |
|---|---|---|
| `service.py` | 1437 | `ProviderService`: registry, discovery, forwarding, health, spend, route targets |
| `logos.py` | 436 | a provider's own mark, fetched coordinator-side and cached with negative entries |
| `runtime.py` | 337 | per-provider live state: health, the two backoffs, spend, in-flight, the allowlist |
| `discovery.py` | 286 | an upstream model list turned into `ProviderModel`, filling in nothing unpublished |
| `stub.py` | 226 | the day-0 service — the real one, over `httpx.MockTransport` |
| `store.py` | 224 | `providers.json`: references, never values, redactor-checked before every write |
| `detect.py` | 223 | probing a node already in the roster for a runtime it is already running |
| `secrets.py` | 191 | `SecretStore`, and the re-export seam onto `control_plane/redaction.py` |
| `kinds.py` | 189 | the per-kind table: base URL, auth style, pricing unit, what a kind can be asked |
| `usage.py` | 125 | reading a response's `usage` block without ever buffering a stream |
| `serialization.py` | 114 | the public JSON shapes; `api_key` is present and always `"***"` |
| `errors.py` | 102 | the exception set the status table above maps from |
| `config.py` | 83 | timeouts, backoff bounds, the refresh interval, the cost blend weights |
| `__init__.py` | 74 | the package's export surface, 25 names |

## `service.py`

`ProviderService` is the front door for all of it: the registry (`add`,
`add_async`, `update`, `remove`, `list`, `get`), discovery (`refresh_async`,
`refresh_all_async`), the public shapes (`public_list`, `public_dict`,
`public_models`, `catalogue`), routing (`route_targets`,
`route_targets_by_served_name`, `target_id`/`split_target_id`, `blended_cost`)
and the request path (`open_upstream`, `forward`, `forward_target`). `start()`
runs the refresh loop; `aclose()` flushes unpersisted spend *before* it cancels
the task.

Two constructions are not obvious from the call sites. `_client()` keys one
`httpx.AsyncClient` per event loop, because `add()` and `refresh()` are
synchronous to satisfy `ProviderPort` and `list_backends()` is a synchronous
wrapper for a caller with no loop at all — `_run_sync` gives each of them a
worker thread with a loop of its own. And `_install_redacting_filter`
walks the logging manager's registry, attaching to every logger already created
under this package's prefix: a `logging.Filter` on a `Logger` runs only for
records logged through *that* object and is never consulted for its children, so
attaching it to `logging.getLogger(__package__)` alone leaves it inert for every
`getLogger(__name__)` in the package — which is all of them.

`_sync()` is where a failed provider goes half-open before any caller reads it;
the reasoning is in **Failure behaviour** above and is not repeated here.

### The key path

`_register` reads a pasted `api_key` and hands it to `redactor.remember` before
any screen below can raise, because the route handler formats an exception's
text into its response and the redactor can only scrub a value it already holds.
The write is last, after every other screen has passed, so a refused spec leaves
no secret behind. `_store_key` refuses to write to a name an environment
variable already holds with a different value: `SecretStore.get` reads the
environment first, so the key would be stored and then shadowed, and the
provider would authenticate as somebody else. `minted_ref` builds
`DERATE_<PROVIDER_ID>_API_KEY`, capped at `_MAX_REF_LEN` (64) because
`ui/src/api/redact.ts` renders anything longer as key material — derate's own
reference, hidden from the operator as though it had leaked. That prefix is also
the only thing that makes `remove` safe to delete a secret: an operator's own
reference may be shared and is never touched.

### The forward path

`_prepare` runs admission and builds the request; `open_upstream` opens it and
raises before the first byte; `forward` is the plain byte-stream form over the
top. Three details in `_prepare`:

- `stream` is injected only for `chat/completions` and `completions`
  (`_accepts_stream`). `/v1/audio/speech` has no such field, and a strict
  upstream answers 400 for an unknown one — injecting it unconditionally would
  break every audio request that ever reached a provider.
- `stream_options.include_usage` is asked for only where the kind is known to
  accept the field and the request can be priced. `meters_cost` counts as
  priced on its own, because a kind that reports its own cost reports it for
  models whose published `pricing` reads `-1` for "varies" — gating on the rate
  card alone would decline to ask for usage on exactly the models that cannot be
  priced without it.
- `serves()` is re-checked here even though the routing index already picked the
  target. That index is cached for its own TTL, and the difference between a
  dict lookup and skipping it is a billed call to a model nobody switched on.

`open_upstream` retries a 5xx once with `jittered_delay()`, feeds every chunk
through a `UsageSniffer` on its way out, calls `note_success` and records spend
only after the body has been consumed, and decrements `runtime.outstanding` in a
`finally`. `_safe_headers` drops the hop-by-hop set plus `authorization`,
`x-api-key` and `set-cookie`, and scrubs what is left.

## `logos.py`

The coordinator fetches a provider's mark; the browser never does. `LogoCache`
is two tiers — an in-process dict in front of a directory, the way
`resolver/cache.py` does it — and remembers a miss as emphatically as a hit,
which is the whole reason it is not a plain dict: without negative caching a
vendor with no favicon is a network request every time the Cluster tab repaints.
`miss_kind()` keeps `"permanent"` and `"transient"` apart so a rate limit is
never reported as "this vendor has no mark"; `is_fresh_miss()` collapses them,
because its only question is whether to fetch now.

`_BRAND` holds full URLs rather than an origin plus a guessed path, because
guessing does not work. Every line was probed by hand on 2026-09-07:
`api.openai.com/favicon.ico` 404s, `openai.com/favicon.ico` 403s behind
Cloudflare, `together.ai/favicon.ico` answers 404 with 222 KB of SPA HTML, and
`ollama.com` keeps its mark under `/public`. `sniff()` decides the type from the
bytes and never from the header; `fetch_image` enforces `MAX_LOGO_BYTES`
(256 KB) *while* reading and refuses anything outside `_ALLOWED_TYPES`. No
credential is ever sent — a public favicon does not need one. `fetch_image` is
public because `resolver/avatars.py` fetches the same kind of thing from the
same kind of place, and the three rules that make that safe should have one
implementation.

## `runtime.py`

`ProviderRuntime` is the mutable half of a provider, and `store.py` persists
only part of it. It carries `healthy`, `last_error`, the two backoffs, the
`auth_rejected` latch, `daily_budget_usd`, `aliases`, `backend_pins`,
`outstanding` and the day-keyed `spend` map. `serves()` is the only reader of
the `enabled_models` tri-state, so there is one place that decides what `None`
means. The transitions — `note_success`, `note_rate_limit`, `note_server_error`,
`note_auth_failure`, `note_transport_error`, `half_open` — are the state machine
the failure list above describes; `retry_in()` returns the later of the two
waits because a screen asking "how long" wants one number.

`record_usage` prefers the provider's own `metered_cost_usd` outright: that is
what the account was charged, where the rate-card arithmetic is a forecast. A
metered request is never counted unpriced, even at 0.0 — a free model answering
costs nothing, and that is knowledge, not a gap. `prune_spend` keeps 30 days.
`parse_retry_after` accepts both a delay and an HTTP date.

## `discovery.py`

`parse_models(payload, spec, aliases=...)` normalizes what a provider publishes
and fills in nothing it does not: `CONTEXT_UNKNOWN` is 0, `_supports_tools` is
true only where the upstream says so, and `_pricing` scales OpenRouter's
per-token figures to per-million while dropping `-1`, which is "varies" and not
a price. `_MODALITY_PATTERNS` only ever moves a model *off* the `TEXT` default,
narrowest family first — "whisper" before "speech", or every ASR model would
classify as TTS — and the same table clears the streaming flag, since a speech,
transcription or embedding endpoint has no token stream to open.

`recognized_envelope` separates "a catalogue we could not parse" from "a
catalogue that is genuinely empty". Ollama with nothing pulled answers
`{"object": "list", "data": null}`, a well-formed empty catalogue wearing a
null, and calling that unrecognized marks the server broken at the exact moment
the operator is about to pull onto it. `parse_endpoints` is OpenRouter's
per-model backend list, keyed by the `tag` its request-time `provider` field
expects.

## `stub.py`

`build_stub_service()` is the day-0 service and is not a mock object: it is the
real `ProviderService` driven through an `httpx.MockTransport`, so discovery,
streaming, usage accounting, backoff and redaction all run the code that runs in
production. Only the network is fake. Its key is a reference like any other,
pointing at a value in a 0600 secrets file inside the stub's own data directory
— nothing about the secret path is special-cased, which is the point. It then
calls `update` to switch the whole stub catalogue on, deliberately: a real
provider starts with nothing enabled and waits for somebody to choose, and there
is nobody here to choose, so a stub serving nothing would rehearse the empty
case instead of the one being built. `sse_chunks` and `completion_body` are the
canned OpenAI shapes, three priced OpenRouter models behind them.

## `store.py`

`provider_to_dict` has no key field at all, and `save()` calls
`redactor.assert_clean` on the serialized text before it touches disk — the
guarantee enforced rather than assumed — then writes through a hardened temp
file and `os.replace`. `healthy` and `last_error` are written for a human reading
the file and deliberately *not* restored: after a restart there has been no
interaction, and restoring "unhealthy" leaves a provider not admitting, which
means no request reaches it, which means nothing ever clears it. The durable
condition, an unresolvable reference, is re-derived on load.

`_modality` records a shipped defect: `_model_to_dict` always wrote the field
and this function always dropped it, so every provider model came back from disk
as `TEXT` and a restart put `whisper-1` in the chat picker as an ordinary chat
model — the exact defect the modality field was added to close.
`_enabled_models` keeps `null` and `[]` distinguishable across a restart, and
`provider_to_dict` writes the set sorted so two people reading the same file do
not see set iteration order.

## `detect.py`

`detect_runtime(address, client)` probes one node for a runtime it is already
running — today Ollama on 11434 — and is narrow in three stated ways. It only
looks at addresses already in the roster, so it cannot find a stranger's server
and adds no reachability the coordinator did not have. It reports rather than
registers, the same position `registry.offer_candidate` takes: registering
silently would put an unowned box in the routing table. And a miss is `None`,
not a warning, because nothing listening is the overwhelmingly common answer.

`RUNTIME_PROBES` matches on the native `api/tags`, not the OpenAI shim, because
the shim answers for several products and would say a server is there without
saying which — while `base_url_template` still registers the `/v1` shim, which
is what the gateway proxies to. `_models` reports downloaded and resident apart
and prefers `size` over `size_vram`, since a CPU-only box honestly reports zero
VRAM for a plainly resident model. `set_resident` is a generate with
`keep_alive` of -1 or 0 at a 60s timeout, forty times `DETECT_TIMEOUT_S` (1.5s),
because loading a model off an SD card outlasts a detection budget. An empty
`control_path` means this build can observe but not change, and the UI must not
draw a control it cannot honour.

## `secrets.py`

`SecretStore` resolves `api_key_ref`: the environment first, then
`secrets.json` at mode 0600. `env_ref()` exists apart from `get()` because a
caller about to `put()` needs to know whether the name is already shadowed —
after the write, `get` returns the environment's value and the question can no
longer be asked. A JSON parse error is logged by exception *type* only: the
message quotes the offending line, and that line is a secret. Valid JSON of the
wrong shape is a warning and an empty store, never a failed startup, and its
content is never logged either. `refs()` returns names, which are safe to show.
Redaction itself moved to `control_plane/redaction.py` — `logfiles.py` needs the
scrubber on every node, and a worker importing this package would pull httpx and
the whole provider stack — and every name is re-exported here unchanged, so
`from .secrets import Redactor` still resolves.

## `kinds.py`

`_SPECS` is what is known about each kind before anyone configures anything, and
it is the reason adding OpenRouter is a display name and a key reference:
`spec_for` and `known_kinds` are the only readers of the dict;
`auth_headers` builds a request's credential header out of one `KindSpec`, and
`join_url` and `native_base` are string surgery on a provider's own base URL
rather than lookups in the table. Each `KindSpec`
carries the base URL, the endpoint paths, an `AuthStyle`, a `PricingUnit` and
the per-kind claims that keep this table honest — `forwardable=False` plus an
`unsupported_reason` naming OpenRouter, on Anthropic alone; `supports_stream_usage`
false for Anthropic, Ollama and Custom, because an unknown field is a 400 on a
strict server; `meters_cost` true only for OpenRouter; `pull_path` non-empty
only for a server the operator runs, since "pull" on a hosted API is a control
that does nothing. `native_base` strips the `/v1` shim segment so one address
names the box and the two can never disagree about which machine is meant.

## `usage.py`

`UsageSniffer` observes bytes on their way through and never withholds one. For
a stream it keeps a `USAGE_TAIL_BYTES` (64 KB) tail and parses it once the
stream is finished; a non-streamed body past `USAGE_BODY_LIMIT_BYTES` (8 MB) is
dropped rather than held. `_cost` reads `usage.cost` only where the kind is
known to publish it. OpenRouter puts one in every usage block, streaming and
not, and that figure is what the account was charged after prompt caching,
long-context tiers and per-modality surcharges — thirteen price components a
flat input/output pair cannot express. An unrecognized upstream's `cost` is a
number in an unknown unit and is never banked. A negative figure is dropped
rather than subtracted: it is a field we do not understand, not a refund.
`cost_usd=None` and `cost_usd=0.0` are different claims, and 0.0 is a free model
answering.

## `serialization.py`

`provider_public_dict`, `model_public_dict`, `kinds_public` and
`assert_no_key_material` are every shape the gateway hands a client or the UI.
The `api_key: "***"` field is covered above. What is not: there are *three*
facts about the allowlist on the wire, not two. `model_count` is what the
provider serves, `catalogue_count` is what it publishes, and `models_chosen`
(`runtime.enabled_models is not None`) is the only thing that separates a record
predating the allowlist from one whose operator switched everything on — the two
counts are equal in both cases, so a screen inferring "nobody ever chose" from
equal counts says that over a provider where somebody chose all of it.
`metered_requests_today` is kept apart from `requests_today` for the same
reason: rendering a charged figure and an estimated one identically asserts a
precision that is not there.

## `errors.py`

Eight classes, every one of them a `ProviderError`, and the status table above
is the map from five of them to what the gateway answers. `UpstreamError`
preserves the upstream's status code and message and its `body` is already scrubbed;
`to_openai_error()` is the shape the gateway hands back verbatim.
`MissingKeyError` carries the *reference*, which is a name, and its message
names both places the value could come from. `ProviderNotAdmittingError` carries
`retry_after_s` when there is one. Two more exist that the table does not list
because the pull path raises them, not `forward`: `PullUnsupportedError` for a
kind that hosts no weights of its own, and `PullRefusedError`, which carries
both `needs` and `free` — a refusal that does not say what to change is one the
operator has to guess their way past.

## `config.py`

The operational constants, all package-local and none of them frozen contracts.
The timings: `PROVIDER_REFRESH_S` (6 h), `BACKOFF_MIN_S`/`BACKOFF_MAX_S` (1 s to
60 s), `RETRY_AFTER_MAX_S` (300 s, deliberately larger and separate, because
re-admitting earlier than an upstream explicitly asked risks tripping its
limiter again), `SERVER_ERROR_RETRIES` (1), `CONNECT_TIMEOUT_S` (10),
`READ_TIMEOUT_S` (120), `STREAM_READ_TIMEOUT_S` (300), `DISCOVERY_TIMEOUT_S`
(20) and `SPEND_PERSIST_INTERVAL_S` (30). The blend weights
`COST_BLEND_INPUT_WEIGHT` (0.25) and `COST_BLEND_OUTPUT_WEIGHT` (0.75) turn two
published prices into `RouteTarget`'s one scalar. `PULL_HEADROOM`
(`DERATE_PULL_HEADROOM`, default 0.8) is the share of free memory a pulled
model's weights may occupy, and it refuses rather than warns: a machine that
swaps a model is not slow, it is unusable. `data_dir()` delegates to
`control_plane/paths.py` so a native install with no writable `/data` still
keeps its secrets file across a restart, and `PROVIDERS_FILE` and
`SECRETS_FILE` name the two files that land under it. `REDACTED` is re-exported
here from `control_plane/redaction.py` because this is where the package has
always looked for it — `gateway/serialize.py` still imports it from this module
rather than from the scrubber.

## `__init__.py`

The import path, 25 names in `__all__`. Not everything in the package is on it,
and that is deliberate rather than an oversight: `logos`, `detect` and `usage`
are reached as submodules by the four call sites that want them —
`resolver/avatars.py` and `gateway/internal_api.py` for `logos`,
`gateway/runtime_api.py` for `detect`, `gateway/proxy.py` for `usage`
(`from control_plane.providers.logos import LogoCache`,
`from ..providers.detect import detect_runtime`) — which keeps the package's
top-level import from dragging a cosmetic fetcher and a LAN prober into every
site that only wanted `UpstreamError`. `Redactor`, `SecretRedactingFilter` and
`looks_like_secret` reach this file from `control_plane/redaction.py` by way of
`secrets.py`, and `REDACTED` by way of `config.py`, so every historical spelling
of those imports still resolves.

## Where the rest of the tree reaches in

- **`node.py`** builds the one real `ProviderService` inside
  `build_gateway_deps`, imported *there* rather than at module scope, because
  the worker path must not pull httpx and this package.
- **`gateway/proxy.py`** takes the error set and `UsageSniffer`, and drives
  `open_upstream(provider_id, upstream_id, body, streaming, endpoint=path)` for
  each remote target it tries.
- **`gateway/internal_api.py`** owns the routes: `GET`/`POST /api/providers`,
  `PATCH`/`DELETE /api/providers/{id}`, `/refresh`, `/pull`, `/models`,
  `/backends` and `/logo`, plus `/api/providers/kinds` and
  `/api/providers/secret-refs`.
- **`gateway/runtime_api.py`** takes `detect_runtime` and `set_resident` for
  `GET`/`POST /api/nodes/{id}/runtime` and `POST /api/nodes/{id}/runtime/model`.
- **`gateway/serialize.py`** takes `REDACTED`; **`gateway/stubs.py`** takes
  `looks_like_secret`, `spec_for`, `minted_ref` and `KEY_IN_REF_FIELD`, so the
  day-0 store refuses the same paste with the same sentence — a stub that
  accepts what the coordinator refuses teaches the add form the opposite of the
  rule.
- **`resolver/avatars.py`** takes `LogoCache` and `fetch_image`.
- **`inventory/api.py`** calls `public_list` and `catalogue` duck-typed off
  whatever is wired as `deps.providers`; a port carrying only the frozen
  protocol contributes no provider rows rather than raising.
- **`control_plane/redaction.py`** is imported *by* this package, never the
  other way round. That direction is the whole reason it was moved out.

## Things that look like details and are not

**`_screen_passthrough` is deliberately weaker than `_screen_free_text`.** A
model name is handed to the upstream and never persisted or echoed in a
listing, so the entropy fallback buys nothing there and costs every repository
whose name happens to carry a 32-character run — all 96 variants of
Mistral-Small-24B-Instruct-2501, none of Qwen2.5's. A pasted key is still caught
by its vendor prefix. The stronger screen stays on `display_name` and on both
halves of every alias, which *are* displayed and persisted verbatim.

**Rotating a key does not overwrite an operator's reference.** `update` reuses
the provider's existing reference when derate minted it, so everything pointing
at that name keeps working, and mints a new one when the provider was configured
against a name of the operator's own — which is not ours to overwrite. A
reference named in the same patch wins over both. `key_status()` says which of
the two places answered, because a key in the environment belongs to whatever
started the process and pasting a replacement does not replace it.

**`backend_pins` is written one model at a time, unlike `aliases` and
`enabled_models`.** Those two are complete replacements; a pin is not, because
the control that writes it is per-model on the Models tab and must not have to
know every other model's pin on the same provider just to avoid erasing it by
omission. An empty string clears one, since `""` can never be a real backend tag.

**The logo path never makes a screen wait.** A cold cache answers 404
immediately and starts `_warm_logo` behind the already-sent response;
`cache.lock_for` collapses a grid of rows into one request, and the miss it
records on failure is what stops a vendor without a favicon costing a fetch per
repaint. Every failure in `logos.py` is a 404, and a 404 draws a monogram.

**A pull is streamed so the size gate can fire before the transfer.** The
response's first frames carry the download total, and that is the only moment
before several minutes of copying at which anything can decide the weights are
too big for the machine. `on_size` raising leaves the `async with` to close the
connection, which is what stops the transfer; `on_progress` is called only
*after* that gate, so a refused pull never puts a progress bar on screen for a
transfer aborted at its first byte, and it is handed the upstream's raw frame
because `status` is the server's own sentence and paraphrasing it would put a
second, worse vocabulary in front of the real one.

## Failure behaviour the list above does not cover

- **Corrupt `providers.json`**: logged, and the coordinator starts with no
  providers. A malformed individual record is skipped by type name, not by
  content.
- **Unreadable or wrongly shaped `secrets.json`**: a warning and an empty store,
  never a failed startup, and never the file's content in the log. Group- or
  world-accessible modes are warned about by mode and still read.
- **A model list that parses to nothing**: for a kind with a `pull_path` whose
  envelope was recognized, the provider stays healthy with an empty catalogue —
  which routes nowhere on its own, since an empty catalogue contributes no
  targets. For anything else, empty is indistinguishable from unparsed, so the
  cached list is kept and `last_error` is set.
- **A pull that fails in-band**: the upstream reports its own failures at status
  200 inside a frame, and that becomes `UpstreamError(502)`. A successful pull
  re-reads the catalogue before returning, or the model would be on the box and
  unaddressable until the six-hour refresh came round.
- **A logo that cannot be fetched**: a permanent miss is cached for
  `PERMANENT_MISS_TTL_S` (24 h); a transient one backs off from
  `TRANSIENT_BASE_S` (60 s), doubling to `TRANSIENT_MAX_S` (30 min). Only hits
  are written to disk, so a vendor that *adds* a favicon does not stay blank
  until somebody clears a directory by hand.
- **A node that does not answer a runtime probe**: a refused connection, a
  timeout and a reply that does not speak `api/tags` all return `None`. Something
  answering on 11434 that is not Ollama is never claimed as one — an unknown
  server on that port is more likely somebody else's.
- **The refresh loop**: any exception is logged with `log.exception` and the loop
  continues. A refresh cycle must never take the service down.

## Also deliberately not built

**A reveal control for keys.** `key_status()` is the whole of what can be said:
whether the reference resolves, and from which of the two places. No value, no
length, no prefix. `ui/src/api/redact.ts` scrubs on the way into the browser and
there is no inverse.

**A third-party favicon service.** One URL shape would answer all seven kinds,
and using it would mean telling that service which vendors this operator has
configured.

**A subnet scan, and silent registration of what it found.** `detect.py` reads
only addresses a human already admitted, and produces a suggestion rather than a
provider. Discovery proposes; a person accepts.

**Validating a backend tag against a live endpoints call.**
`_screen_backend_pins` checks the model id against the provider's own catalogue
and takes the tag as given — the same tradeoff already made for pricing, which
is accepted as a forecast rather than a fact.
