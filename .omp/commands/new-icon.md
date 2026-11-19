---
description: Add an icon to the set, to the spec, or answer with the existing one
argument-hint: "<name or meaning>"
---

Add the icon `$ARGUMENTS` to the derate icon set — or prove it already exists.

1. Call `icon_search` with `$ARGUMENTS`, and again with its synonyms and with any glyph it would replace (`✓`, `✕`, `&#10005;`, `▸` …). If a match exists, answer with that icon's usage snippet from `icons.json` and **stop; create nothing**.
2. Otherwise draw `.omp/design/icons/svg/<kebab-name>.svg` to the spec in `.omp/design/icons/README.md`: `viewBox="0 0 24 24"`, geometry inside 2…22, root `fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"`, children carrying geometry only, no literal colour. A generic shape, never a trademark.
3. Add the entry to `.omp/design/icons/icons.json`: `name`, `category` (`action|status|arrow|view|file|nav|brand`), `tags` (what people will search), `usage` (`<Icon name="…" label="…" />` or `decorative`), `added` (today, `YYYY-MM-DD`), `replaces` (the glyphs or entities it retires, or `[]`).
4. Run `cd .omp && npm run icons:build && npm run icons:lint`. Both must be clean; the lint names the rule any finding breaks.
5. Replace the glyph or inline `<svg>` it retires with `<Icon name="<name>" …>` at every call site, choosing `label` (announced) or `decorative` (hidden) at each one.
6. The `tool_result` hook appends a CHANGELOG entry; complete its `why` line. Follow `.omp/RULES.md`, then run `/pack-sync`.
