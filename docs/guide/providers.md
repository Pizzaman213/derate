# Put somebody else's API behind the same endpoint

A provider is a remote API — OpenRouter, OpenAI, a Groq key, an Ollama box on
the far side of the LAN — turned into an ordinary target on your own
coordinator. Its models appear on your `/v1/models`, take requests on your
`/v1/chat/completions`, get routed by the same policies as your own hardware,
and report what they cost on the Spend screen. You need a coordinator you can
reach, and a key for whatever you are adding.

## What you can add

The picker's list is the coordinator's, not a list typed into the screen. Six
kinds:

| Provider | Address it dials by default | Key | Worth knowing |
|---|---|---|---|
| OpenRouter | `https://openrouter.ai/api/v1` | yes | Reports what it actually charged on every response. Publishes per-token prices. Can pin one backend host per model. |
| OpenAI | `https://api.openai.com/v1` | yes | Publishes no prices, so its spend is counted but not priced. |
| Together AI | `https://api.together.xyz/v1` | yes | Publishes per-million prices. |
| Groq | `https://api.groq.com/openai/v1` | yes | Publishes no prices. |
| Ollama | `http://localhost:11434/v1` | no | A server you run. Change the address — see below. |
| Custom | you supply it | optional | Anything that speaks the OpenAI core. |

**Anthropic is not in the picker, deliberately.** The coordinator knows the
kind, and discovery and health work against it, but the Messages API is not
OpenAI-compatible and this build has no adapter for it — so the form offers
only kinds that can actually be routed to, and forwarding refuses by name:

```
provider 'anthropic': the Anthropic Messages API is not OpenAI-compatible and this build has no adapter for it; add Anthropic models through an OpenRouter provider instead
```

Do that: Claude models are on OpenRouter and arrive as ordinary models. (The
project README lists Anthropic alongside the others. Discovery and health are
what that covers; forwarding is not built.)

## 1. Add one

There are two places, and they add the same record.

**During the first-run walkthrough.** Step two is `Add a cloud provider as
backup?` — one field for an OpenRouter key, `Add key`, or `Skip — local only`.
Under the field: `Stored on the coordinator and never shown again, not even to
you.`

**Settings → Providers, any time.** See [`../screenshots/settings.png`](../screenshots/settings.png).
The card is a table of what you have and a row of fields under it:

1. **Provider** — the dropdown. Choosing one fills the **Base URL** field with
   that kind's own default rather than leaving a blank the coordinator fills in
   silently, so you can see the address that is about to be dialled.
2. **Base URL** — edit it for anything you host. For an Ollama box this is the
   whole job, and the card warns you why:

   ```
   This address is dialled from the coordinator, so localhost means the coordinator itself. For a server on another machine, use its address.
   ```

   So a box at `192.168.1.24` wants `http://192.168.1.24:11434/v1`, not the
   default. For **Custom**, any OpenAI-compatible server: Kokoro-FastAPI,
   openedai-speech, faster-whisper-server, an inference server somebody else on
   the network runs. A base URL is required and a key is not.
3. **The key field** — labelled with the provider's own name, `OpenRouter key`,
   and carrying two radio buttons: `Paste a key` and `Name a reference`. The
   next section is entirely about this. For a kind that needs no key the field
   is not shown at all, and the card says why:

   ```
   Ollama takes no key — it is a server you run, reached over the network. Only the address matters.
   ```
4. **Add provider**.

The catalogue is read immediately, so the row appears with a model count and a
`Refreshed` time already on it. After that it is re-read every six hours.

## 2. The key: pasted or named, never shown

The card states its own contract above the table:

```
A key you paste is written to secrets.json at 0600 and only its reference is kept on the provider. Keys are resolved at request time and are never displayed, logged, or included in exports. Each row says whether its reference resolves and which of the environment and secrets.json answered — the whole of what can be shown about a key — and Set key gives one to a provider that already exists, or replaces the one it has.
```

**Paste a key** is the default because it needs nothing set up first. The
coordinator writes the value into `secrets.json` at mode 0600 and keeps only
the *name* on the provider record. The field tells you the name before you
commit to it:

```
Stored as DERATE_OPENROUTER_API_KEY in secrets.json.
```

That name is minted from the provider's id, so the second OpenRouter provider
you add becomes `openrouter-2` and its key lands under
`DERATE_OPENROUTER_2_API_KEY`. The `DERATE_` prefix is also what makes removing
a provider safe to delete its secret — a reference you brought yourself may be
shared with something else and is never touched.

**Name a reference** is for keys you already manage. The value is the name of
an environment variable, or of a key in `secrets.json`, and never the key
itself. The environment is read first, so a variable of that name wins over the
file.

**No key is ever rendered.** The API always reports `"***"` for one. There is
no reveal control and no endpoint for one to call, and every response is
scrubbed again on its way to the screen — there is deliberately no inverse of
that function. What you get instead is the reference, which is a name, and
whether it resolves:

```
resolves from environment
resolves from secrets.json
does not resolve
no key needed
```

To change a key later, press `Set key` on the row (`Replace key` if it already
has one), which opens the same two-mode field, then `Save key`. A pasted
replacement always lands under derate's own minted name, and if the provider
was on a reference of yours, the field says so before you save: *This provider
moves onto that reference, and `OPENROUTER_API_KEY` stops being what
authenticates it.*

## 3. A provider you have just added is serving nothing

This surprises people, so it is worth saying flatly: **adding a provider does
not put any of its models on your endpoint.** A new provider starts with an
empty allowlist. Its row reads `0 of 312`; `/v1/models` is unchanged; the chat
picker is unchanged.

That is the fix for what used to happen. Enrolling OpenRouter put every one of
the several hundred models it publishes onto this cluster's `/v1/models` the
moment a key was added — a wall nobody chose.

It also means the walkthrough's closing line, *standing by, takes over only
when your machine is full*, describes a provider that has not been given
anything to serve yet. Switch a model on and it is true.

**To switch one on:** go to Models, find the model, open it. For a model on
somebody else's hardware the whole pane is one card — there is no plan to run,
no machine to pick and no quantization to choose. It shows the name a client
would send, the context length, the price per million in and out, whether it
streams and whether it takes tools, and one button.

Before:

```
Not served. OpenRouter publishes this model, and nothing on this cluster’s API answers to it until you switch it on.
```

Press **Serve on the API**. After:

```
Served. Requests naming meta-llama/llama-3.1-8b-instruct at this cluster’s /v1 are forwarded to OpenRouter and billed there.
```

Nothing restarts. `/v1/models`, the chat picker, the models list, routing and
the cluster graph all change together, because they are all built from the same
filtered view. **Stop serving** is the same button the other way round.

If the button is greyed out, hovering it says why:

```
Waiting for this provider’s catalogue — the switch needs every other model’s current state to write the allowlist.
```

The allowlist is written as the complete set rather than as a change to it, so
a switch computed from a catalogue that failed to load would send exactly one
id and turn off everything else this provider serves. There is no safe partial
version of that edit, so it is refused until the catalogue arrives. If it
failed rather than being slow, the card says that instead, with the reason
underneath.

### The banner offering to fix it

One case still serves everything: a provider record written before the
allowlist existed. Those keep passing the whole catalogue through, on purpose,
because the alternative is an upgrade that silently stops routing traffic
somebody depends on. The Models screen says so, above the list:

```
OpenRouter is serving all 312 models it publishes, because nothing was ever chosen. Every one of them is a name on this cluster’s API.
```

with a **Serve none** button beside it. Pressing it writes an empty allowlist —
an explicit "serve nothing", which is a different record from the absent choice
it replaces — and then you switch models on one at a time as above.

The same thing happens implicitly if you switch a single model *off* on such a
provider, and the card warns you before you do:

```
Nothing has ever been chosen for OpenRouter, so it serves every model it publishes. Switching this one off chooses the other 311 — that is what the rest of them become, rather than staying unchosen.
```

## 4. Make a provider the backup for a model you run

A provider model whose served name matches one of your deployments is merged
into that name's routing entry: one line on `/v1/models`, two targets behind
it. Routing then picks `local_first` for it on its own and hands the provider
traffic only once every local replica stops admitting.

That hinges on the two names being equal, and they usually are not — your
deployment is `Llama-3.1-8B-Instruct` and OpenRouter's is
`meta-llama/llama-3.1-8b-instruct`. The **Backs up** button on the provider's
row is where you make them equal. Its own summary:

```
A model answering to a name this cluster already serves becomes that name's backup: one entry on /v1/models, two targets behind it. Routing picks local_first for a name served both ways on its own, and sends the provider traffic only once every local replica stops admitting — then returns to the cluster the moment one frees. A name nothing here serves is a rename, not a backup.
```

Fill in **Upstream model** (the provider's id — the field suggests from its
catalogue) and **Answers to** (the field suggests the names this cluster
serves), then **Point it there**. The table that results says which of the two
things you just did — `backs up Llama-3.1-8B-Instruct`, or `renamed for clients
— nothing here serves that name` — rather than leaving you to infer it from
whether traffic ever arrives. **Clear** undoes one.

## 5. What it costs

See [`../screenshots/spend.png`](../screenshots/spend.png).

**OpenRouter reports its own cost on every response**, in the `usage` block,
and derate banks that figure rather than its own arithmetic. This is not a
detail. OpenRouter's number already accounts for cached prompt tokens (79 of
its models price those differently), for the long-context tiers 43 of them
switch to above a token threshold, and for reasoning, image, audio and
web-search components — none of which a flat input/output rate pair can
express. Where a provider meters itself, the published rate card is only a
forecast.

So the Spend screen distinguishes what it knows from what it computed:

```
cloud spend is what your provider reports it charged; some cloud spend is estimated from published rates; local cost is derived from measured power draw at your rate.
```

Only OpenRouter and Together publish prices at all. For OpenAI and Groq nothing
is published, so their traffic is counted and cannot be priced — and the screen
says that rather than reporting it as free:

- `≥ $1.23` — unpriced traffic sits underneath this figure, so it is a lower
  bound.
- `spent today · excludes unpriced cloud models`, and `· excludes local
  generation` when no electricity rate is set.
- `418 requests went to models openrouter publishes no price for`
- `set an electricity rate to price local generation` — that is Settings →
  Containment → `Electricity $/kWh`. Local cost comes from measured power draw
  at that rate, and with no rate it is left blank rather than reported as zero.

A provider's `Today` column in Settings shows `$0.42`, or `$0.42 of $5.00` when
it has a daily budget. The budget is a hard stop at request time, not a
warning: once it is reached the provider stops admitting and its State column
reads

```
daily budget of $5.00 reached ($5.42 spent today)
```

There is no field for the budget on the Settings card today. It is set on the
provider record:

```bash
curl -X PATCH http://<coordinator>:8080/api/providers/openrouter \
  -H 'content-type: application/json' \
  -d '{"daily_budget_usd": 5}'
```

## What can go wrong here

**A key in the field that takes a name.** The form warns before you send it:

```
That looks like a key, not a name. Switch to “Paste a key” and it will be stored in secrets.json for you.
```

And the coordinator refuses it if you send it anyway:

```
Could not add provider. ValueError: api_key_ref must be the NAME of an environment variable or secrets.json key, not the key itself -- send the key as "api_key" and it will be stored in secrets.json under a reference for you
```

**A pasted key whose name is already an environment variable.** The environment
is read before the file, so the stored key would be shadowed and the provider
would authenticate as somebody else. Refused, naming the variable and never its
value:

```
Could not add provider. ValueError: an environment variable named DERATE_OPENROUTER_API_KEY already resolves to a different value and takes precedence over secrets.json, so the key would be stored and then ignored; choose another reference name or unset it
```

**A missing field.** `Could not add provider. ValueError: custom needs a
base_url`, or `Could not add provider. ValueError: openrouter needs an api_key
or an api_key_ref`.

**A reference that stops resolving.** The provider is disabled rather than the
coordinator failing to start, and its State column names the fix:

```
api_key_ref 'OPENROUTER_API_KEY' is unresolved
```

A request that reaches it anyway comes back `502 Provider 'openrouter' has no
usable credential configured.` It re-enables itself when the reference resolves
again.

**A key the upstream rejects (401 or 403).** Unhealthy immediately, no retry,
and this is the one unhealthy state that never expires on its own. Retrying a
key an upstream refused burns quota and can get an account locked, and waiting
does not turn a wrong key into a right one. Use `Replace key`.

**Rate limiting (429).** Not admitting for the window — `rate limited for
another 42s` — while `healthy` stays true, because rate limited is busy, not
broken. Requests for that model answer `503 No target for '<name>' is currently
admitting requests.` until it clears. A `Retry-After` from the upstream is
honoured up to 300 seconds; otherwise the wait backs off from 1 second to a
cap of 60.

**Unreachable, or 5xx.** Unhealthy with the same 1-to-60-second backoff. When
the backoff expires one request is let through to settle whether it is back: a
success clears everything, another failure backs off further. `last_error`
stays readable while that probe is out, because that is the admission that
derate no longer knows — not a claim of recovery. This exists because leaving
the flag latched made the block prevent its own cure: an unhealthy provider was
never offered a request, and a successful request was the only thing that could
clear it. One transient read timeout took an Ollama box off the air for a
working day while it answered pings in 11 ms.

**A model switched on, but nothing coming back.** Health is per provider, not
per model, so a model can be served, appear in `/v1/models`, and still refuse
every request. The card says which:

```
Served, but OpenRouter is not answering. The name is on this cluster’s /v1 and a request for it is refused rather than forwarded, until the provider recovers — a successful refresh from Settings is what clears this.
```

Take that last clause loosely: the Settings card has no refresh button today,
only a `Refreshed` column saying when the catalogue was last read. What
actually clears it is the probe described above, which happens on its own.

**A model list that fails to refresh.** The cached list is kept and the
provider stays healthy and keeps serving, because a model list is not an
inference path. On a cold start the list is read from disk, so nothing needs
the network to come up.

**An Ollama box with nothing pulled.** That is a well-formed empty catalogue,
not a broken server, and it is treated as one.

---

**Next:** [Send requests to the one endpoint](the-endpoint.md#routing-which-machine-answers)
— what happens to a name once both your hardware and a provider answer to it.
