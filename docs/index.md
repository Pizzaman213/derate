# The documentation

Two kinds of document, and which one you want depends on which side of the
software you are standing on. **`guide/` is for running derate** — installing it,
putting a model on it, pointing a client at it, and reading the message it
printed when something broke. **The per-folder `README.md` files are for changing
it** — one per package, each naming its files, its public surface and the failure
each of its rules prevents.

Nothing below restates what it links to. This repository keeps one fact in one
place, so this page is addresses only. [`../README.md`](../README.md) is the
pitch and the two `curl` lines; start there if you have not seen the product.

## Start here

The guide, in reading order. Each page names what you need before you open it.

1. [**Install derate on one machine, then on a second**](guide/install.md) — one
   command puts a container on a machine and that machine becomes a working
   single-node cluster; a second command, composed for you by the first machine,
   adds another. Linux and Docker.
2. [**Your first visit to the coordinator**](guide/first-run.md) — what the
   first-run walkthrough asks, and where a second machine's join command comes
   from afterwards.
3. [**Serving a model**](guide/serving-a-model.md) — from "I want to run this
   model" to a deployment answering on your own endpoint. You do not need to know
   in advance whether it fits; working that out is most of the page.
4. [**Send requests to the one endpoint**](guide/the-endpoint.md) — derate from a
   client's side: what is on `/v1`, what a refusal looks like, and how the
   gateway decides which machine answers.
5. [**Put somebody else's API behind the same endpoint**](guide/providers.md) — a
   remote API turned into an ordinary target on your own coordinator, routed by
   the same policies as your own hardware and priced on the Spend screen.
6. [**Diagnosing a failure**](guide/troubleshooting.md) — matches the message you
   are looking at to the thing that has to change. Two of the errors in it are
   near-identical sentences about completely different machines, so match the
   words rather than the gist.

## The control plane

[`../control_plane/README.md`](../control_plane/README.md) is the package as a
whole: one process, one entry point, `python3 -m control_plane.node`, composing
the twelve subpackages below out of the ten leaf modules beside them.

The order here is the dependency order, not the alphabet — each row may import
the rows above it.

| Package | What it claims |
|---|---|
| [`contracts/`](../control_plane/contracts/README.md) | The shapes every other package codes against, plus three generators that dump them so nothing restates them by hand. Nothing here imports back out. |
| [`registry/`](../control_plane/registry/README.md) | The cluster's picture of itself — which machines exist, what they are, whether they are alive — and the node agent that is the whole of a worker. |
| [`links/`](../control_plane/links/) | Measures what the interconnect actually delivers, rather than what the spec sheet claims. The gap between the two is the product. |
| [`resolver/`](../control_plane/resolver/README.md) | A HuggingFace model id in, a complete `ModelShape` out, every field either read from real metadata or carrying a warning that it was not. |
| [`fit/`](../control_plane/fit/README.md) | The blocking pre-launch out-of-memory gate. It refuses, and it names the term that blew the budget and the change that would work. |
| [`planner/`](../control_plane/planner/README.md) | Picks tensor, pipeline, expert and data parallel degrees from measured hardware facts, and hands back a sentence naming what the decision turned on. |
| [`deploy/`](../control_plane/deploy/README.md) | Turns an approved plan into a running sparkrun backend, watches it, and tears it down. Every phase a person sees is a line one of two programs actually printed. |
| [`gateway/`](../control_plane/gateway/README.md) | One OpenAI endpoint, seven routing policies, admission control, and the request path's answer to a node dying under it. |
| [`providers/`](../control_plane/providers/README.md) | Remote OpenAI-compatible upstreams as first-class route targets, alongside local deployments. The cluster is the default; a paid API is the overflow valve. |
| [`inventory/`](../control_plane/inventory/README.md) | One record per model, folded on the server from five sources. A materialized view that owns nothing and that nothing routes off. |
| [`telemetry/`](../control_plane/telemetry/README.md) | Writes the per-request numbers down on the machine that produced them, before anything is averaged and the raw figures are gone. |
| [`runtimes/`](../control_plane/runtimes/README.md) | The inference servers derate ships itself rather than launches. Runs inside a model container, never in the node image. |

## The screen

[`../ui/README.md`](../ui/README.md) is the front end as a build: the npm
commands, and the rule that an unmet requirement in `npm run check` is a skip
reported in its own column, never folded into the passes.
[`../ui/src/README.md`](../ui/src/README.md) is the application — four files at
the top level and everything else a folder with its own README.

**The wire and what is remembered:**

- [`src/api/`](../ui/src/api/README.md) — where the UI talks to the coordinator.
  Eighty-six files import from it, seventy-six of them types only.
- [`src/state/`](../ui/src/state/README.md) — everything the UI knows that is not
  a component: the address bar, the polled resources, one SSE stream, and the
  vocabulary every screen has to agree on.

**The chrome and the shared parts:**

- [`src/components/`](../ui/src/components/README.md) — nine files, each making a
  design rule structural rather than remembered.
- [`src/shell/`](../ui/src/shell/README.md) — the frame every screen lands in,
  and the one thing in this repository that looks at the result.
- [`src/sidebar/`](../ui/src/sidebar/README.md) — the right-hand rail: five
  sections, each fetching its own data, each honest about where its figure came
  from.
- [`src/inspectors/`](../ui/src/inspectors/README.md) — the deployment detail
  pane, drawn as a page rather than a card.
- [`src/inspectors/node/`](../ui/src/inspectors/node/README.md) — the node page:
  left column what the machine **is**, right column what it is **doing**.
- [`src/styles/`](../ui/src/styles/README.md) — the palette and the chrome in
  three sheets imported exactly once. No component carries a literal colour.
- [`src/check/`](../ui/src/check/README.md) — shared machinery for the verifier
  suite. Neither file in it is a verifier.

**The tabs.** [`src/tabs/`](../ui/src/tabs/README.md) is the destination roots,
one file per screen the URL can name.

- [`tabs/dashboard/`](../ui/src/tabs/dashboard/README.md) — four sub-views of a
  cluster that is already running, plus the chart layer the rest of the product
  borrows. Nothing here computes a number.
- [`tabs/models/`](../ui/src/tabs/models/README.md) — the model surface, and the
  only place in the product where anything is launched. One flat list, not five
  source tabs.
- [`tabs/cluster/`](../ui/src/tabs/cluster/README.md) — the machine floor, one
  plate per node with the request flow drawn through it. Position is a pure
  function of the node set, never of a simulation.
- [`tabs/chat/`](../ui/src/tabs/chat/README.md) — the console for talking to what
  the cluster is actually running. It filters nothing out of the picker.
- [`tabs/spend/`](../ui/src/tabs/spend/README.md) — the Spend screen's
  arithmetic, kept out of the component so it can be checked against a live
  coordinator. A number we do not know is `null`, never `0`.
- [`tabs/settings/`](../ui/src/tabs/settings/README.md) — which coordinator this
  browser talks to, the machines and providers behind it, and what they may
  cost. No API key is ever rendered.
- [`tabs/setup/`](../ui/src/tabs/setup/README.md) — the two things the first-run
  screen needs that nothing else does: a QR encoder written from scratch, and a
  stylesheet for the one screen with no header and no rail.

## Everything else

- [`../tests/README.md`](../tests/README.md) — the front door over the sweeps,
  the frozen fixtures and the captured resolver corpus, with
  [`../tests/unit/README.md`](../tests/unit/README.md) over the forty modules
  the suite is made of.
- [`../tests/load/README.md`](../tests/load/README.md) — finds where the gateway
  breaks, and produces its derating curve. A harness, not a suite.
- [`../docker/README.md`](../docker/README.md) — one image on every machine, role
  resolved at runtime. No coordinator image, no worker image, no second service
  for the UI.
- [`screenshots/README.md`](screenshots/README.md) — every image the root README
  shows, and the two commands that produce them: eight captures taken by a real
  Chromium against a real coordinator, and the banner and bare lockup in
  `screenshots/brand/`, redrawn from the header's own CSS and the token file.
- [`repo-map.md`](repo-map.md) — the thirteen files sitting above the packages:
  what this is, how it gets onto a machine, what it is pinned against, and how
  hard you can push it.

## Regenerating what is generated

Four things in this tree are drawn from the code rather than typed, and go stale
the moment their source moves.

```bash
python3 -m control_plane.contracts.manifest --write   # control_plane/contracts/manifest.json
python3 -m control_plane.contracts.routes --write     # control_plane/contracts/routes.json
python3 docs/screenshots/brand/build.py                           # all four SVGs under docs/screenshots/brand/
cd ui && npm run screens                              # ui/screens/<dest>.png, promoted by hand
```

`manifest.py` and `routes.py` both print the `--write` line themselves when the
checked-in JSON no longer matches what they generate, so the failure tells you
the command. `build.py` takes no arguments and always writes all four files.
`npm run screens` runs `screens.check.mjs --capture-only` — captures into
`ui/screens/`, which is gitignored; the eight images in `screenshots/` are copied
across deliberately.
