# Agent H: Web UI

Read `00-architecture.md` first. The visual direction is in this brief; there is no separate design document.

**You own:** `ui/**`
**You depend on:** Agent G's HTTP surface only. Never call another component directly.
**Downstream of you:** nobody. You are the surface everything else is judged by.

---

## What you build

The screen that opens after install. Three jobs, in priority order:

1. Show the cluster: which nodes exist, what they are, whether they are healthy.
2. Add a node without typing an IP address.
3. Show a model running, and show why it is running the way it is.

Build against Agent G's day-0 fixture stub from hour one. Do not wait for live data.

---

## Design direction

**Lab instrument, not SaaS dashboard.** Bench measurement equipment: enamelled panel, printed legends, real readouts, one indicator lamp that means something. That framing is what earns the cream palette and keeps it from reading as a generic warm-minimal template.

Consequences: numbers are the hero, chrome is structural rather than decorative, exactly one saturated color reserved for live state, no decoration that does not encode information.

### Tokens

Light:

```
--panel            #EDE9E0    page background, the enamel
--panel-recessed   #E3DED3    inset areas, sidebar, table stripes
--ink              #1A1917    primary text, readout digits
--ink-muted        #6E6A62    labels, units
--rule             #C9C2B4    hairlines, panel divisions
```

Dark:

```
--panel            #1A1917
--panel-recessed   #232120
--ink              #EDE9E0
--ink-muted        #8A8479
--rule             #38352F
```

Signal, identical in both modes:

```
--live    #3D8B4A    serving, healthy, connected
--warn    #B8862C    thermal or memory pressure
--fault   #A93A2E    unreachable, out of memory, crashed
```

Three signal colors and no others. Nothing is colored decoratively. If it has color, it is reporting state.

The green is deliberately not NVIDIA's brand green. Using `#76B900` on a tool pitched at NVIDIA judges reads as costume, and it vibrates against cream.

### Type

IBM Plex Sans for interface text, IBM Plex Mono for numeric readouts only.

Plex was drawn for technical documentation, the mono is a true sibling rather than an unrelated pairing, and both carry real tabular figures. That last part is functional: every number updates once a second, and without tabular figures the digits jitter horizontally and the panel looks broken.

Mono is for digits that change in place. Not for labels, headings, or captions.

```
readout-xl   48px / 1.0   Plex Mono 500, tabular    the one hero metric
readout      28px / 1.1   Plex Mono 500, tabular    per-node values
body         15px / 1.5   Plex Sans 400
label        13px / 1.3   Plex Sans 500
unit         12px / 1.0   Plex Sans 400             tok/s, W, ms, GB
```

Sentence case throughout. No all-caps labels, no tracked-out eyebrows, no arrow glyphs appended to buttons.

12px spacing unit. Radius 3px on interactive controls, 0 elsewhere. Panels separated by 1px rules, not gaps and shadows.

---

## Main view

```
┌────────────────────────────────────────────┬──────────────────┐
│                                            │                  │
│   Qwen3-72B-Instruct        ● serving      │  Nodes           │
│                                            │  ─────────────   │
│   127.4                                    │  spark-01        │
│   tokens per second                        │  GB10 · 128 GB   │
│                                            │  ● 71 W  62 °C   │
│   ────────────────────────────────────     │                  │
│                                            │  spark-02        │
│   [ throughput, last 60s              ]    │  GB10 · 128 GB   │
│   [                                   ]    │  ● 68 W  60 °C   │
│                                            │                  │
│   ────────────────────────────────────     │  ─────────────   │
│                                            │  + Add node      │
│   first token   142 ms                     │                  │
│   cache hits     84 %                      │  ─────────────   │
│   total draw    139 W                      │  Plan            │
│                                            │  PP 2            │
│                                            │  link 10.2 GB/s  │
│                                            │  why ▸           │
└────────────────────────────────────────────┴──────────────────┘
```

One hero number: tokens per second. Everything else subordinate. This is where boldness gets spent and nowhere else.

Throughput graph is a plain line on a hairline baseline. No gradient fill, no axis chrome beyond two end labels.

Left aligned throughout. Numeric columns align on the digits.

### The Plan panel

This is the most important element on the screen and it is not decoration.

It shows the derived plan, the measured link bandwidth the decision came from, and an expandable reason. Collapsed: `PP 2 · link 10.2 GB/s`. Expanded: the planner's `reason` string verbatim, then the `rejected` list, one line each.

That expansion is the entire product thesis made visible. It is what a judge screenshots. Design it as carefully as the hero number.

Never paraphrase the planner's strings. Render them as given.

---

## Cluster graph

A second view, reachable from the sidebar. Machines as nodes, measured links as edges, deployments overlaid on the machines running them.

```
        ┌──────────────┐   10.2 GB/s    ┌──────────────┐
        │  spark-01    │═══════════════ │  spark-02    │
        │  GB10        │   ConnectX-7   │  GB10        │
        │  ● 78%  71 W │                │  ● 74%  68 W │
        │  coordinator │                │              │
        └──────┬───────┘                └──────┬───────┘
               │                               │
               └───── gpt-oss-120b · PP 2 ─────┘
                          127.4 tok/s

        ┌──────────────┐
        │  ws-3090     │ ╌╌╌ 1.1 GB/s ╌╌╌ (ethernet)
        │  RTX 3090    │
        │  ● 31%  210 W│
        │  qwen3-8b    │
        └──────────────┘
```

Data comes from `GET /api/topology`. Positions are yours to decide; the payload has no coordinates.

**Do not use a force-directed layout.** With two to four nodes a physics simulation produces drifting, unrepeatable positions and reads as a toy. Use a fixed layout: nodes on a row or a ring by index, deterministic, stable across refreshes. A machine should be in the same place every time someone looks.

Edges encode the measurement. Thickness scales with `all_reduce_gbps`, and the number is printed on the edge, because that number is the product. An edge with `measured: false` renders dashed and grey with a "measure" action, and shows no bandwidth figure. Never draw a number that was not measured.

A deployment spanning nodes draws as a band beneath the machines it occupies, labelled with its served name, its plan, and its live throughput.

Node fill indicates memory used, and the border carries the signal color. Under weighted routing, show each node's share as a small proportion bar, so an unequal split is visible rather than mysterious.

Clicking a node opens its detail. Clicking an edge offers to re-measure.

---

## Routing panel

Per served name, in the sidebar under Plan when more than one replica exists.

Shows the active policy, the live weights, and outstanding request counts per target. A policy dropdown writes to `PUT /api/routing/{served_name}` and takes effect immediately.

When the policy auto-selected weighted capacity because targets differ by more than 25 percent, say so in one line. Someone seeing a 70/30 split needs to know it was deliberate.

A target held at zero weight for being too slow is shown greyed with the reason, not hidden. Hiding it makes the cluster look smaller than it is.

Remote provider targets appear in the same list, marked as remote and never drawn as machines in the cluster graph. They are not part of the cluster; they are somewhere traffic can go. Under LOCAL_FIRST, show plainly whether the current request flow is local or spilled, because that is the state someone actually wants at a glance.

A providers section lists each configured upstream with its health, model count, and today's spend. API keys render as `***` and there is no reveal control. Never build one.

---

## Adding a node

Discovery runs continuously. Found machines appear under the roster, greyed, with detected hardware. One click to add.

```
  Found on your network
  ─────────────────────
  spark-02
  GB10 · 128 GB · 10.0.4.19
  [ Add to cluster ]
```

No IP field in the primary path. A manual-entry fallback behind a small link, because discovery will fail for someone and a dead end is worse than an ugly form.

Heterogeneous nodes show their real profile. When one cannot join the current serving pool, the roster says so plainly rather than hiding it or silently degrading.

---

## Refusal states

The fit gate refusing a launch is a designed screen, not an error toast. When Agent D returns `WONT_FIT`, show the memory breakdown as a proportional bar against the usable line, with the limiting term marked, and the reason verbatim. When there is a `max_context_that_fits`, offer it as a one-click adjustment.

`FITS_DEGRADED` gets its own treatment: it will load, and the predicted decode is shown next to the launch button so the choice is informed rather than blocked.

Other states:

- Single node, nothing running: the hero area holds the machine's own profile and a model picker. Not blank, and not advertising features that need a second machine.
- Node unreachable: roster entry goes `--fault`, keeps last known values greyed, states what happened. Serving continues on remaining nodes when the plan allows.
- No link measurement yet: the Plan panel says so and offers to run one, since a measurement is disruptive and should not start unasked.

---

## Data

Everything over Agent G's HTTP surface. `GET /api/cluster` and `GET /api/topology` on load, `GET /api/metrics/stream` via EventSource for live values, `POST /api/plan` for the dry-run panel, `GET /api/routing` and `PUT /api/routing/{name}` for the routing panel, `GET /api/providers` for the providers section.

Reconnect the stream with backoff on drop. During a gap, grey the live values rather than freezing them at the last reading or zeroing them. A frozen number that looks live is worse than an obviously stale one.

---

## Acceptance

- Renders fully against Agent G's fixture stub with no live cluster.
- Hero number updates at 1 Hz without layout shift. This is what tabular figures are for; verify it.
- Plan panel expansion shows the planner's reason and rejected list verbatim.
- A discovered node appears in the roster without any configuration and adds in one click.
- The cluster graph draws every node and every measured link, with bandwidth printed on the edges and unmeasured links dashed with no number.
- Graph layout is deterministic: the same cluster produces identical positions across refreshes.
- A deployment spanning two nodes draws as a band across both.
- The routing panel shows live weights and changing the policy takes effect without a reload.
- Remote providers appear in the routing panel but never as nodes in the cluster graph.
- Under LOCAL_FIRST, the UI shows at a glance whether traffic is currently local or spilled.
- No API key is rendered anywhere, and there is no reveal control.
- A `WONT_FIT` response renders the breakdown, the limiting term, and a working one-click context adjustment.
- Dark mode is a token swap with no component changes.
- Stream disconnect greys values and reconnects without a reload.
- Keyboard focus visible, reduced motion respected, readable at 1280px wide.

## Traps

Do not paraphrase planner or fit strings, they are more precise than a rewrite. Do not add a chart library for one line graph. Do not use non-tabular figures for anything that updates. Do not build the deep-dive metrics page, it is explicitly cut. Do not use a force-directed graph layout, it drifts and reads as a toy at this node count. Do not draw a bandwidth number for an unmeasured link. Do not hide a zero-weight target. Do not let dark mode become a second set of components.
