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

# /v1/models
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

## The seam with Agent H

`public_list()`, `public_dict(id)` and `public_models(id)` return the JSON for
`GET /api/providers` and `GET /api/providers/{id}/models`: health, admission,
model count, today's spend, and any budget. `kinds_public()` gives the add-form
defaults so adding OpenRouter is a name and a key reference.

`api_key` is always `"***"`. There is no reveal control and nothing for one to
call. `api_key_ref` is shown, because it is a name, and showing it is how
someone fixes a provider whose reference does not resolve.

## Keys

`api_key_ref` is the **name** of an environment variable or of a key in
`/data/secrets.json` at mode 0600. Environment wins. Values are resolved at
request time and nowhere else.

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
- **429**: `Retry-After` when present, otherwise exponential from 1s to a 60s
  cap. `admitting` goes false for the window; `healthy` stays true, because rate
  limited is busy, not broken.
- **5xx**: one jittered retry, then unhealthy until a request succeeds.
- **401/403**: unhealthy immediately, no retry, `last_error` names the reference.
- **Failed model refresh**: keeps the cached list and sets `last_error`. Stays
  healthy and keeps serving, because a model list is not an inference path.
- **Cold start**: reads the cached model list from disk. No network needed.
