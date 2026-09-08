# spend

The Spend screen's arithmetic, kept out of the component so it can be checked
against a live coordinator rather than only typechecked. Everything here is
pure.

One rule runs through every function: **a number we do not know is `null`, never
`0`.** That rule is not a preference. `spend_today_usd` arrives from
`providers/serialization.py` as `round(runtime.spend_today(now), 6)` and is never
`None`, so a provider that served two hundred requests through models it
publishes no price for reports exactly the `$0.00` of one that served nothing —
and the tile said "spent today" over both. `providers/runtime.py` had counted
`unpriced_requests` all along; this folder is what finally reads it.

## Layout

| File | Lines | What it owns |
|---|---|---|
| `rows.ts` | 224 | `SpendBasis`, `cloudSpend`, `fleetSpend`, `cloudTokens`, `publishedRange`, `formatRange`, `money`, `spendCaption`, `spendNotes`, `basisNote` |
| `rows.check.mjs` | 256 | 35 hermetic checks plus a pass over every provider the live coordinator serves. `// requires: coordinator` |

## `rows.ts`

`SpendBasis` is the type that carries the distinction the module exists for, and
it has six members because there are six different things a dollar figure can
be: `metered` (the provider told us what it charged — OpenRouter puts a `cost` in
every usage block, after caching, long-context tiers and per-modality
surcharges, so it is a ledger entry rather than a projection), `estimated`
(computed here from published input/output rates: right for a plain request,
blind to a cached prompt), `mixed`, `unpriced` (requests were served and none of
them could be priced at all), `idle` (a real zero) and `unknown` (the port does
no accounting — an absence, not a zero).

`cloudSpend(p)` maps one provider onto that type and returns `{ usd, floor,
basis, unpricedRequests }`. `floor` is what makes a partial answer honest: real
traffic sitting under the figure unpriced means `usd` is a lower bound, and
`money(usd, floor)` renders `≥ $1.50`. `fleetSpend(providers)` sums only what is
known, seeds `usd` from `null` rather than `0`, and pushes a provider onto
`blindProviders` only when its basis is `unpriced` — a non-accounting port may
have had no traffic at all, which is a different claim and must not be printed as
missing money. `spendCaption` and `spendNotes` then name both exclusions — an
unset electricity rate and unpriced cloud models — from the same code, so
neither can be forgotten alone. The header names its verifier `spend.check.mjs`;
the file on disk is `rows.check.mjs`.

## `rows.check.mjs`

The sibling of `tabs/models/rows.check.mjs`, and for the same reason: there is no
test runner in `ui/`, and everything interesting here is about what a real
payload *means*. A green `tsc` says `spend_today_usd` is a number. It cannot say
the number is knowledge, and that distinction is the whole module.

It bundles `rows.ts` with esbuild's JS API — not the launcher under
`node_modules/.bin`, which is a POSIX script with no `.cmd` twin — then runs 35
hermetic checks over four sections: the `$0.00`-that-is-not-a-zero cases,
metered versus estimated (including a pre-metering record whose
`metered_requests_today` key is absent rather than zero, which must read as
`estimated` and not crash), the fleet total, and `publishedRange`. A fifth
section then walks every provider on `$DERATE_CHECK_ORIGIN/api/providers`.

Two of its own bugs are recorded in it. It used to catch an unreachable
coordinator, print SKIP and carry on to `process.exit(0)`, so a coordinator that
was simply not running produced the same green as one whose every number checked
out; the `// requires: coordinator` line now makes `ui/check.mjs` skip the file in
its own column, and `--strict` turns that into a failure. And it used to assert
`provs.length > 0` — turning "the operator has not added a provider yet" into a
test failure — then read `provs[0]` anyway and crash.

## The seam with `SpendTab.tsx`

`SpendTab.tsx` is the only consumer, and imports nine names plus the
`SpendBasis` type:

```tsx
import type { SpendBasis } from './spend/rows'
import {
  basisNote, cloudSpend, cloudTokens, fleetSpend, formatRange,
  money, publishedRange, spendCaption, spendNotes,
} from './spend/rows'
```

The wire side is `useProviders()` from `state/resources.ts` against
`GET /api/providers`. The verifier reads the same route directly and asserts the
keys this module depends on are actually on it — `metered_requests_today` present,
`tokens_today` an object — because a server-side rename would otherwise surface
as a silent em dash rather than as a failure.

## Things that look like details and are not

**`publishedRange` reads `Provider.models`, not the catalogue, and that is the
whole point.** `/api/providers` is served through `internal_api.py`'s
`_servable(...)`, which filters to what the operator switched on; the unfiltered
catalogue lives at `/api/providers/{id}/models` alone. Read from there instead, a
provider serving one $0.12 model out of 428 would quote `$0.000–$600.000` — true
of OpenRouter's price list, and a wild misstatement of what anyone can be charged
here. The verifier pins this by asserting `models[]` matches `model_count`, and
that the catalogue endpoint holds at least as many rows as the servable list.

**The rate shown is the output rate.** It matches
`gateway/targets.py::remote_cost_per_mtok`, the figure the router itself weighs
on the reasoning that decode dominates a serving workload, so the screen quotes
the number the system acts on rather than a second opinion computed beside it.
The input rate is a fallback only where a model publishes that and nothing else.

**`cloudTokens` is the output half, never input + output.** A local target's
`counters.total_tokens` is filled by `gateway/proxy.py` from the completion count
alone; summing both halves here would make one "tokens generated" tile compare a
cloud number against a local one measuring something else.

**`$0.000` and `—` are different answers.** A free model publishes a price of
zero and that is knowledge; an unpriced one publishes nothing. `formatRange`
returns the em dash only for the second, and the verifier checks both.

**A metered `$0.00` stays priced.** The unpriced test is `unpriced >=
requests` — request counts, never the dollar figure — so three metered requests
that happened to cost nothing report `usd: 0` with basis `metered`, and a free
model does not read as an unpriced one.

## Failure behaviour

- **A port that does no accounting.** `requests_today` and `spend_today_usd`
  arrive null together (`gateway/ui_detail.py`'s `_UNKNOWN_SPEND`); basis is
  `unknown`, `usd` is null, and nothing is added to `blindProviders` — there may
  be no traffic behind it at all.
- **Every request unpriced.** `usd` is null and basis is `unpriced`. The provider
  is named in `blindProviders`, the fleet total is marked a floor, and
  `spendNotes` says how many requests went to models that provider publishes no
  price for.
- **Some requests unpriced.** The figure is real and `floor` is true, so it
  renders `≥ $x.xx` and the caption lists the exclusion.
- **An empty fleet, or a fresh install with no providers.** `fleetSpend([]).usd`
  is `null`, not `$0.00`. The verifier treats a provider-less coordinator as a
  real state and checks that case rather than demanding it away.
- **A record written before metering existed.** `metered_requests_today ?? 0`
  reads an absent key as zero requests metered, so the basis is `estimated`.
- **No coordinator on `$DERATE_CHECK_ORIGIN`.** `ui/check.mjs` skips the whole
  verifier and prints the reason in its own column; under `--strict` that is a
  failure. It never reports as a pass.

## Deliberately not built

**An average $/Mtok.** A provider serving a cheap model and an expensive one has
no single rate, and the midpoint is a number nobody is ever charged, so
`formatRange` prints `$0.120–$6.000` instead.

**A "saved versus all-cloud" tile.** `SpendTab.tsx` records it as one of two
inventions from `mockups-next/js/spend.js` that are dead on arrival: it rested on
a `0.0009 Mtok/request × $0.60` constant that exists nowhere on the real wire.
"Tokens generated" replaced it with a real sum. The other was a "managed remote"
tier — a target is local or a provider, never a third kind.
