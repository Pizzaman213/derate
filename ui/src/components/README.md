# components

Nine files, 800 lines, imported by 34 modules across `tabs/`, `sidebar/` and
`inspectors/` — `shell/` is the one screen folder that takes nothing from here.
Each one exists to make a design rule structural rather than remembered: a
server sentence that cannot be paraphrased because the component that renders it
has no code path that would, a missing reading that cannot be drawn as a zero
because null takes a different picture, a refusal that cannot be waived without
ticking a box whose text names the measurement being waived.

The dependency runs one way. Nothing here imports from a screen; the only
non-React imports in the whole folder are `../format`, `../api/types` and, in
`CacheTable.tsx`, two of its neighbours.

## Layout

| File | Lines | What it owns |
|---|---|---|
| `Verbatim.tsx` | 70 | planner and fit-gate strings, rendered exactly as received |
| `Readout.tsx` | 56 | the fixed-width mono numeric readout, and what it does when the stream stops |
| `Lamp.tsx` | 38 | the one indicator lamp: live / warn / fault / idle, filled or hollow |
| `Bars.tsx` | 177 | `SegmentBar` (the memory breakdown against the line the verdict used) and `ProportionBar` |
| `OverrideGate.tsx` | 73 | a refusal the operator may overrule, and the sentence they read first |
| `CacheTable.tsx` | 138 | one node's downloaded weights, largest first, deletable only where deletion is allowed |
| `Copyable.tsx` | 86 | a command block with a copy button that works on a plain-HTTP LAN address |
| `Panel.tsx` | 72 | `Section` and `Disclosure` — rules, not gaps and shadows |
| `Popover.tsx` | 90 | a field-sized disclosure that hands `close` to its children |

## `Verbatim.tsx`

`Verbatim({ text, size })` and `VerbatimList({ items })`. The most-imported file
here — 22 modules take one or both. `Verbatim` is a `<p>` with
`whiteSpace: 'pre-wrap'`, `fontWeight: 400` and `color: var(--ink)`, and no
other behaviour: there is no truncation, no line clamp, no summary mode and no
sentence-casing, so no call site can apply one by accident. The strings it
carries are `control_plane/fit/`'s refusals and `control_plane/planner/`'s
reasons, which name the term that blew the budget and the specific change that
would work. Shortening one does not tidy the screen; it deletes the sentence
that says what to change.

`size` is `'body' | 'label' | 'unit'` and is **scale only**. Every size keeps
`--ink`, including `unit`, even though `.unit` in `styles/base.css` is
`var(--ink-muted)`: a `.unit` is muted because it is chrome, and a sentence the
server wrote is content no matter how small it is set.

`VerbatimList` is the planner's rejected list, one line per `<li>`, muted, each
behind an `aria-hidden` `×` in a `10px 1fr` grid, still `pre-wrap`. An empty
array returns `null` rather than an empty `<ul>`.

## `Readout.tsx`

`Readout({ value, decimals, unit, width, size, align, stale, tone, title })`,
imported by 8 modules. `width` is in `ch` and lands as `minWidth: '<n>ch'`
beside `fontVariantNumeric: 'tabular-nums'` — mono plus tabular figures makes
`1ch` exact, so reserving the widest expected value means a number crossing from
two digits to three moves the digits and never the unit. That is the whole
reason for the mono face. `ui/README.md` records the check: over twelve samples
the dashboard hero cycled through nine values with its left edge, box width and
the caption below it all fixed to the pixel.

The value goes through `fmt()` from `../format`, which returns an em dash for
`null` and for anything non-finite. A missing reading is shown as missing.

`stale` is the other half. When the stream is down the digits go to
`--ink-muted` and **the last reading is kept** — not frozen looking live, and
not zeroed. `TONE` maps five names onto `--ink`, `--live`, `--warn`, `--fault`
and `--ink-muted`; `stale` overrides whichever was chosen. The unit rides in a
separate `.unit` span with a 4px margin, outside the reserved width.

## `Lamp.tsx`

`Lamp({ signal, hollow, label, size })`, 38 lines and 16 importers — the second
most-used file in the folder. Four signals map to four tokens: `live`, `warn`
and `fault` to their own, `idle` to `--ink-muted`. The docstring is the rule:
*if it is coloured, something is actually true.*

`hollow` is the state the product needs and most status dots do not have. It
draws the same hue as a `1.5px` inset ring over a transparent centre, and it
means **we are not currently hearing from the source** — the lamp stops claiming
the state is fresh without pretending the state changed. A node that has gone
quiet is not a node that turned green, and it is not a node that turned red
either.

The dot is `role="img"` with both `aria-label` and `title` set from the required
`label` prop, so the colour is never the only carrier of the claim.

## `Bars.tsx`

Two bars and one shared thesis: colour reports state, never category.

`SegmentBar({ segments, usable, ceiling, height })` draws the fit gate's memory
breakdown. `usable` is the line the verdict was measured against and the **only**
one the overrun test uses (`overruns = usable != null && total > usable`);
segments past it paint `--fault`. A `null` budget is not a budget of zero — no
line is drawn and no fault region appears, rather than implying everything fits.
`span` is `max(total, usable, ceiling) * 1.02`, so the total and both lines are
always on screen and an overrun puts the line partway across where it can be
seen. `Segment.limiting` — the term the gate named — is **outlined** in `--ink`,
not recoloured, because a recolour would make one memory term look like a
verdict.

`ceiling` is context only: the static ceiling drawn behind the blocks when
`usable` is the live figure. It is a dashed `--ink-muted` hairline at
`zIndex: 0`, which the segments paint over so it reads as "the old ceiling, now
covered", while the live line is 2px solid `--ink` at `zIndex: 1`. Weight and
dash separate the two, never hue. `showCeiling` demands all three of
`ceiling != null`, `usable != null` and `ceiling > usable` before `COINCIDENT`
(0.01) is consulted, so a ceiling handed over without a live line, or at or
below one, is never drawn at all; `COINCIDENT` then suppresses one within a
hundredth of the span of the live line, where two rules would render as a
smudge. `shade(i, n)` walks one colour from 0.85 down by `0.55/(n-1)`.

`ProportionBar({ value, width, height, tone, label })` is the null/zero rule in
four lines of style. `value: null` draws a dashed `--rule` outline over a
transparent track and no fill; a real value draws a solid border over
`--panel-recessed` with a fill inset from the right. A missing value must never
be pixel-identical to zero — a solid 0% bar states that a transfer nothing has
measured is 0% done. `ui/README.md` traces this to the sidebar's Activity rows,
where a download with no reported total yet gets `value: null` and only the
checkpoint loader, which counts its own shards, ever supplies a real fraction.
The fill clamps with `Math.max(0, Math.min(1, value))`, and the bar is
`role="img"` with a required `label`.

## `OverrideGate.tsx`

`OverrideGate({ reason, sentence, checked, onChange, onLaunch, launching,
launchLabel = 'Serve anyway' })`. Two surfaces reach a refusal from opposite
directions. `tabs/models/ServePanel.tsx` learns about it before anything is sent:
it builds a `gates` list from `serve.overrides`, falling back to one gate
synthesised from `serve.override_required` for a gateway that predates the list,
and hands the list to `tabs/models/Verdict.tsx` — which is the module that
imports this component. ServePanel itself never does.
`tabs/models/QuantLadder.tsx` learns about it from a 409 after trying, and there
are two of those: `live_memory_insufficient` reopens on `allow_over_live_memory`,
`pull_over_memory` on `allow_over_memory`. Matching only the first left a real
pull refusal rendering as a bare red line with the way past it unreachable. So
this component takes no verdict and no plan — only the two strings and the
callbacks — and each caller builds the sentence from what it actually knows.

**The checkbox label is the claim, not "I understand".** A box whose text does
not name the measurement it is waiving can be ticked without reading it, which
is the failure the gate exists to prevent. `reason` renders in `--fault` with
`pre-wrap`, verbatim: it names the numbers the gate used and a rewrite would
drop them.

`onLaunch` is optional because one launch can need several permissions.
`Verdict.tsx` maps over its `gates` array rendering one claim and one checkbox
each and passes no `onLaunch`, so no gate draws a button. The button is not a
footer under the stack — it is the sibling branch. `canServe` is
`gates.every((g) => granted[g.param] === true)`, and the moment the last box is
ticked the stack is replaced by one button labelled `Serve anyway`: one launch,
one button, however many permissions it took. While boxes are outstanding and
there is more than one, a `<n> of <m> agreed` line sits under them, because
otherwise ticking the first box appears to do nothing. Where the gate does own
the button — `QuantLadder.tsx` is the caller that passes `onLaunch` — it renders
only once `checked`, so the override is always a second, deliberate act.

## `CacheTable.tsx`

`CacheTable({ node, servedFolders, onDelete, busy })` — one node's downloaded
weights, largest first, over `NodeStorage` and `CachedModel` from
`api/types.ts`. Three early returns come before the table: no `node.models` at
all renders nothing; `available: false` prints the node id, "not measured" and
`cache.reason` through `Verbatim`; an empty repo list prints "nothing downloaded"
and the cache path.

**Each bar is a proportion of the largest repository, not of the total.** This
is a ranking of what is worth deleting, and against a 894 GB total every bar but
the first would be invisible.

The read/write split is carried by optional props rather than a flag.
`tabs/models/InstalledModelsCard.tsx` passes `node` alone and the actions column
is not rendered at all — the read-only case has no delete button to disable.
`tabs/storage/ModelCacheCard.tsx` passes all four. `inUse` is
`servedFolders?.has(m.folder) ?? false`, and a repo that is serving shows the
word `serving` in `--live` instead of a greyed button: marked, not merely
disabled, because the server refuses that delete with a 409 and the reason is
the useful half. `busy` is keyed `<node_id>/<folder>`, so only the row being
deleted says "Deleting…".

## `Copyable.tsx`

`Copyable({ text, label = 'Copy' })`, one importer:
`tabs/settings/AddNodeCard.tsx`, which uses it twice — the first-node install
command and the minted join command. New ground when it was written: nothing in
this UI copied to the clipboard before, because nothing in it was meant to be run
somewhere else. It is not the only clipboard path any more, and the other one is
the counter-example rather than a second implementation — `tabs/SetupTab.tsx`
calls bare `navigator.clipboard?.writeText` twice, for the enrollment command and
for the endpoint, with nothing under it when the API is undefined.

**The `execCommand` fallback is the path that runs in production, not the legacy
one.** The gateway binds the LAN and is served over plain HTTP, so on
`http://spark-01:8080` — the address an operator actually opens —
`navigator.clipboard` is undefined outside a secure context. `localhost` is the
one origin where the Clipboard API works, which is exactly the origin a
developer tests on and nobody deploys to. A Clipboard API that is present but
*refuses* (a permissions policy, an unfocused document) falls through rather
than reporting a failure the fallback may not have.

`legacyCopy` puts its textarea off-screen at `top: -1000px` with `opacity: 0`,
never `display: none` or `visibility: hidden`: a hidden element cannot be
selected and the copy silently does nothing. The node is removed in a `finally`.
There is no toast system in this UI, so the button reports into itself and
reverts after 2000ms, with the timer cleared on unmount. When both paths fail it
says so in `--warn` and points at the block, which is `.cmd` — `white-space:
pre`, `overflow-x: auto`, selectable by hand.

## `Panel.tsx`

`Section({ title, aside, children })` and `Disclosure({ summary, open, onToggle,
children })`. `Section` is a printed legend, a hairline, then content: an
`h2.label` baseline-aligned against an optional `aside`, an `<hr>`, and the
body. Panels here are separated by **rules rather than gaps and shadows**.

`Disclosure` is a disclosure whose trigger is a plain line of text — `border: 0`,
full width, `--ink-muted`, with a `+`/`–` glyph marked `aria-hidden` — and still
a real `<button>` carrying `aria-expanded`, so it is focusable and announced
without inventing a keyboard contract. `open` and `onToggle` are the caller's
state, never internal, so a screen decides what starts expanded and a URL could
drive it. Three modules import it: `sidebar/PlanSection.tsx`, which uses it for
the plan's reason — the most important expansion in the product —
`tabs/models/QuantLadder.tsx` and `tabs/models/ServePanel.tsx`.

`Section` has no importer today. The five sidebar sections each write their own
`<section>` and `<h2>`.

## `Popover.tsx`

`Popover({ label, trigger, children })`, where `children` is a function taking
`close`. Its styles are live in `styles/derate.css` (`.popwrap`, `.pop`,
`.pop .poprow`, `.pop .popfoot`, from line 1266), and no module in `ui/src`
imports the component as of this reading.

Three things it deliberately is not, each recorded with its reason:

- **Not `shell/Sheet.tsx`.** That is a portalled modal with a scrim, a scroll
  lock and a focus trap, all right for a detail surface and wrong for a dropdown
  in a field row — a trap would make Tab loop inside a five-row list you are
  trying to leave.
- **Not a listbox.** The rows hold real checkboxes, so checked state is
  announced, Space toggles and Tab navigates, all for free and all correct.
  There are no arrow keys and no roving tabindex on purpose: arrow keys are a
  menu affordance and this is not a menu.
- **Not portalled.** `main` is `overflow: hidden`, so `.pop` caps at
  `max-height: 320px` with `overflow-y: auto` and anchors left rather than
  escaping the layout. The fix, if a cluster ever grows wide enough for that to
  bite, is `createPortal` plus a measured rect — which then owes
  reposition-on-scroll and on-resize, so it is not built until something needs
  it.

Dismissal is on `pointerdown`, not `click`: a press that lands on another
control should both dismiss this panel and reach that control, which a click
listener firing after the fact cannot do. Escape is handled on the wrapper
rather than on `document`, so an open Sheet's own Escape handler is never
shadowed, and it returns focus to the trigger. `onBlur` closes only when focus
leaves the wrapper entirely. `close` is handed to `children` so a row can
dismiss the panel when that is the whole gesture; a machine tick deliberately
does not, because you are usually choosing several and closing after each one
would make that four gestures.

## The seam with the screens

Every consumer is a screen module, and it imports by path — there is no
`index.ts` barrel, so a grep for `components/<Name>` finds every call site.

| Component | Importers | Where |
|---|---|---|
| `Verbatim` | 22 | every surface that renders a server sentence |
| `Lamp` | 16 | roster, routing, activity, model cards, the node page |
| `Readout` | 8 | dashboard aggregates and strip, roster, node board, filesystems, the verdict, the node inspector |
| `Bars` | 7 | the verdict, the node board, the roster, activity, two storage cards, the deployment inspector |
| `Panel` | 3 | plan section, quantization ladder, serve panel — `Disclosure` only |
| `OverrideGate` | 2 | `tabs/models/Verdict.tsx`, `tabs/models/QuantLadder.tsx` |
| `CacheTable` | 2 | `tabs/models/InstalledModelsCard.tsx`, `tabs/storage/ModelCacheCard.tsx` |
| `Copyable` | 1 | `tabs/settings/AddNodeCard.tsx` |
| `Popover` | 0 | — |

The call pattern that matters is the one where the two lines come from two
different verdicts, in `tabs/models/Verdict.tsx`:

```tsx
<Verbatim text={(live ?? fit!).reason} size="label" />
<SegmentBar
  segments={segments}
  usable={(live ?? fit)?.usable_per_node ?? null}
  ceiling={live ? fit?.usable_per_node ?? null : null}
/>
```

The live allocatable figure is the line the verdict used; the static ceiling
sits behind it, and passes `null` when there was no live reading at all, so it
is never drawn twice.

`CacheTable.tsx` is the only file here that composes its neighbours — it takes
`ProportionBar` and `Verbatim` — which is why a change to either shows up on
the Storage card as well as on its own screen.

## Things that look like details and are not

**Colour reports state, never category.** `SegmentBar` outlines the limiting
memory term instead of recolouring it, and `shade()` runs every segment through
one hue at descending weight, precisely so that the only coloured thing in the
bar is the overrun. The same three tokens carry every state in the folder, and
`styles/tokens.css` records what they cost to get right: `--live: #2F6E3A` at
5.08:1, `--warn: #7F5C14` at 5.03:1 — `#8A6318` measured 4.47 and missed AA —
`--fault: #9A3327` at 6.02:1, and `--ink-muted: #5C5851`, which replaced
`#6E6A62` after it failed AA at 4.44:1 on 12px text.

**Null and zero take different pictures, in three separate components.**
`ProportionBar` draws `null` as a dashed empty track and zero as a solid one.
`SegmentBar` draws no line and no fault region for a `null` budget, because a
missing budget is not a budget of zero. `fmt()` gives `Readout` an em dash
rather than a `0`. Any one of the three alone would leave a screen where a
measurement nobody took looks like a measurement that came back empty.

**Every graphic carries its own text.** `Lamp` and `ProportionBar` both take a
required `label` and set it as `aria-label` and `title` on a `role="img"`
element; each `SegmentBar` block carries a `title` naming its term and marking
it `(limiting)`. A colour-only signal is not a signal.

**Width is reserved, not measured.** `Readout` takes `width` as a caller's
declaration of the widest value it expects and reserves it in `ch` up front.
Nothing here measures rendered text and resizes, which is why a 1 Hz stream does
not make the layout breathe.

**The override is two acts, and the second one is a sentence.**
`OverrideGate` renders no button until the box is ticked, and the box's label is
the claim itself. Both halves are structural: neither can be skipped by a
caller, because neither is a prop the caller can turn off.

## Failure behaviour

- **No reading.** `Readout` prints `fmt`'s em dash. Never a zero, and never a
  blank that reflows the row.
- **The stream is down but the last value is known.** `stale` greys the digits
  to `--ink-muted` and holds the reading; the matching `Lamp` goes `hollow` —
  same hue, no fill.
- **No budget was measured.** `SegmentBar` with `usable={null}` draws no line
  and no fault region. The bar still shows the breakdown; it just makes no
  claim about whether it fits.
- **No proportion was measured.** `ProportionBar` with `value={null}` draws a
  dashed empty track. Out-of-range values clamp to 0..1.
- **Nothing was rejected.** `VerbatimList` returns `null` rather than an empty
  list.
- **The node never measured its cache.** `CacheTable` prints "not measured" and
  the server's own reason through `Verbatim`; an empty cache prints "nothing
  downloaded" and the path. Neither is an error state.
- **The repo is being served.** The delete button is replaced by the word
  `serving`, because the server would answer 409 and the reason is what the
  operator needs.
- **The clipboard is unavailable or refuses.** Fall through to `execCommand`;
  if that fails too, a `--warn` line says so and points at the block, which is
  selectable and scrollable by hand. Unmounting mid-report clears the timer.
- **A press lands outside an open `Popover`.** `pointerdown` closes it and the
  press still reaches its target. Escape closes it and returns focus to the
  trigger; focus leaving the wrapper closes it too.

## Deliberately not built

**A portal for `Popover`.** `createPortal` plus a measured rect would escape
`main`'s `overflow: hidden` — and would then owe reposition-on-scroll and
on-resize. The bounded `max-height: 320px` panel is the answer until a cluster
is wide enough to need the other one.

**Arrow keys and a roving tabindex.** The panel's rows are native checkboxes, so
Space toggles and Tab navigates correctly with no code. Adding arrow keys would
mean re-implementing what the native control already does properly, and would
claim to be a menu.

**A focus trap outside `shell/Sheet.tsx`.** Right for a modal detail surface,
wrong for a dropdown in a field row.

**A toast system.** `Copyable` reports into its own button and reverts after two
seconds. One consumer does not justify a global surface.

**A third colour region in `SegmentBar`.** Repainting past the ceiling as well
as past the live line would put three colour regions in a bar whose whole thesis
is that colour reports state rather than category, and two rules of equal weight
read as a range rather than as a measurement and its context.

**A muted variant of `Verbatim`.** `size` changes scale and nothing else. A
server sentence set at `unit` size is still content, and giving it the `.unit`
colour would have made it look like chrome at exactly the moments it matters
most.
