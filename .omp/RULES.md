Design pack lives in `.omp/`; index in `.omp/README.md`. These rules apply to every future session in this repo.

1. Check before you create. Before adding any icon, color, token, or component, search the pack first (`icons.json`, `tokens.json`, `components/registry.json`). If an existing one covers it, use it; no near-duplicates.
2. New things go into the pack first, then app code uses them. Icons: `design/icons/svg/*.svg` + `icons.json`, never inline SVG. Colors, sizes, space, durations: `design/tokens/tokens.json`, never a literal in `ui/src`.
3. Icons follow the spec in `design/icons/README.md`; `npm run icons:lint` enforces it.
4. Tokens must pass the contrast gate: add a fg/bg pair to `design/tokens/pairs.json` and run `npm run tokens:check`.
5. Every new or changed shared component gets a row in `components/registry.json` and a spec in `design/DESIGN.md`.
6. When you find a premade asset outside the pack (an icon someone dropped in, a hardcoded color, an unregistered component), migrate it into the pack and replace the original.
7. If the stack, folders, or tooling change, update `design/DESIGN.md`, `README.md`, and these rules to match. The pack must never drift from the code.
8. You run the checks (`cd .omp && npm run check`), not the user. Only report done when everything is green.
9. Every task ends with `cd .omp && npm run validate` and the reviewers in `rules/validation.md` (`duplication-reviewer` and `repo-steward` and `breaker` always, all in one parallel batch; `design-reviewer`, `security-reviewer` by what changed). Report what each found.
