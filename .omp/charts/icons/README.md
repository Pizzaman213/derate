# Chart icons

One icon per chart type, drawn to the design pack's icon spec
(`.omp/design/icons/README.md`) so they sit beside the product's own icons:

- `viewBox="0 0 24 24"`, all geometry inside `2…22` on both axes.
- Root exactly: `fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"`.
- Children carry geometry only: `path d`, `circle cx cy r`, `rect x y width height rx ry`,
  `line x1 y1 x2 y2`, `polyline points`, `polygon points`. No fill/stroke/class/style/opacity.
- No literal colour anywhere; colour is `currentColor`.
- File name = chart id (`svg/<id>.svg`); every file has an `icons.json` entry and vice versa.
- No two icons with identical geometry. A recognisable miniature of the chart, not a logo.

`icons.json` entries: `{ name, chart, category, tags, usage, added }` -- `category` is the
chart's category. `npm run charts:icons` lints all of it (`icons/lint.mjs`, which reuses the
design pack's path-bounds code), builds `icons.svg` (a sprite) and fails on stale output.

Using one: `<svg><use href="icons.svg#bar"/></svg>`, or the raw markup from
`icons/svg/<id>.svg`; in the React app, the design pack's `Icon` component renders the
same spec.
