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

`tests/test_providers.py::test_no_key_material_in_any_output` asserts this
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
