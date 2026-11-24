# derate design pack

Tokens, icons, the component registry and the chart pack, in one place, with a script for
the facts that can be checked. Lives under `.omp/` so omp (the coding agent) discovers the
agents and `RULES.md` automatically when started in the repo root.

`npm install` here once (`.omp/node_modules` is gitignored). Node ≥ 20.

| Path | What it is | Script |
|---|---|---|
| `design/tokens/tokens.json` | the source of every colour, size, space, duration: W3C format, three layers, three themes | — |
| `design/tokens/build.mjs` | `tokens.json` → `tokens.css` (shipped; `ui/src/main.tsx` imports it), `tokens.ts` (resolved values for canvas/xterm), `tokens.flat.json` (gitignored, read by the chart theme build) | `npm run tokens:build` |
| `design/tokens/pairs.json` + `check.mjs` | the contrast gate: every promised fg/bg pair per theme, plus a stale-output check | `npm run tokens:check` |
| `design/icons/svg/*.svg` + `icons.json` | the icon set, one file per name, and its metadata | — |
| `design/icons/build.mjs` | svgo in place → `icons.svg` sprite + generated `components/icons.ts` (data for `ui/src/components/Icon.tsx`) | `npm run icons:build` |
| `design/icons/lint.mjs` | the icon spec enforced: viewBox, padding, stroke, currentColor, metadata, duplicates, staleness | `npm run icons:lint` |
| `design/logos/*` | third-party brand marks (inference engines), upstream artwork in its own colours; not icons, not linted by the icon spec | — |
| `design/DESIGN.md` | the design system: principles, colour/type/space/motion, a spec per component, how to add things | — |
| `design/components/registry.json` | every component the pack knows: props, variants, states, tokens, icons, spec anchor | — |
| `charts/` | the six chart types the app mounts, one file each; see `charts/README.md` | `cd charts && npm run charts:test` |
| `agents/*.md` | `design-reviewer`, `icon-smith`, `duplication-reviewer`, `repo-steward`, `breaker` | omp |
| `rules/validation.md` | the always-on end-of-task gate and review requirement | omp |
| `scripts/validate.mjs` | `npm run validate`: the mechanical gate (also run by `.githooks/pre-push`) | node |
| `line-budget.json` | files over their budget, recorded at current size; the gate stops them growing (`npm run validate -- --update-budget`) | node |
| `validate.allow.json` | optional: `duplicate-names` and `paths` the gate must ignore, and `budget` caps for a change's own deliberate growth (record + this change's lines, with a reason) | node |
| `rules/chart-pack.md`, `RULES.md` | the maintenance rules, sticky in every session | omp |
| `work/auth-keys/sets/*.json` | route and limit sets read by `tests/unit/` in the Python suite | — |

```
npm run tokens:build   # regenerate tokens.css / tokens.ts / tokens.flat.json
npm run tokens:check   # contrast gate + staleness; exit 1 on any failure
npm run icons:build    # optimise svg/, regenerate icons.svg + components/icons.ts
npm run icons:lint     # the icon spec; exit 1 on any finding
npm run check          # tokens:check + icons:lint
npm run validate       # end-of-task gate: static checks, generated-file staleness, typecheck, UI verifiers
npm run validate -- --fast   # static checks only
# A fresh worktree first needs: node_modules linked or installed in ui/, .omp/ and .omp/charts/,
# and `npm run tokens:build` (tokens.flat.json is generated and ignored by git).
```
