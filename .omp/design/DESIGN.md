# derate — design

The system behind the UI in `ui/src`: what it is for, the rules it keeps, and
the one place each rule is enforced. Tokens are in `tokens/tokens.json`, icons
in `icons/`, the living style guide is `index.html` (open it from disk), and
the gates are `cd .omp && npm run check`. When this document and the code
disagree, a check has failed somewhere; fix the check's subject, then this.

Styling is plain CSS custom properties. There is no Tailwind, no CSS modules,
no CSS-in-JS, and therefore no Tailwind preset in this pack: the token layer is
`tokens.css` and the `var(--name)` vocabulary it defines.

## Principles

Four, each with the rule it implies and the place that rule is structural.

1. **Verbatim is the product.** The planner's reasons and the fit gate's
   refusals name the term that blew the budget and the change that would work.
   *Rule:* server sentences render through `Verbatim`/`VerbatimList`, which
   have no truncation, clamp, summary or re-casing code path. A component that
   paraphrases a refusal deletes the one useful thing on the screen.
2. **Colour reports state, never category.** `live`/`warn`/`fault`/`info` mean
   something is true of the thing they colour. Identity (`tag-1..4` for chat
   turns, `avatar-1..10` for publishers) is its own ramp, ≥ 40° of hue from any
   status hue (`tabs/chat/tags.check.mjs`). *Rule:* a new colour is either a
   status, an identity, or a surface; nothing is coloured decoratively.
3. **A missing value never looks like zero.** No reading is drawn differently
   from a reading of nothing: `ProportionBar` with `value: null` is a dashed
   hollow track, `Readout` with `null` is an em dash, `Lamp` with `hollow` is a
   ring. *Rule:* every numeric component takes `null` and has a distinct
   picture for it.
4. **Figures do not shudder.** Readouts refresh every second; a digit changing
   width must not move the unit after it. *Rule:* numbers are mono with
   `tabular-nums` and a reserved `ch` width (`Readout.width`), and the mono
   size is one notch tighter than its sans partner because Plex Mono renders
   optically larger.

## Colour

### Three layers

| Layer | Path in `tokens.json` | References | CSS name |
|---|---|---|---|
| primitive | `color.<hue>.<step>`, `space.*`, `font.*`, `radius.*`, `shadow.*`, `duration.*`, `easing.*`, `z.*`, `breakpoint.*`, `opacity.*`, `size.*` | literals only | only `space.*` → `--space-<px>` and `opacity.*` → `--opacity-<n>` are emitted |
| semantic | `semantic.color.*`, `semantic.type.*`, `semantic.layout.*`, `semantic.motion.*` | primitives only | the last path segment: `--panel`, `--ink`, `--size-body`, `--dur`… |
| component | `component.<name>.<part>` | semantics only (`space.*` allowed; anything else needs `$extensions."derate.pinned"` with a reason) | `--<name>-<part>`, emitted as `var(--semantic)` so it follows the cascade |

`build.mjs` fails on a reference that crosses a layer the wrong way, on an
unresolved reference, and on a themed value with a missing theme.

### Theme mechanism

A semantic token whose value differs per theme has
`$value: { "light": …, "dark": …, "high-contrast": … }`. The build emits light
plus every invariant token in `:root`, then only the values that differ in
`@media (prefers-color-scheme: dark) :root:not([data-theme='light']):not([data-theme='high-contrast'])`,
`:root[data-theme='dark']`, `:root[data-theme='high-contrast']` and
`@media (prefers-contrast: more) :root:not([data-theme])`. `ui/src/theme.ts`
sets `data-theme` (`system` removes it); the stored value is validated against
the list. Dark inverts the surface ramp as a whole so "raised" still means
lighter-than-page. High contrast is 7:1 for text, 3:1 for UI, a 3px focus ring
and a hard outline where the other themes use a shadow.

### Vocabulary

The brief's generic roles map onto derate's existing names. The CSS names were
kept because every component already spends them.

| Role | derate token | Light | Dark | High contrast |
|---|---|---|---|---|
| background | `--panel` | `#EDE9E0` | `#191817` | `#FFFFFF` |
| surface / elevated | `--panel-raised` (+ `--shadow-raised`) | `#F7F4EE` | `#232120` | `#FFFFFF` |
| recessed | `--panel-recessed` | `#E3DED3` | `#141312` | `#F2F2F2` |
| sunk (tracks, wells) | `--panel-sunk` | `#D6D0C2` | `#0E0D0C` | `#E6E6E6` |
| overlay / scrim | `--panel-overlay` | `rgba(26,25,23,.35)` | `rgba(0,0,0,.55)` | `rgba(0,0,0,.7)` |
| text primary | `--ink` | `#1A1917` | `#EDE9E0` | `#000000` |
| text secondary | `--ink-secondary` | `#423D37` | `#C9C2B4` | `#1F1F1F` |
| text muted | `--ink-muted` | `#5C5851` | `#979186` | `#3A3A3A` |
| text inverse | `--on-fill` on `--fill` | `#EDE9E0` / `#33302B` | `#191817` / `#E2DCD0` | `#FFFFFF` / `#000000` |
| border default (decorative) | `--rule` | `#C9C2B4` | `#423D37` | `#6B6B6B` |
| border strong (decorative) | `--rule-strong` | `#B4AC9A` | `#5A544C` | `#000000` |
| border perceivable (controls, keylines) | `--edge` | `#82796B` | `#7C746A` | `#000000` |
| accent | `--accent` (= `--flow`; a selected quant card or hub card borders in it) | `#4A6FA5` | `#7FA3D4` | `#003E9C` |
| success / warning / danger / info (text) | `--live` `--warn` `--fault` `--info` | `#2F6E3A` `#7F5C14` `#9A3327` `#3E6194` | `#72C182` `#D6A64A` `#E58273` `#7FA3D4` | `#0A5F1E` `#6B4A00` `#9B1500` `#003E9C` |
| the same, as fills and dots | `--live-solid` `--warn-solid` `--fault-solid` `--info-solid` | `#3D8B4A` `#B8862C` `#A93A2E` `#4A6FA5` | `#4E9E5C` `#C4922F` `#BC4A3B` `#6489BC` | = text |
| focus ring | `--focus` + `--focus-ring-width` | `#1A1917` 2px | `#EDE9E0` 2px | `#000000` 3px |
| request in flight / stream / batch | `--flow` `--stream` `--batch` | `#4A6FA5` `#377A86` `#7A62A9` | `#7FA3D4` `#79C0CE` `#B49BDB` | `#003E9C` `#005A66` `#4B1E8F` |
| chat-turn identity | `--tag-1..4` (use order: blue, magenta, teal, violet) | see `tokens.flat.json` | | |
| publisher identity | `--avatar-1..10` | theme-invariant `hsl()` | | |

### Contrast promises

`tokens/pairs.json` is the list; `npm run tokens:check` measures it (alpha
composited over the surface) and prints 126 ratios. Text pairs are 4.5:1 (7:1
in high contrast); UI marks 3:1. Deliberately not promised, with the measured
numbers on record: `--rule` (1.47:1) and `--rule-strong` (1.86:1) are
decorative hairlines; `--flow`/`--stream`/`--batch` are filled marks at
4.0–4.2:1 light and are promised at 3:1, never used as body text;
`--warn-solid` is 2.67:1 on the light page and is a dot, not a surface for
text.

**Remaining debt:** `tabs/spend/OverviewSub.tsx` paints `cloud 42%` in `--on-signal` on
`--warn-solid` (2.67:1 light). The migration was pixel-exact so it was not
recoloured; the fix is `color: var(--ink)` on that span (5.6:1) or a darker
light `warn-solid`, and either is a visible change to decide, not a waiver to
add.

Near-duplicate primitives (ΔE < 3: `stone.900/925`, `stone.650/700`,
`stone.150/200`) are kept distinct for the same reason and listed in
the audit as merge candidates.

## Typography

Two faces, bundled through `@fontsource` (Plex Sans 400/500, Plex Mono
400/500), as `--font-sans` and `--font-mono`. Two scales, the mono one a notch
tighter:

| Role | sans | mono | Where |
|---|---|---|---|
| readout-xl | `--size-readout-xl` 48 | `--size-mono-readout-xl` 46 | the one big number |
| hero | `--size-hero` 42 | — | `.big` on Spend |
| readout | `--size-readout` 28 | `--size-mono-readout` 27 | card readouts |
| h1 / plate / readout-sm / wordmark / title | 26 / 22 / 21 / 18 / 17 | — | setup, four-up readouts, the wordmark, inspector titles |
| body | `--size-body` 15 | `--size-mono-body` 14 | prose; commands |
| body-sm / title-sm | 14 / 13.5 | — | setup facts; model card name |
| label | `--size-label` 13 | `--size-mono-label` 12 | labels |
| unit | `--size-unit` 12 | `--size-mono-unit` 11 | units, log bodies |
| caption / micro / badge | 11.5 / `--size-micro` 11 / 10 | — | owner line; smallest sans; verified badge |

Weights are 400 and 500 only. Tracking: `--tracking-tight` (−0.3px) on the
wordmark, `--tracking-snug` (−0.2px) on the setup h1, otherwise normal.
Numbers set `font-variant-numeric: tabular-nums`.

## Spacing and layout

Spacing is named by its pixel value: `--space-<px>` for every value the UI
uses (`0 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 20 22 24 30 32 34 36 48
66 96`). There is no second vocabulary; the old `--s-1..7`, `--s-half`,
`--s1..3` are gone. Negative offsets are `calc(-1 * var(--space-N))`. Layout
constants: `--gutter` 20, `--sidebar-w` 300, `--control-h` 32. Breakpoints are
`breakpoints` in `tokens.ts` (760 / 900 / 980 / 1100 / 1200) and the only px
allowed in `@media`.

## Elevation, radius

Surfaces are the ramp (`sunk → recessed → panel → raised`), not shadows;
`--shadow-raised` exists for the sheet and the popover and becomes a 2px
outline in high contrast. The z scale is five names: `--z-below` −1
(`mcard-mark::before`), `--z-base` 0 (bar tracks), `--z-sticky` 1 (sticky
section heads), `--z-rail` 5 (the rail, popovers), `--z-overlay` 9 (the
sheet). Radii: `--radius-sm` 2 (chips, bars), `--radius` 3 (controls,
cards), `--radius-lg` 6 (setup plates), `--radius-full` for lamps.

## Motion

`--dur-fast` 120 (opacity flips), `--dur-mid` 160 (size and colour),
`--dur` 180 (default), `--dur-slow` 600 (progress width), `--dur-dots` 1400
and `--dur-sweep` 1600 (the two loops). `--ease` is the house curve
`cubic-bezier(.2,.7,.3,1)`; `--ease-default` and `--ease-inout` exist for
the legacy transitions and the loops. `base.css` clamps everything to
0.001ms under `prefers-reduced-motion: reduce`. Two places where no motion
is not enough: the setup dots and the loading band's sweep both show
progress that exists only as motion, so under reduced motion they render
their static third frame rather than nothing.

## Components

Every shared component (`ui/src/components/`, plus `shell/Sheet.tsx` and the
CSS-only patterns) has a row in `components/registry.json` and a subsection here.

States below are: default, hover, active, focus-visible, disabled, loading,
error, empty. "n/a" carries the reason.

### Button

`<button>` (bare) and `.ghost`. **Anatomy:** label, optional leading
`<Icon decorative>`. **Variants:** bare (bordered on `--edge`), `.ghost`
(no border, `--ink-muted` until hover); a pressed toggle inside `.chips`
fills with `--fill`/`--on-fill`.
**States:** default; hover `--hover` wash; active `--pressed`; focus-visible
`outline: var(--focus-ring-width) solid var(--focus)`; disabled `--ink-muted`
and `cursor: not-allowed`; loading — label changes to the progressive verb
("Killing…"), the control stays the same width; error n/a (errors render
beside the control, not in it); empty n/a. **Keyboard:** Enter/Space.
**ARIA:** `type="button"` always (the static lint enforces it); icon-only
buttons carry `aria-label`; toggles carry `aria-pressed` or `aria-expanded`.
**Tokens:** `--button-bg/fg/border/hover-bg/pressed-bg/disabled-fg/radius/height/pad-x/pad-y/font-size`.

### Input / Select / Textarea

Native controls styled in `base.css`. **States:** default; hover n/a (native);
focus-visible ring; disabled; loading n/a; error `aria-invalid="true"` and a
`.unit` line under the field in `--fault` linked by `aria-describedby`; empty
is the placeholder in `--ink-muted`. **Keyboard:** native. **Tokens:**
`--input-bg/fg/border/placeholder/radius/height/pad-x`. The custom `Select`
takes a per-option `disabled` (+ `title` saying why): the row stays listed,
`aria-disabled`, `.ddopt.off` in `--ink-muted`, and is never committed.

### Card

`.card` (on the page), `.card2` (nested). **Anatomy:** optional `h3`,
body. No shadow in any theme; a 1px `--rule` border on `--panel-recessed`.
**States:** default; hover/active n/a (cards are not controls); focus n/a;
disabled n/a; loading — skeleton rows (see Skeleton); error — a `Verbatim` of
the server's sentence inside the card; empty — one `.unit` sentence saying
what would fill it. **Tokens:** `--card-bg/border/radius/shadow/pad`.

### Chip / Pill

`.pill` (`.mono` for figures) is the read-only chip; a toggling chip is a
`<button aria-pressed>` inside `.chips`, filled when pressed. **States:** default, focus
(when a button), disabled n/a, others n/a. **Tokens:** `--chip-bg/fg/border/radius/pad-x`.
The Models tab's outlined facts (`.mdl-pill`: Dynamic, best fit, a task) use
`--pill-fg/border`; its pressed toolbar toggles and segmented controls use
`--toggle-on-bg/fg`, which are `--fill`/`--on-fill` under a name that says
what they are for.

### Nav

Header `.dest`: links to the six destinations. **States:** default
`--ink-muted`; hover `--ink`; active page `aria-current="page"` with an
underline; focus ring. **Keyboard:** Tab, Enter. **Tokens:** `--nav-fg/fg-active/underline`.

### Table

`CacheTable` and the `table`/`td.num` classes. Numerals right-aligned in
mono; `th scope="col"`; header in `--ink-muted`; rows separated by `--rule`.
**States:** empty — one row spanning the table ("no models cached here");
loading `aria-busy="true"`; error — `Verbatim`; hover n/a; the per-row delete
button follows Button. **Tokens:** `--table-header-fg/row-border/cell-pad-x/cell-pad-y`.

### Sheet

`shell/Sheet.tsx`, the one modal. Portalled; scrim `--panel-overlay`; body
`--panel-raised` with `--shadow-raised`; `role="dialog" aria-modal="true"
aria-labelledby`; Tab trapped in both directions, Escape closes, focus returns
to the opener. Hosts the node, deployment and model inspectors by `kind`.
**States:** open/closed only; the inspector inside owns loading/error/empty.
**Tokens:** `--sheet-scrim/bg/shadow/radius/z`.

### Popover

`components/Popover.tsx`: a field-sized disclosure, not a modal (no trap: a
trap in a five-row list you are trying to leave is wrong). Button with
`aria-expanded aria-controls`; panel `role="group" aria-label`; Escape and
outside-click close; children receive `close`. **Tokens:** `--popover-bg/border/shadow/radius/z`.

### Disclosure

`components/Panel.tsx`: a `label` button with `aria-expanded`, `plus`/`minus`
icon, children below when open. Enter/Space toggle.

### Lamp

`signal: live|warn|fault|idle`, `hollow` (not hearing from the source),
`label` (required, `role="img"`), `size`. Filled means the state is true now;
hollow means the last known state with the stream down. **Tokens:**
`--lamp-live/warn/fault/idle/ring`.

### Readout

`value: number|null|undefined`, `decimals`, `unit`, `width` (ch, required),
`size: xl|readout|body`, `align`, `stale`, `tone: ink|live|warn|fault|muted`,
`title`. `null` renders an em dash in `--ink-muted`; `stale` greys the digits
and keeps the last reading. **Tokens:** `--readout-ink/live/warn/fault/muted/stale`.

### PackChart

`type: stacked-bar|stacked-bar-100|grouped-bar|multi-line|pie|donut`, `title` (the pack's visible title and the
start of its aria-label), `data` (`{label, series, value}[]` for bars, `{x, series, y}[]`
for `multi-line`, `{label, value}[]` for pie/donut), `height` (px),
`options` (the type's own: `orientation`, `showValues`, `showTotals`,
`valueFormat`, `emptyMessage`). Mounts a chart-pack type by id
(`.omp/charts/`) and lazy-loads it with `import()`, so d3 and zod are not in
the main chunk. Colours come from the chart pack's `--oc-*` theme
(`.omp/charts/theme/tokens.json`), not from `tokens.json`; series colour follows
series order. A null value is left out and noted by the pack, never drawn as 0.
Keyboard and ARIA are the pack's: mark navigation, a generated aria-label, a
hidden data table. A failed load renders its message through `Verbatim`.
Adding a type is one `LOADERS` entry and a member of `PackChartType`.
**Tokens:** none.

### Verbatim / VerbatimList

`text`, `size: body|label|unit` (scale only — every size keeps `--ink`);
`items: string[]` (empty → `null`). `white-space: pre-wrap`. No other
behaviour, by design.

### SegmentBar / ProportionBar

`SegmentBar({segments, usable, ceiling, height})`: the memory breakdown
against the line the verdict used; past `usable` is painted `--fault`;
`usable: null` draws no fault region. `ProportionBar({value, width, height,
tone, label})`: `value: null` is a dashed hollow track, distinct from 0.
**Tokens:** `--bar-track/keyline/limiting/fill`. `--bar-fill` is a filled
share outside a SegmentBar: the Models tab's node bars and download progress.

### Models tab tiles and rows

Not shared components: classes in `derate.css` under `/* Models tab */`,
drawn only by `ui/src/tabs/models/`. Quant cards, hub cards and worth-trying
tiles are `--tile-bg/border/border-hover`; list rows and suggestions hover in
`--row-hover` and select in `--row-selected` with an inset `--ink` rule.
Engine marks sit on `--logo-tile`: each engine's own logo from
`design/logos/` (vLLM, SGLang, llama.cpp, Ollama — upstream assets, kept in
their brand colours, so they are not icons and not under the icon spec); an
engine with no mark (derate's TTS server) shows initials in `--logo-ink`.
Both tokens are pinned light/dark in every theme because a brand mark is
drawn for a light ground (pair in `pairs.json`). **States:** default, hover,
selected (`aria-current` /
`aria-pressed`), focus-visible; disabled n/a.

### Copyable

`text`, `label`. Copies via the Clipboard API or the `execCommand` fallback
(plain-HTTP LAN origins have no secure context); the button reports
"Copied"/"Copy failed" into itself and reverts — there is no toast.

### RevealModal

`components/RevealModal.tsx`: `secret`, `keyId`, `onDone`. The only place a
`drt_` API key is ever on screen, and only once: the coordinator keeps a
hash, not the key, so there is no second reveal — a lost key is rotated. It
is shown for the three `secret_once` responses (`POST /api/setup/admin`,
`POST /api/keys`, `POST /api/keys/{id}/rotate`); the caller holds the secret
in state until `onDone` and drops it then.
**Anatomy:** a portalled `.sheet.on` scrim (the Sheet's tokens) over a card:
heading "API key <keyId>", one sentence saying this is the only time it is
shown and why, a `Copyable` of the whole secret (`--font-mono`, wraps, never
truncated or masked), the `Authorization: Bearer` usage line, and the "I
saved it" button.
**Behaviour:** focus moves to the dialog on open; Escape and scrim clicks do
**not** dismiss it (a stray key press would lose a key nobody wrote down);
"I saved it" is the only exit and calls `onDone` once, after which the secret
is not in the DOM. Tab and Shift+Tab stay inside the card (the Sheet's
containment, shared in `components/focusTrap.ts`).
**Copy:** say what the button commits to ("I saved it", not "Close"/"OK");
name the key by id, never by a prefix of the secret.
**A11y:** `role="dialog" aria-modal="true" aria-labelledby` the heading.
**States:** `default`, `long` (43-char secret dense in `-`/`_`, must wrap
inside the card). **Tokens:** `--sheet-*`, plus Copyable's.

### ProviderAddForm

`components/ProviderAddForm.tsx`: `providers`, `kinds`, `knownRefs`,
`onAdded?`. Adding a provider, shared by Settings' `ProvidersCard` and the
Models tab's `ProviderAddDialog` so the two cannot drift.
**Anatomy:** one sentence on where the credential goes (follows the kind and
the key mode), then one wrapping row: the kind `Select` (the server's
`/api/providers/kinds`, never a local list), Base URL (hidden for sign-in
kinds; a localhost address adds the "dialled from the coordinator" note),
the two-mode `KeyField` (hidden for keyless and sign-in kinds), and "Add
provider". A sign-in kind renders `ProviderSignIn` below instead of the button.
**Behaviour:** changing the kind carries its default base URL into the field.
Exactly one of `api_key` / `api_key_ref` is sent. Success clears the fields,
invalidates, and calls `onAdded`. The server's rejection renders verbatim in
`--fault`. **States:** `default`, `busy` ("Adding…"), `error`.
**Tokens:** `--rule`, `--fault`, `--space-*`.

### ProviderAddDialog

`components/ProviderAddDialog.tsx`: `providers`, `kinds`, `onClose`. The
Models toolbar's Provider button. `RevealModal`'s shell (portalled
`.sheet.on`, a `.card.wide`, focus moved in on open, Tab contained via
`focusTrap.ts`) around a heading row ("Add a provider" and a `close` icon
button), one sentence on what a provider is, and `ProviderAddForm`.
**Behaviour:** unlike the reveal nothing is lost by leaving, so Escape and
the close button both call `onClose`; a successful add or sign-in closes it.
**A11y:** `role="dialog" aria-modal="true" aria-labelledby` the heading; the
close button is `aria-label="Close"`. **States:** `default`.
**Tokens:** `--sheet-*`, plus the form's.

### ProvidersCard

`tabs/settings/ProvidersCard.tsx`: provider identity, credentials and accounting
remain on the provider row. The existing `Disclosure` opens a read-only model
status table; it opens by default when a served model is blocked. Each row
names the served model (and upstream ID when aliased), its admission reason,
and its full upstream rate-limit error. The server's polled reason carries the
remaining cooldown; no provider-wide retry label is inferred from model state.
Circuit state is matched to the exact provider/upstream-model target.

**States:** admitting, model-cooldown, provider-failure. Sibling models retain
their own admission states. Expired cooldown errors disappear; provider-wide
auth, budget and transport failures remain on the provider row.
**A11y:** native tables with headers and an accessible model-table name;
Disclosure supports keyboard toggle and `aria-expanded`. Long model IDs and
errors wrap without clipping. Uses existing semantic tokens and table styles.

**Key check:** a key-based row whose kind reports `supports_key_check` carries
one line under the key state -- `key not checked yet`, `key check passed 3m
ago` (`--live`), `key check rejected …` (`--fault`) or `key check inconclusive
…: <detail>` (`--ink-muted`) -- and a **Test key** button first in the actions
cell while a key is set. `key_warning` (refused, expired, or inside the warn
window) renders verbatim in `--warn` behind a decorative 12px
`alert-triangle`; the sidebar alert carries the same sentence. A key read from
the coordinator's environment adds a muted note that a new value there needs a
restart and that Replace key takes effect at once. When the upstream reports
an expiry (`key_expires_at`, OpenRouter), a `warn [n] days before expiry`
number input (0–365, width `--space-66`) patches `key_expiry_warn_days` on
blur. A refused Test key or warn-days value shows on its own row in `--fault`,
pre-wrapped.

**Enable / Disable:** the first action on every row toggles `enabled` with
`PATCH {enabled}` (`aria-pressed` carries the state). A disabled provider's
State cell reads `disabled` in `--ink-muted`; its key, models and settings are
kept, and enabling it re-resolves the key on the server.

### RateLimitsSub

`tabs/requests/RateLimitsSub.tsx`: Usage's Rate limits tab. `?dep=` selects
a served model or resolves a live deployment to its served name; `?provider=`
and `?target=` narrow attribution. A bare `/usage?dep=…` opens this pane,
canonicalized to `/usage/rate-limits?dep=…`.

**Anatomy:** model/provider filters; two errors-only donuts with failure
totals in their centres; current per-model cooldowns; a paged durable log of
429s linking to request detail. No separate numeric counter strip.
Full recorded upstream errors and retry delays render without truncation.
Past errors are never inferred from a current cooldown or a generic 503.
Old rows without error details say not recorded. The log and CSV use the
same archive filters; history lasts for the raw request retention window.
Error composition uses the pack's donut: explicit 429s vs other failures,
excluding successful requests. The failure total uses the archive's `failed`
status filter; 429s retain their exact status filter. A second donut sums the
usage buckets' `failed` counts by served model across all models for the
selected provider and time window, explicitly ignoring model/target selectors.
Models with no failures are excluded, even if they received successful traffic.
It folds every usage bucket rather than counting a page of request logs.
The existing `chartgrid`
places charts side by side on desktop and stacks them below 900px.
All-zero windows use the pack's empty state.
The model donut hides its redundant legend; model names remain in slice
labels, tooltips and the accessible data table. The top row combines model and
provider filters with right-aligned 24h/7d/30d buttons (default 30d). No repeated
panel heading, explanatory paragraph or date-time readout.

**States:** loading, empty, recorded, cooling-down, error. Unknown current
admission stays unknown rather than claiming availability.
**A11y:** labelled native selects and tables, real request links and
keyboard-accessible navigation. Existing tokens, PackChart,
Verbatim, and table styles only. Checks live in `providers-status.check.mjs`.

### ProviderSignIn

`tabs/settings/ProviderSignIn.tsx`: required `flow: 'chatgpt' | 'claude'`,
`providerId?`, `onDone`, `onCancel`. The server's `sign_in` discriminator
selects the flow for both the add form and row reconnect; neither offers a key,
Base URL or Add button. `ProviderSignInView` is the shared pure gallery render.
`ProviderSignIn` keys its controller by flow and provider, so replacing a form
cannot reuse an attempt or callback. API methods are `startProviderSignIn`,
`completeProviderSignIn` and `providerSignInStatus`, using ordinary redacted
`req` responses at `/api/sign-in/{flow}`.

**Variants:** `chatgpt`, `claude`. ChatGPT retains the loopback instructions
(`127.0.0.1:1455`), labelled URL input, automatic callback completion through
the two-second status poll, and plan allowance / Manage usage done state.
Claude uses a labelled masked `Authorization code or callback URL` input for
`code#state` or the hosted callback; it needs no loopback port. Its start and
done states warn that third-party subscription routing is unofficial, is not
permitted by Anthropic, and may restrict the account, linking to the official
policy. They explicitly promise no API-key fallback and disclose possible
Anthropic account-level extra-usage billing and unofficial compatibility
identity. Done directs the operator to enable models in the existing catalogue;
connecting alone never enables models.

**Anatomy:** `role="group"` with a flow-specific name, a `--space-8` grid,
start action, paste instructions and blocked-popup fallback link, labelled
input (`autocomplete="off"`, spellcheck off), Finish sign-in, Start again,
done acknowledgement and ghost Cancel on every step. All external links use
`target="_blank" rel="noopener noreferrer"` and the existing decorative
`external-link` icon. Existing `.unit`, `.fld`, `.label`, button and ghost
styles only; no duplicate layout, icon or token.

**Behaviour:** two-second status polling stops on a terminal answer or expiry;
Start again obtains a new attempt. Cancel, reset, flow/provider replacement and
unmount invalidate in-flight operations. Late start, finish or poll responses
cannot update another form or invoke its completion handler. Callback content
is cleared on success, cancel and restart, and never rendered as text. Success
invalidates shared resources immediately. Reconnect calls `onDone` at once;
a new provider shows done, then Got it calls `onDone`. Tokens remain server-side.
Claude removal reports local deletion without claiming remote revocation.

**A11y / errors:** server refusals render verbatim in `role="alert"` with
`--fault` and `white-space: pre-wrap`; labels are associated, all actions are
keyboard accessible. **States:** `start`, `paste`, `error`, `done` for each
flow. Tests cover the pure views plus the real controller over a scripted
HTTP boundary, including stale completions, callback secrecy and recovery.
**Tokens:** `--space-8`, `--space-4`, `--space-24`, `--fault`.

### OverrideGate

`reason` (verbatim refusal; `null` when the caller already printed that exact
sentence, so it is not said twice), `sentence` (the first-person claim that is the
checkbox label), `checked`, `onChange`, `onLaunch?`, `launching`,
`launchLabel`. The button appears only once the box is ticked; stacked gates
share one button below the stack.

### CacheTable

See Table. `node`, `servedFolders?`, `onDelete?`, `busy?`; without
`servedFolders` the in-use column says "unknown", never "not in use".

### Icon

`name: IconName`, `size: 12|14|16|20|24` (16), `className`, and exactly one
of `label` (meaningful: `role="img" aria-label`) or `decorative`
(`aria-hidden focusable="false"`). Colour is `currentColor`. Full spec in
`icons/README.md`.

### Skeleton

Specified, with no consumer since the Models tab redesign (2026-11-20): the
quantization section now says "Reading the quantizations on the Hub…" while
its ladder loads. When one is built: `--skeleton-bg` with a
`--skeleton-shimmer` sweep (`--dur-sweep`), `aria-busy="true"` on the
container and a visually hidden "loading" sentence; reduced motion shows the
static bar.

### PasswordField

`ui/src/components/PasswordField.tsx`: `id`, `name`, `value`, `onChange(value)`,
`autoComplete` (`current-password` | `new-password`), `autoFocus`, `disabled`,
`inputClassName` (class for the `<input>`; setup and sign-in pass
`setup-field`), `className`, `aria-invalid`, `aria-describedby`. Every
password input in the product uses it: setup's `AdminStep`, `LoginTab`, and
the add-admin and change-password forms in settings' `AdminsCard`.
**Anatomy:** one visible box with the eye inside its right edge. The
`.pwfield` wrapper draws the box -- Input's `--panel` background, `--rule`
border, `--radius` -- and holds a borderless, transparent input (grows,
`min-width: 0`) followed by the ghost icon toggle (`eye` while hidden,
`eye-off` while shown), margin `--space-3`, padding `0 --space-6`. The toggle
sits *beside* the input inside the box, never over it, so typed text never
runs under the eye (and the layout check's no-overlap rule holds). Around a
`.setup-field` the box takes that field's `--panel-raised` / `--edge`
(setup.css). The icon is `--ink-muted`, `--ink` on hover and while shown. No
font override: the input takes whatever `inputClassName` says.
**States:** `hidden` (default, `type="password"`), `shown` (`type="text"`),
`disabled` (input and toggle both disabled), `error` (`aria-invalid` plus
`aria-describedby` naming the message under it); focus-visible on the input
draws the `--focus` ring around the whole box; hover as Button.
**Keyboard:** the toggle is the next Tab stop after the input; Enter/Space
flip it. Only `type` changes, so the value and the caret survive the swap.
**ARIA:** icon-only button, so per Button: `aria-label` "Show password" /
"Hide password" and `aria-pressed` reflecting the state; the icon is
`decorative`. **Tokens:** `--panel`, `--rule`, `--radius`, `--focus`,
`--space-3`, `--space-6`, `--ink`, `--ink-muted`, `--panel-sunk` (ghost
hover); the input keeps its own padding (`--space-6 --space-12`, or the
context's).

### BlockFrame

`title` (also the accessible name), `sub`, `icon`, `signal: {value, label}`
(a `Lamp` beside the title), `meta` (right-aligned header content: a count, a
chip, a checkbox), `selected`, `tone: warn|fault|null`, `onRemove` +
`removeLabel` (an `x-circle` ghost button, for blocks the operator added),
`footer`, `children`. The chrome every wiring-editor block on `/cluster`
shares; it carries no React Flow, so the sockets are the caller's. **States:**
default; selected (`--graph-block-selected` border and ring); with-lamp; tone
warn/fault (`--graph-warn` / `--graph-fault` border — amber is worth a look,
red is broken, nothing else colours a block). **Keyboard:** the remove button
only. **ARIA:** `role="group" aria-label={title}`. **Tokens:**
`--graph-block-bg`, `--graph-block-border`, `--graph-block-selected`,
`--graph-warn`, `--graph-fault`, `--rule`, `--radius`, `--space-6/8/12`.

### IssueList

`label` (the list's accessible name), `issues: {id, level: error|warn,
message, actions?: {label, onClick}[]}[]`, `empty` (said when there is
nothing). Errors sort first; each row carries an icon (`x-circle` /
`alert-triangle`) and its colour (`--fault` / `--warn`), so neither channel
is the only one. Messages render verbatim with `pre-wrap` — a refusal names
what to change. Used by `/cluster`'s Checks card (actions `Focus`, `Take
server`). **States:** errors; warnings; empty. **Keyboard:** each action is a
ghost button. **ARIA:** `<ul aria-label>`; the icon is labelled Error or
Warning. **Tokens:** `--fault`, `--warn`, `--space-6/8`.

### HostTable

OpenRouter's backend hosts for one model, as a pickable table
(`ui/src/components/HostTable.tsx`, export `HostTable`). Used by the Models
tab's `BackendPicker`; the cluster editor's provider rows reuse it. Pure: the
caller fetches `GET /api/providers/{id}/backends` and writes the pin.
**Anatomy:** a `<table>` — radio column first, host (`provider_name` then the
tag in `.unit mono`), input and output `$/Mtok` (`$x.xx`, `—` when unpriced),
context, quantization (`—` when null). The first row is `Automatic
(OpenRouter's default)`, checked when nothing is pinned, and picks `null`. The
row with the lowest non-null output price carries a `cheapest` `.pill`. Under
the table, the checkbox `Fall back to other hosts when this one is down`.
**States:** default (Automatic checked, fallback disabled); pinned (that row
checked, fallback enabled); loading (a quiet `.unit` line above the table);
error (the server's sentence via `Verbatim`, never rewritten); empty
(`OpenRouter lists no hosts for this model yet.`, the Automatic row still
shown); disabled (every input disabled while a write is in flight).
**Keyboard:** native radios share one `name` per instance (`useId`), so arrows
move the pick; Tab reaches the checkbox. **ARIA:** each radio
`aria-label="Use <provider_name>"`; the radio column's header is `.sr-only`.
**Tokens:** `--space-6`; table chrome is the global `table`/`td.num` rules.

### BalancerDialog

One served name's load balancer on `/cluster`
(`ui/src/tabs/cluster/BalancerDialog.tsx`, a screen, not a shared
component), opened by double-clicking its split or merge. A Sheet-pattern
modal: `.sheet on` scrim, `.card wide`, focus moved in and trapped
(`containTab`), Escape closes, `role="dialog" aria-modal` labelled by its
title `<served> load balancer`. **Anatomy:** sub line `<n> local · <m> cloud`;
**Policy** — a `<select aria-label="Routing policy">` whose first option is
`auto — what the router picks (<resolved>)` and then the seven policies, the
router's `auto_reason` through `Verbatim` while auto, and the triangle's rule
when the name runs on `failover`; **Limits** — rpm/tpm inputs bound to the rate
limit block plugged into the name (the first value creates and plugs one,
clearing both unplugs it), with `A limit pins the policy: it is saved as
failover. Pick a policy to keep both.` in `--warn` while auto; **Targets** —
a read-only table of the name's routing targets (target, kind, admitting
`Lamp`, weight, outstanding, circuit, admission blocks); **Remove this load
balancer** (not on a draft name), behind a `window.confirm` that names
everything it stops and switches off; **Done**. Every control edits the
draft: nothing saves here, and the edits show in Pending changes.
**Tokens:** `--space-8`, `--space-16`, `--space-20`, `--warn`, `--size-unit`.

### Toast

Specified, not built. Inline status is the default everywhere (Copyable
reports into its own button; a launch reports in its row). A toast is only
for an outcome whose subject has left the screen. If one is needed: `--toast-bg/fg/border/radius/shadow/z`,
`role="status"` for success, `role="alert"` for failure, no auto-dismiss under
4s, Escape dismisses, never more than one at a time.
scaffolds it with the checks pre-wired.

### Banner

`ui/src/components/Banner.tsx`. A full-width strip directly under the header
for a state the whole app is in -- today a node unreachable or a served name
losing targets (`shell/HealthBanner.tsx`). **Anatomy:** a left rule
(`--space-3` wide) in the tone colour, the text in the tone colour on
`--panel`, padding `--space-8 --space-16`, and an optional single action at
the end rendered as a real link (`<a href>`), so it can be opened in a second
tab. **Variants:** `tone` -- `fault` (something is down; `role="alert"`,
announced at once) and `warn` (degraded, e.g. only half-open circuits;
`role="status"`, announced politely). **States:** default; with-action.
**No dismiss control**, deliberately: unlike a Toast, which reports an outcome
and may go away, a Banner reflects persistent state, so it leaves exactly when
the state clears -- a dismissed banner would be a hidden outage. Never more
than one at a time; the caller joins its sentences with ` · `. **Keyboard:**
the action link only. **Tokens:** `--fault`, `--warn`, `--panel`,
`--space-3`, `--space-8`, `--space-16`.

## Patterns

- **Forms and validation.** Validate on submit and on blur, never on
  keystroke. The error is one sentence under the field in `--fault`,
  `id`-linked by `aria-describedby`, with `aria-invalid="true"` on the control.
  The submit button stays enabled; the error explains.
- **Empty.** One sentence in `.unit` saying what would appear and, when it is
  an action, how ("No nodes yet — Settings → Add a node"). Never an
  illustration, never a zero.
- **Loading.** `aria-busy="true"` on the region and `aria-live="polite"` on
  the sentence that replaces it; skeleton rows for lists, the last reading
  greyed (`stale`) for figures. Never a spinner over a figure.
- **Errors.** The server's sentence, through `Verbatim`, beside the thing it
  is about. A refusal names what to change; rewriting it destroys that.
- **Toast vs inline.** Inline by default; see Toast.
- **Modal vs drawer.** The Sheet is the modal and there is no drawer; the
  sidebar is a rail that collapses, not an overlay.
- **Tables.** Numerals right-aligned in mono; `th scope`; one unit per
  column, in the header, not in every cell.

## Copy and voice

Sentence case everywhere, including buttons and headings. Numbers carry
their unit in mono (`119.7 GB`, `273 GB/s`). Refusals name what to change.
No exclamation marks. The vocabulary is *node*, *deployment*, *model*,
*provider*; a machine is a node, a running model is a deployment. "Spark" is
NVIDIA's hardware name and never appears bare; `sparkrun` is their binary.
An absent value is an em dash, never 0 and never "N/A".

## Adding things

### A component

1. Add `ui/src/components/<Name>.tsx` and a row in `components/registry.json`.
2. Fill the spec below: anatomy, variants, every state or its n/a reason, keyboard,
   ARIA, tokens.
3. Only `var(--token)` and `<Icon>`; no literal px, colour, duration or SVG.
4. `npm run check`. See `.omp/RULES.md`.

### An icon

1. `icon_search` (or grep `icons.json` tags/replaces) — if it exists, use it.
2. Draw `svg/<name>.svg` to the spec; add the `icons.json` entry with
   `replaces`; `npm run icons:build && npm run icons:lint`.
3. See `.omp/RULES.md`.

### A colour

1. `token_lookup` by value — if a step within ΔE 3 exists, use it.
2. Decide the layer: a ramp step is a primitive, a role is a semantic, a part
   of one component is a component token. Add it with `$description`.
3. Add its pairs to `pairs.json`; `npm run tokens:build && npm run tokens:check`.
4. See `.omp/RULES.md`.
