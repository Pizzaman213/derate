# Agent I: Remote Providers

Read `00-architecture.md` first. Contracts there are frozen.

**You own:** `control_plane/providers/**`, `tests/test_providers.py`
**You depend on:** contracts only
**Downstream of you:** G routes to your targets. H lists your providers.

---

## What you build

Remote OpenAI-compatible upstreams as first-class route targets alongside local deployments. OpenRouter, OpenAI, Anthropic, Together, Groq, an Ollama box on the LAN, or anything with a base URL and a bearer token.

The point is not to be a proxy service. The point is that the cluster is the default and a paid API is the overflow valve. Someone who owns two Sparks should serve from them until they saturate and then spill, without their client knowing anything changed.

---

## 1. Provider registry

Add, list, enable, disable, prioritize, remove. Persist to `/data/providers.json`.

For known kinds, prefill `base_url` and the model-list path so adding OpenRouter is a name and a key reference, not a form. For `CUSTOM`, take a base URL and assume OpenAI-compatible.

### Secrets, and this is the part to get right

`api_key_ref` is the **name** of an environment variable or a key in `/data/secrets.json` at mode 0600. It is never the key itself.

Keys are resolved at request time and nowhere else. A key must never appear in:

- an API response, where it renders as `"***"` and nothing more
- a log line, at any level, including debug
- the topology payload
- a persisted provider record
- an error message, including upstream errors echoed back

Write a test that serializes a fully configured provider and asserts the key string appears nowhere in the output. Run it against every response shape you emit. A leaked key in a screenshot is an unrecoverable mistake and this is a tool people will screenshot.

If a referenced env var or secret is missing, the provider is `enabled: false` with `last_error` naming the missing reference. Do not fail startup over it.

---

## 2. Model discovery

Pull each provider's model list on add, on `POST /api/providers/{id}/refresh`, and every 6 hours. Cache to disk so a cold start does not depend on network reachability.

Normalize to `ProviderModel`. Map upstream IDs to a `served_name` clients will use. Default to the upstream ID unchanged, since people already know `anthropic/claude-sonnet-4.5` and renaming it helps nobody, but allow an alias so a remote model can deliberately share a `served_name` with a local deployment. That sharing is the whole spill mechanism, not a collision to prevent.

Capture `context_length`, streaming support, tool support, and per-million-token input and output costs where the provider publishes them. OpenRouter publishes pricing in its model list; most do not. Leave costs `None` rather than guessing, and let `COST_AWARE` skip targets it cannot price.

A provider whose model list fails to refresh keeps serving from its cached list and sets `last_error`. Do not drop models because one refresh failed.

---

## 3. Upstream adapters

Most providers are OpenAI-compatible and need no translation. Handle the ones that are not.

Anthropic's Messages API differs in request shape, system-prompt placement, and streaming events. Either translate, or route Anthropic through OpenRouter and skip the adapter. **Recommendation: skip it.** Native Anthropic support is a day of work that OpenRouter already solves, and it is not what the four days are for. Support it later.

Per adapter: translate the request, forward with the resolved key, stream the response back without buffering, and translate errors into a consistent shape while preserving the upstream status code and message. A client debugging a rate limit should see the rate limit.

---

## 4. Health and rate limits

Track per provider: healthy, last error, and current backoff.

On 429, read `Retry-After` when present, otherwise back off exponentially from 1 second to a 60 second cap. Mark the provider `admitting: false` for the backoff window so Agent G routes around it, and clear it when the window expires. A rate-limited provider is temporarily unavailable, not unhealthy.

On 5xx, retry once with jitter, then mark unhealthy and let it recover on the next successful request. On auth failure, mark unhealthy immediately and set `last_error` to something actionable, since retrying a bad key just burns time.

Track spend per provider per day from token counts and known costs. Expose it. Optional `daily_budget_usd` marks a provider not admitting when exceeded. Someone spilling to a paid API from a UI panel deserves a ceiling they set themselves.

---

## Interface you must satisfy

```python
class ProviderPort(Protocol):
    def list(self) -> list[Provider]: ...                     # keys redacted
    def add(self, spec: dict) -> Provider: ...
    def refresh(self, provider_id: str) -> Provider: ...
    def models(self) -> list[tuple[str, ProviderModel]]: ...
    def resolve_key(self, provider_id: str) -> str: ...       # request time only
    def health(self, provider_id: str) -> tuple[bool, str | None]: ...
```

Plus:

```python
async def forward(self, provider_id, upstream_id, body, stream) -> AsyncIterator[bytes]
def route_targets(self) -> list[RouteTarget]      # kind=REMOTE, for Agent G
def spend_today(self, provider_id: str) -> float
```

`route_targets` is the seam with Agent G. Emit `RouteTarget` with `kind=REMOTE`, `admitting` reflecting health, backoff, and budget, and `cost_per_mtok` where known.

---

## Day 0 stub

One fake OpenRouter provider with three models, healthy, with plausible pricing, and a `forward` that streams canned tokens. Agent G wires `LOCAL_FIRST` against this before any real key exists.

---

## Acceptance

- Adding OpenRouter with a key reference pulls its model list and the models appear in `/v1/models` through the gateway.
- A serialized provider, in every response shape, contains no key material. Assert this explicitly.
- A missing key reference disables the provider with a clear `last_error` and does not fail startup.
- Streaming from a remote provider passes through without buffering.
- A 429 sets a backoff window, flips `admitting` to false, and clears on expiry.
- An upstream error preserves its status code and message rather than becoming a generic 502.
- A remote model can share a `served_name` with a local deployment, and both appear as targets in one `RoutingConfig`.
- A refresh failure keeps the cached model list rather than emptying it.
- Exceeding `daily_budget_usd` marks the provider not admitting.

## Traps

Do not store keys, only references. Do not log request bodies, they contain prompts and sometimes credentials. Do not buffer streams. Do not build the native Anthropic adapter in the first pass, use OpenRouter. Do not invent pricing for a provider that does not publish it. Do not drop cached models on a failed refresh.
