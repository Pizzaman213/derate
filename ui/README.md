# UI

Agent H. Owns `ui/**` and nothing else. Reads everything over Agent G's HTTP
surface as specified in `00-architecture.md` §4.8, and calls no other component
directly.

```bash
npm install
npm run dev        # http://localhost:5173, proxies /api and /v1 to :8080
npm run build      # static assets into dist/, served by the coordinator
npm run typecheck
```

## Running before there is a cluster

`VITE_API_MODE` selects the backend, and defaults to `auto`:

| value | behaviour |
|---|---|
| `auto` | probe `GET /api/cluster` once; use it if it answers, otherwise fixtures |
| `live` | always the coordinator |
| `fixture` | always the in-browser stub, even if a coordinator is running |

In fixture mode the nameplate says **fixture data** and offers three scenarios —
two Sparks serving, one node with nothing running, spark-02 unreachable. Demo
numbers are always labelled as demo numbers; a fixture that looks live is worse
than one that says what it is.

`src/api/fixtures.ts` is Agent G's day-0 stub reimplemented in the browser. Node
profiles, the 10.2 GB/s ConnectX-7 measurement and the model shapes are copied
from `tests/fixtures/`, so this renders the same demo data every other
workstream tests against. **Delete it at integration**, along with the fixture
branch of `src/api/client.ts`.

## Layout

```
src/api/        types mirrored from the frozen contracts, the HTTP client,
                the fixture stub, and a defensive credential scrub
src/state/      polled resources and the 1 Hz metrics stream
src/styles/     the token layer; dark mode redefines five variables
src/components/ Readout, Lamp, Sparkline, Bars, Panel, Verbatim
src/panels/     sidebar: roster, discovery, plan, routing, providers
src/views/      instrument, cluster graph, plan and refusal, node detail
```

## Things that look like details and are not

**Tabular figures.** Every number updates once a second. `Readout` reserves its
width in `ch` and formats to a fixed decimal count, so a value crossing from two
digits to three moves the digits and never the unit. Verified: over twelve
samples the hero cycled through nine values with its left edge, box width and
the caption below it all fixed to the pixel.

**`Verbatim` exists to make a rule structural.** Planner and fit strings are
more precise than any rewrite, and they are the product. They are rendered
exactly as received — never truncated, re-cased, or summarised. Rendering one
through anything else is the bug.

**Graph layout is a pure function of the sorted node ids.** Row up to four
machines, ring beyond. No force simulation: at this node count physics produces
drifting, unrepeatable positions, and a machine that moves between refreshes is
a machine you cannot learn the position of.

**Edge thickness is scaled against the 40 GB/s tensor-parallel threshold**, not
against the fastest link present, so a link drawn at full weight is a link where
TP is viable. An unmeasured link is drawn dashed, carries no figure, and offers
a measurement. A measurement saturates the link, so it is never started unasked.

**A stream gap greys values and keeps the last reading.** Not frozen as if live,
not zeroed. The lamp goes hollow so it stops claiming freshness, and the trace
breaks rather than drawing a straight line across the gap. Reconnect is
exponential backoff from 1 s to 30 s, with no reload.

**No API key is rendered and there is no reveal control.** `src/api/redact.ts`
scrubs every response on the way in as a backstop; `api_key_ref` is an
environment variable name and is kept, anything key-shaped is replaced. Do not
add an inverse.

## Deliberately not built

Per `00-architecture.md` §1: no WAN endpoint, no chat interface or history, no
log browser, no deep-dive metrics page, no model catalog browser. The picker is
the four curated shapes plus one free-text HuggingFace ID field. The main view's
readouts are the whole metrics surface.
