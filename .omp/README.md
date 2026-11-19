# derate design pack

Everything that decides how the UI looks, in one place, with a script for every
fact so none of it can drift from the code without a check saying so. Lives
under `.omp/` so omp (the coding agent) discovers the extension, commands,
agents and `RULES.md` automatically when started in the repo root.

`npm install` here once (`.omp/node_modules` is gitignored). Node ≥ 20. The
React-bound pieces typecheck against `../ui/node_modules`, so `cd ui && npm ci`
first.

## Index

| Path | What it is | Script |
|---|---|---|
| `package.json` | the scripts below, pinned deps (`esbuild`, `playwright-core`, `svgo`, `axe-core`, `typescript`, `@oh-my-pi/pi-coding-agent` types) | — |
| `tsconfig.json` | typechecks `extensions/`, `tests/ui/`, `design/**/*.ts` against the UI's React types | `npm run typecheck` |
| `design/audit.mjs` | scans `ui/src` and writes `design/AUDIT.md` (`--json` for machines) | `npm run audit` |
| `design/AUDIT.md` | generated: stack, colour literals + ΔE clusters, scale values, component/icon inventories, a11y flags, ranked debt | — |
| `design/CHANGELOG.md` | dated entries: what changed in the pack, which files, why | — |
| `design/tokens/tokens.json` | the source of every colour, size, space, duration: W3C format, three layers, three themes | — |
| `design/tokens/build.mjs` | `tokens.json` → `tokens.css` (shipped; `ui/src/main.tsx` imports it), `tokens.ts` (resolved values for canvas/xterm), `tokens.flat.json` (for scripts) | `npm run tokens:build` |
| `design/tokens/pairs.json` + `check.mjs` | the contrast gate: every promised fg/bg pair per theme, plus a stale-output check | `npm run tokens:check` |
| `design/icons/svg/*.svg` + `icons.json` | the icon set, one file per name, and its metadata (category, tags, usage, added, replaces) | — |
| `design/icons/build.mjs` | svgo in place → `icons.svg` sprite + generated `components/icons.ts` (data for `ui/src/components/Icon.tsx`) | `npm run icons:build` |
| `design/icons/lint.mjs` | the icon spec enforced: viewBox, padding, stroke, currentColor, metadata, duplicates, staleness | `npm run icons:lint` |
| `design/DESIGN.md` | the design system: principles, colour/type/space/motion, a spec per component, patterns, copy, how to add things | — |
| `design/components/registry.json` | every component the pack knows: props, variants, states, tokens, icons, test, spec anchor | — |
| `design/styleguide.mjs` → `design/index.html` | the living style guide, one self-contained file; open from disk | `npm run styleguide:build` |
| `tests/ui/gallery/states.tsx` | every variant × state of every registered component, over the real components; feeds the style guide and the browser gallery | — |
| `graph/build.mjs` | code graph (graphify, code-only) + design graph → `graph/graph.json`, `god-nodes.json`, `GRAPH_REPORT.md`, the DESIGN.md diagram | `npm run graph:update` / `graph:build` |
| `graph/design-graph.mjs` | token / icon / component / spec nodes and their edges to the code, merged in graphify's own shape | — |
| `graph/diagram.mjs` | the Mermaid architecture diagram between the `arch` markers in DESIGN.md | — |
| `graph/GRAPHIFY.md` | layout, rebuild, queries, and the rule: graph before grep | — |
| `tests/ui/static/lint.mjs` | the 12 static design rules over `ui/src`; `--json` for machines | `npm run ui:static` |
| `tests/ui/gallery/` | every state mounted live (`main.tsx`), esbuild bundle (`build.mjs`), recorded coordinator fixtures, text stress | — |
| `tests/ui/lib/` | the browser harness (loopback server, Chromium, axe), the shared component checker, the case-sensitive esbuild resolver | — |
| `tests/ui/components/*.check.mjs` | one verifier per registered component: renders, keyboard, axe | discovered by `ui/check.mjs` |
| `tests/ui/layout.check.mjs` | theme × width × stress: overflow, clipping, overlaps, target size, contrast, reduced motion; screenshots | discovered by `ui/check.mjs` |
| `tests/ui/styleguide.check.mjs` | opens `design/index.html` from disk, switches the three themes, captures `design/screens/styleguide-*.png` | discovered by `ui/check.mjs` |
| `tests/ui/generated.check.mjs` + `static.check.mjs` | hermetic: generated outputs fresh, registry covered, static rules clean | discovered by `ui/check.mjs` |
| `tests/ui/scaffold.mjs` | a new shared component with states, check, registry row and spec stub; `--delete` reverses it | `npm run ui:new -- <Name>` |
| `tests/ui/run.mjs` | the design gate: tokens:check, icons:lint, ui:static, then every `.omp/tests` verifier through `ui/check.mjs` | `npm run ui:check [-- --strict] [path]` |
| `pack-sync.mjs` | reconcile the pack with `ui/src`: unregistered components, raw icons, literals, unused tokens/icons; refreshes `screen` registry rows | `npm run pack:sync` |
| `extensions/design-pack/` | the omp extension: tools, slash commands, hooks | loaded by omp in this repo |
| `commands/*.md` | `/new-component`, `/new-icon`, `/new-color` | omp |
| `agents/*.md` | `design-reviewer`, `ui-tester`, `icon-smith` | omp |
| `RULES.md` | the nine maintenance rules, sticky in every session | omp |
| `design/tokens/migrate.mjs` | the one-shot codemod that moved `ui/src` onto the vocabulary; idempotent, kept for the record | — |

## Scripts

```
npm run audit          # design/AUDIT.md
npm run tokens:build   # regenerate tokens.css / tokens.ts / tokens.flat.json
npm run tokens:check   # contrast gate + staleness; exit 1 on any failure
npm run icons:build    # optimise svg/, regenerate icons.svg + components/icons.ts
npm run icons:lint     # the icon spec; exit 1 on any finding
npm run styleguide:build  # design/index.html
npm run graph:update   # refresh .omp/graph/graph.json (incremental)
npm run graph:build    # ...from scratch
npm run ui:static      # static design rules over ui/src
npm run ui:new -- X    # scaffold component X (--delete X reverses)
npm run ui:check       # the design gate (add -- --strict to fail on skips)
npm run pack:sync      # reconciliation report
npm run check          # typecheck + ui:check
```

## On this box

`DERATE_CHECK_STRICT=1 npm run ui:check` is the mode here (Chromium is in the
Playwright cache); it takes about 4½ minutes, most of it the layout matrix.
Scope it: `npm run ui:check -- ui/src/components/Readout.tsx`.

