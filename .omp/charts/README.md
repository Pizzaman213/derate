# derate chart pack

The chart types the app mounts, one file each, behind one API. TypeScript + D3 to SVG,
switching the marks layer to a HiDPI canvas above 5,000 marks, themed by the design
pack's tokens (light, dark, high contrast). No network at runtime: fonts are bundled.

| Chart | File |
|---|---|
| `stacked-bar`, `stacked-bar-100`, `grouped-bar` | `types/<id>/index.ts` |
| `multi-line` | `types/multi-line/index.ts` |
| `pie`, `donut` | `types/pie/index.ts`, `types/donut/index.ts` |

Each file holds the chart's input schema (zod), its options, and its drawing. Code shared
between charts is in `types/_shared/`; the engine (`defineChart`, scales, theme, export,
validation) is in `core/`, with `adapters/` for reshaping rows and `time/` for date axes.
The app mounts a type by id through `ui/src/components/PackChart.tsx`.

```ts
import { chart, validate, listCharts } from '.omp/charts'

const h = chart('multi-line', el, rows, { fields: { x: 'month', y: 'revenue', series: 'region' } })
h.update(newRows)      // options merge
h.exportSVG(); h.exportCSV()
h.destroy()            // no DOM, listeners or observers left
```

`cd .omp/charts && npm install`, then `npm run charts:typecheck`, `npm run charts:test`
and `npm run theme:build` (regenerates `theme/chart-tokens.css` and `tokens.generated.ts`).

The 93 other types, the chooser, the datasets, the CLI and the gallery this pack once had
are in git under the `charts-full-pack` tag. Rules for changing anything: [`RULES.md`](RULES.md).
