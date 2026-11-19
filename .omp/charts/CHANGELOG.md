# Chart pack changelog

Dated entries: what changed, why, which files use it. Newest first.

## 2026-11-19 — reduce generated review bulk

Compacted all 1,271 `types/*/samples/*.json` files without changing their parsed
values. The chart gallery, build, and checks retain every dataset. Generated
`.omp/graph/` JSON outputs and community report are now ignored and rebuilt locally,
not included in the development PR. This removes generated bulk without removing
the chart/design assets used by the application.

The full check exposed a stale implementation-alias test in `tests/unit/theme.test.ts`:
it pinned the tooltip to the old inverted design token. Removed that wiring test
rather than re-pinning it; contrast gates and browser accessibility checks remain.

## 2026-11-17 — phase 9: verified from a clean install

**What.** `git archive HEAD` of `.omp/charts` (+ `.omp/design/tokens`, `.omp/extensions/chart-pack`)
into `/tmp/cleanpack`, `npm ci`, `npx playwright-core install chromium-headless-shell`, then
`npm run charts:check`: theme, build, icons, typecheck, datasets, unit, browser (visual, layout,
a11y, perf), registry, gallery and `gallery:verify` all pass. A second visual run against the
baselines the first one recorded passes too (1,782 screenshots), so the snapshots are deterministic.
`gallery.html` opened from disk renders 3,813 cards in "all three" and 1,271 per single theme with
no errors and no network requests (`tests/browser/gallery.mjs`, screenshots in `tests/.build/gallery-*.png`).
Dry runs in the clean copy: `/chart-new donut` → "reuse donut", nothing created; `/chart-new spine-chart`
→ full folder + icon, scoped check all green; `/chart-data add` with a CSV holding `'abc'` in a number
column → `row 2: field 'value' is 'abc', expected a number like the other rows`, and with broken JSON →
`not valid JSON at line 1, column 15`, registry byte-identical; a valid CSV → appended
(`sprint-velocity`, 6 rows, intents and suggested charts inferred), rendered by every intent in
`chooser.test.ts`; adding it again → refused ("only added to, never replaced").

**Why.** The pack must work for whoever checks it out next, not only in this working tree.

**Fix found by it.** `datasets/datasets.test.ts` pinned every dataset's `source` and `added` to the
values of the first ten, so the first appended dataset failed the suite. It now checks they are
present and well-formed. Files: `datasets/datasets.test.ts`.

## 2026-11-17 — chart pack created

**What.** The whole pack, built in nine phases (commits `chart pack: phase …`):

| Total | |
|---|---|
| Chart types | **99** in 11 categories, all `stable` in `registry.json` |
| Samples | **1,271** (basic, realistic, and edge cases per chart: empty, single, zeros, negatives, nulls, duplicates, large >5k marks on canvas, long labels, unicode/RTL, outliers, unsorted dates, mixed time zones) |
| Shared datasets | **10** (`datasets/datasets.json`) |
| Icons | **99**, one per chart, lint-clean against the design pack's icon spec |
| Unit tests (vitest, jsdom) | **4,454**: 4,450 passed, 0 failed, 4 skipped (end-to-end extension tests, run with `CHART_PACK_E2E=1`) |
| Browser checks (Playwright) | **7,765** passed, 0 failed: visual baselines (basic + realistic × 3 themes × 320/768/1280), layout (overflow, clipped text, overlapping axis labels, legend fit, tooltip in viewport), a11y (axe-core WCAG 2.2 AA, keyboard traversal, aria-label summary), perf budgets |
| Chooser | 17 intents × 10 datasets = 170 picks, each rendered (`chooser/chooser.test.ts`) |
| Pass rate | **100%** (`npm run charts:check`: all green) |

Perf: every chart under budget (`tests/perf-report.md`): ≤1k SVG marks < 100 ms, 50k
canvas marks < 500 ms (bar 50k ≈ 230 ms, scatter/line/beeswarm 50k included).

**Why.** One API (`chart`, `chartFor`, `recommend`, `listCharts`, `getSample`,
`getDataset`) so future work asks for a chart by type or intent and gets a working,
themed, accessible one — instead of the hand-rolled charts the audit found in `ui/src`
(`AUDIT.md`: uPlot `Chart`/`ChartGrid`, `ChartCell` sparkline, CSS bars).

**Files.** `.omp/charts/**`; `.omp/extensions/chart-pack/` (commands `/chart`, `/chart-for`,
`/chart-new`, `/chart-data`, `/chart-sample`, `/chart-check`, `/chart-gallery`; tools
`chart_recommend`, `chart_lookup`, `dataset_lookup`, `schema_validate`; hooks);
`.omp/agents/chart-smith.md`; `.omp/rules/chart-pack.md` and a pointer in `CLAUDE.md`.
Theme tokens alias `.omp/design/tokens` (no colour literal outside the categorical
palette, which the design pack lacks and which is gated in `theme/build.mjs`).

**Used by.** `ui/src/components/PackChart.tsx` (stacked-bar, stacked-bar-100), which the
dashboard's Usage card mounts. Migration of the audited app charts is planned in
`AUDIT.md` (RULES.md #6); the uPlot telemetry charts need the pack's canvas line to
match `spanGaps:false` and the synced crosshair first.
- 2026-11-18: core/cartesian.ts — removed the unused `tickGranularity` import and renamed
  `fmtFor`'s unused first parameter to `_s`, so the pack typechecks under `ui/`'s
  `noUnusedLocals`/`noUnusedParameters` flags. Why: `ui/src/components/PackChart.tsx`
  now imports stacked-bar and stacked-bar-100 into the app build.
- 2026-11-18: core/theme.ts + core/define.ts — `.oc-legend[hidden]{display:none}`: the
  class's `display:flex` beat the `hidden` attribute, so a hidden legend still took its
  6px margin and pushed the SVG (or the empty/error plate) 6px past the root's bottom on
  every titled single-series chart; and the empty/error plate now subtracts the title's
  4px margin as the drawn path already does. Why: both clipped the bottom of
  `PackChart`'s empty plate (`layout.check` in `.omp/tests/ui`).
  Visual baselines re-recorded (`--update`): every titled single-series chart is 6px
  shorter, which is that margin gone; no other pixel change failed.
- 2026-11-18: theme/tokens.json + core/theme.ts — the tooltip follows the theme in force
  instead of inverting it: `tooltip-bg` → `{design.panel-raised}`, `tooltip-fg` →
  `{design.ink}`, and new `tooltip-border` (`{design.rule}`) and `tooltip-shadow`
  (`{design.shadow-raised}`) drawn by `.oc-tooltip`, which is how the app's own popovers
  (`.pop`) look. Why: in dark mode the hover plate was cream on a near-black page.
  Used by every chart; first seen on the dashboard Overview (`ui/src/components/PackChart.tsx`).
- 2026-11-19: core/define.ts + core/types.ts + types/_shared/TrendA.ts — new `ChartDef.hover:
  'nearest-x'`: a pointer anywhere over the plot hovers the data mark nearest in x (ties by y)
  and rings it; line, area, step-line and multi-line opt in. With several series, a point's
  tooltip is titled by its x and lists every visible series' value there. Why: hover only
  answered on the 2.5px point dots, which are invisible with `points: 'never'`, so the
  dashboard's "Failures per day, by error type" never said what failed on a day. Used by
  `ui/src/tabs/dashboard/UsageCard.tsx` via `ui/src/components/PackChart.tsx`. Pinned by
  `types/multi-line/multi-line.test.ts` ("hovering anywhere over the plot…").
- 2026-11-19: types/_shared/TrendA.ts — a lone named series is named in its tooltip row and
  focus label instead of "Value" (unnamed `line` data still reads "Value"). Why: a window
  with a single failure class drew one series, no legend and no end label, so the dashboard's
  "Failures per day" hover said only "Value 6". Used by `ui/src/tabs/dashboard/UsageCard.tsx`;
  pinned by `types/multi-line/multi-line.test.ts` ("a single named series…").
