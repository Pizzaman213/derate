# screenshots

Every image the root README shows or links, and the two commands that produce
them. Nothing here is authored by hand and nothing here is drawn.

They arrive by two different routes, and the difference is worth holding onto.
The **eight PNGs** are photographs: a real Chromium against a real coordinator
serving a real cluster, so the numbers in them are numbers the software actually
produced on a box with three nodes and four deployments — a claim in the README
can be checked against the picture under it. The **four SVGs in `brand/`** are
derived: every proportion re-computed from what `ui/src/shell/Header.tsx`
actually renders, every colour a literal copied out of `ui/src/styles/tokens.css`.

What they have in common is that both go stale silently. A capture ages the
moment the UI changes; the banner ages the moment the header or a token moves.
Neither has a test, and no failure is reported in either direction. Both are one
command away from correct:

```bash
cd ui && npm run screens                       # the eight PNGs, into ui/screens/
python3 docs/screenshots/brand/build.py        # all four SVGs, in place
```

## Layout

The captures are 1440 × 900, PNG, dark theme — the viewport, colour scheme and
reduced-motion setting are fixed in `ui/src/shell/screens.check.mjs` and are not
a property of any individual file. `cluster.png` and `model-detail.png` are
the two exceptions: see their own sections below.

| File | Size | What it shows |
|---|---|---|
| `chat.png` | 124 KiB | the model picker grouped by endpoint, and the composer |
| `cluster.png` | 622 KiB | hand-captured, nodes dragged into a row, the routing graph out to each deployment and provider, the Links card and the legend |
| `dashboard.png` | 94 KiB | four headline numbers, the deployment list, the node rail |
| `model-detail.png` | 835 KiB | hand-captured, `qwen3-30b-a3b`'s refusal beside `Qwen2.5-0.5B-Instruct`'s Advanced fit breakdown, verbatim |
| `models.png` | 286 KiB | the model grid — what runs here and what does not |
| `settings.png` | 121 KiB | the Connection tab and the cluster summary |
| `setup.png` | 36 KiB | first run, step 1 of 4, on a machine with nothing on it |
| `spend.png` | 116 KiB | where requests went today, and what they cost, by target |
| `brand/` | 5 files | the README's banner and bare lockup, and the script that draws them |

`models.png` is nearly eight times `setup.png` — 293,643 bytes against 37,882 —
because it is a wall of publisher avatars fetched from huggingface.co, where
`setup.png` is one card on an empty page.

Inside `brand/`:

| File | Lines | What it owns |
|---|---|---|
| `build.py` | 373 | the whole generator: font outlining, the header-derived geometry, the pulse, and both themes |
| `derate-banner-dark.svg` | 1 | 1200 × 367.95, 36,132 bytes. The dark banner: `#191817` field, `#EDE9E0` ink, `#7FA3D4` pulse |
| `derate-banner.svg` | 1 | 1200 × 367.95, 36,132 bytes. The light banner, and the `<img src>` the root README falls back to |
| `derate-lockup-dark.svg` | 1 | 216.37 × 43.18, 3,350 bytes. Mark plus wordmark in `#EDE9E0`, no field, no pulse |
| `derate-lockup.svg` | 1 | 216.37 × 43.18, 3,350 bytes. The same in `#1A1917` |

Every SVG is one line because the generator emits one line. `wc -l` is not a
useful measure of them; the byte counts and the viewBox are.

---

# The eight captures

## `chat.png`

The left rail lists every servable model grouped under the endpoint it actually
answers — `POST /v1/chat/completions` over six rows, `POST /v1/audio/speech`
over `Audio8-TTS-Preview-0.6b`, `POST /v1/audio/transcriptions` over
`whisper-base.en` — each labelled `local` or `remote` with its target count and
context. That grouping is the screenshot's reason for existing: it is the
evidence for the chat picker listing every modality rather than filtering audio
models out. The header line reads `POST /v1/chat/completions · not stored ·
this transcript is gone on reload`.

## `cluster.png`

The one file in this folder that `screens.check.mjs` did not take. It is a
hand capture from a live browser against this same box's coordinator, at
3240 × 2228 rather than the fixed 1440 × 900 viewport, with the three
machines dragged into a row — the toolbar says so on screen
(`ui/src/tabs/cluster/GraphToolbar.tsx`: "Drag a machine anywhere — it stays
where you put it") and `ui/src/tabs/cluster/order.ts` persists the
displacement per cluster, so this layout is a choice, not a default. Below
the floor, the routing graph is fully expanded: three
`POST` endpoints on the left running into every deployment, `Qwen2.5-0.5B-
Instruct` selected and its link highlighted in blue, and the three
off-cluster rows (`deepseek/deepseek-v4-flash`, `inclusionai/ling-3.0-flash`,
`poolside/laguna-xs-2.1`) drawn out to a `2 providers · third party · no
telemetry` panel that a default-layout capture doesn't have room to show
without overlap. The Links card still reads `3 node pairs · 0 measured · 3
never measured`, and every pair chip still says `never measured` — worth
knowing before quoting this image as evidence of a measured interconnect,
which is what the root README's `alt` text on it claims. The legend under
that is the full key: thick for a measured all-reduce against the 40 GB/s
tensor-parallel threshold, dashed for never measured, teal versus violet for
streamed versus not.

Because it was captured and promoted by hand, it will not survive the next
`cp` in **Promotion is a copy** below — that command still pulls a fresh,
default-layout `cluster.png` out of `ui/screens/`, which would silently undo
this. Re-promote it manually, from a browser, if it's still wanted after the
next round of captures.

## `dashboard.png`

The Overview tab: 1099 tokens per second across all models, 4 deployments
serving, 66 watts drawn, 99.8 GiB addressable free, over a deployment list where
each row carries its plan (`single node`, `single node · speech`) and its target
count. Two rows show a `—` for tokens per second rather than a zero: nothing has
been asked of them.

The right rail beside it — three nodes with watts and GPU utilisation, the plan
panel for the selected model, the routing policy with its own reason text, and
cost per Mtok derived from watts at $0.14/kWh — is on five of the eight images:
this one, `cluster.png`, `chat.png`, `spend.png` and `settings.png`. `models.png`
and `model-detail.png` give that space to the grid and to the open model sheet,
and `setup.png` has no shell around it at all.

## `model-detail.png`

The most useful image here, and the one that justifies the folder. Also, like
`cluster.png`, a hand capture from a live browser rather than a
`screens.check.mjs` run — 3200 × 2220, not the fixed 1440 × 900 viewport.

On the left, the refusal for `qwen3-30b-a3b` rendered in full:

> Won't fit: weights alone are 56.9 GiB per rank against 0.6 GiB allocatable
> right now (on spark-4d38; its 90% static ceiling is 107.7 GiB). Over budget by
> 81.4 GiB in total. Context and concurrency cannot fix this at bf16 on 1 rank —
> it needs a different machine.

On the right, `Qwen2.5-0.5B-Instruct`'s own sheet, scrolled to the machine
table and past it into the Advanced tab — a fuller view than the Basic tab a
`screens.check.mjs` run captures. `spark-26af` carries "the planner picked
this" and "This machine is already running this model as
Qwen2.5-0.5B-Instruct. A node runs one copy of a model: a second copy shares
the same GPU and the same unified memory."; `Connor-Pi` is marked `CPU only`
with "This machine reports no addressable GPU memory. It can be a cluster
member; it cannot carry a rank." Below that, the Advanced tab's own verbatim
text:

> single node on spark-26af: the model fits within 107.7 GiB of usable memory
> on one node at 32768 context and concurrency 8, so nothing crosses the
> interconnect; run a second replica on spark-4d38 and let the gateway load
> balance; excluded connor-pi because pooling hardware unlike the NVIDIA GB10
> group would make the slowest node the tail latency for every request, so it
> is offered as a separate deployment target instead.

— followed by the two rejected alternatives (`PP=2`, `TP=2`, each declined
because the model already fits on one node so splitting it would add
cross-node exchanges per token for nothing), the resolver's own note (`tts:
Qwen2ForCausalLM is not in tts's supported architecture list; the vllm
runtime loads it`), the idle-hardware and right-now fit lines with their
predicted decode rate and max context, and the per-node memory breakdown
(weights, kv cache, activations, framework overhead) that back the slider
above them.

That is what "planner and fit strings are the product" looks like on a
screen, and this PNG is the only proof of it outside a running cluster.

## `models.png`

The grid: `Running here` (4) above `Not checked` (84), each card with its
publisher avatar and its on-disk size. The header states the rule the screen
runs on — "Fit on spark-4d38, each at the largest context it can hold up to its
own window, against what the nodes can hand out right now. The first 10 rows
with no verdict are checked as the list settles; the rest say 'not checked'
rather than guessing." This and `settings.png` are the two linked inline from
the README rather than placed in the table.

## `settings.png`

The Connection tab, plus the Cluster card: cluster id `c-c788`, coordinator
`spark-4d38`, 3 nodes 3 healthy, 239 GiB total memory, discovery over mDNS on
`_derate._tcp.local.`. `In use` reads `http://localhost:8088 — this page's own
origin`, which is this box's live coordinator and is visible in the shipped
image; the tab row above it (`Connection · Nodes · Storage · Providers · Policy ·
Appearance · About`) is also the evidence that Storage is a tab here and not a
destination of its own.

## `setup.png`

First run on a machine with nothing configured: `step 1 of 4` in the header, one
card reading `NVIDIA GB10 / 119.7 GB · 273 GB/s memory bandwidth`, and two
choices — `Use this machine` or `I only want cloud models`. The root README's
caption under this image says "a five-step walkthrough"; the capture says four.
The PNG is the evidence and the caption is the thing to reconcile.

## `spend.png`

`Where requests went today`: 100% local, 1,045,180 requests, $0.00 spent,
2,317,724 tokens generated, over a by-target table that separates `local` rows
from `cloud` ones and gives openrouter a `$0.063–$0.177` per-Mtok range where
the local rows read `$0.000`. The subhead states the distinction the screen
exists for — "cloud spend is what your provider reports it charged; local cost
is derived from measured power draw at your rate."

---

# `brand/` — the banner and the lockup

None of it is hand-drawn art. Every proportion is re-derived from what
`Header.tsx` actually renders and every colour is a literal copied out of
`tokens.css`, which makes the four SVGs stale the moment the header or a token
moves — and correct again one command later. `build.py` takes no arguments and
always writes all four files.

## `brand/build.py`

One module, no `main()` and no argument parsing. It reads two woff2 files out of
`ui/node_modules/@fontsource/ibm-plex-sans/files`, outlines the wordmark and the
quote to glyph paths, derives the lockup's proportions from the header's own CSS
geometry, and writes four files. It needs fontTools with brotli (woff2 is
brotli-compressed) and it needs `cd ui && npm install` to have been run, because
that is where the fonts come from.

The public surface is small and all of it is module level: `Face` (one font
file, `.outline(text, size, tracking_em) -> (path, bounds, advance)`),
`path_length(d)`, `pulse_path(name, d, colour, opacity)`, `lockup(ink, dx, dy,
scale, pulse)` and `svg(width, height, body, label, head)`. The constants that
decide the result are `TEXT` (`"derate"`, lowercase, as the header's own
wordmark), `SIZE` (40.0, the em the lockup is expressed in), `PAD` (3.0),
`MARK`, `THEMES`, `PULSE_DUR` and `PULSE_WINDOWS`.

### The checkout is found, not counted to

`_repo_root()` walks up from the script looking for `pyproject.toml`. The
previous `REPO = HERE.parent.parent` was right for exactly one location, and
this folder has since moved under `docs/screenshots/` — which silently
repointed `FONTS` at `docs/ui/node_modules/` and left the build unable to find
a font it was standing three directories away from. Nothing about that failure
says "the path arithmetic is one level out"; it says `FileNotFoundError` on a
woff2. Searching for the marker means the next move costs nothing.

### The header is the source, and the numbers are re-derived from it

`Header.tsx` renders a 32×24 `<svg>` over a `viewBox="0 0 64 50"` next to an
18px span, in a flex row with `gap: 14px; align-items: center` (`derate.css`,
the bare `header` rule), and nudges the mark down 2px. Those five numbers are
`H_FONT`, `H_SVG_W`, `H_SVG_H`, `H_GAP` and `H_NUDGE`, and three quantities are
computed from them in ems so the lockup can be drawn at any size: the ink-to-ink
gap, the mark's scale against the text, and where the mark's centre sits
relative to the baseline.

**32×24 over a 64×50 box is not a uniform scale, and pretending it is puts the
wordmark 0.64px too far out.** 32/64 is 0.5, 24/50 is 0.48; `preserveAspectRatio`
defaults to `meet`, so the browser draws at the *smaller* and letterboxes the
mark 0.64px either side. `h_scale` takes the `min` and `h_letterbox` carries
the remainder into `gap_em`, which measures ink to ink rather than box to box —
the svg box carries the letterbox plus the mark's own right margin, and the `d`
carries a left side bearing. The result is greppable: `MARK_SCALE` works out to
0.48 / 18 × 40, and both lockup files contain the literal `scale(1.0667)`.

`MARK_CENTRE` reproduces the header's judgement rather than its pixels.
`align-items: center` centres the svg box on the text's *line* box, and
`Header.tsx` then pushes the mark down 2px because "the monogram's solid stroke
sits above the icon's own bounding-box centre — the faint echo stroke below it
doesn't carry the same visual weight — so flex centring against the wordmark
leaves the mark looking high." `centre_em` is that same nudge as a fraction of
the mark, so it survives a change of size.

### The wordmark is outlined, not set

`Face` opens a woff2 with `TTFont`, and `outline()` walks each glyph through a
`TransformPen` carrying `Transform(scale, 0, 0, -scale, x, 0)` — the sign flip
is font space being y-up and SVG being y-down — into an `SVGPathPen`, running
the same loop a second time through a `BoundsPen` to get the ink bounds. It
returns the path, the bounds and the advance.

`<text>` would have been shorter and is wrong: IBM Plex Sans is not installed on
most machines that will open this README, including the one the files were built
on. `MEDIUM` is the 500 weight and draws the wordmark at `letterSpacing:
'-.3px'` scaled off the header's 18px; `REGULAR` is the 400 weight and draws the
two-line quote. Outlining is also why `derate-banner.svg` is 36 KB against the
lockup's 3.3 KB — the quote is 93 characters set as outlines.

### Both themes are baked, because an `<img>` inherits no colour

`THEMES` is two dicts of four literals each, copied from `tokens.css`: light is
`ink #1A1917`, `muted #5C5851`, `panel #EDE9E0`, `flow #4A6FA5`; dark is
`#EDE9E0`, `#979186`, `#191817`, `#7FA3D4`. `Header.tsx` uses `currentColor`,
which is the right answer inside the app and useless here — an SVG loaded
through `<img>` inherits nothing from the host page, so `currentColor` resolves
to black and the mark vanishes against a dark README. Two files and a
`<picture>` element are the substitute.

`flow` is not a colour picked for the banner. It is the token `tokens.css`
reserves for "a request in flight. An accent, not a grey: it is a real measured
event", which is exactly what the pulse running the mark depicts.

### The pulse is one dash on a path whose length was measured

`path_length(d)` sums an axis-aligned `M`/`H`/`V` path and **raises on anything
curved** — `path_length handles M/H/V only` — so the animation cannot keep
running the length of a mark that has since been redrawn. The solid branch
measures 62 units and the echo 44, and both numbers are in the banner as
`stroke-dasharray="6 44"` and `stroke-dasharray="6 62"` — echo first, because
`MARK` lists it first.

`pulse_path` overlays each branch with a travelling block. Three details are
load-bearing:

- **`stroke-linecap` is `butt`, not the mark's `square`.** A square cap adds 3
  units at each end, which would draw a 6-unit dash 12 long. The dash is 6
  because that is the mark's own `stroke-width`, so the block is square by
  construction rather than by a tuned number.
- **The gap is the whole path length**, so exactly one block is ever on it.
- **The two branches are sequenced with `keyTimes` inside one duration, never
  with `begin` offsets.** Under `repeatCount="indefinite"` a `begin` offset
  repeats on the animation's own period rather than on the cycle's, and the two
  branches would drift apart. `PULSE_DUR` is `5s` and `PULSE_WINDOWS` gives the
  solid branch 0.00–0.34 of the cycle and the echo 0.42–0.76; the rest of the
  cycle both blocks sit off the path.

The pulse paths are emitted inside the same `<g>` as the mark, so they inherit
its transform and `stroke-width` and can never be laid over a differently placed
mark. Only the banner passes a `pulse` colour — the bare lockup stays still.

### It runs at import, and `scale=1` emits no transform

There is no `if __name__ == "__main__"`. The two `for suffix, theme in
THEMES.items()` loops are at module level, so importing this module writes four
files. It is a script and is only ever run as one.

`lockup()` folds the offset into the two inner transforms when `scale == 1.0`
instead of wrapping a group, so the bare lockup file carries no transform that
does nothing — it is the artifact people open and read. That is visible in
`derate-lockup.svg`: `translate(-2.333 -4.622) scale(1.0667)` is `PAD` already
folded into the mark's own ink offset, and there is no outer `<g>`.

## `brand/derate-banner-dark.svg` and `brand/derate-banner.svg`

1200 × 367.95, both 36,132 bytes, differing only in the four theme literals. A
rounded `<rect width="1200.0" height="367.95" rx="18">` in the panel colour, the
lockup centred on it at `LOGO_INK_W` (380 units of *ink* width, not box width),
and the two quote lines in the muted colour under it. `BANNER_PAD` is 84 at the
top, `LOGO_TO_QUOTE` is 58 of ink gap from the mark's echo stroke to the quote's
cap height, `QUOTE_SIZE` is 34 and `QUOTE_LEADING` 1.45.

Two measurements decide the vertical rhythm and neither is guessed. The first
baseline is placed off the real ink — `min(b[1] for _, b, _ in lines)` — because
the quote opens with a quotation mark whose top is higher than the `I` beside
it. And `BANNER_H` is measured to the last *baseline* plus the padding, not to
the descenders below it. Only the `y` of "your" and the `g` of "recoding" drop
below the last line's baseline, and padding from those two leaves the banner
reading bottom-heavy against a logo whose top edge is a solid stroke.

The banner alone carries `REDUCED_MOTION`, a 75-byte
`@media (prefers-reduced-motion:reduce){.pulse{display:none}}` — the docstring
above it rounds that to "seventy". `derate-lockup.svg` has no `<style>` at all.

## `brand/derate-lockup-dark.svg` and `brand/derate-lockup.svg`

216.37 × 43.18, both 3,350 bytes: the mark and the wordmark, ink-flush, with
`PAD` (3.0) of margin so that centring the file centres the *ink*. No field, no
tagline, no animation. The `MARK` list is two paths lifted from `Header.tsx`
verbatim — `M26 25 V39 H56` at opacity 0.32, the derated branch stepping down,
and `M8 25 H26 V11 H56` at full opacity, full rate stepping up — stroked 6 wide
with square caps. That puts the ink at x 5..59, y 8..42: symmetric in the 64×50
box, which is why flex centring in the header centres the ink and not merely the
element.

Nothing in the repository references these two files today. They exist because
a bare mark is what anything other than the README banner wants, and generating
them costs one extra `write_text` per theme.

---

# The seam with the root README

`README.md` is the only consumer of anything in this folder, in both directions.

The banner is lines 1–6, and nothing else in the repository reads it:

```html
<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/brand/derate-banner-dark.svg">
    <img src="docs/screenshots/brand/derate-banner.svg" width="100%" alt="derate — I want it to be simple to run multiple models on your own machine, without recoding the API.">
  </picture>
</p>
```

Six of the eight captures sit below it in a two-column `<table>` with a `<sub>`
caption each; `models.png` and `settings.png` are linked inline under that
("More: ..."); and `cluster.png` appears a second time, at `width="820"`, under
**The cluster**. Paths are repo-relative, so everything resolves on GitHub and
in any local Markdown viewer without a base URL.

The `alt` text on the banner is the quote, and so is each *banner's* `<title>`
and `aria-label`, assembled by the generator from `QUOTE` with the surrounding
smart quotes stripped. The two lockups are labelled `TEXT` instead — `derate`,
one word — because a bare mark with a sentence for a name reads as a sentence
everywhere it is embedded. The captures' `alt` text is different in kind: those
sentences are hand-written in the README, are not regenerated with the images,
and have already drifted — see `cluster.png` and `setup.png` above.

The other direction of the brand seam is the one that rots.
`ui/src/shell/Header.tsx`, `ui/src/tabs/SetupTab.tsx` line 336 (`width="30"
height="23" viewBox="0 0 64 50"`, the same mark at setup size) and
`ui/src/styles/tokens.css` are read by hand, by a person, at the moment somebody
runs the script. Nothing tests that they still agree; the only guard is a note
at each of the three ends — `ui/src/shell/README.md` and
`ui/src/styles/README.md` both say to rerun the script after touching the
monogram or a colour.

# Regenerating

**The four SVGs**, in place, no arguments, always all four:

```bash
python3 docs/screenshots/brand/build.py
```

**The eight captures** come from `ui/src/shell/screens.check.mjs`, wired as two
npm scripts in `ui/package.json`:

```bash
cd ui && npm run screens     # node src/shell/screens.check.mjs --capture-only
cd ui && npm run check       # the same file, with its assertions, via check.mjs
```

`npm run screens` captures and asserts nothing. It needs a coordinator on
`$DERATE_CHECK_ORIGIN` (default `http://localhost:8088`) and the Chromium
already in the Playwright cache — nothing is downloaded, and `findBrowser()`
says plainly when there is none. It writes `ui/screens/<name>.png` and exits.

Promotion is a copy, and it is the only manual step in either pipeline:

```bash
cp ui/screens/{dashboard,cluster,models,model-detail,chat,spend,settings,setup}.png \
   docs/screenshots/
```

Running this as written overwrites the hand-captured `cluster.png` and
`model-detail.png` (see their own sections above) with the automated
versions. That is correct the moment the UI actually changes; it is not
correct if the only thing that happened is a routine re-promotion of the
other six.

**`ui/screens/` holds more than this folder does.** `ui/.gitignore` ignores
`screens/` outright — "both are output, both are regenerated on demand, neither
is reviewable in a diff" — so a capture run leaves whatever it produced plus
whatever earlier runs left behind. On this box that is fourteen files: the eight
here, `node-sheet.png` and `dep-sheet.png` (sheets over a destination, captured
only when the live coordinator has a node and a deployment to open),
`chat-two-models.png` and `chat-two-models-dark.png` from some other
investigation, `console.txt`, and `speech.png` — a destination that was retired
on 2026-09-08 and whose PNG is still sitting there. Copy the eight by name.
`docs/screenshots/` is tracked; `git ls-files docs/screenshots` is the list.

# Things that look like details and are not

**The capture filenames are URL segments, and they are read out of the router
rather than written down.** `SCREENS` is built from `routes.DESTINATIONS` in
`ui/src/state/routes.ts` through `routes.href()`, with the leading `/` stripped
— so `dashboard.png` is `/dashboard`, and a new destination is captured the
first time it exists. The verifier used to carry a literal array of seven paths
under a header claiming it captured "one per destination", and the two
disagreed the moment an eighth was added: `/speech` shipped and the only
verifier that opens a real browser did not know it existed. `model-detail.png`
is the exception — it is `/models/${someModel}` for whichever model the live
coordinator lists first, appended only when there is one.

**Animation is frozen before the shutter, not waited out.** The capture injects
`*,*::before,*::after{animation:none !important;transition:none !important}`
after `networkidle`, because the particle field never goes idle and a shot taken
without it is of an animation mid-stride rather than of a screen. `newPage()`
is also handed `reducedMotion: 'reduce'`, which the injected rule then makes
redundant for anything driven by CSS and does not for SMIL.

**Every capture is dark because `newPage()` is given `colorScheme: 'dark'`.**
There is no light-theme set of captures and no switch to produce one. The banner
does adapt to the reader's theme; the eight PNGs beneath it never will, which is
why the README reads as a dark page with a light-capable header on a light
screen.

**A capture is a photograph of one box at one moment.** The ids in these files
— `spark-4d38`, `spark-26af`, `connor-pi`, cluster `c-c788` — and the counters
in `spend.png` are that box's, and they date the image as surely as a timestamp
would. Re-promoting one image and not the rest leaves the right rail disagreeing
with itself across the README's table: `spark-4d38` draws 34 W in
`dashboard.png`, 37 W in `cluster.png` and 38 W in `chat.png`, and the
cost-per-Mtok card is derived from a different sample under each one.

**`prefers-reduced-motion` does not reach the banner in the README, and that was
measured rather than assumed.** An SVG loaded through `<img>` never sees the
host page's reduced-motion preference. It was probed in Chromium with a square
the query recolours: the square flips when the same markup is inlined into the
page, and never flips through `<img>`. `prefers-color-scheme` *does* propagate
that way, which is exactly why the `<picture>` element works and the media query
inside the file does not. `REDUCED_MOTION` is kept anyway — seventy-five bytes,
and it applies wherever the file is inlined or opened on its own. The honest
mitigation for the README is the motion itself: one small block, most of a
five-second cycle at rest, and a mark that is never caught incomplete.

**The brand colours are literals, not `var(--panel)`.** A standalone SVG has no
stylesheet to resolve a custom property against, so the four tokens are pasted
in per theme. `tokens.css` carries its own history for one of them —
`--ink-muted: #5C5851; /* was #6E6A62: 4.44:1 failed AA on 12px text */` — and
that change reached the banner only because somebody re-ran the script.

**`MARK_SCALE` is derived from a `min`, not from a division.** Using 32/64
because it is the obvious ratio produces a mark 4% too large and a gap 0.64px
too wide, in a file nothing typechecks and no test opens.

**The tagline is a quotation and is stored as one.** `QUOTE` is two lines with
the smart quotes included in the first and last, and the banner's call to
`svg()` slices them off in the `label` argument it passes — `QUOTE[0][1:]` and
`QUOTE[1][:-1]` — so the accessible name is a sentence rather than a fragment of
punctuation. `svg()` itself only interpolates whatever `label` it is handed.

# Failure behaviour

A PNG and an SVG have none. These are the two generators.

**The capture** (`screens.check.mjs`):

- **No coordinator on `$DERATE_CHECK_ORIGIN`.** The three seed fetches
  (`/api/models`, `/api/topology`, `/api/deployments`) each `.catch(() => null)`,
  so `model-detail`, `node-sheet` and `dep-sheet` are simply not appended and
  the destination screens are captured against a dead origin — which under
  `npm run check` fails on `#root` height and on any coordinator response of 400
  or worse (`if (r.status() < 400) return`, so a redirect or a 304 is not a
  failure), and under `npm run screens` does not, because `--capture-only`
  asserts nothing.
- **No Chromium in the Playwright cache.** `findBrowser()` returns no path and
  the script exits 1 with the reason printed. It never downloads a browser.
- **A screen that never settles.** `waitForLoadState('networkidle', { timeout:
  15000 })` is wrapped in `.catch(() => {})`; the shot is taken anyway.
- **Off-site requests that fail.** Recorded in `ui/screens/console.txt` and
  deliberately not gated. The publisher avatars come straight from
  huggingface.co and come back 429 in bulk, and failing the gate on somebody
  else's rate limiter would make it useless. Only responses whose URL starts
  with the coordinator's own origin count as this UI's fault.
- **A secret on screen.** Under `npm run check` the rendered `body.innerText` is
  handed to the server's own `Redactor`, primed with the real stored values, and
  a hit fails the run. `--capture-only` skips that check — so a screenshot
  promoted from a `npm run screens` run has not been scanned for keys by
  anything. Look at it before committing it.

**The generator** (`brand/build.py`):

- **Not inside the checkout.** `_repo_root()` exits with `no pyproject.toml
  above <dir>` rather than resolving `FONTS` against something arbitrary.
- **No `ui/node_modules`.** `Face.__init__` raises `FileNotFoundError` on
  `ui/node_modules/@fontsource/ibm-plex-sans/files/...` at import time, before
  anything is written. `cd ui && npm install` is the fix; the dependency is
  `@fontsource/ibm-plex-sans` in `ui/package.json`.
- **fontTools without brotli.** The woff2 files cannot be decompressed and
  `TTFont` raises. Nothing partial is emitted, because both `Face` objects are
  constructed before the first `write_text`.
- **A character not in the font.** `outline()` indexes `self.cmap[ord(ch)]`
  directly and raises `KeyError`. `TEXT` and `QUOTE` are the only strings that
  reach it.
- **A curved path added to `MARK`.** `path_length` raises
  `ValueError("path_length handles M/H/V only, got ...")` rather than returning
  a wrong length, because a wrong length here is a silently drifting animation
  that no test and no typecheck would catch.
- **A path not starting with `M`.** `assert seen.startswith("M")`.

**Stale output, either kind.** There is no failure mode at all. A UI change
leaves eight correct-looking photographs of a screen that no longer exists; a
header or token change leaves four correct-looking SVGs that no longer match the
app. Nothing reports either. This README is the only mechanism.

# Deliberately not built

**`currentColor` and one file per brand asset.** It is what `Header.tsx` uses
and it does not survive `<img>`. Two files per asset and a `<picture>` element
is the substitute, at the cost of the colours being pasted rather than
referenced.

**`<text>` with a font-family.** Shorter, and it renders in a fallback face on
every machine that does not have IBM Plex Sans installed — including the machine
these were built on.

**Eyeballed proportions.** The alternative to deriving the gap, the scale and
the mark's centre from the header's own geometry is nudging numbers until the
lockup resembles the app. That resemblance decays silently at the first header
change; a script does not.

**A test for either.** Nothing asserts that the four SVGs match the current
`Header.tsx` and `tokens.css`, and nothing asserts that the eight PNGs are of
the current UI. Regenerating and diffing would be the check for the SVGs, and it
would need `ui/node_modules` present in CI to run at all; the captures would
need a live coordinator, which CI does not have.
