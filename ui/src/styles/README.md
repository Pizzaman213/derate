# styles

The palette and the chrome, in three sheets imported exactly once. `main.tsx`
loads them in order — `tokens.css`, `base.css`, `derate.css` — and that order is
the cascade: the palette first, the element baseline on top of it, the
structural chrome on top of that. No component in this app carries a literal
colour, so dark mode is a token swap rather than a second set of components.

## Layout

| File | Lines | What it owns |
|---|---|---|
| `tokens.css` | 227 | the entire palette, the type scale, the spacing grid, and both dark-theme blocks |
| `base.css` | 143 | the reset, `html`/`body`/`#root` sizing, the typographic classes, and the bare control rules |
| `derate.css` | 1762 | the structural chrome: every screen's layout, ported from the design source |

## `tokens.css`

Every colour, size, space and duration the product uses. Its header comment
names the three decisions in it that are load-bearing, each one written after
the bug it fixes:

- **`--fill` is split from `--ink`.** They were the same value, which made every
  selection indicator in the cluster graph invisible — an outline drawn
  `stroke: var(--ink)` on a `fill: var(--fill)` rect is 1.00:1 in light and
  1.13:1 in dark. Selection is drawn outside the plate with
  `--select-on-panel`, or inside it with `--select-on-fill`.
- **The surface ramp runs both directions.** `--panel-sunk`,
  `--panel-recessed`, `--panel`, `--panel-raised`. There was no token for
  "above the page", so a dialog painted `--panel` over a dimmed page measured
  1.07:1 against its own scrim in dark mode.
- **Status colours are per theme.** They render as text, and `#A93A2E` on
  `#191817` is 2.81:1 — the failure state was the least readable thing in dark
  mode. `--live` / `--warn` / `--fault` are tuned for text on `--panel`; the
  heavier originals live on as `--live-solid` / `--warn-solid` /
  `--fault-solid` for fills and dots.

Individual values carry their own measurements. `--ink-muted` was `#6E6A62`,
4.44:1, which failed AA on 12px text. `--warn` at `#8A6318` measured 4.47 and
missed AA too. `--edge` (3.54:1 light, 3.85:1 dark) exists because
`--panel-sunk` is only 1.47:1 against the page and a meter track that cannot be
seen is a proportion with no denominator.

**`--tag-1` through `--tag-4` are in use order, not spectrum order.** The first
draft ran blue-teal-violet-magenta, which put the two closest hues on the two
commonest slots: a two-model transcript came out blue against teal, 29 degrees
apart, correctly coloured and unreadable at a glance. The ramp is blue,
magenta, teal, violet — the two-model case is the 100-degree pair and the worst
pair anywhere is 43 — and nothing is within 42 degrees of a status hue, because
a model's identity must never read as a verdict on it. Reordering those four
lines re-pairs them. `tabs/chat/tags.check.mjs` asserts the hue separations and
that every `--tag-N` is defined in all three theme blocks.

The type scale carries one non-obvious rule: IBM Plex Mono renders optically
larger than Plex Sans at the same px, so each `--size-mono-*` is one notch
tighter than its sans partner. Spacing carries two grids on purpose — the
12px-base `--s1`/`--s2`/`--s3` are kept because the six shared components read
them, and the 4px `--s-1` .. `--s-7` grid is what everything new uses.

## `base.css`

The reset and the typographic baseline. `box-sizing: border-box` on everything,
`height: 100%` on `html`, `body` and `#root`, and a `body` that paints
`var(--panel)` and `var(--ink)` and sets the sans stack, `--size-body` and
`line-height: 1.5`.

`.mono`, `.readout` and `.readout-xl` switch to the mono stack with
`font-variant-numeric: tabular-nums` and `'tnum' 1, 'zero' 1`. Every numeral in
this interface updates in place, and tabular figures are the reason the panel
does not shudder once a second. `.label`, `.unit`, `.muted`, `hr`/`.rule`,
`h1`/`h2`/`h3`, `::selection` and the bare `button`, `select` and `input` rules
are defined here and nowhere else — `derate.css` carries no bare element rule
for any of them. The three interaction states are on `button` alone:
`button:hover:not(:disabled)`, `button:active:not(:disabled)` and
`button:disabled`. `select` and `input` get one declaration each and take their
focus ring from the `:focus-visible` rule below.

`:focus-visible` gets a 2px `var(--focus)` outline at 2px offset, on every
control, always: this panel is usable by keyboard. `.sr-only` is the clipped
visually-hidden box, used in six places. The
`@media (prefers-reduced-motion: reduce)` block clamps every animation and
transition in the **whole document** to 0.001ms and forces `scroll-behavior:
auto`, which is why motion elsewhere needs no opt-out of its own.

## `derate.css`

The structural chrome for every screen, ported from
`mockups-next/styles/derate.css` — the design source, which is not in this
checkout, so this file is the only copy of that layout you can read here. Its
header comment is a manifest of what did *not* come across, and each omission is
a duplicate that would have gone stale:

- the mockup's own theme-palette blocks — `tokens.css` owns the whole palette,
  including `prefers-color-scheme`, and a second copy is the thing that rots
  the next time only one is edited;
- the universal `*{box-sizing;margin;padding}` reset and the bare `body{}` rule;
- `.mono`, `.label`, `.unit`, `.readout`, the muted-text shorthand, and the bare
  `button`/`select`/`input` rules with their states. This file's job is the
  chrome around those, not a second definition of them.

Four token families were renamed mechanically at integration —
`--panel-recessed`, `--panel-sunk`, `--ink-muted` and the `--on-fill` pair — and
the contrast fixes were folded in at the same time: `.sheet`'s scrim became
`var(--panel-overlay)`, `.card` moved onto `var(--panel-raised)` with
`var(--shadow-raised)`, and the four meter classes (`.bar`, `.budget`,
`.split`, `.phase`) gained an `inset 1px var(--edge)` keyline.

Ninety-seven top-level class selectors, in about seventy families, in screen
order: `header`/`.dest`,
the sidebar and its `.rail` pull tab, the cluster floor (`.clusterstage`,
`.machine`, `.band`, `.edge`), the node page, the deployment sheet, the charts,
chat, the model cards and master/detail split, the toolbar controls, the
machine board, and the node terminal. Eleven `@media` blocks — nine of them
width breakpoints — and four `@keyframes`.

`--rail` is defined on `.wrap`, not in `tokens.css`, and that is correct: it is
`var(--sidebar-w)` normally, `0px` under `.wrap.narrow`, and `260px` below
1200px. A value that changes with component state is not a palette entry.

## The seam with the app

`main.tsx` is the only importer, and it imports the four `@fontsource` files
first, then these three:

```tsx
import './styles/tokens.css'
import './styles/base.css'
import './styles/derate.css'
```

- **`theme.ts`** owns the `data-theme` attribute — `applyTheme` sets `light` or
  `dark` on `document.documentElement` and *removes* it for `system`, which is
  what hands the decision back to the `prefers-color-scheme` block.
- **`tabs/dashboard/chartTheme.ts`** reads tokens at runtime with
  `getComputedStyle(document.documentElement)` and re-reads them on a
  `MutationObserver` watching `data-theme` plus the media-query change, because
  either can flip the palette. It invents no colour of its own.
- **`tabs/chat/tags.ts`** writes `var(--tag-N)` as a string; `tags.check.mjs`
  is the only thing that compares those strings against what `tokens.css`
  actually defines.
- **`tabs/setup/setup.css`** is a separate sheet imported by `SetupTab.tsx`. It
  is not in this folder and is not loaded by `main.tsx`.
- **`docs/screenshots/brand/build.py`** holds literal copies of `--panel`, `--ink`,
  `--ink-muted` and `--flow` for both themes in its `THEMES` dict, because a
  README image cannot import from a stylesheet. It also derives the lockup's
  proportions from `derate.css`'s `header` rule (`gap: 14px;
  align-items: center`) and `Header.tsx`'s 32x24 svg, 18px span and 2px nudge.
  Change one of those four colours and the dict has to change with it, then
  `python3 docs/screenshots/brand/build.py`.

## Things that look like details and are not

**`prefers-color-scheme` is owned by `tokens.css` and by nothing else.** The
dark palette is written twice on purpose —
`@media (prefers-color-scheme: dark) :root:not([data-theme='light'])` for the
OS default, and `:root[data-theme='dark']` for an explicit choice — so the
toggle wins in both directions. A token defined in one block and forgotten in
the other is a screen that loses its colours the moment the OS flips.

**A `var()` naming a token that does not exist renders as no colour at all, and
does it silently.** Nothing in the type system, and nothing in the build, says
a word. `tags.check.mjs` is the only verifier that closes that hole, and it
closes it for the four `--tag-*` names only.

**Status colour has two forms and they are not interchangeable.** Running text
takes the plain `--live`/`--warn`/`--fault`, which are tuned for 4.5:1 on
`--panel`; a filled `.dot` or a filled meter takes the `-solid` twin with
`--on-signal` text, which is the 3:1 bar for non-text marks. `derate.css` sets
none of them on a `.dot` directly — those are inline, per instance, from the
component that fills it.

**`flex: 1` with `min-height: 0` at every level *between* `.clusterstage` and
the graph's own element.** `.clusterstage` is the one that is not in the chain:
it is a column flex container on `min-height: calc(100vh - 95px)`, and it is
`.clusterstage .floorcard` and `.clusterstage .floor` beneath it that each carry
`flex: 1; min-height: 0`. A flex-basis of 0 means the height comes from the
container's free space and never from the content, which is what keeps
`ClusterGraph`'s `ResizeObserver` (`ClusterGraph.tsx`) from feeding back into
its own size. Wrapping the floor in a block added a level to that chain, and a
level that sizes to its content would reintroduce the loop. `.chat` documents
the same rule as `grid-template-rows: minmax(0, 1fr)`.

**`transform-box: fill-box` on `.bandsweep-move`.** Without it an SVG element
measures percentage transforms against the whole viewport and the sweeping
highlight leaves the floor entirely.

**uPlot's cursor colours are handed back to the tokens.** `uPlot.min.css` is
imported by `Chart.tsx` and is entirely scoped to its own `.u-*` classes, so it
collides with nothing — but it ships literal colours, and this product has no
literal colours. Four rules override them, and the crosshair is a solid
hairline rather than uPlot's dashed default, because dashing means "projection"
or "threshold" everywhere else on that screen.

**`textarea` is styled in `derate.css`, and that is not a violation of the rule
above.** There was no textarea anywhere in the product until the chat composer,
so there is nothing in `base.css` to duplicate; the rule copies `base.css`'s
`input` declaration rather than inventing a surface.

## Failure behaviour

- **An undefined token.** The property is dropped and the element renders with
  no colour — no console warning, no build error. Only a verifier catches this.
- **A token defined in the light block only.** The screen renders correctly
  until the OS or the toggle flips to dark, then loses exactly that mark. Both
  dark blocks are therefore full copies, not deltas.
- **`prefers-reduced-motion`.** `base.css` clamps every animation and
  transition document-wide, so the cluster floor's plate motion simply does not
  play. `derate.css` adds the cases where "no motion" is not enough on its own:
  `.bandsweep-move` is swapped for the static `.bandsweep-still`, and the three
  waiting dots are pinned visible rather than animated — still visibly in
  progress, without the movement.
- **IBM Plex missing.** Both stacks fall through to a face the platform
  already has: `--font-sans` is `'IBM Plex Sans', ui-sans-serif, system-ui,
  sans-serif` and `--font-mono` is `'IBM Plex Mono', ui-monospace, 'SF Mono',
  monospace` — the mono stack names no `system-ui`, because a UI face is the
  wrong fallback for a column of figures. Metrics shift; nothing breaks. The
  tabular-figure feature settings are requested from whatever face resolves.
- **A narrow window.** Nine width breakpoints, the widest at 1200px and the
  narrowest at 760px. Below 980px the sidebar moves under the content and
  `.rail` is hidden outright, because a pull tab for a panel that is no longer
  beside you is a control that does nothing.

## Deliberately not built

**A second palette in `derate.css`.** The port dropped the mockup's theme
blocks rather than carrying them, on the grounds that two palettes are one
palette and one stale copy.

**A CSS-only theme toggle.** `theme.ts` owns the attribute and persists the
choice to `localStorage` under `derate.theme`; the stylesheet only reacts to
it. `system` is the absence of the attribute, not a third value in CSS.

**Component-scoped stylesheets.** There are no CSS modules and no
styled-components here. Class names are global, flat and named for what they
are on screen, which is what lets a comment in `Header.tsx` point at
`.stream-fault` in `derate.css` and be checkable.
