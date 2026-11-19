# Design pack changelog

Newest first. Each entry: date, what changed, the files it touched, why. The
`tool_result` hook appends an entry with `why: <to be completed>` whenever a
token, icon or component changes; the person (or agent) making the change fills
the `why` line in before finishing.

## 2026-11-19 — Rate limits: compact model chart and header controls

- what: hid the model donut's duplicate legend and shortened chart scope
  captions. Removed the repeated panel heading, explanatory paragraph,
  Last/Updated text and unused date formatter. Kept 24h/7d/30d controls at right.
- files: `ui/src/tabs/requests/RateLimitsSub.tsx`,
  `.omp/tests/ui/providers-status.check.mjs`, `.omp/design/DESIGN.md`,
  `ui/src/tabs/requests/README.md`.
- why: reduce repeated model names and header clutter while preserving model
  identification through slice labels, tooltips and accessible chart data.

## 2026-11-19 — Rate limits: paired errors-only donuts

- what: added an Errors by model donut beside Request errors; both exclude
  successful requests. The existing grid stacks the pair on narrow screens.
- files: `ui/src/tabs/requests/RateLimitsSub.tsx`,
  `ui/src/tabs/requests/README.md`, `.omp/design/DESIGN.md`,
  `.omp/tests/ui/providers-status.check.mjs`.
- why: show both failure composition (429 versus other errors) and which models
  failed, without diluting the breakdown with successful traffic. The second
  chart spans all models in the selected provider/window and folds complete
  usage failure counts rather than a paginated log.

## 2026-11-19 — registry: registry changed (values only)

- what: registered donut as a PackChart variant with a matching gallery state.
- files: `.omp/design/components/registry.json`, `.omp/tests/ui/gallery/states.tsx`.
- why: keep the cleaner Rate limits summary on the premade chart API and cover
  its centre total, keyboard access and responsive layout.

## 2026-11-19 — component: component changed (values only)

- what: changed the Rate limits pie to a donut and removed the four standalone
  numeric counters and their unused computations/imports.
- files: `ui/src/components/PackChart.tsx`, `ui/src/tabs/requests/RateLimitsSub.tsx`,
  `.omp/tests/ui/providers-status.check.mjs`.
- why: show the request total once in the ring centre, with the rate-limit
  breakdown in the chart, instead of repeating four prominent numbers.

## 2026-11-19 — registry: registry changed (values only)

- what: registered the existing chart-pack pie as a supported PackChart variant.
- files: `.omp/design/components/registry.json`, `.omp/tests/ui/gallery/states.tsx`.
- why: the Rate limits page now mounts pie through the shared wrapper; the
  gallery, accessibility and layout checks must cover that public variant too.

## 2026-11-19 — component: components added: RateLimitsSub

- what: mounted the existing pack pie chart for rate-limited vs other requests,
  and moved the selected window plus refresh date/time into the top-right header.
- files: `ui/src/components/PackChart.tsx`, `ui/src/tabs/requests/RateLimitsSub.tsx`,
  `.omp/tests/ui/{gallery/states.tsx,components/packchart.check.mjs,providers-status.check.mjs}`.
- why: make the request proportion visible without falsely labelling non-429
  errors successful, and make the freshness/timezone of the numbers explicit.

## 2026-11-19 — registry: registry changed (values only)

- what: added the Usage Rate limits screen and per-model statistics/logs,
  provider/model history links in Settings, and rate-limited request labels.
- files: `ui/src/tabs/{UsageTab.tsx,requests/RateLimitsSub.tsx,requests/RequestsSub.tsx,requests/RequestRows.tsx,requests/RequestDetail.tsx,settings/ProvidersCard.tsx}`,
  `ui/src/state/{routes.ts,router.check.mjs}`, `ui/src/api/types.ts`,
  `.omp/design/{components/registry.json,DESIGN.md}`,
  `.omp/tests/ui/providers-status.check.mjs`.
- why: current “admitting” state erased the visible context of earlier model
  rate limits. The new pane separates current availability from durable
  failures, carries provider/model scope into Requests and records full
  sanitized reasons/retry delays. Uses existing Readout, Disclosure and tables.
- backend: request JSON/CSV gain an exact provider filter; upstream HTTP and
  translated in-band 429 metadata are retained in existing attempt-chain JSON,
  without a new database or schema columns. The existing proxy log records
  provider, model, wait and sanitized reason.

## 2026-11-18 — Settings: model-scoped admission and upstream errors

- what: ProvidersCard reuses Disclosure for a read-only model status table,
  auto-expanded when blocked. Model rows carry their own admission reason,
  cooldown and full upstream error; provider failures remain provider-wide.
- files: `ui/src/tabs/settings/{ProvidersCard.tsx,README.md}`, `ui/src/api/types.ts`,
  `control_plane/providers/{runtime.py,serialization.py,service.py}`,
  `control_plane/gateway/{ui_detail.py,serialize.py}`,
  `.omp/design/{components/registry.json,DESIGN.md}`,
  `.omp/tests/ui/providers-status.check.mjs`. Used by Settings provider rows.
- why: an Anthropic model's 429 was shown as a retrying provider, incorrectly
  suggesting its sibling models were unavailable. No new tokens or icons.

## 2026-11-19 — registry: components added: ProviderSignIn, HostTable, BalancerDialog, OrgsCard, SessionsCard

- what: components added: ProviderSignIn, HostTable, BalancerDialog, OrgsCard, SessionsCard; components removed: ChatGptSignIn.
- files: .omp/design/components/registry.json
- why: reconcile the registry with the existing shared components and screens;
  their individual design changes are documented below. ProvidersCard now also
  names its model-status spec and browser check rather than leaving its states implicit.

## 2026-11-18 — registry: components added: ProviderSignIn

- what: generalized the registered ChatGPT sign-in into ProviderSignIn with chatgpt/claude variants, masked Claude callback, explicit unofficial-policy/billing warning, and stale-operation guards. Migrated its gallery, behavioral checks and settings documentation.
- files: `ui/src/tabs/settings/{ProviderSignIn.tsx,ProvidersCard.tsx,README.md,keyfield.check.mjs}`, `ui/src/api/{types.ts,client.ts}`, `.omp/design/{components/registry.json,DESIGN.md}`, `.omp/tests/ui/{gallery/states.tsx,gallery/main.tsx,components/provider-sign-in.check.mjs}`. Used by Settings provider add/reconnect.
- why: connect Claude subscriptions through the existing provider workflow without duplicating layout or hiding Anthropic's restrictions, leaking callback content, or allowing an old completion to close a different provider's form.
- backend: added the separate `claude_code` kind, PKCE/profile authentication, generation-checked token rotation/removal, paginated native discovery and Messages request/SSE translation. Added session-gated Claude routes and provider-specific sign-in audit kinds. Updated generated contracts, root/provider documentation, and behavior tests. Live Anthropic acceptance still requires operator authorization; the isolated browser/API smoke uses a scripted HTTP boundary.
- stream edge verification: deferred tool-argument validation until the terminal reason so `max_tokens` can preserve partial JSON; protected buffered cancellation cleanup and recorded in-band failure state before emitting frames. `ui/src/api/client.ts` now surfaces translated error envelopes through Chat's existing error display and handles split CRLF boundaries; `ui/src/api/chatstream.check.mjs` covers these failures without synthesizing successful turn metadata.
- operator-facing name: the existing Providers picker lists `claude_code` as **Anthropic plan (unofficial)**; the sign-in remains Claude subscription OAuth, distinct from API-key Anthropic, with all policy/account-risk disclosures retained.

## 2026-11-18 — gallery: ChatGptSignIn states follow the ProviderSignIn rename

- what: the gallery's four sign-in states now render `ProviderSignInView` (`flow="chatgpt"`) as component `ProviderSignIn`, and `components/provider-sign-in.check.mjs` checks that name. `design/index.html` regenerated.
- files: `.omp/tests/ui/gallery/states.tsx`, `.omp/tests/ui/components/provider-sign-in.check.mjs`, `.omp/design/index.html`. The component is `ui/src/tabs/settings/ProviderSignIn.tsx`, used by `ProvidersCard.tsx`.
- why: `ChatGptSignIn.tsx` was renamed to `ProviderSignIn.tsx` and the registry updated, but the gallery still imported the old path, so every gallery-built check (style guide, passwordfield, every component check) failed to bundle.

## 2026-11-18 — tooling: style guide renders at a fixed clock

- what: `design/styleguide.mjs` renders the gallery states with `Date.now` pinned to `STYLEGUIDE_NOW_MS` (2025-11-17T15:46:40Z, just after the newest fixture timestamp in `tests/ui/gallery/states.tsx`), so the cache table's `relativeTime()` cells read `1h ago` / `26h ago` instead of hours-since-fixture. `design/index.html` regenerated.
- files: `.omp/design/styleguide.mjs`, `.omp/design/index.html`. Checked by `tests/ui/generated.check.mjs`.
- why: the output was a function of the wall clock, so `generated.check` failed ("style guide: design/index.html matches its sources") within an hour of every rebuild regardless of what changed.

## 2026-11-18 — screens: cluster editor round 2 (BalancerDialog, wire ×, flag chips, host picker)

- what: new screen `BalancerDialog` (double-click a split or merge on `/cluster`: policy select, its rate limit, live targets, Remove this load balancer), a `Cut this wire` × on a selected editable wire (`.wcut`), launch-flag chips with a remove × on machine rows (`.wflag`, `.wflag-removed`) and `relaunch pending` / `stops on Save` pills (`.pill.wpending`), the shared `HostTable` in an OpenRouter provider row's footer (`.whosts`, sideways scroll), split/merge port labels moved inside the triangles, `Reset layout` replaced by an always-present `Arrange`. Existing tokens only (`--warn`, `--panel-raised`, `--space-*`, `--opacity-45`, `--size-unit`); icons `x-circle`, `route` (existing).
- files: `ui/src/tabs/cluster/{BalancerDialog.tsx,WiringCanvas.tsx,edges.tsx,GraphToolbar.tsx,doc.ts,sockets.ts,blocks/Balancer.tsx,blocks/Containers.tsx,blocks/editor.tsx}`, `ui/src/tabs/ClusterTab.tsx`, `ui/src/styles/derate.css`, `.omp/design/components/registry.json` (screen row, via pack:sync), `.omp/design/DESIGN.md` (BalancerDialog).
- why: the operator asked to configure a load balancer in place, re-route and cut wires directly, remove a deployment's launch flags from its row, pick an OpenRouter host with its price, and never have blocks spawn on top of each other.

## 2026-11-18 — tooling: esbuild ^0.25

- what: the direct `esbuild` devDependency moves from `^0.21.5` to `^0.25.12` in the pack, the chart pack and the UI (installed: 0.25.12). Vite 5's own nested esbuild is unchanged.
- files: `.omp/package.json`, `.omp/package-lock.json`, `.omp/charts/package.json`, `.omp/charts/package-lock.json`, `ui/package.json`, `ui/package-lock.json`. Used by every verifier that bundles TS through `ui/src/check/harness.mjs` `load()` and by the pack's own scripts.
- why: security audit SC-11 — esbuild < 0.25 carries GHSA-67mh-4wv8-2f99 (its dev server answers any origin). Dev-only and never served, but the pin was the only thing keeping it.

## 2026-11-18 — component: HostTable

- what: new shared component `HostTable` (`ui/src/components/HostTable.tsx`, exports `HostTable`, `perMtok`): OpenRouter's backend hosts for one model as a radio table (Automatic row, host + tag, in/out $/Mtok, context, quantization, `cheapest` pill) with a "Fall back to other hosts" checkbox. States default, pinned, loading, error, empty. No new CSS classes or tokens (global `table`, `td.num`, `.pill`, `.unit`, `.sr-only`; `--space-6`).
- files: `ui/src/components/HostTable.tsx`, `ui/src/tabs/models/BackendPicker.tsx` (replaces its `<select>`), `.omp/tests/ui/gallery/states.tsx`, `.omp/tests/ui/components/hosttable.check.mjs`, `.omp/design/components/registry.json`, `.omp/design/DESIGN.md`. The cluster editor's provider rows will use it too.
- why: a pinned host now sets the model's price and can allow fallback, so the pick needs prices side by side and a fallback switch; one shared table serves both the Models tab and the cluster editor.

## 2026-11-18 — screens: SessionsCard and OrgsCard (containment)

- what: two screen cards, both built from existing classes and components only (`card2`, `pill`, `ghost` buttons, `Icon` `user`/`log-out`, `Verbatim`; tokens `space-6`/`space-8`). `SessionsCard` (Settings → Admins, under `AdminsCard`) lists every live admin session — signed-in-from, last-seen-from with a `moved` pill, browser, last seen — with a per-admin "Sign out everywhere" behind an inline confirm. `OrgsCard` (Usage → Keys, under the key table) lists orgs with their active-key count and a "Disable every key" action behind a confirm card with an optional reason, the same shape as `KeyActions`' confirm.
- files: ui/src/tabs/settings/SessionsCard.tsx, ui/src/tabs/keys/OrgsCard.tsx, ui/src/tabs/SettingsTab.tsx, ui/src/tabs/keys/KeysSub.tsx, ui/src/api/{client.ts,types.ts}, .omp/design/components/registry.json (screen rows, via pack:sync)
- why: security plan step 3.5 — containment after an intrusion. `GET /api/auth/sessions`, `DELETE /api/admins/{username}/sessions` and `POST /api/orgs/{org_id}/keys/disable` are new server routes, and an operator needs them on screen rather than in curl when a cookie or key is known to be stolen.

## 2026-11-18 — screens: Overview has failures, spend, keys-by-day, cache and fallbacks

- what: Dashboard → Overview's tile line gains `failed`, `$ spent` (marked "at least" when unpriced requests sit beside priced ones), `prompt cache hits` and `fallbacks` over the window. New charts: "Requests per day by key" (stacked-bar, top 5 keys + other) and "Failures per day, by error type" (`multi-line`, one line per `error_class`; a window with no failures is one flat `failed` line at 0, not an empty plate, which the bar types draw for all-zero data). `PackChart` gains `grouped-bar` and `multi-line` (`PackTrendRow` input; registry variants, DESIGN.md, gallery states).
- what (later the same day): "Requests per key", "Speed per model" and "Keys near a limit" were added and then hidden at the operator's request; their folds were removed with them. Charts sit two per line: tokens/day + requests/day by key, local vs cloud + models, then failures per day full width; one per line below ~860px.
- files: ui/src/components/PackChart.tsx, ui/src/tabs/dashboard/{UsageCard.tsx,usageSummary.ts,usageSummary.check.mjs}, .omp/design/components/registry.json, .omp/design/DESIGN.md, .omp/tests/ui/gallery/states.tsx, .omp/tests/ui/components/packchart.check.mjs
- why: the Overview showed only what worked and who sent it; an operator also needs what failed, when, and what it cost. A failures chart has to show "none" as a reading, so it is a line that sits at zero.

## 2026-11-18 — component: tokens added: limit, graph-block-bg, graph-block-border, graph-block-selected, graph-req, graph-resp, graph-limit, graph-deploy, graph-link, graph-fault, graph-warn, graph-dots

- what: tokens added: limit, graph-block-bg, graph-block-border, graph-block-selected, graph-req, graph-resp, graph-limit, graph-deploy, graph-link, graph-fault, graph-warn, graph-dots; icons added: route, server, cloud, gauge, zoom-in, zoom-out, fit-view, x-circle; components added: BlockFrame, IssueList, Markdown, SessionStats, SplitBlock, MachineBlock, EditorContext, ModelBlock, InputBlock, PathRegistry, ParticleLayer, WiringCanvas, UsageCard; components removed: ClusterGraph, BandSweep.
- files: ui/src/components/PackChart.tsx, ui/src/components/BlockFrame.tsx, ui/src/styles/derate.css, ui/src/tabs/cluster/blocks/Balancer.tsx, ui/src/tabs/cluster/particles.ts, ui/src/tabs/cluster/sockets.ts
- why: `PackChart` / `UsageCard` (this session): `grouped-bar` added to `PackChart` for the Overview's "Speed per model" chart; see the "Overview has failures, speed, spend…" entry below. The tokens, icons and the BlockFrame…WiringCanvas rows are another session's wiring-editor work in flight; its owner owes their why.

## 2026-11-18 — tokens: `batch` kept with a reason; editor screen rows

- what: `semantic.color.batch`'s `$description` now says why it stays with no ui/src usage. The screen rows the hook lists (SplitBlock, MachineBlock, ModelBlock, InputBlock, EditorContext, PathRegistry, ParticleLayer, WiringCanvas) are the wiring editor's, in place of the floor's ClusterGraph and BandSweep.
- files: .omp/design/tokens/tokens.json; ui/src/tabs/cluster/{WiringCanvas,ParticleLayer,edges}.tsx, ui/src/tabs/cluster/blocks/*.tsx.
- why: the editor's response blocks are `--stream` only (decision: one colour per socket type), which left `batch` unused; pack-sync asks for removal or a reason, and the chat tag palette still keeps its hues clear of it.

## 2026-11-18 — components + screen: the /cluster wiring editor (BlockFrame, IssueList)

- what: two shared components. `BlockFrame` (`ui/src/components/BlockFrame.tsx`): the titled panel every wiring-editor block shares — selection and the warn/fault tone are its border, nothing else colours it. `IssueList` (`ui/src/components/IssueList.tsx`): errors first, icon + colour per level, messages verbatim, per-row actions. The `/cluster` floor (ClusterGraph, layout, motion, LoadingBand, order and their checks) is replaced by a React Flow editor in `ui/src/tabs/cluster/` (sockets.ts, doc.ts, WiringCanvas, blocks/*, edges, ParticleLayer, positions, GraphToolbar) that reads and writes two new server fields, `RoutingConfig.limits` (persisted in routing.json, enforced as the `route` limit scope) and `Deployment.speculative_tokens` (MTP), through the new `POST /api/wiring` batch. derate.css: the floor rules (`.floorcard`, `.floor`, `.machine`, `.band`, `.edge`, `rect.ring`, `.bandsweep-*`, `.banddot*` and their reduced-motion block) are gone; `.graphcard`, `.graph`, `.graph-box`, the `.w*` block classes, `.sock*`, `.wlabel`, `.wparticles` are new, all on pack tokens (the `--graph-*` group in the entry below).
- files: ui/src/components/{BlockFrame,IssueList}.tsx, .omp/tests/ui/gallery/states.tsx, .omp/tests/ui/components/{blockframe,issuelist}.check.mjs, .omp/design/components/registry.json, .omp/design/DESIGN.md#blockframe, #issuelist, ui/src/styles/derate.css; used by ui/src/tabs/ClusterTab.tsx and ui/src/tabs/cluster/blocks/*.
- why: the cluster tab becomes a typed wiring editor — every socket and wire is a pack colour by type, every block shares one frame, and the Checks card needs one list that keeps the server's refusals verbatim.

## 2026-11-18 — screens: Chat tab stats, thinking and markdown

- what: two new screen rows, `Markdown` (`ui/src/tabs/chat/Markdown.tsx`, an answer rendered as React elements only — links http(s) only, raw HTML and images stay text) and `SessionStats` (`ui/src/tabs/chat/SessionStats.tsx`, a `.quad` of `Readout` tiles copied from `AggregateRow`'s idiom). New chat-block rules in derate.css — `.chattop`, `.chatstats`, `.think`, `.turnmeta`, `.md …` — all on existing tokens (`--space-*`, `--rule`, `--ink-muted`, `--panel-sunk`, `--font-mono`, `--size-label`, `--radius`); `.chathead` lost its own rule and padding to `.chattop`. The thinking control reuses `.chips` + `button[aria-pressed]` (`style={{margin:0}}` in the composer row) and the Thinking block reuses `Disclosure`. No token, icon or shared component added.
- files: ui/src/tabs/chat/{Markdown.tsx,SessionStats.tsx,Transcript.tsx,Composer.tsx,turn.ts,reasoning.ts,stats.ts,README.md}, ui/src/tabs/ChatTab.tsx, ui/src/api/{client.ts,types.ts,think.ts}, ui/src/styles/derate.css, .omp/design/components/registry.json; checks ui/src/api/{think,chatstream}.check.mjs, ui/src/tabs/chat/{reasoning,stats,markdown,chat}.check.mjs.
- why: the chat tab printed one meta line and plain text; it now shows every stat per turn and per session (ttft, thinking time, decode time, tok/s, prompt/completion/reasoning tokens, context used), surfaces the model's reasoning, offers a per-request thinking switch or effort level matched to the model family, and says under each answer whether reasoning came back and whether the deployment's `--reasoning-parser` separated it.

## 2026-11-18 — icons: tokens added: limit, graph-block-bg, graph-block-border, graph-block-selected, graph-req, graph-resp, graph-limit, graph-deploy, graph-link, graph-fault, graph-warn, graph-dots

- what: tokens added: limit, graph-block-bg, graph-block-border, graph-block-selected, graph-req, graph-resp, graph-limit, graph-deploy, graph-link, graph-fault, graph-warn, graph-dots; icons added: route, server, cloud, gauge, zoom-in, zoom-out, fit-view, x-circle; icons removed: building; components added: ChatGptSignIn, PackChart, ActorPanel, AuditTable, UsageCard, KeysSub, RequestsSub, OverviewSub, UsageTab; components removed: KeysTab, OrgDetail, OrgFields, OrgsTab, RequestsTab, SpendTab.
- files: .omp/design/icons/icons.json, ui/src/styles/derate.css, ui/src/tabs/cluster/sockets.ts
- why: the eight icons a peer added for the /cluster wiring editor (entry below) were registered without the `replaces` field every other entry carries, which crashed `styleguide.mjs` and with it `generated.check`; each now has `"replaces": []` (none of them replaced a glyph), and icons.svg, icons.ts and index.html were rebuilt. The token, component and removal lists above are that entry's and earlier ones' changes, picked up by the hook's diff, not new here.

## 2026-11-18 — tokens + icons: the /cluster wiring editor

- what: semantic `limit` (`--limit`; magenta.600 / .300 / .800, the `tag-2` steps), promised at 3:1 on panel in pairs.json; `component.graph.*` (`--graph-block-bg`, `--graph-block-border`, `--graph-block-selected`, `--graph-req`, `--graph-resp`, `--graph-limit`, `--graph-deploy`, `--graph-link`, `--graph-fault`, `--graph-warn`, `--graph-dots`), all references; icons `route`, `server`, `cloud`, `gauge`, `zoom-in`, `zoom-out`, `fit-view`, `x-circle`; a `.react-flow` block in derate.css mapping `--xy-*` onto them, and `@xyflow/react/dist/style.css` imported before tokens.css in main.tsx.
- files: .omp/design/tokens/{tokens.json,pairs.json}, .omp/design/icons/{icons.json,svg/*.svg}, ui/src/styles/derate.css, ui/src/main.tsx; used by ui/src/tabs/cluster/* (sockets, blocks, edges, toolbar).
- why: the /cluster floor is replaced by a typed React Flow node editor whose socket/wire colours must be pack tokens, never new hex.

## 2026-11-18 — component: ChatGptSignIn is two plain steps again

- what: removed the forwarder block (the `curl … /auth/forwarder | python3 -` and `ssh -L` `Copyable` rows), the `origin` and `loopback` props and the loopback status line. `start` is one line and "Continue with ChatGPT"; `paste` is one instruction and the paste field. The coordinator still listens on 127.0.0.1:1455 itself, silently. Server: `GET /auth/forwarder`, its script, `DERATE_FORWARDER_PORT` and the start response's `loopback` field are gone. The UI check now asserts no step asks the operator to run a command.
- files: ui/src/tabs/settings/{ChatGptSignIn.tsx,README.md}, ui/src/api/types.ts, control_plane/gateway/internal_api.py, control_plane/envspec.py, .omp/design/DESIGN.md#chatgptsignin, .omp/tests/ui/gallery/states.tsx, .omp/tests/ui/components/chatgpt-sign-in.check.mjs, .omp/work/auth-keys/sets/setup_routes.json, tests/unit/test_chatgpt_loopback.py
- why: the target user is not going to run a terminal command to sign in; the screen has to stay simple.

## 2026-11-18 — component: ChatGptSignIn says when the coordinator holds 127.0.0.1:1455 itself

- what: `ChatGptSignInView` takes a `loopback` prop (`start`'s new `loopback` field: `'listening'` or the coordinator's reason, verbatim); the `paste` step leads with one `.unit` line saying so. Server side, `control_plane/gateway/loopback.py` binds 127.0.0.1:1455 from a sign-in's start until the attempt expires or one callback finishes, answering `/auth/callback` through the same handler as the gateway route. No token, icon or component added.
- files: ui/src/tabs/settings/ChatGptSignIn.tsx, ui/src/api/types.ts, control_plane/gateway/{loopback.py,internal_api.py}, .omp/design/DESIGN.md#chatgptsignin, .omp/tests/ui/gallery/states.tsx
- why: an operator on VS Code Remote-SSH had no callback reach the coordinator; with the coordinator listening on the redirect port, VS Code's automatic port forwarding (or `ssh -L 1455:127.0.0.1:1455`) carries the redirect with nothing to run, the way omp and the Codex CLI sign in.

## 2026-11-18 — component: ChatGptSignIn shows the commands that make the redirect land by itself

- what: the `start` and `paste` steps of `ChatGptSignInView` render a forwarder block — a `.unit` line and two existing `Copyable` rows: `curl -fsSL <origin>/auth/forwarder | python3 -` (the coordinator's new open route `GET /auth/forwarder` serves a stdlib script that listens on 127.0.0.1:1455 and 302s the browser on to this coordinator's `/auth/callback`) and `ssh -L 1455:127.0.0.1:<port> <host>`. The view takes a new required `origin` prop (`coordinatorBase()` or `window.location.origin`); the gallery passes `http://spark-01:8080`. The paste instruction now says the tab finishes by itself with a command running, and keeps the paste fallback. No token, icon or component added; `Copyable` is reused.
- files: ui/src/tabs/settings/{ChatGptSignIn.tsx,README.md}, control_plane/gateway/internal_api.py, .omp/design/DESIGN.md#chatgptsignin, .omp/tests/ui/gallery/states.tsx, .omp/tests/ui/components/chatgpt-sign-in.check.mjs, .omp/work/auth-keys/sets/setup_routes.json
- why: OpenAI accepts only a loopback `redirect_uri` (an `https://<lan-ip>:8090/auth/callback` is refused 400 `invalid_authorize_request`), so a sign-in from a laptop could only finish by copying the address out of the browser; a listener on the laptop's 1455 is the only way to make it automatic.

## 2026-11-18 — screens: Dashboard Overview is one tile line plus usage charts; Deployments and free memory on Telemetry

- what: Overview is one line of tiles — tok/s and serving (`AggregateRow`), then tokens, requests, cloud share, models used over the window (`UsageCard`) — and the usage charts: tokens per day, local vs cloud, models, and a new "Requests per key" bar (`usageSummary.ts::requestsByKey`, `GET /api/usage?group_by=key_id` plus `GET /api/keys` for names). The 7d/30d/90d chips (`UsageWindow`) sit right of the sub-tab bar. Removed from Overview: the watts tile, the "Spend and keys" link, the Usage heading. `DeploymentsStrip` and a "GiB addressable free" tile (`AggregateRow.tsx::freeGiB`) moved to Telemetry. No token, icon or component added; `Tile` is shared from `AggregateRow.tsx`.
- what (later the same day): the Load sub-tab is gone; `LoadSub` renders at the bottom of Telemetry under a "Load" heading, so Dashboard has three sub-tabs.
- files: ui/src/tabs/DashboardTab.tsx, ui/src/tabs/dashboard/{AggregateRow.tsx,UsageCard.tsx,usageSummary.ts,usageSummary.check.mjs,README.md}, ui/src/tabs/README.md
- why: the Overview was crowded — two tile rows, a heading and the deployments strip; the operator wanted one glanceable line plus who is sending requests.

## 2026-11-18 — component: PackChart, and a Usage card on Dashboard → Overview

- what: new shared component `PackChart` (`ui/src/components/PackChart.tsx`), the React mount for chart-pack types — `stacked-bar` and `stacked-bar-100` today — lazy-loaded with `import()` so d3/zod stay out of the main chunk. Registry row, `DESIGN.md#packchart`, gallery states (stacked-bar default/empty, stacked-bar-100 default) and `.omp/tests/ui/components/packchart.check.mjs`. New screen row `UsageCard` (`ui/src/tabs/dashboard/UsageCard.tsx`): tokens/requests/cloud-share/models tiles, tokens per day split local/cloud, local-vs-cloud and model-mix 100% bars, over 7/30/90 days from one `GET /api/usage` query. No token or icon added: chart paint is the chart pack's `--oc-*` theme.
- files: ui/src/components/PackChart.tsx, ui/src/tabs/dashboard/{UsageCard.tsx,usageSummary.ts,usageSummary.check.mjs}, ui/src/tabs/DashboardTab.tsx, .omp/design/components/registry.json, .omp/design/DESIGN.md, .omp/tests/ui/gallery/states.tsx, .omp/tests/ui/components/packchart.check.mjs, .omp/charts/core/{cartesian,theme,define}.ts, Dockerfile, .dockerignore, .github/workflows/publish-image.yml. Harness: `.omp/tests/ui/lib/component.mjs` re-looks-up each button before every Enter/Space (the pack's legend replaces its buttons on toggle); `.omp/tests/ui/layout.check.mjs` skips sr-only content (≤1px computed box that clips), since the pack's hidden data table is wider than that box by design.
- why: the dashboard had no overview of how much the cluster is used or how much of it goes to cloud providers; RULES.md forbids one-off charts in app code, so the charts come from the pack and `PackChart` is its first `ui/src` consumer.

## 2026-11-18 — component: ChatGptSignIn finishes from the redirect, or from the Network panel

- what: the paste step polls `GET /api/sign-in/chatgpt/{attempt}` every 2 s, so a redirect that reaches the coordinator's new `GET /auth/callback` (browser on the coordinator, or `ssh -L 1455:127.0.0.1:<port>`) moves the form to `done` without a paste. The instruction now says where the address is when the tab hangs on the redirect instead of landing: F12 → Network → the request to 127.0.0.1:1455. The coordinator accepts a URL, a `Location:` line or a copied cURL command. No token, icon or component added.
- files: ui/src/tabs/settings/ChatGptSignIn.tsx, ui/src/api/{client.ts,types.ts}, ui/src/tabs/settings/README.md, .omp/design/DESIGN.md
- why: OpenAI's consent page hung on the 127.0.0.1 redirect in an operator's browser, so the address bar never showed the code; the code was only visible in the Network panel.

## 2026-11-18 — screens: audit log rows are one line; double-click opens an actor sub-tab

- what: every Settings → Audit log row is one line — the Changes cell shows the first change (`diff.ts::changeText`, clipped at 32ch) and an unclipped `(+N more)`, with every change in the cell's `title`; Target ids are ellipsis-clipped, every cell `nowrap`, narrow screens scroll sideways. Double-click (or Enter on a focused row) opens an **Actor · <name>** sub-tab in a `.subs` tablist inside the card, closed by an `×` (icon `close`): the event in full, a "Seen from IPs" summary (`ipSummary`), every `key_created` row the actor wrote, and the actor's whole history paged; the row being shown is lifted to `--panel` with an inset `--select-on-panel` left edge (`AuditTable.tsx::rowStyle`). New screen rows `ActorPanel` (`tabs/audit/ActorPanel.tsx`) and `AuditTable` (`tabs/audit/AuditTable.tsx`: the shared table, `TargetCell`, `Pager`, `rowStyle`); `AuditTab` loses `arrow-right`/`chevron-*` (moved with the list and pager) and gains `close`. No token, icon or shared component added.
- files: ui/src/tabs/audit/{AuditTab,AuditTable,ActorPanel}.tsx, ui/src/tabs/audit/{diff.ts,audit.check.mjs,README.md}, .omp/design/components/registry.json
- why: multi-line Changes cells made the log hard to scan; after a suspected intrusion every action an account took — and the keys it minted and the addresses it came from — has to be re-checkable from one place. Built from the existing `GET /api/audit` (`actor` is an exact match); no server change.

## 2026-11-18 — screens: Requests, Spend and Keys merged into one Usage destination

- what: the header's Requests, Spend and Keys entries are one destination, **Usage** at `/usage`, with three URL panes in a `.subs` tablist: Overview (`/usage`, the old spend screen), Requests (`/usage/requests[/<id>]`), Keys (`/usage/keys[/<id>]`, "New key" still opens the `KeyCreate` modal). Registry screen rows renamed with their files — `SpendTab` → `OverviewSub` (`tabs/spend/OverviewSub.tsx`), `KeysTab` → `KeysSub`, `RequestsTab` → `RequestsSub` — and one added, `UsageTab` (`tabs/UsageTab.tsx`). No token, icon or component added: the pane bar reuses `.subs` as Dashboard and Settings draw it. Old `/spend`, `/keys/...`, `/requests/...` URLs parse as aliases and are re-spelled.
- files: ui/src/state/{routes.ts,router.check.mjs}, ui/src/tabs/UsageTab.tsx, ui/src/tabs/spend/OverviewSub.tsx, ui/src/tabs/keys/{KeysSub,KeyCreate,KeyDetail}.tsx, ui/src/tabs/requests/{RequestsSub,RequestDetail,RequestRows}.tsx, ui/src/shell/{AppShell,Header,HealthBanner}.tsx, ui/src/shell/screens.check.mjs, ui/src/tabs/{SetupTab.tsx,chat/Transcript.tsx,audit/diff.ts,audit/audit.check.mjs,keys/reveal.check.mjs}, .omp/design/components/registry.json, .omp/design/DESIGN.md, .omp/design/tokens/pairs.json
- why: three nav entries for one subject (who used what, what it cost, under which key) that already shared `keys/rows.ts`, `RequestRows` and the `?win=` field; the nav goes from eight entries to six.

## 2026-11-18 — component: ChatGptSignIn (Sign in with ChatGPT as a provider credential)

- what: new shared component `ChatGptSignIn` (+ its pure `ChatGptSignInView`), states `start`, `paste`, `error`, `done`, icon `external-link`, tokens `space-8`, `space-4`, `space-24`, `fault`. `ProvidersCard` renders it in place of Base URL / `KeyField` / Add for a kind with `sign_in: 'chatgpt'`, and as "Sign in again" in a signed-in row's key expander; the row's key cell shows `sign_in_account` above the key-state note (`reauth` → "sign-in expired — sign in again", warn). No new tokens or icons; no ChatGPT mark (brand marks are not pack icons).
- files: ui/src/tabs/settings/{ChatGptSignIn,ProvidersCard}.tsx, ui/src/tabs/settings/{keyfield.ts,keyfield.check.mjs}, ui/src/api/{types,client,redact}.ts, ui/src/shell/screens.check.mjs, .omp/design/components/registry.json, .omp/design/DESIGN.md#chatgptsignin, .omp/tests/ui/gallery/states.tsx, .omp/tests/ui/components/chatgpt-sign-in.check.mjs
- why: the new `chatgpt` provider kind's credential is an OAuth sign-in, not a key, so it needs its own control; the coordinator never listens on the 127.0.0.1 redirect, so the control walks the operator through pasting the address back from any browser.

## 2026-11-18 — screens: key creation is a modal opened from the API keys card

- what: `KeyCreate` is no longer its own `card2` with a "Create a key" disclosure. It renders a "New key" button (icon `plus`) in the API keys card's heading, and the form opens as a modal (`.sheet` scrim + `.card.wide`, `role="dialog"`, labelled "New API key"): focus lands on Name, Tab is held inside (`focusTrap.containTab`), Escape / scrim / Close / Cancel dismiss it and drop the draft, focus returns to the button. On success the form gives way to `RevealModal`, unchanged. Registry rows: `KeyCreate` gains icon `plus`; `KeysTab` gains `space-8`.
- files: ui/src/tabs/keys/{KeyCreate,KeysTab}.tsx, ui/src/tabs/keys/reveal.check.mjs, ui/src/tabs/keys/README.md, .omp/design/components/registry.json
- why: the create form was a second card above the table it adds to; one card with the action in its heading, and the form in a modal, keeps the list in place while a key is made.

## 2026-11-18 — screens: orgs dropped from the UI; icon `building` removed

- what: the `/orgs` destination and its screen (`OrgsTab`, `OrgDetail`, `OrgFields`) are gone, with the header's Orgs tab, the keys table's Org column and filter, the create form's Org select, the key page's Org row, Spend's "By org" card and the audit log's org links. Icon `building` removed (it only drew orgs). `limitLamp` moved from `tabs/orgs/rows.ts` into `tabs/keys/rows.ts` (used by `SpendTab.tsx`). (The hook's "components added: Banner, PasswordField, …" is the earlier eye/eye-off entry's list again, not this change.)
- files: .omp/design/icons/icons.json, .omp/design/icons/svg/building.svg (deleted), .omp/design/icons/icons.svg, .omp/design/components/icons.ts, .omp/design/components/registry.json, ui/src/tabs/orgs/ (deleted), ui/src/state/routes.ts, ui/src/shell/{AppShell,Header}.tsx, ui/src/tabs/keys/*, ui/src/tabs/SpendTab.tsx, ui/src/tabs/audit/{diff.ts,AuditTab.tsx}, ui/src/api/{client,types}.ts
- why: every setting a key is governed by — expiry, limits, allowed models, routing, fallback — is set on the key itself, so the UI has one Keys tab and no orgs. The backend is unchanged: a new key lands in the server's default org because the form no longer sends `org_id`.

## 2026-11-18 — screens: audit log moved into Settings

- what: `AuditTab` is no longer the `/audit` destination; it is the Settings → Audit log sub-tab, beside Admins. `audit` removed from `Dest`/`SEGMENT` and the header nav; `/audit` now reads as an unknown path. Component unchanged.
- files: ui/src/tabs/SettingsTab.tsx, ui/src/shell/{AppShell,Header}.tsx, ui/src/state/routes.ts, ui/src/state/router.check.mjs, ui/src/tabs/README.md, ui/src/tabs/audit/{AuditTab.tsx,README.md}
- why: the audit log is read by the people who manage admins and did not need a top-level header slot.

## 2026-11-18 — registry: registry changed (values only)

- what: registry changed (values only).
- files: .omp/design/components/registry.json
- why: `PasswordField` no longer uses `space-34`: the input stopped reserving right padding for an overlaid eye once the wrapper became the box and the eye moved beside the input inside it.

## 2026-11-18 — registry: registry changed (values only)

- what: registry changed (values only).
- files: .omp/design/components/registry.json
- why: the `PasswordField` row's `tokens` now lists what the in-box toggle uses (`space-3`, `ink`, `panel-sunk` added; `space-8` dropped); see "PasswordField: toggle inside the input" below.

## 2026-11-18 — PasswordField: toggle inside the input

- what: the `PasswordField` show/hide eye now sits inside the text box. The `.pwfield` wrapper draws the box (background, border, radius, and the focus ring via `:has(:focus-visible)`); the input inside is borderless and transparent and the toggle follows it inside the same box (margin `--space-3`). Around `.setup-field` the box takes `--panel-raised` / `--edge` (setup.css). Icon `--ink-muted`, `--ink` on hover/while shown. Also: `.setup-main` drops the stray `border-right` it inherited from derate.css's shell `main` rule, so setup and sign-in are one centred column with no vertical line.
- files: `ui/src/components/PasswordField.tsx`, `ui/src/styles/base.css`, `ui/src/tabs/setup/setup.css`, `.omp/design/components/registry.json`, `.omp/design/DESIGN.md`. Used by: `ui/src/tabs/setup/AdminStep.tsx`, `ui/src/tabs/login/LoginTab.tsx`, `ui/src/tabs/settings/AdminsCard.tsx`.
- why: asked for — the eye belongs in the text box, the way password fields usually show it, rather than as a separate button next to it. A wrapper-drawn box rather than a button absolutely placed over the input, because the layout check refuses overlapping interactive elements and an overlay can hide typed text.

## 2026-11-18 — component: icons added: eye, eye-off

- what: icons added: eye, eye-off; components added: Banner, HealthBanner, RequestDetail, RequestRows, RequestsTab, PasswordField.
- files: ui/src/components/PasswordField.tsx
- why: auto-appended by the hook when `PasswordField` landed; the PasswordField and eye/eye-off half is the 2026-11-18 "PasswordField, icons eye / eye-off" entry below. The other components it lists were a peer's in-flight work, not this change.

## 2026-11-18 — PasswordField, icons eye / eye-off

- what: new shared component `PasswordField` (password input + trailing show/hide ghost toggle, `aria-pressed`, `aria-label` Show/Hide password) and new icons `eye`, `eye-off` (category action); `.pwfield` rules in `ui/src/styles/base.css`.
- files: `ui/src/components/PasswordField.tsx`, `ui/src/styles/base.css`, `.omp/design/icons/svg/eye.svg`, `.omp/design/icons/svg/eye-off.svg`, `.omp/design/icons/icons.json`, `.omp/tests/ui/gallery/states.tsx`, `.omp/tests/ui/components/passwordfield.check.mjs`, `.omp/design/components/registry.json`, `.omp/design/DESIGN.md`. Used by: `ui/src/tabs/setup/AdminStep.tsx`, `ui/src/tabs/login/LoginTab.tsx`, `ui/src/tabs/settings/AdminsCard.tsx`.
- why: no password box in the product (setup, sign-in, add-admin, change-password) let you see what you typed; one shared component gives all five the same toggle, keyboard and ARIA contract.

## 2026-11-18 — component: component changed (values only)

- what: component changed (values only).
- files: ui/src/components/Banner.tsx
- why: the action link took the browser's default blue (1.89:1 on dark `--panel`, layout.check); it now inherits the tone colour and stays underlined.

## 2026-11-18 — component: components added: HealthBanner

- what: components added: HealthBanner.
- files: ui/src/shell/HealthBanner.tsx (screen row), ui/src/components/Banner.tsx (action link min-height `--space-24`), ui/src/shell/AppShell.tsx (mount)
- why: HealthBanner announces unreachable nodes, lost targets and failed-over request counts under the header on every screen; the Banner action was 23px tall and failed the 24×24 target rule (WCAG 2.5.8).

## 2026-11-18 — registry: registry changed (values only)

- what: registry changed (values only).
- files: .omp/design/components/registry.json
- why: Banner's scaffolded row replaced by the real one (props tone/children/action, tone variants fault/warn, states default/with-action, its actual tokens) so the registry describes the component `HealthBanner` uses.

## 2026-11-18 — component: components added: Banner, RequestDetail, RequestRows, RequestsTab

- what: components added: Banner, RequestDetail, RequestRows, RequestsTab.
- files: ui/src/components/Banner.tsx
- why: the Requests destination (one row per client request, per-attempt detail) and a shell-wide health banner (UX review 2026-11-17, findings on request log and node/provider health); Banner is used by `ui/src/shell/HealthBanner.tsx`.

## 2026-11-18 — scaffold Banner

- what: new shared component `Banner` (scaffolded).
- files: `ui/src/components/Banner.tsx`, `.omp/tests/ui/gallery/states.tsx`, `.omp/tests/ui/components/banner.check.mjs`, `.omp/design/components/registry.json`, `.omp/design/DESIGN.md`.
- why: a node going down or a provider's targets failing had no app-wide announcement; Banner is the persistent strip `HealthBanner` (mounted in `AppShell.tsx` under the header) renders.

## 2026-11-18 — component: RevealModal traps Tab; Sheet and RevealModal share `components/focusTrap.ts`

- what: `RevealModal` now contains Tab/Shift+Tab inside its card; the containment logic moved out of `shell/Sheet.tsx` into `ui/src/components/focusTrap.ts` (`FOCUSABLE`, `focusables`, `containTab`) and both modals call it. `reveal-modal.check.mjs` asserts the trap; DESIGN.md#revealmodal no longer lists it as a gap.
- files: ui/src/components/RevealModal.tsx, ui/src/components/focusTrap.ts, ui/src/shell/Sheet.tsx, .omp/tests/ui/components/reveal-modal.check.mjs, .omp/design/DESIGN.md
- why: a modal that cannot be dismissed by Escape must not let focus wander to the page behind it; the Sheet's boundary rules were found by getting them wrong once, so they are shared rather than copied.

## 2026-11-18 — component: component changed (values only)

- what: component changed (values only).
- files: ui/src/components/RevealModal.tsx, ui/src/styles/derate.css (`.reveal .cmd`)
- why: gate finding from reveal-modal.check.mjs (axe scrollable-region-focusable): Copyable's `.cmd` is `white-space: pre; overflow-x: auto`, so the 58-char key scrolled out of the card in an unfocusable region; a key you cannot see whole cannot be written down, so the card gets `reveal` and the secret wraps.

## 2026-11-18 — registry: registry changed (values only)

- what: registered RevealModal (shared; props secret/keyId/onDone; states default, long; Sheet + Copyable tokens), spec DESIGN.md#revealmodal, gallery states + REVEAL_FIXTURES, check reveal-modal.check.mjs.
- files: .omp/design/components/registry.json, .omp/design/DESIGN.md, .omp/tests/ui/gallery/states.tsx, .omp/tests/ui/gallery/main.tsx, .omp/tests/ui/components/reveal-modal.check.mjs; used by ui/src/components/RevealModal.tsx (setup admin step, key create/rotate).
- why: inbound auth (auth-keys plan W3) needs a one-time reveal of the drt_ secret the server keeps only as a hash; the dialog must not be dismissable by Escape/scrim and is the only place a key may render, so it is registered, specced and checked.

## 2026-11-18 — registry: icons added: key, building, scroll, log-out, lock, user

- what: icons added: key, building, scroll, log-out, lock, user; components added: RevealModal.
- files: .omp/design/components/registry.json
- why: auth-keys W3 — the login, keys, orgs and audit screens and the header's sign-out control use these glyphs; RevealModal is the one-time `drt_` key reveal (see the registry entry above).

## 2026-11-18 — icons: icons added: key, building, scroll, log-out, lock, user

- what: icons added: key, building, scroll, log-out, lock, user.
- files: .omp/design/icons/icons.json, .omp/design/icons/svg/{key,building,scroll,log-out,lock,user}.svg, .omp/design/icons/icons.svg, .omp/design/components/icons.ts (generated)
- why: auth-keys W3: the login, keys, orgs and audit screens and the header's sign-out control. `icon_search` found no existing match for any of the six (nor for logout / sign out / audit / person). Intended users: `key` → KeysTab + header nav, `building` → OrgsTab + header nav, `scroll` → AuditTab + header nav, `log-out` → Header sign-out, `lock` → LoginTab, `user` → Header signed-in admin / AdminsCard.

## 2026-11-18 — icons: icons changed (values only)

- what: icons changed (values only).
- files: .omp/design/icons/svg/user.svg
- why: same change as the entry above — `icons:build` (SVGO) rewrote the freshly drawn `user.svg` source in place; geometry unchanged in meaning. auth-keys W3: the login, keys, orgs and audit screens and the header's sign-out control.

## 2026-11-18 — auth-keys: login, keys, orgs, audit screens; RevealModal

- what: new screens for inbound auth — `LoginTab` (`/login`), `KeysTab` (`/keys`, `/keys/<id>`), `OrgsTab` (`/orgs`, `/orgs/<id>`), `AuditTab` (`/audit`) — the `AuthProvider` session state (`ui/src/state/auth.tsx`), and `RevealModal` (`ui/src/components/RevealModal.tsx`), the one place a `drt_` API key secret is shown. (The hook's "components removed: Throwaway" was the earlier dry run's scaffold, already gone.)
- files: ui/src/components/RevealModal.tsx, ui/src/state/auth.tsx, ui/src/shell/{AppShell,Header}.tsx, ui/src/tabs/{login,keys,orgs,audit}/
- why: the gateway now answers /api only to an admin session and /v1 only to a `drt_` key (control_plane/gateway/authz.py); the UI needs a way in, a way to mint and manage keys, and a one-time reveal that never re-renders the secret. Registry rows, DESIGN.md specs and component checks follow in the same wave (auth-keys W3, `.omp/work/auth-keys/PROGRESS.md`).

## 2026-11-17 — Phase 8 — verify

- what: every gate run green on this box: `tokens:check` (126/126 pairs, 3 themes), `icons:lint` (42 icons), `ui:static` (clean), `.omp` typecheck, `DERATE_CHECK_STRICT=1 npm run ui:check` (4/4 steps), `ui` typecheck + `vite build`, graph query on `.omp/graph/graph.json` through the `graphify-out` symlink. Dry run: `/new-icon bell` created + registered + rebuilt (entry below); `close` is registered (`replaces` ✕, `&#10005;`), so `/new-icon close` stops at its `icon_search` step. A scaffolded `Throwaway` with `#ff0000` and a 400-char unbroken string was caught by the edit hook (`color-literal` at 27:30, same turn), by `ui:static`, by axe (2.98:1), and by `layout.check` (clipped text, every theme × width); then `ui:new -- --delete Throwaway` removed it.
- files: `.omp/tests/ui/run.mjs`, `.omp/tests/ui/layout.check.mjs` (fixes below), this file.
- why: the dry run found two gate bugs. A scoped `ui:check -- ui/src/…` resolved the path against `.omp/` and linted nothing (vacuous pass); it now resolves against `INIT_CWD`, then the repo root, and exits 2 on a path that does not exist. A component-scoped run skipped `layout.check` entirely, because `ui/check.mjs` launches verifiers with no arguments and `--only=` was argv-only; it now also reads `DERATE_GALLERY_ONLY` and `run.mjs` adds `layout.check` to the scoped filters.
- remaining debt (from `npm run audit`): 26 inline px values in `style={{}}` that the static rules allow (border/outline widths, 0); 8 glyph/inline-SVG sites (data drawings under the `data-svg` marker); 86 unused tokens (mostly the component layer — `--button-*`, `--input-*`, `--card-*`, `--sheet-*`, `--popover-*` — specified ahead of the CSS that would read them); 6 dead exports (`Section`, `Popover`, `BandLaunch`, `PhaseRow`, …); 23 literal breakpoints/transitions in `@media` and keyframe timings; 17 icons with no `ui/src` usage yet (`bell`, file and arrow icons).

## 2026-11-17 — Phase 8 — verified; summary of the pack

**Created.** `.omp/`: tokens (353 leaves → 209 CSS names, 3 themes, 126 contrast pairs gated), 42 icons + typed `<Icon>`, 20 registered components + 87 screen rows, DESIGN.md, the style guide, the merged code + design graph (10.5k code nodes + 523 design nodes), 12 static design rules, 18 component checks, the 90-cell layout matrix, generated-output freshness, scaffold, `pack-sync`, the omp extension (5 tools, 7 commands, 3 hooks), 3 markdown commands, 3 agents, RULES.md.

**Migrated.** 74 files under `ui/src`: 501 spacing literals, 142 old `--s-*` names, 25 font sizes, 15 radii, 8 z-indexes, 14 durations/easings, 8 font-family literals, 11 opacities, every colour literal (29 → 0), every glyph/entity icon (36 → 0; the 8 remaining `<svg>` are data drawings carrying `// design-pack: data-svg`), 82 type-less buttons, 4 clickable non-interactive elements, the duplicated monogram, the `THEMES` copy in `docs/screenshots/brand/build.py`. `ui/src/styles/tokens.css` is gone; the generated sheet is imported in its place.

**Verified today.** `tokens:check` 126/126; `icons:lint` 42; `.omp` typecheck; `graph:build`; `ui:check --strict` (static clean; generated fresh; 18 component checks; layout 720/720; style guide 9/9); `ui` typecheck + build; `ui/check.mjs --strict` against a coordinator on :18088. Dry run through omp: `/new-icon close` reused the icon and created nothing; `/new-icon bell` drew `svg/bell.svg` to spec, registered it, built and linted green (kept); a scaffolded `Throwaway` with `#ff0000` + `marginTop: 7` + a 400-char string got `color-literal` and `px-literal` with `file:line:col` in the same turn from the `tool_result` hook, the stop hook continued the session with the failing checks, and `layout.check.mjs` named the spilling element (`div in Throwaway/default text 3042px wide in a 200px box`); `ui:new -- --delete Throwaway` left no trace.

**Remaining debt** (from `npm run audit`, which now reports it): 26 inline px values the codemod left because they are not spacing/size props (`width`, `height`, `minWidth` …); `on-signal` on `warn-solid` at 2.67:1 light in `SpendTab`'s split bar (DESIGN.md, Contrast promises); 16 unused tokens (`ink-secondary`, `info`, `info-solid`, `focus-ring*`, `opacity-*`, `gutter`, `select-on-fill`, two mono readout sizes, `space-0/36`, `opacity-85`) and 16 unused icons, all designed ahead of a use; the three ΔE<3 stone pairs kept distinct for pixel-exactness; `Section` and `Popover` with zero importers; the Toast and Skeleton specs with no component yet. The `session_stop` hook runs the gates that fit omp's 30 s handler budget and asks for `/ui-check` for the rest.

## 2026-11-17 — icons: icons added: bell

- what: icons added: bell (category `status`; tags notification, notify, alert, alarm, reminder, subscribe; `replaces: []`). Bell body, clapper arc and top stem, all inside 2…22.
- files: .omp/design/icons/svg/bell.svg, .omp/design/icons/icons.json; generated icons.svg and components/icons.ts. Used by: nothing in `ui/src` yet.
- why: requested through `/new-icon bell`. `icon_search` for bell, notification, notify, alarm, 🔔 and `&#128276;` found nothing; `alert` returns only `alert-triangle`, which means warning, not notification. `ui/src` has no bell glyph or inline bell `<svg>`, so there is no call site to migrate. The first draw put the clapper at y=22.5, past the 2px padding, and `icons:lint` did not catch it because it does not check arc extents. It was raised to y=21.5 in a second "values only" rebuild, merged into this entry.

## 2026-11-17 — Phase 6 — guardrails and tests

- what: `ui/check.mjs` walks `.omp/tests/ui` as a second root. `tests/ui/static/lint.mjs` (12 rules: color-literal, px-literal, z-index-literal, breakpoint-literal, duration-literal, font-family-literal, raw-svg, img-alt, icon-button-label, onclick-non-interactive, button-type, tabindex-positive; `--json`), wrapped as `static.check.mjs`. `tests/ui/gallery/` (every state mounted live over the product stylesheets, fake coordinator from `fixtures/api.json` recorded off a real one, `?theme ?stress ?zoom ?font ?only`), `lib/browser.mjs` (loopback server, Chromium from the Playwright cache, network blocked, axe-core), `lib/component.mjs` (renders, Tab order, Enter/Space, axe per section), 18 `components/*.check.mjs` with per-kind keyboard contracts (Popover Escape + focus return, Sheet trap both ways + Escape + focus hand-back, Disclosure Enter/Space, OverrideGate button gating, Copyable self-report), `layout.check.mjs` (3 themes × 4 widths × 8 stresses: overflow, escape, clipped text, overlaps, ≥24px targets, computed contrast, reduced-motion; screenshots under `tests/ui/screens/`), `generated.check.mjs` (tokens/icons/style guide/diagram freshness + registry coverage), `scaffold.mjs` (`ui:new`, reversible), `run.mjs` (`ui:check`). CI runs the pack's hermetic gates.
- files: `ui/check.mjs`, `.omp/tests/**`, `.omp/design/components/registry.json` (+87 `screen` rows from pack-sync), `.github/workflows/publish-image.yml`, `.gitignore`; debt fixed in `ui/src`: 81 `type="button"` (one `submit`), `DeploymentsStrip` row (`role="button"` + keys; it holds its own buttons), `RosterSection` row → `<button>`, `Sheet` scrim `role="presentation"`, `ClusterGraph` providers `role="group"`, two multi-line transitions → tokens, `Disclosure` button `min-height: var(--space-24)` (WCAG 2.5.8; it was 21px).
- why: the rules are now failures, not advice. Two findings the gate produced on its first run were real (`Disclosure` under 24px; nothing else) and one was the gate's own (content inside `overflow:auto` is reachable, not escaped). `zoom: 2` runs at 1280/1920 only: CSS zoom halves the viewport without re-evaluating breakpoints, which is not what browser zoom does.

## 2026-11-17 — Phase 7 — omp tooling

- what: `extensions/design-pack/` (tools `token_lookup`, `icon_search`, `component_lookup`, `graph_query`, `contrast_check`; commands `/tokens /icon /ui-check /graph /impact /design-audit /pack-sync`; hooks: `session_start` context block, `tool_result` lints touched `ui/src` files at once and, two seconds after a token/icon/registry/component edit, rebuilds + appends a CHANGELOG entry + `graph:update`, `session_stop` runs `ui:check` when `ui/src` or the pack changed since the last green run and continues the session with the failures), `commands/new-component.md`, `new-icon.md`, `new-color.md`, `agents/design-reviewer.md`, `ui-tester.md`, `icon-smith.md`, `RULES.md` (the nine maintenance rules, verbatim), `pack-sync.mjs`, the `## Design pack` section in `CLAUDE.md`.
- files: `.omp/extensions/design-pack/**`, `.omp/commands/*.md`, `.omp/agents/*.md`, `.omp/RULES.md`, `.omp/pack-sync.mjs`, `CLAUDE.md`, `.omp/README.md`.
- why: the pack only stays true if the agent that edits the UI is told, in the same turn, what it broke; the hooks are that, and the tools make "check before you create" cheaper than guessing.

## 2026-11-17 — Phase 5 — graph

- what: `.graphifyignore`; `graph/build.mjs` (extract --code-only into `GRAPHIFY_OUT=.omp/graph/graphify-out`, cluster-only, design merge, god-nodes, diagram, the `graphify-out -> .omp/graph` symlink), `graph/design-graph.mjs` (436 design nodes — tokens, primitives, icons, components, specs — and 1,248 edges merged into the 10.1k-node code graph in graphify's own shape), `graph/diagram.mjs` (the Mermaid architecture diagram in DESIGN.md: 25 areas, 76 cross-area dependencies), `graph/GRAPHIFY.md`.
- files: `.graphifyignore`, `.omp/graph/{build,design-graph,diagram}.mjs`, `.omp/graph/GRAPHIFY.md`, `.omp/graph/graph.json`, `.omp/graph/design-graph.json`, `.omp/graph/god-nodes.json`, `.omp/graph/GRAPH_REPORT.md`, `.omp/design/DESIGN.md` (architecture section).
- why: "what uses `--fault`", "what breaks if Readout changes", "which check covers it" are graph questions, and they now have one graph that spans the Python and the TypeScript. `merge-graphs` was not used (it prefixes ids and drops direction); `graphify update` was not used (it indexes markdown headings, unlike the code-only extract).

## 2026-11-17 — Phase 3 — design doc and style guide

- what: `design/DESIGN.md` (principles, the three token layers and the theme mechanism, the role → derate-name table with all three themes, type/space/elevation/motion, a spec per component with every state or its n/a reason, patterns, copy, the add-a-thing checklists; the architecture section is filled by Phase 5), `design/components/registry.json` (20 components: 14 shared, 6 CSS patterns), `tests/ui/gallery/states.tsx` (96 variant × state fixtures over the real components), `design/styleguide.mjs` → `design/index.html` (self-contained, file://; tokens with per-theme chips and contrast badges, type scale, spacing, radii, elevation, motion, the icon set, every non-interactive gallery state rendered with react-dom/server, component tokens, the 126 contrast pairs; theme switcher and side-by-side).
- files: `.omp/design/DESIGN.md`, `.omp/design/components/registry.json`, `.omp/design/styleguide.mjs`, `.omp/design/index.html`, `.omp/tests/ui/gallery/states.tsx`, `ui/src/tabs/cluster/loading.check.mjs` (its ✓●○ assertions now read the icons' `data-icon`, same semantics).
- why: the rules have one written home and one rendered home, both generated from the sources the product ships, so a spec and a swatch cannot drift from the code without `generated.check.mjs` (Phase 6) failing.

## 2026-11-17 — Phase 4 — icon pack

- what: `design/icons/`: 41 SVGs to one spec (24-box, 2px padding, 1.5px round strokes, `currentColor`, geometry-only children; `brand` keeps the monogram's 2.25/square), `icons.json` (category, tags, usage, added, replaces), `svgo.config.mjs`, `build.mjs` (→ `icons.svg` sprite + generated `components/icons.ts`), `lint.mjs` (every rule, plus duplicate geometry and stale outputs), `README.md`. `ui/src/components/Icon.tsx`: typed `<Icon name size label|decorative>`; omitting both `label` and `decorative` is a type error.
- files: `.omp/design/icons/**`, `ui/src/components/Icon.tsx`; migrated: `shell/Header.tsx` + `tabs/SetupTab.tsx` (one `derate-mark` instead of two drawings; `.brand-mark` in derate.css/setup.css keeps the 27px/25px ink), `shell/AppShell.tsx` (chevron), `inspectors/DeploymentInspector.tsx` + `SetupTab` phases + `tabs/cluster/LoadingBand.tsx` (✓●○ → check-circle/circle-dot/circle; the band nests the icon as an inner `<svg>`), `ModelCard`/`ModelInspector`/`CoordinatorCard` (✓/✕), `CustomServes`/`ModelsTab` (`&#10005;`, `&#8981;`, ▦▤), `ResidentProcesses` (↗ + the missing `aria-label`), `DeploymentsStrip` (↗), `BandSection`/`CatalogList` (▾▸), `Verdict` (⚠), `components/Panel.tsx`/`QuantTableCard`/`QuantLadder` (+/–). `Chart.tsx`, `Qr.tsx`, `ClusterGraph.tsx`, `LoadingBand.tsx` carry `// design-pack: data-svg`.
- why: one drawing per meaning, searchable by name and by the glyph it retired; the accessibility decision (announced or hidden) is forced at every call site instead of being an `aria-hidden` somebody remembers.

## 2026-11-17 — Phase 2 — tokens

- what: `design/tokens/tokens.json` (W3C format, 353 leaves: primitive ramps and scales, 100 semantic roles under derate's existing CSS names, 72 component tokens), `build.mjs` (→ `tokens.css`, `tokens.ts`, `tokens.flat.json`; enforces the layer rules), `pairs.json` + `check.mjs` (the contrast gate: 126 pairs × 3 themes, stale-output check), `migrate.mjs` (the one-shot codemod, kept for the record). A third theme, `high-contrast` (7:1 text, 3:1 UI, 3px focus ring, outlines instead of shadows), reachable by `data-theme` and by `prefers-contrast: more` under `system`. `ui/src/styles/tokens.css` is deleted; `main.tsx` imports the generated sheet in the same cascade position.
- files: `.omp/design/tokens/*`, `ui/src/main.tsx`, `ui/src/styles/tokens.css` (deleted), 74 migrated files under `ui/src/**` (501 spacing values → `--space-<px>`, 142 `--s-*` renames, 25 font sizes → `--size-<role>`, 15 radii, 8 z-indexes, 12 durations/easings, 8 font-family literals, 11 opacities, 3 trackings, `rgb(255 255 255 / .94)` → `--on-accent`, `#f7f4ee` → `--qr-tile`), `chartTheme.ts` + `Terminal.tsx` fallbacks from `tokens.ts`, `owner.ts` → `--avatar-1..10`, `Qr.tsx` → `--qr-ink`, `theme.ts` + `AppearanceCard.tsx` (high-contrast option, validated storage), `tabs/chat/tags.check.mjs` (reads `tokens.flat.json`, now asserts all three themes), `docs/screenshots/brand/build.py` (reads `tokens.flat.json`; SVGs byte-identical), READMEs that pointed at the old path.
- why: one source for every colour, size, space and duration, with the contrast promises checked by a script instead of by the comments that used to carry them. Pixel-exact: every value maps to the token that holds exactly that value, including the three ΔE<3 near-duplicate stone pairs, which are kept distinct and recorded in AUDIT.md as merge candidates rather than merged. Two decisions from the measurements: `flow`/`stream`/`batch` are promised at 3:1 (filled marks, 4.0–4.2:1 light as text), and `on-signal` on `warn-solid` is 2.67:1 light — `SpendTab`'s split bar paints text with it today; that is remaining debt, listed in DESIGN.md, not a waiver in `pairs.json`.

## 2026-11-17 — Phase 1 — audit

- what: `design/audit.mjs` and its first output `design/AUDIT.md`; `.omp/package.json`, `tsconfig.json`, `README.md`; `.gitignore` entries for the pack's generated output; `ui/vite.config.ts` `server.fs.allow: ['..']`.
- files: `.omp/design/audit.mjs`, `.omp/design/AUDIT.md`, `.omp/package.json`, `.omp/package-lock.json`, `.omp/tsconfig.json`, `.omp/README.md`, `.gitignore`, `ui/vite.config.ts`
- why: the pack starts from measured facts — 29 colour literals outside the palette, 348 inline px values, 36 glyph/inline-SVG icons, 81 type-less buttons, 4 clickable divs — so every later phase has a number to drive to zero and a script that re-measures it.
