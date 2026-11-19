# settings

The cards behind the Settings destination: which coordinator this browser talks
to, the machines and providers behind it, what they are allowed to cost, and
what this project has said it will not build. Fourteen files: twelve modules
`tabs/SettingsTab.tsx` composes into seven sub-tabs, and two `*.check.mjs`
verifiers nothing in the app imports.

One rule governs the folder and it is a negative: **no API key is ever
rendered.** `api/redact.ts` scrubs every response on the way in, there is no
inverse of it, and there is no endpoint that returns a key for a reveal control
to call. Everything this folder shows about a credential is two names — a
reference, and which of the environment and `secrets.json` answered to it.

## Layout

| File | Lines | What it owns |
|---|---|---|
| `ProvidersCard.tsx` | 462 | one row per provider: key state and source, spend, the served/published count, Set key |
| `AddNodeCard.tsx` | 332 | where a node's join token comes from, and there is nowhere else to look |
| `ProviderBackupPanel.tsx` | 213 | the alias map — which local name a provider's model answers to |
| `keyfield.check.mjs` | 203 | the verifier that computes its expectations by running the server's own Python |
| `CoordinatorCard.tsx` | 175 | which gateway this browser tab talks to, and a probe that finds out if it answers |
| `NodesCard.tsx` | 167 | the node table, plus discovered candidates and the one click that admits them |
| `ContainmentCard.tsx` | 165 | the three mutable cost and locality settings, each showing its `SettingSource` |
| `allowlist.check.mjs` | 138 | the verifier that proves only switched-on provider models reach a screen |
| `KeyField.tsx` | 137 | the two-mode credential field, shared by the add form and the per-row editor |
| `keyfield.ts` | 131 | the pure half: shape warnings, minted names, predicted ids, key-state prose |
| `ScopeCards.tsx` | 101 | three read-only cards — planned, reversed, and never |
| `InstanceCard.tsx` | — | pick a node and, optionally, a deployment on it; renders NodeInspector's own panels plus `NodeLogFiles` |
| `ReliabilityCard.tsx` | 61 | one toggle, `auto_restart_crashed_deployments`, with its source |
| `ClusterCard.tsx` | 42 | five read-only cluster facts, and two rows deliberately absent |
| `AppearanceCard.tsx` | 29 | the theme select, moved out of the header |

## `ProvidersCard.tsx`

One table row per provider: display name, key reference and whether it
resolves, served-of-published model count, `$/Mtok`, today's spend against its
budget, admission state, when the catalogue was last refreshed, and three
buttons — Backs up, Set key, Remove. The middle one reads Replace key once
`key_state` is `set`, because a provider that already authenticates is not
being given a first key.

**The kind list is the server's, fetched from `/api/providers/kinds` rather
than restated here.** A local copy went stale in both directions: it offered
Anthropic, which this build cannot talk to and rejects at POST time, and it
could not say that Ollama needs no key or that its default `base_url` resolves
on the coordinator — so "leave blank for the default" pointed at the wrong
machine and failed as a bare connection timeout. `isLocalUrl` is the residue of
that second half, warning that `localhost` in this field means the coordinator
and not the box the operator is picturing.

`$/Mtok` is the single model's `output_cost_per_mtok`, dashed unless the
provider serves exactly one model and that model carries a price. A provider's
own accounting is provider-wide, so there is no honest single rate for one
serving many at different prices.

`backupRow` and `keyRow` are separate pieces of row state rather than one mode
enum two questions fight over: which of this cluster's names a provider's model
answers to, and which credential the provider uses, are different questions
about the same row and both expand at once without either meaning anything
different. `rowMode`/`rowKey`/`rowRef` are the key editor's own fields, and
`openKeyRow` clears all three on open: carrying a half-typed key from one row
into the next is the one way this control could send a credential to a provider
nobody chose to send it to. `setKey` PATCHes `api_key` or
`api_key_ref` and renders the server's refusal verbatim, which is the useful
half of the control — the coordinator refuses a reference an environment
variable already answers to and names the variable.

## `AddNodeCard.tsx`

The install command, and what turns up after it runs. Two commands, and they
are not interchangeable. The first node has nothing to fetch a script from, so
its command names the repository; it is static, carries no credential, and is
shown always — including on a cluster that already has a coordinator, because
it is the command for the machine you have not set up yet.
`FALLBACK_INSTALL_URL` is hardcoded here and overridden by
`public_install_url` off the mint response, because `enroll_api.py`'s
`PUBLIC_INSTALL_URL` is where that constant actually lives and a UI that
guessed it would go stale silently.

**The countdown is anchored to when the token arrived, not to `expires_at`.**
`remaining` is `expires_in_s` minus the wall time since `mintedAt`; trusting
the server's timestamp against the browser's clock shows a negative timer on a
machine whose time is off, which is most of them. The 1 Hz interval runs only
while a token is on screen.

`before` snapshots the node ids that existed at mint, so `arrived` is exactly
what the operator is standing there waiting for. `elsewhere` lists tokens
minted in another tab; their commands cannot be shown again, so they appear
only so they can be revoked. The token is shown once — minting another costs
nothing, so losing one is not a state worth building around.

## `ProviderBackupPanel.tsx`

One question: which name should this provider's model answer to here. Pointing
it at a deployment's served name makes it that deployment's backup —
`gateway/targets.py` merges the two into one routing entry, and `router.py`
picks `LOCAL_FIRST` for a name served both ways, handing the provider traffic
only once every local replica stops admitting. Pointing it anywhere else is a
rename for clients, and the row says which of the two it just did rather than
leaving the operator to infer it from whether traffic ever arrives.

**The alias map is composed from `/api/providers/{id}/models`, not from
`/api/providers`.** That is the distinction that makes the panel correct.
`/api/providers` carries only the models the allowlist admits, and PATCH
replaces the alias map wholesale — so composing the next map from the filtered
list would silently drop the alias of every model somebody had switched off,
and switching one back on later would restore it under the wrong name.

The upstream field is a `datalist`, not a `select`: several hundred options is
a list you type into. `REFRESH_MS` is 300000, because the catalogue only
changes when the upstream publishes something and `invalidate()` covers every
edit made here.

## `keyfield.check.mjs`

`// requires: python`. esbuild-bundles `keyfield.ts` and exercises it, because
a green `tsc` says nothing about what a predicate answers.

**The expectations are not restated by hand — they are computed by running the
server.** One `python3 -c` call returns `looks_like_secret` over eighteen case
strings, `minted_ref` over five provider ids, the `key_state` and `key_source`
vocabularies scraped out of `ProviderService.key_status` with
`inspect.getsource`, and `kinds_public()`'s list of kinds that require a key.
So a provider kind added on the server is checked here without anybody editing
this file, and the check can still fail after somebody edits the Python and not
the TypeScript.

The drift it guards already shipped once: `redact.ts` tested a reference
against `/^[A-Z][A-Z0-9_]{0,63}$/` while the server accepted
`my-openrouter-key`, so a correctly configured name rendered on screen as
`***`. Hence the paired assertions that every minted name reads as a name and
fits in 64 characters — a minted reference the UI masks is derate hiding its
own name from the operator who needs to read it. Each kind's placeholder is
completed to a full-length token and asserted twice: that it would not trip the
field's own warning, and that `looksLikeSecret` reads it as key material.

## `CoordinatorCard.tsx`

Which gateway this browser tab sends `/api` and `/v1` to. It lives in
localStorage, per browser, and is deliberately not part of `/api/settings` — a
setting you have to already be connected to read cannot be the one that says
how to connect.

**Test and Save are separate controls.** Testing probes the address in the
field without adopting it, so a typo comes back as an error message rather than
an app that has just pointed itself at nothing and cannot load the page you
would fix it on. `normalizeBase` runs on every keystroke rather than latching on
submit, so the complaint appears while there is still a cursor in the field and
Save can simply be unavailable rather than being a button that silently does
nothing.

The card reads `origin` off `useBackend()` instead of calling
`coordinatorBase()` directly: same value, but it arrives through the store
subscription, so the "In use" row re-renders with the rest of the app. The
footer names `DERATE_ALLOWED_ORIGINS` — without it every request from another
origin fails with no detail at all, which is the browser withholding it rather
than the coordinator being down.

## `NodesCard.tsx`

The roster table — name, id, address, GPU and compute capability, addressable
of total memory, memory bandwidth, driver, last seen — plus the discovered rows
underneath it and a Remove button per member. The mockup's address/kind form is
not carried over: join is worker-to-coordinator and token-gated, so there is no
endpoint an address field could call. `AddNodeCard` is the answer to that, and
it works by making the *other* machine call in.

**The Admit button is never disabled on `c.eligible`.**
`serialize._eligibility` is explicit that an unrecognised device class is
"cannot confirm" rather than an exclusion the system enforces — nothing in the
planner or the fit gate filters placement on device class. Disabling the button
turned that hedge into a refusal, and it is a refusal a GPU-less machine, a Mac
or a Pi, hits on the one path a human drives. The reason is printed and the
operator admits anyway.

The footnote under the table says addressable memory is what the fit gate
budgets against at a 90% guardrail, and that on a unified-memory node the
operating system shares that pool, so the nameplate total overstates what a
model can actually have.

## `ContainmentCard.tsx`

Local-only routing, the daily spend cap, and the electricity rate — the only
place these three are mutable — each with `SOURCE_LABEL` printing its
`SettingSource` as "set by env", "file" or "default".

**`commitCap` returns early when `capInput` is still `null`.** Untouched means
the value on screen is the server's own, arriving through the `capValue`
fallback; committing anyway would PATCH that value straight back, which moves
it into the settings file and flips its source hint from "set by env" or
"default" to "file" purely because focus passed through the input. Blank means
"no cap" and sends `null`; a typed number including zero is a real instruction
and must reach the server as that number rather than collapsing into the same
`null` a cleared field sends.

`CAP_UNENFORCEABLE_SENTENCE` is copied from `gateway/ui_api.py`'s own literal
string, not paraphrased, so the refusal reads identically whichever way the cap
gets refused. The mockup's trailing "Cloud targets stop admitting once the cap
is hit." was dropped: it is false whenever `daily_spend_cap_enforceable` is
false, and the per-field sentence already covers that case where the input
actually lives.

## `allowlist.check.mjs`

`// requires: coordinator`, `:8088` by default or whatever
`DERATE_CHECK_ORIGIN` names. It proves an agreement between two endpoints and a
routing table: that the only provider models reaching a screen are ones
somebody switched on, and that they are the same ones reaching `/v1/models`.

**That is precisely the class of bug `tsc` cannot see.** `Provider.models` and
`ProviderCatalogueModel[]` are structurally near-identical, so serving the
unfiltered list from the filtered endpoint typechecks perfectly and puts three
hundred models back on screen.

Four wire paths are cross-checked per provider: `/api/providers` for what the
UI renders, `/api/providers/{id}/models` for what is published, `/api/topology`
for the router's own target index, and `/v1/models` for the OpenAI surface.
`remotes` is read rather than inferred because two providers may publish the
same model and one of them serving it puts the name in `/v1/models` whether or
not the other does — attribution needs the target id. `models_chosen` is
asserted to have reached the wire at all, with a message naming
`ui_detail._SPEND_KEYS` as the reason it might not have, because
`model_count === catalogue_count` in both the "chose everything" and "never
chose" cases and only that tri-state tells them apart.

**An empty coordinator is a result, not a reason to skip.** This used to
`process.exit(0)` at that point, which gave "there was nothing to check" and
"every provider checks out" the same exit code. Whether the file runs at all is
now the runner's decision, made once against a probe off the `// requires:`
line.

## `KeyField.tsx`

The two ways to give a provider a credential, as one field with a radio pair:
"Paste a key" and "Name a reference". Shared by the add-provider form and the
per-row Set key editor, because they are the same decision made at two moments
and catch the same mistake.

**`idPrefix` is required and unique per instance.** Two of these are on screen
at once — the add form and whichever row is being edited — and a shared radio
`name` makes them one group, so choosing "Paste a key" in the row would
silently switch the form above it.

The reference input is deliberately not `type="password"`. A reference is a
name, and masking it is what invited a key into the field in the first place;
the single masked input labelled "Key reference" was the whole original bug. On
a valid paste the field names its `destination` — "Stored as
`DERATE_OPENROUTER_API_KEY` in secrets.json" — and, when `displaced` is set,
which reference stops being what authenticates the provider. What this
component will never have is a control that shows the current key.

## `keyfield.ts`

The pure half, and a port of the server's own screen.

`mintedRef` mirrors `minted_ref` in `providers/service.py`: a `DERATE_` prefix,
an `_API_KEY` suffix, and the id uppercased into the 64-character budget
between them. `predictedProviderId` mirrors `_mint_id` — the kind, then the
first free `-N` — predicted rather than known so the form can name the
reference it is about to create instead of describing one in the abstract.

`keyPlaceholder` returns a per-kind hint for `openrouter`, `openai`,
`anthropic` and `groq`, and `sk-…` otherwise. Every prefix is one
`api/keyshape.ts` already recognises rather than one remembered off a vendor's
documentation, so the hint the field shows and the screen that reads what was
pasted under it cannot disagree. A kind with no known prefix gets the generic
hint rather than a guess: Together publishes bare hex.

`keyFieldWarning` is always a warning and never a block. These are heuristics,
and a heuristic that disables the button turns a false positive into an
operator who cannot add their provider at all; the server holds the actual
screen. `keyStateNote` and `keyStateTone` describe four states, and the fourth
is the one worth spelling out: a null state is a provider port that does not
answer `key_status`, not a provider without a key, and rendering it as
"missing" would put a warning beside a provider authenticating perfectly well
on nothing but the absence of a method. `looksLikeSecret` and `looksLikeRefName`
are re-exported from `api/keyshape.ts` so the form and its verifier have one
import.

## `ScopeCards.tsx`

Three read-only cards: six rows under "Not built yet", five under "Scope
changed", four under "Deliberately out of scope". Nothing here is a setting,
which is why the sub-tab holding them is called About and not Policy — the
mutable card that used to lead this file is `ContainmentCard.tsx` now.

**`SCOPE_CHANGED` keeps reversals on screen rather than quietly deleting
them.** The "will not build" card exists precisely because these things get
built by accident — each looks like a small addition to a screen that already
exists — so building one on purpose has to be visible and dated rather than
tidied away. All five carry an amendment date of 2026-09-07: manual placement,
the model catalog browser, the deep-dive metrics page, the log browser
(narrowed, not dropped), and the node shell, which `procs.py` had bounded the
kill verb to avoid and which is now that hole opened deliberately behind
`DERATE_SHELL=1`.

## `InstanceCard.tsx`

Settings → Instance: a node `<select>` and a deployment `<select>`, and below
them, whichever of `NodeInspector`'s own panels apply — `ServingBlock`,
`RequestsTable`, `ResidentProcesses`, `NodeRuntimeCard`, `EventsAndLogs`,
`DeploymentLog`, and the new `NodeLogFiles` (`inspectors/node/`) — reused
directly rather than reimplemented, so this card and the node sheet cannot
silently disagree about what a machine is doing. `runningOrPrevious`
(`tabs/cluster/layout.ts`) is shared with `NodeInspector` for the same reason.

**It adds no selection state of its own.** The picker reads and writes
`?node=`/`?dep=`, the query params every other screen already shares, so a
link to `/settings?node=X&dep=Y` opens this card pre-filled — see
`SettingsTab.tsx`'s lazy `useState` initializer, which is what makes the
first render land here instead of on Connection.

**`NodeLogFiles` gets no search box, on purpose.** Same rule `EventsAndLogs`
already follows (see `ScopeCards.tsx`'s "Searchable logs" row): a file toggle
(`node.log`/`proxy.log`) and a line-count choice are the only controls. It
reads `GET /api/nodes/{id}/logs`, which proxies to the node agent's own
`GET /agent/logs` — the coordinator's own process log, not a deployment's
serving log (`DeploymentLog`) and not the structured archive
(`EventsAndLogs`).

## `ReliabilityCard.tsx`

One switch — `auto_restart_crashed_deployments` — with `SOURCE_LABEL` under it
saying whether the value came from the environment, the settings file or the
default. Split out of `ContainmentCard` rather than folded into it: bringing a
crashed deployment back is a reliability concern and not a cost or locality
one, and each card's heading names exactly one policy family.

## `ClusterCard.tsx`

Five read-only rows, four of them `ClusterSummary`'s whole surface:
`cluster_id`, `coordinator`, `node_count` and `healthy_count` in one cell, and
`total_addressable_memory`. The fifth, Discovery, is the literal
`mDNS · _derate._tcp.local.` typed into the card — no endpoint reports it, and
it is a copy of `registry/config.py`'s `MDNS_SERVICE_TYPE`.

**Two rows are deliberately absent, for the same reason stated twice.** The
join token names a real field — a secret, no less — that no endpoint here ever
returns, so the row would be either fabricated or broken. A Gateway row has no
candidate value but the browser's own `window.location.host`, which is not a
coordinator-reported fact: it is same-origin with the gateway only by
deployment convention and is actively wrong under `npm run dev`, where Vite
serves the UI on its own port and proxies. A row that reads correctly in
production and lies in dev is worse than no row. `CoordinatorCard` next door
answers the same question honestly, because there it is the operator's own
instruction rather than a fact being guessed at.

## `AppearanceCard.tsx`

The colour-scheme select — System, Light, Dark — reading and writing `theme.ts`.
Moved out of `shell/Header.tsx`, which put a local-only browser preference in
the one bar every screen shares. A colour scheme is a setting, not a destination
control.

## The seam with `SettingsTab.tsx`

`tabs/SettingsTab.tsx` is the only importer of this folder. It mounts nine of
the fourteen files directly; `KeyField`, `keyfield.ts` and
`ProviderBackupPanel` are reached through `ProvidersCard`, and the two
`*.check.mjs` files are never imported by the app at all.

```tsx
<div id="st-policy" role="tabpanel" aria-labelledby="st-tab-policy" hidden={sub !== 'policy'}>
  <ContainmentCard />
  <ReliabilityCard />
</div>
```

Sub-tabs: Connection (`CoordinatorCard` + `ClusterCard`), Nodes
(`AddNodeCard` + `NodesCard`), Instance (`InstanceCard`), Storage, Providers,
Policy, Appearance, About. Storage is the one composed from outside this
folder — four cards from `tabs/storage/`.

**Every section stays mounted and is hidden with `hidden`, not unmounted.**
`AddNodeCard` holds a minted enrollment token, its countdown and the list of
machines that have turned up since — state a tab switch must not throw away —
and the same is true of a half-typed coordinator address.

Outward, the cards call `state/backend` and `state/resources`:

- `useProviders`, `useProviderKinds` and `useProviderSecretRefs` →
  `/api/providers`, `/api/providers/kinds`, `/api/providers/secret-refs`
- `addProvider`, `removeProvider`, `patchProvider`, `providerModels` →
  POST / DELETE / PATCH `/api/providers[/{id}]` and
  `GET /api/providers/{id}/models`
- `useCluster`, `useCandidates`, `useEnrollments`, `useTopology` →
  `/api/cluster`, `/api/nodes/candidates`, `/api/enroll`, `/api/topology`
- `mintEnrollment`, `revokeEnrollment` → `POST /api/enroll`,
  `DELETE /api/enroll/{token_id}`
- `admit`, `removeNode` → `POST /api/nodes/{id}/admit`, `DELETE /api/nodes/{id}`
- `nodeLogTail` → `GET /api/nodes/{id}/logs`, called by `NodeLogFiles`
- `useSettings`, `patchSettings`
- `api/origin`'s `normalizeBase` and `describeBase`, which are pure, and
  `setCoordinatorBase`, which writes localStorage and wakes every subscriber
- `probeCoordinator`, the one request in this folder that does not go through
  `Backend`: `GET /healthz` on the *candidate* base, then `/api/cluster` to say
  what was found. Deliberately not a `Backend` method — it probes an address
  that has not been adopted, which is the whole point of testing before saving

`invalidate()` follows every successful write, so no card keeps its own copy of
what it just changed.

## Things that look like details and are not

**The enrollment token is the one credential this UI renders, and that is a
decision rather than an oversight.** It arrives inside `Enrollment.command` — a
whole composed shell line, not a field named `token` — so it passes
`redact.ts`'s filter. What makes it acceptable is what the token *is*: minted on
demand for one install, spent on first use, expiring within the hour, revocable
from the same card. No endpoint returns the permanent cluster token, which is
the secret that filter exists to keep off the screen; before this card, the
documented way to add a machine was to copy that one by hand.

**The Models count is a figure, not a way in.** That cell used to open a flat
list of every model the provider publishes, each with a checkbox. At
OpenRouter's several hundred, that was a bulk edit with no model in front of
it — no context window, no price, no verdict, nothing but a name and a tick.
The decision is about one model, so it is made on that model's own page next to
the facts that answer it. The count stays because the gap between served and
published *is* the allowlist, and seeing it is what sends somebody to the
Models tab. The reverse is not offered here either: a bulk control that could
only take things away would be the old list with half its verbs.

**A provider's spend figures are provider-wide, not per-model.** That is why
`$/Mtok` is dashed for anything but a single-model provider, and why the Today
column reads `$x of $y` only when `daily_budget_usd` is set.

**The `key_source` distinction is not cosmetic.** A key in `secrets.json` is one
this coordinator wrote and can replace; a key in the environment belongs to
whatever started the process, and pasting a replacement does not overwrite it —
the coordinator mints a reference and moves the provider onto it, and the
variable stops being what authenticates. `KeyField`'s `displaced` line says so
before the paste rather than after.

## Failure behaviour

- **Every server error is rendered verbatim**, under `whiteSpace: 'pre-wrap'`
  and never paraphrased. On the key path this is the useful half of the
  control: the coordinator's refusal names the environment variable that would
  shadow the reference, and summarising it would delete the only sentence that
  says what to change.
- **Data that has not arrived degrades to empty.** `providers.data ?? []`,
  `cluster.data?.nodes ?? []`, `candidates.data ?? []`. A card with no data
  renders its chrome and an empty table rather than nothing.
- **A malformed coordinator address** makes `normalizeBase` return `null`, which
  disables both Test and Save and prints the expected shapes. Nothing is
  adopted, so the app is still pointed somewhere it can load from.
- **An unenforceable spend cap** disables the input and replaces the source hint
  with the server's own sentence explaining that no provider port reports spend,
  so there is nothing to measure a cap against.
- **A key state the port cannot report** renders "state unknown" in the muted
  tone, never "missing". `missing` itself is a warning and not a fault: nothing
  is broken, the provider is switched off for want of a key, and the row's State
  column is where a fault belongs.
- **A provider catalogue that fails to load** leaves `ProviderBackupPanel`
  showing the fetch error and an empty alias table, and the add form still
  accepts a typed upstream id — the datalist is a suggestion, not a constraint.
- **A verifier whose requirement is unmet** is not a pass. `check.mjs` puts it
  in its own column with the reason printed, and `--strict` makes it a failure.

## Deliberately not built

**A reveal control, and any inverse of `redact.ts`.** Nothing sends a key back
and there is no endpoint for such a control to call. Adding either would be the
bug.

**An address field that joins a node from here.** Join is
worker-to-coordinator and token-gated, so there is no endpoint it could call.
The mockup had the form; `AddNodeCard` replaced it with the line that makes the
other machine call in.

**A bulk model allowlist editor.** Retired with the checkbox list, in both
directions — switching a model off is the same one-model decision as switching
it on.

**A join-token row and a Gateway row on `ClusterCard`.** The first names a
secret no endpoint returns; the second has no source but
`window.location.host`. Add a gateway field to `/api/cluster` before reviving
that one.
