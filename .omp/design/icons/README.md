# Icons

Forty-seven generic shapes plus the derate monogram, drawn to one spec and
rendered by one component. `lint.mjs` enforces every rule below; `build.mjs`
optimises the sources with SVGO and generates the sprite and the data module.

```
svg/<name>.svg          the source of truth, one file per icon, hand-drawn
icons.json              name, category, tags, usage, added, replaces
build.mjs               svgo in place → icons.svg (sprite) + components/icons.ts
lint.mjs                the spec, enforced; also fails on stale outputs
svgo.config.mjs         preset-default, shapes kept, child attrs stripped
components/icons.ts     GENERATED, React-free: ICONS, IconName
```

The React component is `ui/src/components/Icon.tsx`, in the UI because React
resolves from `ui/node_modules`; the pack only generates its data.

## The spec

- `viewBox="0 0 24 24"`, geometry inside `2…22` on both axes (2px padding).
- Root: `fill="none" stroke="currentColor" stroke-width="1.5"
  stroke-linecap="round" stroke-linejoin="round"`. Children carry geometry only
  (`path d`, `circle`, `rect`, `line`, `polyline`, `polygon`) — no colour, no
  stroke attributes, no class, no style.
- No literal colour anywhere. Colour comes from `currentColor`, i.e. the
  surrounding text's token.
- File name is the icon name, kebab-case; every file has an `icons.json` entry
  and every entry a file.
- No two icons with identical geometry.
- Category `brand` is the exception: it may set its own stroke width and caps
  (the monogram is `2.25`/`square`, re-based from the 64×50 header drawing)
  and its children may carry `opacity`. Still `currentColor`.

## Using one

```tsx
import { Icon } from '../components/Icon'

<Icon name="close" decorative />                    // inside a button that has its own label
<Icon name="alert-triangle" label="Warning" />      // the icon IS the information
<Icon name="derate-mark" label="derate" size={24} className="mark" />
```

`size` is `12 | 14 | 16 | 20 | 24` (default 16). A meaningful icon takes
`label` and renders `role="img" aria-label`; a decorative one takes
`decorative` and renders `aria-hidden focusable="false"`. Omitting both is a
type error, on purpose: the choice is the accessibility decision.

## Adding one

`/new-icon <name>` in omp does all of this; by hand:

1. `icon_search` / grep `icons.json` for an existing match first — `tags` and
   `replaces` are there to be searched.
2. Draw `svg/<name>.svg` to the spec above.
3. Add the `icons.json` entry: `name`, `category`, `tags`, `usage`, `added`
   (today, `YYYY-MM-DD`), `replaces` (the glyphs or entities it retires).
4. `cd .omp && npm run icons:build && npm run icons:lint`.
5. Append a CHANGELOG line. See `.omp/RULES.md`.

Data drawings — charts, the cluster graph, the QR code, the loading band, the
lamp, the bars — are not icons. They stay as inline `<svg>` with the marker
`// design-pack: data-svg` on line 1, which is what exempts them from the
`raw-svg` lint.
