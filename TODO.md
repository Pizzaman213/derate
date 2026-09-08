# TODO

Future features to implement, consolidated from `ROADMAP.md`,
`00-architecture.md`'s dated appendices, and `ui/mockups-next/derate.html`'s
"not built yet" card. See `ROADMAP.md` for the full reasoning behind each
onboarding/model-surface/cluster item; this file is the flat checklist.

## Evidence

- [ ] **Measure PP=2 against TP=2 on two Sparks and put real numbers
      somewhere public.** No document in this repo currently claims a ratio
      between them — the README's old "Why this is defensible" section made
      that claim and was cut rather than backed with data, so there is
      nothing left asserting more than the code can show, but the head-to-head
      throughput comparison itself still doesn't exist. `tests/load/` is the
      harness. Run `openai/gpt-oss-120b` at 131072 context and concurrency 16
      both ways on `spark-4d38` and `spark-26af`, and write the resulting
      tokens/sec figures and ratio into the README once they exist.
      Blocked on the link actually being probed first — all three links on
      :8088 currently read `measured: false`, so the planner is running on
      the no-measurement rung, not on a real bandwidth figure.

## Onboarding and reach

- [ ] Double-click installer for Mac and Windows (needs signing/notarization;
      blocked on native support below)
- [ ] Run natively on macOS and Windows (Linux-only today; `derate` is taken
      on PyPI, needs a distribution name/channel)
- [ ] Inbound authentication on the gateway — currently none; biggest gap,
      blocks guest sharing below
- [ ] Shareable guest access via QR code (person-scoped, revocable token
      variant of the existing node-enrollment model)
- [ ] Auto-discovery on the local network / `derate.local` hostname (mDNS
      advertises nodes only today; needs `_http._tcp` too)
- [ ] Household framing in the product copy (gated on the four items above)

## Model surface

- [ ] Curated "latest" model strip, larger and refreshable (today: 4 frozen
      research shapes capped at 8)
- [ ] Never show a model that can't run — offer the quant that does (backend
      arithmetic already correct, needs UI to act on it)
- [ ] Simple on/off switch per model everywhere (exists for provider models
      only; deployments need the same toggle)
- [ ] Custom quant support behind an advanced flag (resolver already knows 33
      formats, needs UI gating)
- [ ] One-click speculative decode — no launch path exists yet (largest item
      in this section)

## Cluster

- [ ] Split coordinator role from worker role explicitly (decision logic
      exists; needs its own deployment shape/image)
- [ ] Handle coordinator-down honestly — needs an architecture decision
      (standby + roster handoff, or client-side cloud fallback) before any
      implementation
- [ ] Full downloads tab, server-side transfer records that survive a refresh
- [ ] Kill a worker mid-stream and have the stream continue — pick the
      guarantee (replay vs. clean fail) before building it
- [ ] Manual placement (the planner has no placement field)
- [ ] Link utilisation surfaced in the UI (no bytes-on-the-wire telemetry
      exists)
- [ ] Managed remote node tier (today: a target is local or a provider, no
      third kind)
- [ ] Add a node by address (today: join is worker-to-coordinator and
      token-gated)
- [ ] Prefix cache hit rate (backend reports null for it)
- [ ] Scheduled model swaps (time-based placement)
- [ ] Auto-eviction policy (currently always manual)
- [ ] Alerting — node down, cap reached, OOM

## Housekeeping / smaller items

- [ ] Trim CUDA graph capture time (`enforce_eager` or a trimmed
      `cudagraph_capture_sizes`) — deliberately deferred since it's a
      launch-time contract change, not a default
- [ ] Verify sglang's `TORCHINDUCTOR_CACHE_DIR`/`TRITON_CACHE_DIR` settings
      against a real image (untested — image not on this box yet)
- [ ] Reap orphaned `sparkrun_*_solo` containers and empty
      `/tmp/derate-load-logs-*` directories
- [ ] Account for audio spend on the multipart TTS path (still unaccounted)
