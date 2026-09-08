# What is left to build

The gap between "this works on my two Sparks" and "somebody else can use this".
Everything below is unbuilt or half-built; each item says what already exists,
so the work starts from the real state of the tree rather than from zero.

Ordered within each section by what unblocks the most.

## Onboarding and reach

- [ ] **Double-click installer for Mac and Windows**, wrapping the same install
      script, so a non-technical user never opens a terminal.
      *Today:* `install.sh` does the Linux container path, with `--dry-run` and
      `--uninstall`; Mac and Windows already have a native path (`pipx install
      derate`) because the container flags are Linux-host features. So the
      shipping logic exists and nothing needs re-implementing.
      *Needs:* a `.pkg`/`.dmg` and an `.msi`/winget entry that bundle a Python
      runtime, run the same steps, and register a login item. **The real work is
      signing** — Apple notarization and Windows SmartScreen — not packaging. An
      unsigned double-click installer is worse than a terminal command, because
      the OS tells the user it is malware.

- [ ] **Inbound authentication on the gateway.** Prerequisite for everything
      below it in this section, and the single biggest hole in the product.
      *Today:* there is none. `gateway/settings.py` says it plainly: "every
      /api route is unauthenticated cluster control". Anyone who can reach the
      port can stop deployments and remove nodes.
      *Needs:* a credential on the way in, and a scope split so `/v1` and `/api`
      are not the same permission. Until that exists, "share your hardware with
      a friend" means "give a friend your cluster".

- [ ] **Shareable guest access: a QR a friend scans**, backed by a per-guest
      token you can revoke, never the raw endpoint.
      *Today:* the QR encoder is already written and already shipping —
      `ui/src/tabs/setup/qr.ts`, self-contained on purpose because a derate
      cluster is routinely on a LAN with no route out. The Setup flow ends by
      showing one. The enrollment-token model (minted on demand, expires in an
      hour, spent by the machine that uses it) is the right shape and already
      exists for *nodes* in `registry/enrollment.py`.
      *Needs:* the same store, issued to a person instead of a machine, without
      an expiry but with revocation; `/v1` scope only; and a per-token identity
      on the request so spend and rate limits can be attributed. Point the
      existing QR component at that token's URL rather than at the endpoint.

- [ ] **Auto-discovery on the local network, so a phone finds the coordinator
      without an IP address.**
      *Today:* mDNS already advertises `_derate._tcp.local.` with role,
      cluster_id and node_id in TXT (`registry/discovery.py`) — but that is how
      *nodes* find each other. No phone browser browses a custom service type.
      *Needs:* advertise `_http._tcp` alongside it and claim a stable
      `derate.local` hostname, so Safari and Chrome resolve it with no app
      installed. Then the QR above can encode a name instead of a DHCP lease.

- [ ] **Household framing in the product itself:** every device you own points
      at your machine instead of a cloud.
      *Today:* the product speaks in cluster terms — nodes, ranks, roster.
      *Needs:* copy, and a "connect a device" flow that is the guest flow
      pointed at yourself. Mostly a writing job, and it should not start until
      the three items above make the claim true.

## Model surface

- [ ] **Curated "latest" strip at the top of the model list**, so a beginner
      sees names they recognise from the news before they see a catalogue.
      *Today:* `control_plane/fit/catalog.py::CURATED_MODELS` exists and is
      already server-side rather than in the browser, precisely so the fit gate
      and the picker cannot disagree. But it is four frozen research shapes
      chosen to exercise the planner (MoE, GQA, sliding window, MLA), capped at
      `MAX_CATALOG_MODELS = 8`, and it lives in the picker rather than on
      `/models`.
      *Needs:* a second, larger list ordered by recognisability rather than by
      shape coverage, rendered as a strip above the grid — and a way to refresh
      it, because a hand-frozen "latest" is stale the week after it ships.

- [ ] **Never show a model that cannot run. When nothing fits, offer the quant
      that does.**
      *Today:* the arithmetic is already there and already correct. The fit
      gate's refusal names a working quant (`_suggest_quant` in
      `fit/calculator.py`: "requantize to fp8 (65.7 GiB per rank)"), and
      `ui/src/tabs/models/QuantLadder.tsx` renders a per-variant verdict lamp.
      *Needs:* the default view to act on it — filter or demote what cannot run,
      and promote the suggested variant to the primary action instead of leaving
      it inside a refusal string. This is presentation work on top of a
      finished backend, which makes it the cheapest high-value item here.

- [ ] **Simple on/off switch per model, everywhere a model appears.**
      *Today:* half of it exists and only for half the models. Provider models
      carry a `served`/`enabled` allowlist (`inventory/build.py`,
      `inventory/records.py`); a launched deployment has no such flag — it is
      running or it is stopped, which is a much more expensive toggle.
      *Needs:* one switch with one meaning across both kinds.
      `control_plane/inventory/` is where it belongs: it already merges both
      into one materialized view, and it exists because fourteen screens used to
      each re-derive their own answer.

- [ ] **Custom quant support behind an advanced flag**, so the beginner path
      keeps zero choices in it.
      *Today:* `contracts/quant.py` knows 33 quantization formats, and the
      resolver detects them.
      *Needs:* UI gating only — a disclosure that hides the ladder until asked.
      Cheap, and it should land in the same pass as the item above so the
      beginner path is decided once.

- [ ] **One-click speculative decode**, with an advanced row accepting any HF
      model ID as the draft model.
      *Today:* the resolver already reasons about it — `resolver/params.py`
      deliberately excludes draft-head parameters from the weight budget and
      records that "enabling speculative decoding adds it back". So the memory
      accounting knows the feature exists.
      *Needs:* an actual launch path, which does not exist: nothing in
      `deploy/flags.py` or the recipes passes a draft model to a runtime. That
      means a `RuntimeSpec` field, a fit-gate term for the draft model's
      weights on every rank, and a resolve of the draft ID so a bad one is
      refused before launch rather than at load. The largest item in this
      section.

## Cluster

- [ ] **Split the coordinator role from the worker role explicitly.**
      Coordinator on the always-on box — a Pi on a battery is enough — workers
      come and go.
      *Today:* closer than it looks. The *decision* already exists
      (`registry/bootstrap.py::resolve_role`: `DERATE_ROLE=coordinator`, else an
      explicit join address, else mDNS), `install.sh` already takes `--no-gpu`
      and retries without the GPU flags when Docker refuses them, and a machine
      that genuinely has no GPU already reads as `DeviceClass.CPU` and is "a
      cluster member in good standing". What is missing is the *deployment
      shape*: one image still runs both roles.
      *Needs:* an arm64 image, and a coordinator profile that never gets planned
      onto rather than one that merely reports no GPU. Verify the current state
      on real hardware before estimating — the pieces may already compose.

- [ ] **Handle coordinator-down honestly.** Today the endpoint disappears with
      it, and cloud fallback cannot help, since the thing that would fall back
      is the thing that died.
      *Today:* the gateway is the single HTTP surface, the router, *and* the UI
      host. There is no second copy of the roster, the routing table, or the
      provider credentials.
      *Needs:* a decision before any code. Either a standby coordinator with
      roster handoff and a floating address, or a client-side fallback that
      holds its own provider credential so a dead coordinator degrades to cloud
      instead of to nothing. **This is architectural, not a feature** — it
      changes what a node is allowed to know, and it deserves an appendix in
      `00-architecture.md` before an implementation.

- [ ] **Full downloads tab:** every node, every model, percent and rate, read
      from the server so a refresh does not lose it.
      *Today:* two partial sources and neither survives a reload.
      `deploy/progress.py` has a `downloading` phase, but only for the launch in
      flight and only parsed from the runtime's own log markers.
      `registry/modelcache.py` measures what is already on disk and states the
      constraint that shapes this whole item: **"The control plane does not
      download these and never will"** — the runtime fetches into the
      HuggingFace cache, and the node agent can only measure and delete.
      *Needs:* a server-side record of transfers in progress per node, written
      as they are observed rather than derived in the browser. Progress that
      lives in UI state is progress that a refresh destroys.

- [ ] **Kill a worker mid-stream and have the stream continue.** Already the
      good demo; worth making a guarantee.
      *Today:* `FAILOVER` is a real routing policy, and `proxy.py`'s first rule
      is that streams are not buffered — server-sent events pass through chunk
      by chunk.
      *Needs:* that second fact is exactly why this is not free. Once tokens
      have reached the client you cannot silently restart the completion
      somewhere else. The guarantee has to be chosen first: replay the prefix
      to a new worker and accept a seam, or fail the stream cleanly with a
      resumable cursor. Pick the promise, then build it — and until then, do
      not describe it as a guarantee.

## Notes

Two items gate several others and should go first: **inbound authentication**,
without which guest sharing cannot be built at all, and the **coordinator-down**
decision, which changes what a worker is allowed to hold.

Two are nearly free and worth doing early for the demo: **offering the quant
that fits** and **the custom-quant disclosure**, both of which are presentation
work on arithmetic that already ships.
